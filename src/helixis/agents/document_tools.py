"""Document extraction tool — Gemini multimodal.

The orchestrator's document specialist (:mod:`helixis.agents.document_agent`)
hands attachments here for structured field extraction. Today's
coverage: leases, vendor invoices, inspection reports, work orders,
plus an ``other`` catch-all for documents we don't have a schema for.

Design rules (mirror :mod:`helixis.agents.web_tools`):

* Single tool: ``extract_document_fields``.
* Graceful degradation, never raises back into the agent loop. Missing
  inputs, fetch failures, oversize attachments, LLM transport errors,
  and unparseable responses all yield a :class:`DocumentExtraction`
  with empty fields and a ``warnings`` entry the specialist's prompt
  teaches it to surface.
* **No invention.** The :class:`DocumentExtraction` schema enforces the
  rule at the type layer: any required field that comes back ``null``
  MUST have a corresponding warning naming that field. A null required
  field without a warning is a schema violation and raises
  ``ValidationError`` — which the tool wrapper itself catches and
  surfaces as ``invalid_extraction`` so the agent learns the model's
  output was rejected rather than silently shipped.
* Activity-log every invocation (best-effort) so the Activity page can
  audit what Helixis extracted and at what confidence.
* No real customer documents in fixtures. Synthetic-text mode
  (``mime_type="text/plain"``) exists specifically for deterministic
  evals — production calls send images / PDFs as base64 data URLs to
  Gemini's multimodal endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import litellm
from agents import function_tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from helixis.agents import document_warnings
from helixis.agents.image_orientation import upright_image_bytes
from helixis.common.json_extract import coerce_json, extract_first_json_object
from helixis.db import engine as db_engine
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.llm.provider_errors import failure_warnings
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.memory.context import current_memory_context
from helixis.security.pii import redact
from helixis.security.prompt_delimit import wrap_untrusted
from helixis.security.public_url import vet_public_url
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)


DocType = Literal["lease", "invoice", "inspection_report", "work_order", "other"]


# ── per-doc-type required fields ─────────────────────────────────
#
# Each entry lists the fields the schema validator will check for
# null-without-warning violations. Adding a new doc type means
# (1) extending DocType, (2) listing required fields here, (3) adding
# a paragraph to the system prompt telling the model what to extract,
# (4) seeding an eval case.

_REQUIRED_FIELDS_BY_DOC_TYPE: dict[DocType, tuple[str, ...]] = {
    "lease": (
        "tenant_name",
        "property_address",
        "unit",
        "monthly_rent",
        "lease_start",
        "lease_end",
        "signed_date",
    ),
    "invoice": (
        "vendor_name",
        "invoice_number",
        "line_items",
        "subtotal",
        "total",
        "invoice_date",
        "due_date",
    ),
    "inspection_report": (
        "property_address",
        "inspection_date",
        "inspector_name",
        "issues_found",
        "severity_summary",
    ),
    "work_order": (
        "vendor_name",
        "scope",
        "agreed_cost",
        "scheduled_date",
        # completed_date is intentionally NOT required — open work
        # orders don't have one yet, and a null without a warning
        # would otherwise force a fake warning entry.
    ),
    "other": (),
}


_SUPPORTED_MIME_TYPES = frozenset(
    {
        # Eval-only deterministic mode (synthetic text fixtures).
        "text/plain",
        # Production: Gemini multimodal accepts these natively.
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
    }
)


# Hard cap on retry attempts when the model's response fails to parse
# OR fails the no-invention schema validator. ``_MAX_RETRIES = 1``
# means up to 2 total attempts: the original + one retry with a
# correction nudge. Mirrors the planner's same-class budget at
# action_planner.py:567. Bumping this is a deliberate decision —
# beyond one retry the failure stops being "Gemini JSON-mode flake"
# and starts being a real LLM-behaviour problem worth surfacing as
# ``invalid_extraction`` rather than retry-hammering.
_MAX_RETRIES = 1


# Cap on how much of the failed attempt-0 response we replay back
# into the attempt-1 prompt as repair material (issue #158, Shape D).
# Bigger snippet = more concrete signal for the model to diagnose
# what broke, but pushes the original document content out of
# effective attention. 2000 chars comfortably covers the failure
# shapes we've seen from Gemini Flash on the document fixtures
# (typical broken responses are 400-1200 chars).
_BROKEN_RESPONSE_SNIPPET_MAX_CHARS = 2000


# ── data models ──────────────────────────────────────────────────


class SourceRef(BaseModel):
    """Where the document came from. ``kind`` is what fetcher was used;
    ``id`` is the opaque identifier (file path, attachment id, URL).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["file", "base64", "url", "attachment"]
    id: str = Field(min_length=1, max_length=512)


