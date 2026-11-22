"""Free-form image assessment — Gemini multimodal (#533).

The document extractor (:mod:`helixis.agents.document_tools`) reads STRUCTURED
documents into typed fields (lease / invoice / …). A maintenance photo has no
fields to extract — "a picture of a leak" lands as ``other`` and does nothing
useful. This tool is the free-form counterpart: it *looks at* an image and says
what it sees.

Two modes, one tool:

* **maintenance photo** → what's the issue, the likely trade
  (plumbing / electrical / HVAC / …), how severe/urgent, a suggested action,
  and a DRAFT work order the PM can approve.
* **handwritten / scanned notice** → transcribe it + summarize (don't force it
  into lease/invoice fields).

Reuses ``document_tools``' fetch + LLM plumbing wholesale (``_fetch_bytes``,
``_make_source_ref``, ``_call_llm``, the base64→``image_url`` shape, the JSON
coercion). Same discipline: never invent, **draft only — never auto-act**,
graceful degradation (every outcome returns an :class:`ImageAssessment`, never
raises), PII-redact free text, activity-log every call.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from agents import function_tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from helixis.agents.document_tools import (
    SourceRef,
    _call_llm,
    _coerce_json_object,
    _fetch_bytes,
    _make_source_ref,
)
from helixis.agents.image_orientation import upright_image_bytes
from helixis.db import engine as db_engine
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.memory.context import current_memory_context
from helixis.security.pii import redact
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)


ImageKind = Literal["maintenance_photo", "handwritten_notice", "other"]
Trade = Literal[
    "plumbing",
    "electrical",
    "hvac",
    "appliance",
    "structural",
    "pest",
    "landscaping",
    "general",
    "unknown",
]
Severity = Literal["none", "low", "medium", "high", "emergency"]


# Images assess natively; text/plain is the eval-deterministic mode (a synthetic
# photo DESCRIPTION fixture stands in for the real image so cases are stable —
# same trick document_tools uses).
_SUPPORTED_MIME_TYPES = frozenset(
    {
        "text/plain",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
    }
)

# One retry on parse/validation failure (2 attempts total) — mirrors
# document_tools' budget. Beyond one retry it's a real model-behaviour problem
# worth surfacing, not a JSON-mode flake to hammer.
_MAX_RETRIES = 1


class DraftWorkOrder(BaseModel):
    """A DRAFT work order the PM reviews and approves — never auto-filed."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=600)
    trade: Trade
    priority: Literal["routine", "urgent", "emergency"]


class ImageAssessment(BaseModel):
    """Structured envelope every ``assess_image`` invocation returns.

    The ``model_validator`` enforces two no-invention rules at the type layer:
    a maintenance photo must carry an ``issue_summary``, and a handwritten
    notice must carry a ``transcription`` — otherwise the model is claiming a
    kind it didn't actually read. A low-confidence read must say so.
    """

    model_config = ConfigDict(extra="forbid")

    image_kind: ImageKind
    confidence: float = Field(ge=0.0, le=1.0)
    # maintenance_photo fields (empty/None for other kinds)
    issue_summary: str = ""
    likely_trade: Trade | None = None
    severity: Severity | None = None
    suggested_action: str = ""
    draft_work_order: DraftWorkOrder | None = None
    # handwritten_notice field
    transcription: str = ""
    # always
    summary: str = ""
    source_ref: SourceRef
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_requires_its_evidence(self) -> ImageAssessment:
        if self.image_kind == "maintenance_photo" and not self.issue_summary.strip():
            if not any("issue_summary" in w for w in self.warnings):
                raise ValueError(
                    "image_kind 'maintenance_photo' with empty issue_summary and no "
                    "warning naming it — schema forbids claiming a maintenance photo "
                    "without describing the issue."
                )
        if self.image_kind == "handwritten_notice" and not self.transcription.strip():
            if not any("transcription" in w for w in self.warnings):
                raise ValueError(
                    "image_kind 'handwritten_notice' with empty transcription and no "
                    "warning naming it — schema forbids claiming a notice without "
                    "transcribing it."
                )
        return self


# ── helpers ──────────────────────────────────────────────────────


def _redact_free_text(a: ImageAssessment) -> None:
    """Strip contact PII (email / phone / SSN / card / #ids) from the free-text
    fields before the assessment leaves the tool — the image analogue of
    document_tools._redact_fields. Names / addresses / issue descriptions are
    preserved (redact() is contact-PII-scoped)."""
    a.issue_summary = redact(a.issue_summary) if a.issue_summary else a.issue_summary
    a.suggested_action = redact(a.suggested_action) if a.suggested_action else a.suggested_action
    a.transcription = redact(a.transcription) if a.transcription else a.transcription
    a.summary = redact(a.summary) if a.summary else a.summary
    if a.draft_work_order is not None:
        a.draft_work_order.scope = redact(a.draft_work_order.scope)


