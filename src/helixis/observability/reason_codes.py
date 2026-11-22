"""Escalation reason codes — a stable vocabulary for "the agent stopped /
degraded / blocked" moments, emitted to Langfuse as events + scores.

Why: the individual guardrails already *log* when they fire (planner signal
drops, Buildium auth failures, REVERSE_REPORT blocks, budget cutoffs, stream
stalls), but each in its own ad-hoc shape — there was no one place to ask
"how often does Helixis bail out, and why?" before a pilot. Each seam now
also calls :func:`record_reason_code` with a slug from ``REASON_CODES``, so
the Langfuse dashboard can chart per-code counts over time (``reason:<code>``
scores) and drill into any single firing (the ``reason_code`` event payload).

Discipline (matches ``observability.tracing``):
* Best-effort, never raises — observability must not break the path it
  observes.
* No PII in ``detail`` — pass short internal strings (reason slugs, action
  verbs, status codes), never message bodies or names. The tracing layer
  redacts spans, but events pass through, so the contract lives here.
* ``trace_id`` is optional: seams inside an agent run pass the run's trace
  id; seams outside one (worker planner path, pre-run budget gate) omit it
  and we fall back to the request-scoped trace context, or just the log line
  when there is no trace at all.
"""

from __future__ import annotations

import logging

from helixis.observability import tracing
from helixis.observability.context import current_trace_context

logger = logging.getLogger(__name__)

# The stable vocabulary. Add codes here (and only here) so the dashboard
# never fragments across spellings.
REASON_CODES: frozenset[str] = frozenset(
    {
        "planner_signal_dropped",  # signal_validator removed hallucinated signals
        "buildium_auth_error",  # 401 from Buildium — the credentials were rejected
        # 403 from Buildium: the credentials were ACCEPTED and the request
        # refused — an API-key permission or a plan/subscription limit. It had
        # no code of its own and was filed under buildium_auth_error, so an
        # operator asking "how many customers need to reconnect" was counting
        # customers whose connection is fine. Different cause, different remedy,
        # different code.
        "buildium_permission_denied",
        "rr_write_blocked",  # REVERSE_REPORT guardrail rejected a write
        "budget_exceeded",  # daily token budget cutoff refused a run
        "stream_stall",  # SDK stream went silent past the stall budget
        "call_budget_refused",  # per-record read cap stopped a fan-out loop
        "wall_clock_deadline",  # run worked past its clock (NOT a stall)
        # The deadline RESCUE failed and the user got nothing. The detail says
        # which of the three exits fired, because they need opposite fixes:
        # nothing gathered / the provider call blew up / the model returned an
        # empty completion. See ``agents.deadline_synthesis``.
        "deadline_synthesis_empty",
        # A LIVE answer matched a deterministic fair-housing steering pattern.
        # The 41x golden cases gate this shape at BUILD time and nothing looked
        # at production replies, so the first anyone would have known was a
        # complaint. The user-facing half is an advisory footer (#271 forbids a
        # runtime gate on prose); this is the half an operator can act on.
        "fair_housing_flagged",
        # A cross-SKU failover served part of a turn: the primary model failed
        # transiently and a smaller sibling answered. The user gets an advisory
        # footer; this is the half that can be counted, and the DETAIL names
        # the SKU because that is what an operator needs and what must not be
        # in the reply. Gotcha 86 is why it is worth a code at all — the
        # failover SKU was measured answering differently on fair-housing
        # prompts, so "which turns did a backup write" is a real question.
        "model_failover",
        # The MODEL itself ended at Confidence: unverified — it could not stand
        # behind the answer it just wrote. Previously that word sat in a footer
        # a property manager may not read as "do not act on this", and nothing
        # counted it, so "how often are we unsure" had no answer.
        #
        # ⚠ Read BEFORE the claim guards, which rewrite the same line when they
        # fire. This code means the model volunteered doubt; the scope/source/
        # no-evidence/failover cases are separate facts with their own codes.
        # The detail is the WORD only — the reason clause is model prose about
        # the user's portfolio, and this channel carries no message bodies.
        "low_confidence_answer",
    }
)


def record_reason_code(
    code: str,
    *,
    detail: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Emit one escalation reason code. Never raises.

    Always logs (the floor that works with tracing off). When a trace id is
    available — passed in or from the current trace context — also records a
    Langfuse event (``reason_code``, WARNING level, drill-down payload) and a
    score (``reason:<code>``, value 1.0, dashboard-aggregatable).
    """
    try:
        if code not in REASON_CODES:
            # Emit anyway (losing the datapoint is worse), but flag the
            # unknown slug so it gets added to the vocabulary.
            logger.warning("reason_code %r not in REASON_CODES — add it to the vocabulary", code)

        logger.warning("escalation reason_code=%s detail=%s", code, detail or "")

        resolved = trace_id
        if not resolved:
            ctx = current_trace_context()
            resolved = ctx.trace_id if ctx else ""
        if not resolved:
            return

        payload: dict[str, str] = {"code": code}
        if detail:
            payload["detail"] = detail[:500]
        tracing.record_event(
            trace_id=resolved,
            name="reason_code",
            payload=payload,
            level="WARNING",
        )
        tracing.record_score(
            trace_id=resolved,
            name=f"reason:{code}",
            value=1.0,
            comment=detail[:200] if detail else None,
        )
    except Exception:  # noqa: BLE001 — observability must never break the caller
        logger.debug("record_reason_code failed (non-fatal)", exc_info=True)


__all__ = ["REASON_CODES", "record_reason_code"]