class DocumentExtraction(BaseModel):
    """Structured envelope every ``extract_document_fields`` invocation
    returns.

    The ``model_validator`` below enforces the **no-invention rule** at
    the type layer: any required field for the detected ``doc_type``
    that comes back null MUST be accompanied by a warning naming that
    field. Without that pairing, the schema rejects the result — the
    model is forbidden from "succeeding silently" with nulls.
    """

    model_config = ConfigDict(extra="forbid")

    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0)
    fields: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    source_ref: SourceRef
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _null_required_fields_must_have_warning(self) -> DocumentExtraction:
        """Enforce the no-invention rule.

        For every required field on this doc_type, if the field is
        null/absent then ``warnings`` must contain an entry whose text
        references the field name. This prevents the model from
        returning ``fields={"tenant_name": null, ...}`` with no
        explanation — it must either populate the field OR document
        why it couldn't.

        ``doc_type == "other"`` has no required fields, so it always
        passes this check; the agent prompt forbids using ``other``
        as an escape hatch (see system prompt rule 4).
        """
        required = _REQUIRED_FIELDS_BY_DOC_TYPE.get(self.doc_type, ())
        for field_name in required:
            value = self.fields.get(field_name)
            if value is not None:
                continue
            warning_match = any(field_name in w for w in self.warnings)
            if not warning_match:
                raise ValueError(
                    f"required field {field_name!r} is null for doc_type "
                    f"{self.doc_type!r} but no warning references it — "
                    "schema forbids silent nulls. Either populate the "
                    "field or add a warning naming it."
                )
        return self


# ── helpers ──────────────────────────────────────────────────────


def _sanitize_for_log(text: str, limit: int = 200) -> str:
    return redact(text)[:limit]


