"""Audit trail for code-execution runs (Phase 0).

Arbitrary agent-authored code is the highest-authority thing this product can
do, and until now it was the *least* observed: the orchestrator's generic
``record_tool_span`` recorded ``tool:run_computation`` with the first 2000
characters of its OUTPUT and nothing else — no code, no approved plan, no
duration, no indication of whether the user clicked approve or cancel. You
could see that a computation happened. You could not answer "what ran against
company X on the 14th", which is the only question an audit trail exists to
answer (SOC 2 pack, #58).

This module emits that missing record as one structured Langfuse event per run.

**What deliberately does NOT go to Langfuse: the code itself.** Agent-written
Python routinely embeds tenant data as literals (a rent roll pasted into a
list, a tenant name in an f-string), and Langfuse is third-party telemetry
bound by the ``security.pii`` "no identifiers in logs/telemetry" contract. So
the event carries a **sha256 of the code** — enough to prove two runs were
identical, to pin a run to a reviewed snippet, and to detect drift — while the
plain-English ``plan`` and any artifact filenames go through ``redact`` on the
way out, exactly as every tool span already does.

``sandbox_id`` / ``substrate`` are recorded from the first event even though
the current substrate is a local subprocess with no sandbox id. Fixing the
shape now means the swap to a real isolated substrate changes the *value* of a
field rather than the schema of the event, so dashboards built on this survive
the migration.

Best-effort throughout: an audit failure logs at debug and never raises into
the agent loop. Observability must never break the product — but note the
asymmetry with the role floor, which fails CLOSED. Losing a log line is
acceptable; losing a permission check is not.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from helixis.observability import tracing
from helixis.observability.context import current_trace_context
from helixis.security.pii import redact

logger = logging.getLogger(__name__)

#: Langfuse event name. Stable — dashboards and the SOC 2 export key off it.
EVENT_NAME = "codeexec.run"

#: Cap on the redacted plan text carried in the event.
_MAX_PLAN_CHARS = 1_000

#: Cap on how many artifact names are listed (the count is always exact).
_MAX_ARTIFACTS = 25


def code_fingerprint(code: str) -> str:
    """Stable sha256 of the executed code.

    The audit identifier for a run. Never the code itself — see the module
    docstring on why the source stays out of third-party telemetry.
    """
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def build_audit_payload(
    *,
    company_id: str | None,
    user_id: str | None,
    code: str,
    plan: str,
    approved: bool,
    ok: bool,
    duration_ms: int,
    artifacts: list[str] | None = None,
    error: str | None = None,
    timed_out: bool = False,
    sandbox_id: str | None = None,
    substrate: str = "subprocess",
) -> dict[str, Any]:
    """Build the structured record for one run. Pure — no I/O, never raises.

    Split from the emit so the redaction and shape can be tested without a
    Langfuse client, which is the half that actually needs pinning.
    """
    names = list(artifacts or [])
    payload: dict[str, Any] = {
        "company_id": company_id or "unknown",
        "user_id": user_id or "unknown",
        "substrate": substrate,
        "sandbox_id": sandbox_id,
        "code_sha256": code_fingerprint(code),
        "code_bytes": len((code or "").encode("utf-8")),
        # The user approved the PLAN, so the plan is the thing worth keeping —
        # redacted, because it is model-written prose about real tenants.
        "plan": redact((plan or "").strip())[:_MAX_PLAN_CHARS],
        "approved": approved,
        "ok": ok,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "artifact_count": len(names),
        "artifacts": [redact(n) for n in names[:_MAX_ARTIFACTS]],
    }
    if error:
        payload["error"] = redact(str(error))[:500]
    return payload


def record_run(**kwargs: Any) -> None:
    """Emit one run's audit record under the current agent trace.

    Accepts the same keywords as :func:`build_audit_payload`. A run that was
    never approved, that failed, or that timed out is recorded exactly like one
    that succeeded — an audit trail that only keeps the happy path is not an
    audit trail. Failures here are swallowed by design.
    """
    try:
        ctx = current_trace_context()
        trace_id = ctx.trace_id if ctx else ""
        if not trace_id:
            return
        payload = build_audit_payload(**kwargs)
        tracing.record_event(
            trace_id=trace_id,
            name=EVENT_NAME,
            payload=payload,
            level="DEFAULT" if payload.get("ok") else "WARNING",
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a run
        logger.debug("codeexec audit record failed (non-fatal)", exc_info=True)


__all__ = ["EVENT_NAME", "build_audit_payload", "code_fingerprint", "record_run"]
