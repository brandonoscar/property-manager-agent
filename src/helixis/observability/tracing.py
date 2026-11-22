"""Hierarchical run tracing — tool-call + handoff spans under one Langfuse trace.

LiteLLM's Langfuse callback (see :mod:`langfuse_setup`) already turns every
LLM call into a *generation*. What it can't see is the structure *around*
those calls: which tools ran, and which specialist the orchestrator handed
off to. This module adds that — one trace per agent run (a shared
``trace_id`` so the LLM generations group under it), plus a span per tool
call and an event per handoff.

Everything here is **best-effort**:

* Disabled (every call a no-op) when Langfuse keys aren't configured — same
  gate as ``configure_langfuse``.
* Wrapped so a tracing failure logs at debug and NEVER raises into the
  agent run. Observability must never break the product.

The client is separate from the one LiteLLM creates internally; Langfuse
merges everything sharing a ``trace_id`` server-side, so the spans here and
LiteLLM's generations land in the same hierarchical trace.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from helixis.security.pii import redact
from helixis.settings import Settings

logger = logging.getLogger(__name__)


def _redact_value(value: Any) -> Any:
    """Redact identifiers/PII from a span value before it leaves for Langfuse
    (third-party telemetry). Mirrors the DB tool-span redaction
    (``orchestrator_runs._redact``) — strings are redacted, containers
    recursed, everything else passed through. Tool outputs routinely carry
    tenant emails/phones/names (a Buildium/Gmail read), which must not land in
    traces as plaintext per the ``security.pii`` 'no identifiers in
    logs/telemetry' contract."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


# langfuse.Langfuse instance, or None when tracing is off.
_client: Any = None


def configure_tracer(settings: Settings) -> bool:
    """Create the Langfuse client used for run spans. Returns True when active.

    Mirrors ``configure_langfuse``'s gating: off when disabled or keys blank.
    """
    global _client
    cfg = settings.langfuse
    if not cfg.enabled or not cfg.public_key or not cfg.secret_key:
        _client = None
        return False
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
            host=cfg.host,
        )
        logger.info("Langfuse run-tracer enabled → %s", cfg.host)
        return True
    except Exception:  # noqa: BLE001 — never let observability break startup
        logger.exception("Failed to init Langfuse run-tracer — span tracing off")
        _client = None
        return False


def is_active() -> bool:
    return _client is not None


def new_trace_id() -> str:
    """A fresh trace id for one agent run. Shared with LiteLLM metadata so
    the run's LLM generations attach to the same trace."""
    return str(uuid.uuid4())


def model_slug(model_name: str) -> str:
    """Short, tag-safe model identifier for trace metadata.

    LiteLLM model names carry a provider prefix (``gemini/gemini-2.5-flash``)
    and occasionally a deployment/version suffix. For charting quality per
    model version we only want the bare model id, so drop the provider prefix
    (everything up to and including the last ``/``). Empty input → ``unknown``
    so a tag is always produced."""
    if not model_name:
        return "unknown"
    return model_name.rsplit("/", 1)[-1].strip() or "unknown"