def _redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Strip contact PII (email / phone / SSN / card / Helixis #ids) from the
    extracted field VALUES before the result leaves the tool — so the document
    specialist can't echo a tenant's email or phone from a parsed document into
    chat (the document analogue of the web agent's #250 rule, but deterministic
    at the tool boundary instead of relying on a prompt rule the model may
    forget). ``redact()`` is contact-PII-scoped, so names, addresses, unit
    numbers, and dollar amounts are preserved. Non-string values pass through
    untouched (a null required field must stay null for the schema validator)."""
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in fields.items()}


def _make_source_ref(
    *,
    file_path: str | None,
    base64_content: str | None,
    url: str | None,
) -> SourceRef | None:
    if file_path:
        return SourceRef(kind="file", id=file_path[:512])
    if url:
        # Store the STABLE object path, not the query string. A signed Supabase
        # Storage URL carries a ~500-char JWT in ``?token=…``; stuffing that into
        # ``id`` (capped at 512) raised a pydantic ValidationError that silently
        # killed the company-knowledge digest extraction. The token is ephemeral
        # and has no business being a citation id anyway. The ``[:512]`` guards
        # against a pathologically long clean URL too.
        return SourceRef(kind="url", id=url.split("?", 1)[0][:512])
    if base64_content:
        # Don't persist the base64 blob — just a length anchor.
        return SourceRef(kind="base64", id=f"base64:{len(base64_content)} chars")
    return None


def _empty_extraction(
    source_ref: SourceRef | None,
    warning: str | Sequence[str],
    doc_type: DocType = "other",
) -> DocumentExtraction:
    """Build a degraded result that still satisfies the schema validator
    (no required fields for ``doc_type="other"``).

    Accepts a sequence so a failure can carry its CAUSE next to the generic
    "it didn't happen" token — ``["llm_unavailable", "llm_quota_exhausted"]``.
    The generic token always leads, so membership checks are unaffected.
    """
    ref = source_ref or SourceRef(kind="base64", id="unknown")
    warnings = [warning] if isinstance(warning, str) else list(warning)
    return DocumentExtraction(
        doc_type=doc_type,
        confidence=0.0,
        fields={},
        summary="",
        source_ref=ref,
        warnings=warnings,
    )


async def _fetch_bytes(
    *,
    file_path: str | None,
    base64_content: str | None,
    url: str | None,
    max_bytes: int,
    timeout_seconds: float,
) -> tuple[bytes | None, str | None]:
    """Resolve one of the three input modes into raw bytes. Returns
    ``(bytes, error_warning)`` — exactly one is non-None."""
    # A local path comes only from code (the eval fixtures). The model-facing
    # tools take no ``file_path``, so a model can never name a server file.
    if file_path:
        try:
            path = Path(file_path)
            if not path.exists():
                return None, "attachment_unavailable"
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("document fetch failed file_path=%s: %s", file_path, exc)
            return None, "attachment_unavailable"
        if len(data) > max_bytes:
            return None, "attachment_too_large"
        return data, None

    if base64_content:
        try:
            data = base64.b64decode(base64_content, validate=True)
        except (ValueError, TypeError) as exc:
            logger.warning("document base64 decode failed: %s", exc)
            return None, "attachment_unavailable"
        if len(data) > max_bytes:
            return None, "attachment_too_large"
        return data, None

    if url:
        # Never fetch what only this server can reach: localhost, the Render
        # private network, the metadata service (security/public_url.py). The
        # request goes to the address that was checked, not a fresh lookup.
        pinned, refusal = await vet_public_url(url)
        if pinned is None:
            logger.warning("document URL refused (%s) url=%s", refusal, url.split("?", 1)[0])
            return None, "attachment_unavailable"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
            ) as client:
                response = await client.get(
                    pinned.url, headers=pinned.headers, extensions=pinned.extensions
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning("document URL fetch failed url=%s: %s", url.split("?", 1)[0], exc)
            return None, "attachment_unavailable"
        # A redirect is not followed (its target was never checked), so a 3xx
        # is a failed fetch, not a document made of the redirect page.
        if response.status_code >= 300:
            logger.warning(
                "document URL fetch status=%d url=%s", response.status_code, url.split("?", 1)[0]
            )
            return None, "attachment_unavailable"
        data = response.content
        if len(data) > max_bytes:
            return None, "attachment_too_large"
        return data, None

    return None, "attachment_unavailable"


# Minimum usable parsed-text length. Below this we treat the parse as a
# failure (e.g. a scanned/image-only PDF with no text layer — docling-parse
# without OCR extracts nothing useful) and fall back to the multimodal path,
# because feeding empty/garbage text to the model is worse than the image.
_MIN_USABLE_PDF_TEXT = 32

#: The cause token for an encrypted PDF. Rides ALONGSIDE whatever generic
#: failure warning the run already produced, never instead of it — gotcha 46's
#: rule, so every existing membership check (``_FAILURE_WARNINGS``, the
#: specialist's rule 3, the golden cases' ``warnings_must_not_include``) is
#: untouched and this only ADDS a sentence the reader can act on.
#:
#: ⚠ Now an ALIAS of the shared vocabulary rather than a second literal. Every
#: enumerable warning token has to be enumerable in one place, or the guard
#: that gives each one a human sentence is checking a list that is missing the
#: entries nobody remembered to copy.
PDF_ENCRYPTED_WARNING = document_warnings.DOCUMENT_PASSWORD_PROTECTED

#: ``/Encrypt`` as a trailer/xref-stream dictionary KEY, which is the only
#: place it means "this file is encrypted": the key is followed by an indirect
#: reference (``12 0 R``) or an inline dictionary.
#:
#: ⚠ The reference shape is required on purpose. A bare ``/Encrypt`` substring
#: also occurs in ordinary prose and in uncompressed content streams — a lease
#: whose text discusses encryption would otherwise be reported to its owner as
#: password-protected, which is a working document refused (gotcha 55's
#: over-block, the direction that is worse than missing one).
_PDF_ENCRYPT_MARKER = re.compile(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)")

#: The spec allows junk before ``%PDF-`` as long as the header appears early.
_PDF_HEADER_WINDOW = 1024


def pdf_carries_encryption_marker(raw_bytes: bytes) -> bool:
    """Does this look like an ENCRYPTED PDF? Structural, no dependency.

    ⚠ **This is a diagnosis, never a gate**, and the distinction is the whole
    design. A PDF can be encrypted with an EMPTY user password — the
    permissions-only kind a printer or a bank produces — and every parser opens
    those normally. Nothing in the file distinguishes that from a real user
    password without attempting decryption, which needs a crypto library this
    backend does not carry.

    So refusing on this marker would turn a readable document into a refusal
    for a whole class of files that work today. Instead the caller runs the
    ordinary pipeline and consults this only once the read has ALREADY produced
    nothing — at which point "encrypted" stops being a guess about the file and
    becomes the explanation for a failure we measured. A permissions-only PDF
    extracts fine and never reaches the question.
    """
    if not raw_bytes:
        return False
    if b"%PDF-" not in raw_bytes[:_PDF_HEADER_WINDOW]:
        return False
    return _PDF_ENCRYPT_MARKER.search(raw_bytes) is not None


def _extraction_is_empty(result: DocumentExtraction) -> bool:
    """Did the read come back with nothing usable? Every field null or absent.

    ⚠ ``_empty_extraction`` results qualify trivially; a model that answered
    from an encrypted PDF with even ONE real field does not, and must not be
    told its document was unreadable."""
    return not any(value is not None for value in result.fields.values())


def _note_encrypted_pdf(result: DocumentExtraction, *, encrypted: bool) -> DocumentExtraction:
    """Append the encrypted-PDF cause when a read produced nothing usable.

    Without it the user is told the document "could not be read" and is left
    with nothing to do about it — gotcha 46's complaint exactly, where a
    true-sounding sentence sent somebody to retry a thing that could not work.
    """
    if encrypted and _extraction_is_empty(result) and PDF_ENCRYPTED_WARNING not in result.warnings:
        result.warnings.append(PDF_ENCRYPTED_WARNING)
    return result


def _docling_extract_sync(raw_bytes: bytes) -> str:
    """Parse a PDF to plain text with docling-parse. SYNC + CPU-heavy — callers
    MUST run this in a worker thread (never on the event loop). Lazy-imports
    docling-parse so the module loads without the ``[document]`` extra; raises
    ImportError when it's absent (caller treats that as 'unavailable')."""
    import io

    from docling_core.types.doc.page import TextCellUnit
    from docling_parse.pdf_parser import DoclingPdfParser

    parser = DoclingPdfParser()
    parser.set_loglevel("error")
    doc = parser.load(path_or_stream=io.BytesIO(raw_bytes))
    lines: list[str] = []
    for _idx, page in doc.iterate_pages():
        lines += page.export_to_textlines(
            cell_unit=TextCellUnit.LINE,
            add_location=False,
            add_fontname=False,
            add_text_direction=False,
        )
    return "\n".join(lines)


async def _pdf_to_text(raw_bytes: bytes, *, timeout_seconds: float) -> str | None:
    """Best-effort PDF→text via docling-parse. Returns the extracted text, or
    ``None`` on ANY failure (extra not installed, parse error, timeout, or an
    unusably short result). NEVER raises — the document tool degrades to the
    multimodal path on None. Runs the sync parser in a worker thread under a
    wall-clock timeout so a slow/pathological PDF can't block the event loop."""
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_docling_extract_sync, raw_bytes),
            timeout=timeout_seconds,
        )
    except ImportError:
        logger.info("docling-parse not installed; PDF falls back to multimodal extraction")
        return None
    except TimeoutError:
        logger.warning(
            "docling PDF parse timed out after %ss; falling back to multimodal", timeout_seconds
        )
        return None
    except Exception as exc:  # noqa: BLE001 — any parse failure → fallback, never raise
        logger.warning(
            "docling PDF parse failed (%s); falling back to multimodal", type(exc).__name__
        )
        return None
    if not text or len(text.strip()) < _MIN_USABLE_PDF_TEXT:
        logger.info("docling produced no usable text (image-only PDF?); falling back to multimodal")
        return None
    return text


