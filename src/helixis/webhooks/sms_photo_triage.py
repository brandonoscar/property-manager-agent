"""Inbound-MMS maintenance-photo triage (#533 entry point 1).

A tenant texts a photo of a problem — a leak, a broken appliance, water stains.
Twilio POSTs it to the inbound-SMS webhook (:func:`helixis.api.routes.crm.inbound_sms`)
as MMS media. This module pulls the image off that webhook, runs it through the
vision assessor (:func:`helixis.agents.image_tools.assess_image_impl`), and turns
the result into a DRAFT work order the property manager reviews — the "killer"
#533 flow: photo in → assessed → drafted → one-click approve.

It NEVER files or sends anything. The draft is recorded to the activity log for
the PM to act on; approval + actually creating the Buildium work order stays a
human step (same discipline as the email/write paths).

Best-effort throughout: no media, a fetch failure, an unassessable image, or a
missing Maps/LLM key all degrade to "nothing added" — they never raise and never
break the webhook's 200 (Twilio retries non-2xx).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import asyncpg

from helixis.agents.image_tools import ImageAssessment, assess_image_impl
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.settings import Settings

logger = logging.getLogger(__name__)

# Twilio media the assessor can read (mirrors image_tools' supported set).
_IMAGE_MIME_PREFIX = "image/"
# Cap the fetched media so a hostile/huge MMS can't blow memory. Matches the
# document/vision budget (8 MiB) the assessor itself enforces on decode.
_MAX_MEDIA_BYTES = 8 * 1024 * 1024
_FETCH_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class Media:
    url: str
    content_type: str


def extract_media(params: dict[str, str]) -> list[Media]:
    """Parse Twilio's ``NumMedia`` + ``MediaUrl{i}`` / ``MediaContentType{i}``
    form fields into a list. Non-numeric / missing ``NumMedia`` → ``[]``."""
    try:
        n = int((params.get("NumMedia") or "0").strip())
    except ValueError:
        return []
    out: list[Media] = []
    for i in range(max(0, n)):
        url = (params.get(f"MediaUrl{i}") or "").strip()
        ctype = (params.get(f"MediaContentType{i}") or "").strip()
        if url:
            out.append(Media(url=url, content_type=ctype))
    return out


def first_image_media(params: dict[str, str]) -> Media | None:
    """The first MMS attachment that is an image the assessor can read."""
    for m in extract_media(params):
        if m.content_type.lower().startswith(_IMAGE_MIME_PREFIX):
            return m
    return None


async def fetch_media_bytes(
    media: Media,
    *,
    account_sid: str,
    auth_token: str,
) -> tuple[bytes, str] | None:
    """Download a Twilio media URL. Twilio media requires basic auth
    (account_sid:auth_token) and 307-redirects to the actual (S3) object —
    ``follow_redirects`` handles the hop, and httpx drops the auth header on the
    cross-host redirect so it isn't leaked to S3. Returns ``(bytes, mime)`` or
    ``None`` on any failure / oversize (best-effort, never raises)."""
    if not (account_sid and auth_token and media.url):
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(media.url, auth=(account_sid, auth_token))
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("twilio media fetch failed", exc_info=True)
        return None
    data = resp.content
    if len(data) > _MAX_MEDIA_BYTES:
        logger.warning("twilio media too large (%d bytes) — skipping", len(data))
        return None
    # Prefer the response's content-type; fall back to what Twilio told us.
    mime = (resp.headers.get("content-type") or media.content_type or "").split(";")[0].strip()
    return data, mime


async def assess_sms_photo(
    params: dict[str, str],
    *,
    settings: Settings,
) -> ImageAssessment | None:
    """Full triage: pull the first image off the inbound MMS, fetch it with
    Twilio auth, and run the vision assessor. Returns the assessment, or
    ``None`` when there's no image / fetch failed / the assessor couldn't read
    it. Never raises."""
    media = first_image_media(params)
    if media is None:
        return None
    fetched = await fetch_media_bytes(
        media,
        account_sid=settings.twilio.account_sid,
        auth_token=settings.twilio.auth_token,
    )
    if fetched is None:
        return None
    data, mime = fetched
    if not mime.lower().startswith(_IMAGE_MIME_PREFIX):
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return await assess_image_impl(
        base64_content=encoded,
        mime_type=mime,
        hint="This image was texted in by a tenant — assess it as a maintenance photo.",
        settings=settings,
    )


def draft_work_order_summary(a: ImageAssessment, *, source: str = "a texted photo") -> str | None:
    """A one-paragraph, human-readable DRAFT work order from an assessment —
    or ``None`` when the image isn't an actionable maintenance photo (a notice,
    an unreadable image, or a low-confidence read carries no draft). ``source``
    names where the photo came from (a texted photo, a work-order attachment)."""
    if a.image_kind != "maintenance_photo" or not a.issue_summary.strip():
        return None
    wo = a.draft_work_order
    parts = [f"Draft work order (from {source}): {a.issue_summary.strip()}"]
    bits: list[str] = []
    if a.likely_trade and a.likely_trade != "unknown":
        bits.append(f"trade: {a.likely_trade}")
    if a.severity and a.severity != "none":
        bits.append(f"severity: {a.severity}")
    if wo is not None:
        bits.append(f"priority: {wo.priority}")
    if bits:
        parts.append(f"({', '.join(bits)})")
    if wo is not None and wo.scope.strip():
        parts.append(f"Scope: {wo.scope.strip()}")
    if a.suggested_action.strip():
        parts.append(f"Suggested: {a.suggested_action.strip()}")
    parts.append("Review and approve before dispatching — Occupella has not filed anything.")
    return " ".join(parts)


async def record_photo_draft(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    from_number: str,
    assessment: ImageAssessment,
) -> bool:
    """Record the drafted work order to the activity log for the PM to act on.
    Returns True when a maintenance draft was written; False for a non-actionable
    image. Best-effort — an activity-write failure is swallowed (never breaks the
    webhook)."""
    summary = draft_work_order_summary(assessment)
    if summary is None:
        return False
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="webhook",
        action_type="maintenance_photo_drafted",
        description=summary[:500],
        source_label="Tenant SMS photo",
        raw={
            "from": from_number,
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
        logger.warning("failed to record maintenance-photo draft", exc_info=True)
        return False
    return True


async def triage_inbound_photo(
    params: dict[str, str],
    *,
    settings: Settings,
    pool: asyncpg.Pool,
    company_id: str,
    from_number: str,
) -> bool:
    """Top-level hook the inbound-SMS webhook calls: if the text carried an
    image, assess it and record a draft work order. Returns True when a draft
    was recorded. Never raises — a maintenance draft is a bonus on top of the
    normal SMS ingest, never allowed to break it."""
    try:
        assessment = await assess_sms_photo(params, settings=settings)
    except Exception:  # noqa: BLE001 — defensive; assess_* is already best-effort
        logger.warning("sms photo triage failed", exc_info=True)
        return False
    if assessment is None:
        return False
    return await record_photo_draft(
        pool, company_id=company_id, from_number=from_number, assessment=assessment
    )


__all__ = [
    "Media",
    "assess_sms_photo",
    "draft_work_order_summary",
    "extract_media",
    "fetch_media_bytes",
    "first_image_media",
    "record_photo_draft",
    "triage_inbound_photo",
]
