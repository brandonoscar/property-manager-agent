"""Email image-attachment photo triage (#533 entry point 3).

The third and final photo entry point, mirroring the two already shipped —
SMS (:mod:`helixis.webhooks.sms_photo_triage`, PR #548) and Buildium
work-order (:mod:`helixis.webhooks.work_order_photo_triage`, PR #549). When a
surfaced inbound email carries image attachments, pull each attachment's bytes
off Gmail (via Composio), run the vision assessor
(:func:`helixis.agents.image_tools.assess_image_impl`), and record a DRAFT work
order the property manager reviews — the same "assess → draft, never file"
flow, on the email side. It NEVER files or sends anything.

LIVE-CONFIRMED Composio Gmail attachment contract (2026-07-11, against a real
connected Gmail account — this was the #533 blocker that could not be verified
offline):

* ``GMAIL_FETCH_EMAILS`` returns each message with an inline
  ``attachmentList: [{attachmentId, filename, mimeType}]`` — no separate
  full-format fetch is needed. (Parsed onto ``InboundEmail.attachments`` by
  :func:`helixis.webhooks.email_ingest.parse_gmail_attachments`.)
* ``GMAIL_GET_ATTACHMENT(message_id, attachment_id, file_name)`` →
  ``data.file.s3url`` — a short-lived Cloudflare R2 presigned URL that
  downloads the bytes with NO auth (like the work-order presigned
  ``DownloadUrl``), plus ``data.file.mimetype`` / ``data.file.name``.
* ⚠ ``attachmentList.mimeType`` is UNRELIABLE — observed labelling actual JPEG
  bytes as ``image/png``. The downloaded content-type (and Composio's
  ``file.mimetype``) are authoritative, so the MIME passed to the assessor
  comes from the R2 response, never the ref. The ref's declared type is used
  only as a coarse "is this plausibly an image?" pre-filter.

Runs in the webhook worker's email-poll cycle (which has the Gemini key but not
memory — assess only needs the model). Cost-bounded: gated to SURFACED emails
(known Buildium contact + long, the same gate the whole email-ingest feature
uses) and capped at :data:`_MAX_PHOTOS` images per email. Best-effort
throughout — a fetch failure, an unassessable image, a shape mismatch, or a
missing key degrades to "nothing added" and never breaks the poll.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from helixis.agents.image_tools import ImageAssessment, assess_image_impl
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.settings import Settings, get_settings
from helixis.webhooks.sms_photo_triage import draft_work_order_summary

if TYPE_CHECKING:
    import asyncpg

    from helixis.connectors.composio_client import ComposioClient
    from helixis.webhooks.email_triage import EmailAttachmentRef, InboundEmail

logger = logging.getLogger(__name__)

# Composio Gmail tool that fetches one attachment's bytes. ⚠ isolated as a
# named constant (like email_poll.GMAIL_FETCH_SLUG) because the slug is a
# Composio contract that can only be confirmed against a live toolkit —
# Composio occasionally renames slugs.
GMAIL_GET_ATTACHMENT_SLUG = "GMAIL_GET_ATTACHMENT"

_IMAGE_MIME_PREFIX = "image/"
# Cap the fetched attachment so a hostile/huge image can't blow memory. Matches
# the document/vision budget (8 MiB) the assessor itself enforces on decode.
_MAX_MEDIA_BYTES = 8 * 1024 * 1024
_FETCH_TIMEOUT_S = 15.0
# Cap vision calls per email — bounds cost + latency; a maintenance email
# rarely carries more than a couple of photos worth assessing.
_MAX_PHOTOS = 3


def _is_image_ref(ref: EmailAttachmentRef) -> bool:
    """Coarse pre-filter: does the attachment's *declared* type look like an
    image? (The declared type can lie — the real MIME comes from the download —
    but it's enough to skip PDFs/docs before spending a fetch.) A blank
    declared type is treated as a candidate so a mislabelled image isn't
    dropped before we can read its bytes."""
    mime = (ref.mime_type or "").lower()
    return mime.startswith(_IMAGE_MIME_PREFIX) or mime == ""


def extract_attachment_download(result: dict[str, Any] | None) -> tuple[str, str] | None:
    """Pull ``(s3url, mimetype)`` out of a ``GMAIL_GET_ATTACHMENT`` result.

    Live shape: ``{"data": {"file": {"s3url": ..., "mimetype": ..., "name":
    ...}}}``. Tolerant of the ``file`` block sitting at the top level instead
    of under ``data`` (SDK-wrapper drift). Returns ``None`` when no URL is
    present — the caller degrades to "nothing added"."""
    if not isinstance(result, dict):
        return None
    for container in (result.get("data"), result):
        if not isinstance(container, dict):
            continue
        file_block = container.get("file")
        if isinstance(file_block, dict):
            url = file_block.get("s3url") or file_block.get("s3Url") or file_block.get("url")
            if url:
                mimetype = str(file_block.get("mimetype") or file_block.get("mimeType") or "")
                return str(url), mimetype
    return None


async def _download_image(url: str, *, fallback_mime: str = "") -> tuple[bytes, str] | None:
    """Download a presigned attachment URL (no auth — the R2 URL carries its
    own token). Returns ``(bytes, mime)`` only for an image within the size
    cap; ``None`` otherwise. The MIME is resolved from the *downloaded*
    content-type first (authoritative — the declared ref type lies), then the
    ``fallback_mime`` Composio reported, and the result is gated to
    ``image/*``. Never raises."""
    if not url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("email attachment fetch failed", exc_info=True)
        return None
    data = resp.content
    if len(data) > _MAX_MEDIA_BYTES:
        logger.warning("email attachment too large (%d bytes) — skipping", len(data))
        return None
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith(_IMAGE_MIME_PREFIX):
        # R2 sometimes serves octet-stream; fall back to Composio's reported type.
        mime = (fallback_mime or "").split(";")[0].strip().lower()
    if not mime.startswith(_IMAGE_MIME_PREFIX):
        return None
    return data, mime


async def _fetch_attachment_image(
    composio: ComposioClient,
    *,
    user_id: str,
    message_id: str,
    ref: EmailAttachmentRef,
) -> tuple[bytes, str] | None:
    """Fetch one Gmail attachment's image bytes: call ``GMAIL_GET_ATTACHMENT``
    for its presigned URL, then download it (size/MIME-capped). Returns
    ``(bytes, mime)`` or ``None`` on any shape mismatch / failure. Never
    raises."""
    result = composio.execute_tool(
        user_id=user_id,
        slug=GMAIL_GET_ATTACHMENT_SLUG,
        arguments={
            "message_id": message_id,
            "attachment_id": ref.attachment_id,
            # GMAIL_GET_ATTACHMENT requires a non-empty file_name; the ref's
            # filename can be blank, so fall back to a synthetic one.
            "file_name": ref.filename or f"attachment-{ref.attachment_id[:12]}",
        },
    )
    download = extract_attachment_download(result)
    if download is None:
        return None
    url, composio_mime = download
    return await _download_image(url, fallback_mime=composio_mime or ref.mime_type)


async def assess_email_photos(
    composio: ComposioClient,
    *,
    user_id: str,
    message_id: str,
    attachments: tuple[EmailAttachmentRef, ...],
    settings: Settings | None = None,
    max_photos: int = _MAX_PHOTOS,
) -> list[ImageAssessment]:
    """Assess up to ``max_photos`` image attachments on one email. Returns the
    assessments (may be empty). Never raises — every Composio fetch / download
    / assess is best-effort and one bad attachment can't sink the rest."""
    resolved = settings or get_settings()
    out: list[ImageAssessment] = []
    for ref in attachments:
        if len(out) >= max_photos:
            break
        if not _is_image_ref(ref):
            continue
        try:
            fetched = await _fetch_attachment_image(
                composio, user_id=user_id, message_id=message_id, ref=ref
            )
            if fetched is None:
                continue
            data, mime = fetched
            assessment = await assess_image_impl(
                base64_content=base64.b64encode(data).decode("ascii"),
                mime_type=mime,
                hint="This image was attached to an email a tenant/owner sent — "
                "assess it as a maintenance photo.",
                settings=resolved,
            )
            out.append(assessment)
        except Exception:  # noqa: BLE001 — one bad attachment must not sink the rest
            logger.warning(
                "email %s: attachment %s failed", message_id, ref.attachment_id[:12], exc_info=True
            )
            continue
    return out


async def _record_email_photo_draft(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    email: InboundEmail,
    assessment: ImageAssessment,
) -> bool:
    """Record a drafted work order for one actionable email photo. Returns True
    when a maintenance draft was written; False for a non-actionable image.
    Best-effort — an activity-write failure is swallowed (never breaks the
    poll)."""
    summary = draft_work_order_summary(assessment, source="an email photo")
    if summary is None:
        return False
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="webhook",
        source_label="Email photo attachment",
        action_type="maintenance_photo_drafted",
        description=summary[:500],
        raw={
            "message_id": email.message_id,
            "from": email.from_email,
            "subject": email.subject,
            "image_kind": assessment.image_kind,
            "likely_trade": assessment.likely_trade,
            "severity": assessment.severity,
            "draft_work_order": (
                assessment.draft_work_order.model_dump() if assessment.draft_work_order else None
            ),
            "warnings": list(assessment.warnings),
        },
    )
    try:
        await write_activity(pool, entry)
    except Exception:  # noqa: BLE001 — best-effort; the assessment still happened
        logger.warning("failed to record email photo draft", exc_info=True)
        return False
    return True


async def triage_email_photos(
    pool: asyncpg.Pool,
    composio: ComposioClient,
    *,
    user_id: str,
    company_id: str,
    emails: list[InboundEmail],
    settings: Settings | None = None,
    max_photos: int = _MAX_PHOTOS,
) -> int:
    """Top-level hook the Gmail poll calls for a user's surfaced emails: for
    each email carrying image attachments, assess them and record a draft work
    order per actionable maintenance image. Returns the number of drafts
    recorded. Never raises — a maintenance draft is a bonus on top of the
    normal email ingest, never allowed to break it."""
    resolved = settings or get_settings()
    recorded = 0
    for email in emails:
        if not email.attachments:
            continue
        try:
            assessments = await assess_email_photos(
                composio,
                user_id=user_id,
                message_id=email.message_id,
                attachments=email.attachments,
                settings=resolved,
                max_photos=max_photos,
            )
        except Exception:  # noqa: BLE001 — defensive; assess_* is already best-effort
            logger.warning("email %s photo triage failed", email.message_id, exc_info=True)
            continue
        for assessment in assessments:
            if await _record_email_photo_draft(
                pool, company_id=company_id, email=email, assessment=assessment
            ):
                recorded += 1
    return recorded


__all__ = [
    "GMAIL_GET_ATTACHMENT_SLUG",
    "assess_email_photos",
    "extract_attachment_download",
    "triage_email_photos",
]