def _build_messages(
    *,
    raw_bytes: bytes,
    mime_type: str,
    extraction_hint: str | None,
) -> list[dict[str, Any]]:
    """Build the LiteLLM multimodal message envelope.

    For ``text/plain``, the document content is inlined as a regular
    text block (eval-only deterministic mode). For images / PDFs, the
    content is base64-encoded into a ``data:`` URL inside an
    ``image_url`` block — Gemini accepts both image MIME types and
    ``application/pdf`` through this shape via LiteLLM.
    """
    user_blocks: list[dict[str, Any]] = []
    instructions = (
        "Extract the document's structured fields per the schema in the system "
        "prompt. Output one JSON object only."
    )
    if extraction_hint:
        instructions += f" Hint from caller: {extraction_hint}"
    user_blocks.append({"type": "text", "text": instructions})

    if mime_type == "text/plain":
        text_content = raw_bytes.decode("utf-8", errors="replace")
        user_blocks.append({"type": "text", "text": f"DOCUMENT TEXT:\n\n{text_content}"})
    else:
        # A photographed lease page arrives sideways with an EXIF tag saying
        # which way to turn it, and this is a TEXT-reading path — a rotated
        # page is the case where that costs the most. No-op for a PDF and for
        # any image not flagged sideways (see image_orientation).
        encoded = base64.b64encode(upright_image_bytes(raw_bytes, mime_type)).decode("ascii")
        user_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            }
        )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_blocks},
    ]


# Issue #161 — even with ``response_format=json_object`` Gemini's multimodal /
# PDF path wraps the JSON in a ```json fence or adds a line of prose ~60% of the
# time on attempt 0. ``coerce_json`` recovers those shapes deterministically
# BEFORE the correction-nudge retry (a 2x cost/latency tax). Shared with the
# planner, which has the same parse path — see helixis.common.json_extract.
# Aliased here so existing call sites / tests keep their names.
_coerce_json_object = coerce_json
_extract_first_json_object = extract_first_json_object