def _truncate(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def start_trace(
    *,
    trace_id: str,
    name: str,
    company_id: str,
    session_id: str,
    tags: tuple[str, ...] = (),
    user_query: str | None = None,
) -> None:
    """Open the run's root trace. No-op when tracing is off.

    ⚠ **The user's own sentence was the one thing on this trace that was NOT
    redacted.** Every tool span under it goes through ``_redact_value``, and
    ``codeexec/audit.py`` deliberately sends a code sha256 rather than the
    source because "it embeds tenant literals and Langfuse is third-party
    telemetry" (gotcha 33). The question that produced all of it — routinely
    "email Marcus at 555-…", "what's the balance for marcus.w@example.com" —
    went verbatim. We scrubbed the machine's data and shipped the human's.

    ⚠ **Redaction here is pattern-only and does NOT remove names or street
    addresses.** Measured, not assumed:

        in:  Email Marcus Whitfield at marcus.w@example.com or 555-867-5309
             about unit 4B at 1700 W Washington St
        out: Email Marcus Whitfield at [redacted] or [redacted]
             about unit 4B at 1700 W Washington St

    Presidio runs without spaCy on purpose — the NLP pipeline was removed from
    the request path for RAM (gotcha 9), so there is no NER to catch a person
    or a place. Emails and phone numbers stop here; names and addresses do
    not. Say so plainly rather than letting the call read as "the query is
    clean now", because the next reader's decision depends on which it is.
    """
    if _client is None or not trace_id:
        return
    try:
        _client.trace(
            id=trace_id,
            name=name,
            user_id=company_id,
            session_id=session_id,
            tags=list(tags),
            input=_redact_value(_truncate(user_query)) if user_query else None,
        )
    except Exception:  # noqa: BLE001
        logger.debug("start_trace failed (non-fatal)", exc_info=True)


def record_tool_span(
    *,
    trace_id: str,
    name: str,
    output: Any = None,
    input: Any = None,
) -> None:
    """Record one tool call as a span under the run's trace."""
    if _client is None or not trace_id:
        return
    try:
        _client.span(
            trace_id=trace_id,
            name=f"tool:{name}",
            input=_redact_value(_truncate(input)),
            output=_redact_value(_truncate(output)),
        )
    except Exception:  # noqa: BLE001
        logger.debug("record_tool_span failed (non-fatal)", exc_info=True)


def record_handoff_event(*, trace_id: str, from_agent: str, to_agent: str) -> None:
    """Record an orchestrator→specialist handoff as an event under the trace."""
    if _client is None or not trace_id:
        return
    try:
        _client.event(
            trace_id=trace_id,
            name="handoff",
            input={"from": from_agent, "to": to_agent},
        )
    except Exception:  # noqa: BLE001
        logger.debug("record_handoff_event failed (non-fatal)", exc_info=True)


def record_event(
    *,
    trace_id: str,
    name: str,
    payload: dict[str, Any],
    level: str = "DEFAULT",
) -> None:
    """Record an arbitrary structured event under the run's trace.

    Used for run-level signals that aren't a tool call or a handoff — e.g. the
    browser_agent silent-terminal-state canary (issue #156). ``level`` follows
    Langfuse's observation levels ("DEFAULT" / "WARNING" / "ERROR").

    ⚠ The payload is redacted and truncated like every other observation on
    this trace. It used to go raw, which made this the one unguarded door into
    third-party telemetry: any caller adding an event could ship whatever it
    held, and the callers are exactly the paths that hold tenant data. The
    redactor leaves operational values alone — verified on a sha256, a company
    uuid, an ISO timestamp and the reason-code strings — so the audit trail
    keeps its meaning. Same limits as ``start_trace``: patterns, not names.
    """
    if _client is None or not trace_id:
        return
    try:
        _client.event(
            trace_id=trace_id,
            name=name,
            input=_redact_value(_truncate(payload)),
            level=level,
        )
    except Exception:  # noqa: BLE001
        logger.debug("record_event failed (non-fatal)", exc_info=True)


def record_score(
    *,
    trace_id: str,
    name: str,
    value: float,
    comment: str | None = None,
) -> None:
    """Attach a numeric score to the run's trace (dashboard-aggregatable).

    Paired with :func:`record_event` for signals we want both to drill into
    (the event payload) and to chart over time (the score)."""
    if _client is None or not trace_id:
        return
    try:
        _client.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment,
        )
    except Exception:  # noqa: BLE001
        logger.debug("record_score failed (non-fatal)", exc_info=True)


def flush() -> None:
    """Best-effort flush of buffered observations (call at run/shutdown end)."""
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:  # noqa: BLE001
        logger.debug("tracer flush failed (non-fatal)", exc_info=True)


__all__ = [
    "configure_tracer",
    "flush",
    "is_active",
    "model_slug",
    "new_trace_id",
    "record_event",
    "record_handoff_event",
    "record_score",
    "record_tool_span",
    "start_trace",
]
