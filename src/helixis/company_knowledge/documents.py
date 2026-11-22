"""Extract a text digest from an uploaded company document (ADR 0006).

Company knowledge is document-grounded: the facts a lead reply or chat answer
needs live inside the PDFs a PM uploads to Documents & Links. This module turns
one uploaded document into a short factual **digest** and caches it on the row
(``company_documents.text_digest``, migration 0037), so the daily knowledge
generator distills the *contents* — not just the file names — into the profile.

It reuses the battle-tested document extractor
(``agents.document_tools.extract_document_fields_impl``), which already handles
the whole pipeline: fetch the bytes from a signed Storage URL → docling text
(when the ``[document]`` extra is present) OR Gemini multimodal fallback → an
LLM pass that returns a ``summary`` + structured ``fields``. We keep the
``summary`` + a compact rendering of the fields as the digest.

Everything is **best-effort** (ADR 0006 §4 / gotcha 7): a signing failure, an
extraction failure, a missing key, or a ``SystemExit`` on some transitive spaCy
path all degrade to ``None`` — the document simply contributes its name that
cycle, never breaking generation.

⚠ Live seam: the extraction path (Storage sign + Gemini multimodal) can't be
dry-run without live creds — confirm digests are populating on the first
dogfood (query ``company_documents.text_digest``). The reused extractor is the
same one the Document specialist ships on, so the seam is shared, not new.
"""

from __future__ import annotations

import logging

from helixis.agents.document_tools import extract_document_fields_impl
from helixis.agents.document_warnings import nothing_was_read
from helixis.db.repositories import docs_links as docs_repo
from helixis.settings import Settings
from helixis.storage import StorageError, sign_object_url

logger = logging.getLogger(__name__)

_DOCUMENTS_BUCKET = "company_documents"
_SIGN_TTL_SECONDS = 300
_MAX_DIGEST_CHARS = 1200
# Per-field cap. A policy sentence ("$300 refundable deposit, plus $35/mo pet
# rent, restricted breeds excluded") runs ~180 chars, so the old 120 sliced it
# mid-number — turning "$300 … $35/mo" into a confident, WRONG "$30" in the lead
# reply (#461 dogfood). 400 comfortably holds a full policy sentence.
_MAX_FIELD_CHARS = 400

# Warnings from the extractor that mean "no usable content" — treat as no digest
# rather than caching a summary of a failure.
#
# ⚠ This was a hand-typed copy of six of the extractor's tokens, and it is the
# shape that goes stale in silence: a seventh failure token added upstream would
# have been read here as an ordinary caveat, and a digest of an empty result
# cached as though it were the document. It reads the shared vocabulary now, via
# a predicate rather than a set — because `invalid_extraction` carries the
# exception text after it, so a plain membership test against that one could
# never match (the list this replaces did not contain it at all).


def _clip(text: str, limit: int) -> str:
    """Truncate to ``<= limit`` WITHOUT slicing mid-token — back off to the last
    whitespace and append an ellipsis so the cut is visible.

    A hard ``[:limit]`` can split a number ("$300" → "$30"), which then reads as
    a valid, confident, WRONG figure in a lead reply. Cutting on a word boundary
    keeps the last token whole and the "…" signals the value is incomplete
    rather than silently corrupting it. (#461 dogfood.)"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _compose_digest(summary: str, fields: dict[str, object]) -> str:
    """Compose a short digest from the extractor's summary + fields. Pure +
    deterministic (fields sorted) so the same extraction yields a stable digest."""
    parts: list[str] = []
    summary = (summary or "").strip()
    if summary:
        parts.append(summary)
    for key in sorted(fields):
        value = fields[key]
        if value in (None, "", [], {}):
            continue
        text = _clip(str(value).strip().replace("\n", " "), _MAX_FIELD_CHARS)
        if text:
            parts.append(f"{key.replace('_', ' ')}: {text}")
    return _clip("\n".join(parts), _MAX_DIGEST_CHARS).strip()


async def extract_document_digest(
    settings: Settings, *, storage_path: str, content_type: str
) -> str | None:
    """Extract a short factual digest of one uploaded document, or ``None`` on
    any failure. Best-effort: never raises."""
    try:
        url = await sign_object_url(
            bucket=_DOCUMENTS_BUCKET,
            path=storage_path,
            expires_in=_SIGN_TTL_SECONDS,
            settings=settings,
        )
    except StorageError as exc:
        logger.warning("document digest: could not sign %s: %s", storage_path, exc)
        return None

    try:
        result = await extract_document_fields_impl(
            url=url, mime_type=content_type or "application/pdf"
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — never load-bearing
        logger.warning("document digest: extraction failed for %s: %s", storage_path, exc)
        return None

    if nothing_was_read(result.warnings or []):
        logger.info("document digest: no usable content for %s (%s)", storage_path, result.warnings)
        return None

    digest = _compose_digest(result.summary, result.fields)
    return digest or None


async def ingest_company_document_digests(
    pool: object, settings: Settings, *, company_id: str, limit: int = 10
) -> int:
    """Extract + cache digests for the company's not-yet-digested documents.

    Runs once per document (skips those already digested), capped at ``limit``
    per cycle. Returns the number newly digested. Best-effort throughout — a
    missing table (pre-0037) or a single bad document never breaks the cycle.

    ⚠ **TWO KNOWN GAPS HERE, MEASURED AND DELIBERATELY NOT FIXED — both need a
    column this schema does not have.** Written down so the next reader does
    not re-derive them, and does not "fix" the first one into something worse:

    1. ``documents_for_knowledge`` returns the ``limit`` NEWEST documents,
       digested or not — not the ``limit`` UNDIGESTED ones. So a company with
       more than ``limit`` documents can never digest its oldest.
    2. A document the extractor cannot read (oversize, encrypted, a scan with
       no text) returns ``None`` forever and is re-attempted every cycle: sign
       a URL, fetch up to the reader's ceiling in bytes, refuse. No model spend,
       but unbounded repetition.

    ⚠ The obvious one-line fix for (1) — order undigested first — makes (2)
    STRICTLY WORSE past a cliff: the unreadable documents become the permanent
    head of the work-list, and once there are ``limit`` of them nothing is ever
    digested again. Today's "newest N" at least rotates. Closing either needs a
    stored "we tried this and it cannot be read" marker, i.e. a migration, and
    coupling one to an unrelated fix leaves the working half inert until it is
    applied by hand (there is no migration runner — see CLAUDE.md §4)."""
    try:
        docs = await docs_repo.documents_for_knowledge(pool, company_id=company_id, limit=limit)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — table may not exist yet (gotcha 8)
        logger.warning("document digest ingest: could not list documents: %s", exc)
        return 0

    digested = 0
    for doc in docs:
        if doc.text_digest:  # already extracted — don't re-charge the model
            continue
        digest = await extract_document_digest(
            settings, storage_path=doc.storage_path, content_type=doc.content_type
        )
        if digest:
            await docs_repo.set_document_digest(
                pool,  # type: ignore[arg-type]
                company_id=company_id,
                document_id=doc.id,
                text_digest=digest,
            )
            digested += 1
    return digested


__all__ = ["extract_document_digest", "ingest_company_document_digests"]