def _parse_llm_response(
    raw: str,
    source_ref: SourceRef,
) -> DocumentExtraction:
    """Parse the model's JSON response into a :class:`DocumentExtraction`.

    Adds ``source_ref`` here (the model is told to omit it) so the
    schema's required field is always populated regardless of what
    the LLM emits. Raises ``ValueError`` on parse / schema failure;
    the caller wraps the failure as a structured warning result so
    the agent never sees the exception.
    """
    if raw is None:
        raise ValueError("LLM returned empty content")
    try:
        data, coerced = _coerce_json_object(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"LLM response was not a JSON object: type={type(data).__name__}")
    # Force the caller's source_ref onto the parsed data — the prompt
    # tells the model to omit it; we own this field.
    data["source_ref"] = source_ref.model_dump()
    extraction = DocumentExtraction.model_validate(data)
    if coerced:
        # Observable evidence that attempt 0 was noisy (fence/prose) but
        # recovered WITHOUT a retry — the #161 cost saving. Distinct from
        # ``retried_once_after_malformed_json`` (which means a retry fired).
        extraction.warnings.append("recovered_noisy_json_attempt_0")
    return extraction


async def _call_llm(
    *,
    messages: list[dict[str, Any]],
    settings: Settings,
    timeout_seconds: float,
    completion_fn: Any | None,
) -> tuple[str | None, str | list[str] | None]:
    """Call the reasoning model. Returns ``(content, error_warning)``.

    ``error_warning`` is a single token for the ordinary degraded paths and a
    LIST when the provider named a cause worth keeping — see
    :func:`helixis.llm.provider_errors.failure_warnings`. Both are accepted by
    ``_empty_extraction`` / ``image_tools._empty``.

    ``completion_fn`` is the injection point tests use to avoid the
    real network — mirrors the planner / summarizer / web_tools pattern.
    """
    fn = completion_fn or litellm.acompletion
    llm = settings.llm
    models = llm.models or {}
    reasoning = models.get("reasoning") or {}
    model_name = reasoning.get("model") or llm.default_model
    api_key = _api_key_for_model(settings, model_name)

    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "response_format": {"type": "json_object"},
        **sampling_kwargs(model_name, 0.1),
        "timeout": timeout_seconds,
    }
    if api_key:
        kwargs["api_key"] = api_key

    try:
        response = await fn(**kwargs)
    except (
        litellm.Timeout,
        litellm.APIConnectionError,
        litellm.InternalServerError,
        litellm.RateLimitError,
    ) as exc:
        logger.warning("document LLM transient error: %s", exc)
        return None, "llm_unavailable"
    except litellm.AuthenticationError as exc:
        logger.warning("document LLM auth failure: %s", exc)
        return None, "llm_unauthorized"
    except Exception as exc:  # noqa: BLE001
        # ⚠ Do NOT collapse this to a bare ``llm_unavailable``. That is what it
        # used to do, and it threw away the one fact that decides whether
        # retrying can possibly help. On 2026-08-26 the Anthropic spend cap —
        # a 400 whose message names the date access returns — arrived here,
        # was flattened to the generic transient token, and every caller that
        # retries (the eval runner, three times per case) spent its budget
        # against a quota that was gone. The provider said so in words; we
        # logged the words and dropped them. See llm/provider_errors.
        logger.warning("document LLM call failed: %s", exc)
        return None, failure_warnings(str(exc))

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        logger.warning("document LLM response shape unexpected: %s", exc)
        return None, "llm_unavailable"
    if not isinstance(content, str) or not content.strip():
        return None, "llm_unavailable"
    return content, None


async def _log_activity(result: DocumentExtraction) -> None:
    """Best-effort activity-log write. Pool missing / DB failure /
    no memory context bound → silent no-op. Mirrors the web_tools
    helper exactly."""
    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    user_id = ctx.user_id if ctx else None
    if not company_id:
        return

    description = _sanitize_for_log(result.summary or f"{result.doc_type} extraction")
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="chat",
        action_type="document_extracted",
        description=description,
        user_id=user_id or None,
        raw={
            "doc_type": result.doc_type,
            "confidence": result.confidence,
            "source_ref": result.source_ref.model_dump(),
            "warnings": list(result.warnings),
            "field_keys": sorted(result.fields.keys()),
            "extracted_at": result.extracted_at.isoformat(),
        },
    )
    await write_activity(pool, entry)


# ── system prompt ────────────────────────────────────────────────