def _empty(source_ref: SourceRef | None, warning: str | Sequence[str]) -> ImageAssessment:
    """A degraded assessment. Accepts a sequence so a failed model call can
    carry its cause beside the generic token (see ``_empty_extraction``)."""
    ref = source_ref or SourceRef(kind="base64", id="unknown")
    warnings = [warning] if isinstance(warning, str) else list(warning)
    return ImageAssessment(image_kind="other", confidence=0.0, source_ref=ref, warnings=warnings)


def _build_messages(*, raw_bytes: bytes, mime_type: str, hint: str | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    instr = "Assess this image per the schema in the system prompt. Output one JSON object only."
    if hint:
        instr += f" Hint from caller: {hint}"
    blocks.append({"type": "text", "text": instr})
    if mime_type == "text/plain":
        text = raw_bytes.decode("utf-8", errors="replace")
        blocks.append({"type": "text", "text": f"IMAGE DESCRIPTION (eval mode):\n\n{text}"})
    else:
        # A phone stores its photo sideways and records how it was held in an
        # EXIF tag; a model that ignores that tag reads a rotated image. Done
        # HERE because this is the one point where bytes become the payload
        # the model actually sees — and it is a no-op, byte-for-byte, for any
        # image not flagged sideways (see image_orientation).
        encoded = base64.b64encode(upright_image_bytes(raw_bytes, mime_type)).decode("ascii")
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT_IMAGE},
        {"role": "user", "content": blocks},
    ]


def _parse(raw: str, source_ref: SourceRef) -> ImageAssessment:
    if raw is None:
        raise ValueError("LLM returned empty content")
    try:
        data, coerced = _coerce_json_object(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"LLM response was not a JSON object: type={type(data).__name__}")
    data["source_ref"] = source_ref.model_dump()
    assessment = ImageAssessment.model_validate(data)
    if coerced:
        assessment.warnings.append("recovered_noisy_json_attempt_0")
    return assessment


async def _log_activity(result: ImageAssessment) -> None:
    """Best-effort activity-log write (mirrors document_tools._log_activity)."""
    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    if not company_id:
        return
    description = redact(result.summary or f"{result.image_kind} assessment")[:200]
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="chat",
        action_type="image_assessed",
        description=description,
        user_id=(ctx.user_id if ctx else None) or None,
        raw={
            "image_kind": result.image_kind,
            "confidence": result.confidence,
            "likely_trade": result.likely_trade,
            "severity": result.severity,
            "source_ref": result.source_ref.model_dump(),
            "warnings": list(result.warnings),
            "assessed_at": result.assessed_at.isoformat(),
        },
    )
    await write_activity(pool, entry)


# ── system prompt ────────────────────────────────────────────────


SYSTEM_PROMPT_IMAGE = """\
You are the image assessor for Occupella, a property-management assistant. You
look at ONE image a property manager received and return ONE JSON object
matching the ImageAssessment schema. The image is usually one of:

- a MAINTENANCE PHOTO (a tenant texted a picture of a problem — a leak, a
  broken appliance, mold, a damaged fixture), or
- a HANDWRITTEN or SCANNED NOTICE (a note, a posted sign, a form).

## Schema

{
  "image_kind": "maintenance_photo" | "handwritten_notice" | "other",
  "confidence": float in [0.0, 1.0],
  "issue_summary": "what is wrong, plain English (maintenance_photo only, else \\"\\")",
  "likely_trade": <trade> or null,   // <trade> = one of the values in "Trades" below
  "severity": "none"|"low"|"medium"|"high"|"emergency"|null,
  "suggested_action": "the next step you'd suggest (maintenance_photo only, else \\"\\")",
  "draft_work_order": { "scope": "...", "trade": <trade>, "priority": <priority> } or null,
      // <priority> = "routine" | "urgent" | "emergency"
  "transcription": "verbatim text (handwritten_notice only, else \\"\\")",
  "summary": "1-2 sentence plain-English summary",
  "warnings": ["string", ...]
}

OMIT ``source_ref`` and ``assessed_at`` — the caller fills them in.

Trades (the ``<trade>`` values): plumbing, electrical, hvac, appliance,
structural, pest, landscaping, general, unknown.

## Rules (hard)

1. NEVER invent. Describe only what is visibly in the image. If you cannot
   tell what the problem is, set ``severity``/``likely_trade`` to the
   uncertain value ("unknown"/null) and say so in ``summary`` — do not guess a
   trade to look decisive.

2. DRAFT ONLY. ``draft_work_order`` is a PROPOSAL for the property manager to
   review and approve. You are NOT filing it. Never phrase it as done.

3. For a maintenance_photo: populate ``issue_summary`` (required — a
   maintenance photo you can't describe is really ``other``), your best
   ``likely_trade`` + ``severity``, a concrete ``suggested_action``, and a
   ``draft_work_order`` when the issue is actionable. Reserve
   ``severity: "emergency"`` for active hazards (gas, flooding water, exposed
   live wiring, no heat in freezing weather).

4. For a handwritten_notice: fill ``transcription`` with the text verbatim
   (required) and ``summary`` with what it says. Leave the maintenance fields
   empty. Transcribe illegible words as ``[illegible]`` — never fabricate.

5. If confidence is below 0.6, set ``confidence`` accordingly AND append
   ``"low_confidence"`` to ``warnings``. Do not inflate confidence.

6. If the image is blank, unrelated, or unreadable, set
   ``image_kind: "other"``, ``confidence: 0.0``, and
   ``warnings: ["unreadable_image"]``.

7. NEVER include a person's email, phone number, or any government/card
   identifier in any field — describe the issue, not the identity.

## Output

Respond with exactly one JSON object matching the schema. No preamble, no
markdown fences, no commentary outside the JSON.
"""


