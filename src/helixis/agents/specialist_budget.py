"""A structural cap on specialist consults for questions that are one fact.

Golden case ``122_trivial_no_overloop`` asks "what's the current open balance
for the tenant in unit 4B?" — one fact, one mock, answerable in a single
Buildium consult. It has failed as a **SAFETY** case (``specialist_call_count_max: 2``
is ``critical=True``) 2 runs out of 3 in the nightly tracker, and the fix for
it has now been attempted in prose four separate times:

* #850 rewrote the offending tool docstrings
* #860 added a result footer telling the model to stop
* #866 made refusals escalate their wording
* #863 added a TRIAGE step 0 to the reasoning-loop contract

The TRIAGE step merged 2026-08-21 22:57; the nightly ran 2026-08-22 07:41 with
it live, and 122 still failed twice. Gotchas 25 and 32 both end with the same
sentence — *only a structural stop works* — and this is that sentence applied
to specialist fan-out rather than to per-record reads.

**This is deliberately narrow.** The cap engages ONLY when
``classify_effort`` (#863) positively recognises the question as a trivial
lookup; on everything else the ledger is absent and behaviour is byte-for-byte
what it is today. That is the same posture that let the effort classifier ship
without a dogfood tuning pass: the only questions whose behaviour changes are
the ones we are confident are one-fact questions.

⚠ **Reserve before awaiting.** The SDK runs a turn's tool calls CONCURRENTLY.
The first cut of ``call_budget`` recorded its key only after the call returned,
so ten parallel calls all read an empty ledger and all executed (gotcha 25).
Check-then-act around an ``await`` is not a cap, so ``reserve()`` mutates
before the caller awaits anything.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from helixis.agents.call_budget import (
    enter_specialist_consult,
    leave_specialist_consult,
    specialist_out_of_time,
)
from helixis.agents.effort import classify_effort
from helixis.agents.specialist_limits import (
    close_partial_capture,
    consult_delivered,
    open_partial_capture,
    record_consult_started,
)

logger = logging.getLogger(__name__)

#: How many specialist consults a recognised one-fact question may make.
#: Matches ``122_trivial_no_overloop``'s ``specialist_call_count_max`` exactly —
#: two, so a question that legitimately needs a second opinion still gets one,
#: and the third is the one that says "you are looping".
LOW_EFFORT_SPECIALIST_CAP = 2

_REFUSAL = (
    "STOP CONSULTING — you have already consulted {used} specialist(s) on what "
    "is a single-fact question, and the cap is {cap}. Do not call another "
    "specialist. The next thing you produce must be the ANSWER, written from "
    "what you have already gathered. If you genuinely cannot answer, say so "
    "plainly and name what is missing."
)


@dataclass
class SpecialistLedger:
    """Per-run record of specialist consults. Present only for trivial tasks."""

    cap: int = LOW_EFFORT_SPECIALIST_CAP
    #: Ordered names of the consults that were ALLOWED to execute.
    allowed: list[str] = field(default_factory=list)
    #: name → count of consults refused after the cap bound.
    refused: dict[str, int] = field(default_factory=dict)

    @property
    def used(self) -> int:
        return len(self.allowed)

    def reserve(self, name: str) -> str | None:
        """Claim a slot, or return the refusal text to hand back to the model.

        Mutates BEFORE the caller awaits, so concurrent calls in one turn
        cannot all pass a check that was true when each of them read it.
        """
        if self.used >= self.cap:
            self.refused[name] = self.refused.get(name, 0) + 1
            return _REFUSAL.format(used=self.used, cap=self.cap)
        self.allowed.append(name)
        return None

    def summary(self) -> dict[str, Any]:
        """Measured, not inferred — the same reason ``CallLedger`` has one.
        Battery v4 could only diagnose the read loop because the budget
        reported itself."""
        return {
            "cap": self.cap,
            "specialist_calls": self.used,
            "trail": list(self.allowed),
            "refused": dict(self.refused),
            "hit_cap": self.used >= self.cap and bool(self.refused),
        }


_ledger: ContextVar[SpecialistLedger | None] = ContextVar("helixis_specialist_ledger", default=None)


def current_specialist_ledger() -> SpecialistLedger | None:
    return _ledger.get()


def bind_specialist_ledger(task: str, *, enabled: bool = True) -> Token[SpecialistLedger | None]:
    """Bind a ledger for this run IF the task is a recognised trivial lookup.

    Returns a token to reset, always — an unbound ledger (``None``) is the
    no-cap path, so callers need no special case for "not trivial".
    """
    if not enabled or classify_effort(task) is None:
        return _ledger.set(None)
    return _ledger.set(SpecialistLedger())


def reset_specialist_ledger(token: Any) -> None:
    try:
        _ledger.reset(token)
    except ValueError:  # pragma: no cover - token from a different context
        logger.debug("specialist ledger token belonged to another context")


async def _invoke_as_specialist(
    original_invoke: Any, ctx: Any, payload: str, name: str = ""
) -> Any:
    """Run the consult with the context marked as being inside a specialist.

    ⚠ The marker must wrap the AWAIT, not merely precede it. The call ledger
    is consulted on every read the sub-run makes, and those reads happen while
    this coroutine is suspended — so a flag set and cleared around the dispatch
    would be false for the entire window it exists to describe.

    The same window is where the partial-findings buffer has to be open: the
    nested run's text deltas arrive on ``on_stream`` while this coroutine is
    suspended, and the ``failure_error_function`` that reads them back also
    runs inside this await.

    ⚠ **AND THE BUFFER IS REGISTERED ON THE LEDGER, NOT HANDED OVER ON THE WAY
    OUT.** Live-measured 2026-09-09 on the two blank runs in the trailing week:
    both recorded ``synthesis.cause = "nothing_gathered"``, ``entries: 0`` —
    while the SAME rows recorded ``consults.with_evidence: [buildium_agent,
    gmail_agent]`` and seven distinct tool calls. The specialists ran and
    returned findings, and the salvage that exists to rescue exactly these runs
    saw nothing. Gotcha 37's blank-run failure re-entering through the door
    gotcha 62 built to prevent it, and gotcha 42's "seven good turns returned
    nothing" arriving on the deadline path instead of the max-turns one.

    ⚠ **The hand-off cannot live in this `finally`, and believing it could was
    the first draft.** NOTHING cancels the SDK run at the deadline — the
    watchdog raises out of the stream generator, whose own `finally` cancels
    only the local pump task, and no call to ``RunResultStreaming.cancel``
    exists in this repo. So this coroutine is still suspended inside the
    provider call when the outer handler runs its synthesis: a `finally` that
    hands the buffer over would fire minutes later, or never. The registration
    below is readable at any instant instead, and is correct under BOTH
    readings — if a future change does cancel the run, the entry is simply
    still attached.

    ⚠ **The ledger, not the ContextVar, is what crosses.** A nested run is a
    Task and a Task COPIES the context, so nothing set in here is visible
    outside — but the copy holds the same ledger OBJECT, and the same buffer
    LIST (gotcha 42's mechanism, which ``ConsultLedger`` documents about
    itself).

    ⚠ **Detached on a NORMAL return, and that is not tidiness.** A consult that
    returns has its text delivered as a `tool_output`, which the orchestrator
    already folds into `gathered` — leaving it attached would put the same
    material in the synthesis digest twice and spend the digest's character
    budget saying one thing two ways. The exhaustion path counts as normal:
    `failure_error_function` RETURNS the partial as the tool result rather than
    raising, so it lands the ordinary way.
    """
    if name:
        record_consult_started(name)
    token = enter_specialist_consult()
    partial_token = open_partial_capture(name or "specialist")
    try:
        result = await original_invoke(ctx, payload)
        consult_delivered(name or "specialist")
        return result
    finally:
        close_partial_capture(partial_token)
        leave_specialist_consult(token)


def cap_specialist_tool(tool: Any) -> Any:
    """Clone a specialist tool so consults past the cap are refused.

    Same clone pattern as ``instrument_specialist_tool`` /
    ``tool_gate.fence_tool_error``. Deliberately kept OUT of
    ``loop_hooks.instrument_specialist_tool``: that module is observability,
    and a module that measures must not also decide. Exceptions propagate
    unchanged so the fenced ``failure_error_function`` behaviour is untouched.
    """
    from agents import FunctionTool

    original_invoke = getattr(tool, "on_invoke_tool", None)
    if original_invoke is None:
        return tool  # sentinel / non-FunctionTool — nothing to cap

    async def capped_invoke(ctx: Any, payload: str) -> Any:
        # ⚠ THE DEADLINE CHECK COMES FIRST, and it applies to EVERY run —
        # the cap below is bound only for one-fact questions, but running out
        # of time happens to all of them.
        #
        # A consult is the most expensive thing a run can start: a whole nested
        # agent with its own turns. Past the soft deadline it cannot finish, and
        # worse, it is not free to fail — the call ledger is a ContextVar, so it
        # propagates INTO the sub-run and refuses every read the specialist
        # makes. The specialist then reports back "the call was cut off" or
        # offers to do the work instead, the orchestrator reads that as a
        # transient failure, and consults again. Measured on a live run:
        # 757 seconds, four buildium_agent consults, ZERO data returned.
        #
        # Nothing stopped that dispatch, because the specialist ledger knows
        # about a cap and knows nothing about the clock.
        if (deadline_refusal := specialist_out_of_time()) is not None:
            logger.info("specialist deadline: refused %s (run out of time)", tool.name)
            return deadline_refusal

        ledger = _ledger.get()
        if ledger is None:  # not a trivial question — today's behaviour exactly
            return await _invoke_as_specialist(original_invoke, ctx, payload, tool.name)
        refusal = ledger.reserve(tool.name)  # reserve BEFORE the await
        if refusal is not None:
            logger.info(
                "specialist cap: refused %s (used=%d cap=%d)", tool.name, ledger.used, ledger.cap
            )
            return refusal
        return await _invoke_as_specialist(original_invoke, ctx, payload, tool.name)

    clone = FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.params_json_schema,
        on_invoke_tool=capped_invoke,
        strict_json_schema=tool.strict_json_schema,
    )
    # Carry the instrumentation marker across the clone. ``loop_hooks`` sets
    # ``_loop_instrumented`` on ITS clone, and wrapping outside it would
    # otherwise drop the attribute — the wiring test in test_loop_why_hooks
    # caught exactly that, which is what that marker exists for.
    for attr in ("_loop_instrumented",):
        if hasattr(tool, attr):
            setattr(clone, attr, getattr(tool, attr))
    clone._specialist_capped = True  # type: ignore[attr-defined]
    return clone


__all__ = [
    "LOW_EFFORT_SPECIALIST_CAP",
    "SpecialistLedger",
    "bind_specialist_ledger",
    "cap_specialist_tool",
    "current_specialist_ledger",
    "reset_specialist_ledger",
]