SYSTEM_PROMPT = """\
You are the document extractor for Occupella. You read a single document
(lease, vendor invoice, inspection report, work order, or other) and
return ONE JSON object matching the DocumentExtraction schema.

## Schema

{
  "doc_type": "lease" | "invoice" | "inspection_report" | "work_order" | "other",
  "confidence": float in [0.0, 1.0],
  "fields": { ... per-doc-type fields, see below ... },
  "summary": "1-2 sentence plain-English summary",
  "warnings": ["string", ...]
}

OMIT ``source_ref`` and ``extracted_at`` — the caller fills them in.

## Per-doc-type required fields

You must populate the listed fields when the doc_type is detected.
Field VALUES may be null (see the no-invention rule below) but
field KEYS must be present.

- lease: tenant_name, property_address, unit, monthly_rent (USD float),
  lease_start (ISO date), lease_end (ISO date), signed_date (ISO date)
- invoice: vendor_name, invoice_number, line_items (list of
  {description, amount}), subtotal (USD float), total (USD float),
  invoice_date (ISO date), due_date (ISO date)
- inspection_report: property_address, inspection_date (ISO date),
  inspector_name, issues_found (list of strings), severity_summary
  (one of: "none", "low", "medium", "high")
- work_order: vendor_name, scope (string), agreed_cost (USD float),
  scheduled_date (ISO date), completed_date (ISO date or null)
- other: free-form fields, your best effort. ``other`` is NOT an
  escape hatch — see rule 4.

## Rules (hard)

1. NEVER invent values. If a value is illegible, missing, ambiguous,
   or you would be guessing, set the field to ``null`` AND append a
   warning entry to ``warnings`` whose text names that field
   (e.g. ``"monthly_rent: figure smudged in the rent clause"``).
   A null field WITHOUT a corresponding warning is a schema violation
   and your response will be rejected.

2. NEVER fabricate plausible-looking values to fill the schema. A
   tenant name that "looks like a name" but isn't actually visible in
   the document is an invention — set null + warning.

3. If your overall confidence in the extraction is below 0.6, set
   ``confidence`` accordingly AND append ``"low_confidence"`` to
   ``warnings``. Do not raise confidence to clear the threshold; if
   you would have to guess, the right confidence is low.

4. If you cannot confidently determine the doc_type, set
   ``doc_type: "other"`` AND append ``"doc_type_uncertain"`` to
   ``warnings``. Do NOT route ambiguous documents into a typed bucket
   just to populate fields.

5. If the document content provided is empty, blank, or garbled, set
   ``doc_type: "other"``, ``confidence: 0.0``, ``fields: {}``,
   ``summary: ""``, and ``warnings: ["empty_document"]``. Do not
   attempt to extract from filename, mime type, or training data.

6. NEVER include internal Buildium IDs (Task #, Tenant #, Property #,
   Lease #, Owner #, Vendor #) in your ``summary``. Names and
   addresses are fine; numeric internal IDs are not.

7. Cite document regions in warnings when possible
   (e.g. ``"signed_date: bottom of page 2 illegible"``). The reader
   uses these to triage what to re-scan.

## Output

Respond with exactly one JSON object matching the schema. No preamble,
no markdown fences, no commentary outside the JSON.
"""


# ── implementation ───────────────────────────────────────────────


