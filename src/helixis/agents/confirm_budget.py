"""How much of a run's clock an UNANSWERED approval gate may consume.

WHY THIS EXISTS
---------------
``_wait_for_confirmation`` blocks for 300 seconds, and the stall watchdog
deliberately PAUSES for the whole wait (``confirm_wait["pending"]`` in
``orchestrator._stream_with_stall_watchdog``) — a human reading an approval
card is a legitimate silence. Both halves are correct in isolation. Together
they mean a run that raises a gate nobody answers has no upper bound at all:
nothing kills it, and it may raise another.

Measured on ``helixis_pending_confirmations`` (2026-08-25/26): session
``t_1787751705321_0`` raised four gates serially — 13:42, 13:49, 13:54,
14:01 — each expiring at ~300s. Twenty-four minutes on one question.
Session ``t_1787699525300_0`` spent 600s in two batches. The misclassified
Drive reads that provoked them are fixed in ``connectors/tool_gate.py``, but
the shape survives the fix: a *correctly* gated write that the user does not
answer, and that the model then retries, costs exactly the same.

THE RULE
--------
One gate that ran the full timeout with no answer is not bad luck; it is
evidence that nobody is looking at the screen. Every later gate in the SAME
run therefore returns immediately instead of waiting again. The user has lost
nothing they had — the first gate still got its full window, and re-sending
the message starts a fresh run with a fresh budget.

This is gotcha 32's finding pointed at the clock instead of the ledger: *a
refusal is free to the ledger and expensive on the wall*. There, refused reads
each cost a model turn; here, unanswered gates each cost five minutes.

⚠ NOT a cap on concurrent gates. The SDK runs a turn's tool calls in parallel
— the live table shows three ``EXPORT`` gates minted inside the same second —
and all three SHOULD wait, because each is a distinct action a present user
would answer separately. What this bounds is what happens AFTER the first one
comes back empty. That is why the budget is recorded on EXPIRY rather than
reserved before the await: there is no check-then-act race to lose (gotcha 25),
because the fact being recorded is one that has already finished happening.

⚠ Mutated by reference, never re-``set``. The nested specialist run is an
asyncio Task and a Task COPIES the context, so a ``ContextVar.set`` inside it
is invisible out here — the same mechanism ``LoopStats`` and ``CallLedger``
rely on to cross the ``.as_tool()`` boundary (gotcha 42).
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging

logger = logging.getLogger(__name__)

#: How many gates may run the FULL timeout unanswered before the run stops
#: waiting. One. A five-minute silence is not ambiguous, and the second
#: five minutes buys no information the first did not already provide.
RUN_EXPIRED_GATE_CEILING = 1


@dataclasses.dataclass
class ConfirmBudget:
    """What this run's approval gates cost, and whether to keep waiting."""

    #: Gates raised (whatever their outcome).
    raised: int = 0
    #: Gates that ran the full timeout with no answer.
    expired: int = 0
    #: Gates skipped because a previous one had already expired.
    skipped: int = 0
    #: Gates a human actually answered — approve or deny, both count.
    answered: int = 0
    #: Wall-clock seconds spent waiting on gates that were never answered.
    #: The number this module exists to stop growing.
    wasted_s: float = 0.0

    def record_raised(self) -> None:
        self.raised += 1

    def record_answered(self) -> None:
        self.answered += 1

    def record_expiry(self, waited_s: float) -> None:
        self.expired += 1
        self.wasted_s += max(0.0, waited_s)

    def record_skipped(self) -> None:
        self.skipped += 1

    def approval_is_unreachable(self) -> bool:
        """Has this run already proved nobody is answering?

        ⚠ Reads ``expired``, NOT ``raised``. A gate currently in flight proves
        nothing — the user may be reading it right now — and counting it would
        make the first batch of concurrent gates cancel each other.
        """
        return self.expired >= RUN_EXPIRED_GATE_CEILING

    def summary(self) -> dict[str, float | int]:
        return {
            "raised": self.raised,
            "answered": self.answered,
            "expired": self.expired,
            "skipped": self.skipped,
            "wasted_s": round(self.wasted_s, 1),
        }


_current_confirm_budget: contextvars.ContextVar[ConfirmBudget | None] = contextvars.ContextVar(
    "helixis_confirm_budget", default=None
)


def bind_confirm_budget() -> contextvars.Token[ConfirmBudget | None]:
    """Bind a fresh budget for this run. Call beside the confirm emitter."""
    return _current_confirm_budget.set(ConfirmBudget())


def reset_confirm_budget(token: contextvars.Token[ConfirmBudget | None]) -> None:
    _current_confirm_budget.reset(token)


def current_confirm_budget() -> ConfirmBudget | None:
    """The run's budget, or ``None`` outside a bound run.

    ``None`` means UNBOUNDED — a tool called from a test, a script, or the
    webhook worker waits the full timeout exactly as before. The bound-only
    behaviour is deliberate: this must not silently change anything that is
    not a live chat turn.
    """
    return _current_confirm_budget.get()


def approval_is_unreachable() -> bool:
    """Convenience read used by the wait path. Unbound → always False."""
    budget = current_confirm_budget()
    return budget is not None and budget.approval_is_unreachable()


__all__ = [
    "RUN_EXPIRED_GATE_CEILING",
    "ConfirmBudget",
    "approval_is_unreachable",
    "bind_confirm_budget",
    "current_confirm_budget",
    "reset_confirm_budget",
]