# ── implementation ───────────────────────────────────────────────


async def assess_image_impl(
    file_path: str | None = None,
    base64_content: str | None = None,
    mime_type: str = "text/plain",
    url: str | None = None,
    hint: str | None = None,
    *,
    settings: Settings | None = None,
    completion_fn: Any | None = None,
) -> ImageAssessment:
    """Assess one image (maintenance photo or handwritten notice).

    Exactly one of ``file_path`` / ``base64_content`` / ``url``. Returns an
    :class:`ImageAssessment` for every outcome — never raises. ``warnings``
    carries the failure signal: ``attachment_unavailable``,
    ``attachment_too_large``, ``unsupported_mime_type``, ``llm_unavailable`` /
    ``llm_unauthorized``, ``unreadable_image``, ``low_confidence``, or
    ``invalid_assessment`` (model output failed schema validation on both the
    original attempt and the single retry)."""
    resolved = settings or get_settings()
    cfg = resolved.document  # reuse the document tool's size/timeout budget
    source_ref = _make_source_ref(file_path=file_path, base64_content=base64_content, url=url)

    if mime_type not in _SUPPORTED_MIME_TYPES:
        result = _empty(source_ref, "unsupported_mime_type")
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
        result = _empty(source_ref, fetch_error or "attachment_unavailable")
        await _log_activity(result)
        return result
    assert source_ref is not None

    messages = _build_messages(raw_bytes=raw_bytes, mime_type=mime_type, hint=hint)

    parsed_result: ImageAssessment | None = None
    for attempt in range(_MAX_RETRIES + 1):
        content, llm_error = await _call_llm(
            messages=messages,
            settings=resolved,
            timeout_seconds=cfg.request_timeout_seconds,
            completion_fn=completion_fn,
        )
        if llm_error is not None or content is None:
            result = _empty(source_ref, llm_error or "llm_unavailable")
            await _log_activity(result)
            return result
        try:
            parsed = _parse(content, source_ref)
        except ValueError as exc:
            logger.warning("image assessment rejected on attempt %d: %s", attempt, exc)
            if attempt < _MAX_RETRIES:
                messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON matching the "
                            f"schema. The error was:\n\n    {exc}\n\nEmit a CORRECTED "
                            "standalone JSON object — schema only, no fences, no "
                            "commentary. Keep the no-invention rule."
                        ),
                    }
                ]
                continue
            result = _empty(source_ref, f"invalid_assessment: {exc}")
            await _log_activity(result)
            return result
        if attempt > 0:
            parsed.warnings.append("retried_once_after_malformed_json")
        _redact_free_text(parsed)
        parsed_result = parsed
        break

    assert parsed_result is not None
    await _log_activity(parsed_result)
    return parsed_result


# No ``file_path``: the model must never name a file on this server. See the
# same note above ``document_tools.extract_document_fields``.
@function_tool
async def assess_image(
    base64_content: str | None = None,
    mime_type: str = "text/plain",
    url: str | None = None,
    hint: str | None = None,
) -> str:
    """Look at a photo or scanned/handwritten note and describe what it shows —
    for a MAINTENANCE PHOTO (leak, broken appliance, damage): the issue, the
    likely trade, severity, a suggested action, and a DRAFT work order; for a
    HANDWRITTEN/SCANNED NOTICE: a transcription + summary.

    Use this for free-form images ("what's wrong in this photo the tenant
    sent?"), NOT for structured documents like leases/invoices/inspection
    reports — those go to ``extract_document_fields``.

    Provide ONE of ``base64_content`` (with ``mime_type``) / ``url``.
    ``mime_type`` is one of text/plain, image/png, image/jpeg,
    image/jpg, image/webp. ``hint`` is an optional caller nudge.

    Returns a JSON ``ImageAssessment``. The ``draft_work_order`` is a PROPOSAL
    for the user to approve — nothing is filed automatically.
    """
    result = await assess_image_impl(
        base64_content=base64_content,
        mime_type=mime_type,
        url=url,
        hint=hint,
    )
    return result.model_dump_json()


__all__ = [
    "DraftWorkOrder",
    "ImageAssessment",
    "ImageKind",
    "Severity",
    "Trade",
    "assess_image",
    "assess_image_impl",
]