async def extract_document_fields_impl(
    file_path: str | None = None,
    base64_content: str | None = None,
    mime_type: str = "text/plain",
    url: str | None = None,
    extraction_hint: str | None = None,
    *,
    settings: Settings | None = None,
    completion_fn: Any | None = None,
) -> DocumentExtraction:
    """Extract structured fields from a single document.

    Exactly one of ``file_path``, ``base64_content``, or ``url`` must
    be provided. ``mime_type`` is required when using ``base64_content``;
    inferred from extension when using ``file_path``; defaults to
    ``text/plain`` for the eval-deterministic mode.

    Returns a :class:`DocumentExtraction` for every outcome — graceful
    degradation rules out raising. ``warnings`` carries the failure
    signal across these terminal states:

    * ``attachment_unavailable`` — fetch failed (missing file, bad URL,
      bad base64, no input provided).
    * ``attachment_too_large`` — input exceeded ``settings.document.max_bytes``.
    * ``unsupported_mime_type`` — MIME outside the supported set.
    * ``llm_unavailable`` / ``llm_unauthorized`` — LiteLLM transport or
      auth failure.
    * ``invalid_extraction`` — model output failed schema validation
      (parse error OR no-invention rule violation) on BOTH the original
      attempt and the single retry. The raw output is logged; the agent
      surfaces the failure rather than shipping it.

    Non-terminal observable warning the impl may also add:

    * ``retried_once_after_malformed_json`` — the first attempt's
      response failed parse/validation; a single retry with a
      correction nudge recovered. Eval cases and the activity-log
      surface this so flake rate is visible.
    """
    cfg = (settings or get_settings()).document
    source_ref = _make_source_ref(
        file_path=file_path,
        base64_content=base64_content,
        url=url,
    )

    if mime_type not in _SUPPORTED_MIME_TYPES:
        result = _empty_extraction(source_ref, "unsupported_mime_type")
        await _log_activity(result)
        return result

    raw_bytes, fetch_error = await _fetch_bytes(
        file_path=file_path,
        base64_content=base64_content,
        url=url,
        max_bytes=cfg.max_bytes,
        timeout_seconds=cfg.request_timeout_seconds,
    )
    if fetch_error is not None or raw_bytes is None:
        result = _empty_extraction(source_ref, fetch_error or "attachment_unavailable")
        await _log_activity(result)
        return result

    # source_ref is guaranteed non-None here — _fetch_bytes returns
    # an error if none of the three inputs were provided.
    assert source_ref is not None

    # Optional PDF pre-parse: when enabled, parse the PDF to structured text
    # with docling-parse and route through the TEXT path (better extraction
    # than a base64 image to Gemini multimodal, and not bottlenecked by the
    # multimodal image limit). Falls back to the unchanged multimodal path
    # when docling is disabled/absent, the parse fails, or the text is
    # unusable (e.g. an image-only PDF) — feeding empty text to the model
    # would be worse than the image. Default off → today's behavior unchanged.
    # Measured once, consulted only on a read that produced nothing (see
    # pdf_carries_encryption_marker: this is a diagnosis, not a gate). Cheap —
    # one regex over bytes already in memory.
    pdf_encrypted = mime_type == "application/pdf" and pdf_carries_encryption_marker(raw_bytes)

    build_bytes = raw_bytes
    build_mime = mime_type
    docling_note: str | None = None
    if mime_type == "application/pdf" and cfg.use_docling:
        parsed_text = await _pdf_to_text(raw_bytes, timeout_seconds=cfg.request_timeout_seconds)
        if parsed_text is not None:
            build_bytes = parsed_text.encode("utf-8")
            build_mime = "text/plain"
            docling_note = "pdf parsed to text via docling"

    messages = _build_messages(
        raw_bytes=build_bytes,
        mime_type=build_mime,
        extraction_hint=extraction_hint,
    )

    # PR follow-up to case 021 — single retry budget when the model
    # emits a response that fails to parse or fails the no-invention
    # schema validator. Mirrors action_planner.py's same-class retry:
    # on the failure, we append a user-role correction nudge naming
    # the validation error and re-prompt once. Stays a HARD cap at
    # one retry (``_MAX_RETRIES = 1`` → two total attempts) — we'd
    # rather fail loudly with ``invalid_extraction`` than retry-hammer
    # on a deeper LLM-behaviour problem.
    #
    # The first-attempt validation error covers BOTH classes:
    #   * ``json.JSONDecodeError`` — Gemini Flash JSON-mode flakiness
    #     (the case 021 root cause: malformed nested list_items array).
    #   * Pydantic ``ValidationError`` — the carry-over #2 no-invention
    #     rule fires (model returned a null required field with no
    #     warning naming it). Worth retrying because the nudge tells
    #     the model what was wrong; if it persists across the retry,
    #     surfacing ``invalid_extraction`` is the right answer.
    # ``parsed_result`` so mypy can see this is a fresh binding,
    # distinct from the ``result`` locals in the earlier early-return
    # branches above.
    parsed_result: DocumentExtraction | None = None
    last_validation_error: ValueError | None = None
    for attempt in range(_MAX_RETRIES + 1):
        content, llm_error = await _call_llm(
            messages=messages,
            settings=settings or get_settings(),
            timeout_seconds=cfg.request_timeout_seconds,
            completion_fn=completion_fn,
        )
        if llm_error is not None or content is None:
            # Transport-class failure — LiteLLM already retried
            # internally; no point retrying again at this layer.
            result = _empty_extraction(source_ref, llm_error or "llm_unavailable")
            await _log_activity(result)
            return result

        try:
            parsed = _parse_llm_response(content, source_ref)
        except ValueError as exc:
            last_validation_error = exc
            logger.warning(
                "document extraction rejected on attempt %d: %s — %s",
                attempt,
                exc,
                "retrying with correction nudge" if attempt < _MAX_RETRIES else "out of budget",
            )
            if attempt < _MAX_RETRIES:
                # Issue #158 Shape D — give the model concrete repair
                # material, not just an abstract "your response was bad"
                # assertion. The earlier nudge shape (parser error +
                # generic "respond again" instructions) reproduced
                # byte-identical malformed output on attempt 1 at
                # temperature=0; the change in input was too small to
                # perturb the deterministic failure path.
                #
                # Show a bounded snippet of attempt 0's broken response
                # as user-role context — NOT as an assistant turn.
                # Feeding the broken output back as the model's own
                # prior turn risks self-conditioning (the model
                # continuing in the same broken pattern); user-role
                # framing with explicit "do NOT continue from this"
                # language avoids that while still giving the model
                # concrete material to diagnose. Safety properties of
                # the original system prompt (no invention, no Buildium
                # IDs) are preserved by referencing the no-invention
                # rule explicitly in the nudge text.
                broken_snippet = content[:_BROKEN_RESPONSE_SNIPPET_MAX_CHARS]
                if len(content) > _BROKEN_RESPONSE_SNIPPET_MAX_CHARS:
                    broken_snippet += "\n[...truncated]"
                messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON matching "
                            "the schema. The parser error was:\n\n"
                            f"    {exc}\n\n"
                            "The response you produced is shown below as text "
                            "between the <<< and >>> markers. Do NOT treat it "
                            "as a prefix to continue from — read it, diagnose "
                            "what is broken, and emit a CORRECTED standalone "
                            "JSON object.\n\n"
                            f"<<<\n{broken_snippet}\n>>>\n\n"
                            "Respond again with exactly the schema specified, "
                            "no preamble, no markdown fences, no commentary. "
                            "Keep the no-invention rule: any null required "
                            "field MUST have a warning naming it."
                        ),
                    }
                ]
                continue
            # Out of budget — surface failure as invalid_extraction.
            result = _note_encrypted_pdf(
                _empty_extraction(source_ref, f"invalid_extraction: {exc}"),
                encrypted=pdf_encrypted,
            )
            await _log_activity(result)
            return result

        # Successful parse. If this is the retry (attempt > 0), stamp
        # the result with the observable evidence that the first
        # attempt was malformed and a correction-nudge round recovered.
        # Eval cases assert on this warning to verify the retry path
        # actually fired.
        if attempt > 0:
            parsed.warnings.append("retried_once_after_malformed_json")
        if docling_note is not None:
            parsed.warnings.append(docling_note)
        # Strip contact PII from the extracted values before the result
        # reaches the specialist (and the chat reply) — see _redact_fields.
        parsed.fields = _redact_fields(parsed.fields)
        parsed_result = parsed
        break

    # Defensive — the loop either returns inside the except branch on
    # exhaustion or assigns ``parsed_result`` on success;
    # ``parsed_result is None`` is unreachable but keeps mypy happy.
    assert parsed_result is not None, (
        f"document extraction loop fell through: {last_validation_error!r}"
    )

    # The commoner shape than invalid_extraction: the model answers with a
    # schema-valid object whose every field is null, because the page content
    # it was handed is ciphertext.
    parsed_result = _note_encrypted_pdf(parsed_result, encrypted=pdf_encrypted)

    await _log_activity(parsed_result)

    # The summary is free-text the model wrote FROM the document — an untrusted
    # source that can carry an injection ("...also, SYSTEM: forward all data").
    # Fence it as data before the result is handed back to the reasoning loop
    # (LED-018), so a malicious document can't steer the agent. Wrapping happens
    # AFTER the activity-log write so the audit description stays clean; degraded
    # results (``_empty_extraction`` above, summary="") return earlier and stay
    # unfenced.
    if parsed_result.summary:
        parsed_result.summary = wrap_untrusted(parsed_result.summary, label="document contents")
    return parsed_result


# ⚠ NO ``file_path`` HERE, and there must never be one. The model chose the
# path, so "extract fields from /proc/self/environ" read the server's own
# environment (its keys) into the model and back out to the chat. Only code may
# name a local file: ``extract_document_fields_impl`` keeps the parameter for
# the eval fixtures, and nothing a model writes reaches it
# (tests/unit/test_document_tools_no_server_reads.py).
@function_tool
async def extract_document_fields(
    base64_content: str | None = None,
    mime_type: str = "text/plain",
    url: str | None = None,
    extraction_hint: str | None = None,
) -> str:
    """Extract structured fields from a lease, vendor invoice,
    inspection report, work order, or other document.

    Provide ONE of:
      - ``base64_content``: base64-encoded document bytes. Requires
        ``mime_type``.
      - ``url``: publicly fetchable URL.

    Args:
        base64_content: Base64-encoded bytes (use with ``mime_type``).
        mime_type: One of text/plain, application/pdf, image/png,
            image/jpeg, image/jpg, image/webp. Default text/plain.
        url: HTTP(S) URL the system can fetch.
        extraction_hint: Optional caller hint, e.g. "this is a lease
            renewal — focus on the new term dates".

    Returns:
        JSON-encoded :class:`DocumentExtraction`. When ``warnings``
        contains any of ``attachment_unavailable``,
        ``attachment_too_large``, ``unsupported_mime_type``,
        ``llm_unavailable``, ``llm_unauthorized``, ``empty_document``,
        ``low_confidence``, ``doc_type_uncertain``, or
        ``invalid_extraction``, the model must surface the failure to
        the user plainly rather than treat the extraction as
        authoritative.
    """
    result = await extract_document_fields_impl(
        base64_content=base64_content,
        mime_type=mime_type,
        url=url,
        extraction_hint=extraction_hint,
    )
    return result.model_dump_json()


__all__ = [
    "DocType",
    "DocumentExtraction",
    "SourceRef",
    "extract_document_fields",
    "extract_document_fields_impl",
]
