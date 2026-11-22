"""An unanswered approval gate must not cost the run five minutes twice.

WHAT THIS IS DEFENDING
----------------------
``_wait_for_confirmation`` blocks 300s and the stall watchdog PAUSES for the
whole wait (``confirm_wait["pending"]``). Both are correct alone; together
they leave a run that raises unanswered gates with no upper bound.

The live table settles that it is not hypothetical
(``helixis_pending_confirmations``, 2026-08-25/26):

    t_1787751705321_0  EXPORT  13:42:40 → 13:47:40   300.2s
    t_1787751705321_0  EXPORT  13:49:13 → 13:54:13   300.2s
    t_1787751705321_0  EXPORT  13:54:41 → 13:59:42   300.3s
    t_1787751705321_0  EXPORT  14:01:03 → 14:06:03   300.2s

Twenty-four minutes on one question. The misclassification that raised those
particular gates is fixed in ``connectors/tool_gate.py``, but a correctly
gated write that the user does not answer, and the model retries, is the same
shape — so the bound is tested here, independently of the classifier.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents import tools as agent_tools
from helixis.agents.confirm_budget import (
    RUN_EXPIRED_GATE_CEILING,
    ConfirmBudget,
    approval_is_unreachable,
    bind_confirm_budget,
    current_confirm_budget,
    reset_confirm_budget,
)


def test_an_unbound_run_is_unbounded() -> None:
    """⚠ The blast-radius guard. Scripts, the webhook worker and every test
    that predates this file run with no budget bound, and must behave
    byte-for-byte as before — a tool that waits the full timeout still does."""
    assert current_confirm_budget() is None
    assert approval_is_unreachable() is False


def test_one_full_silence_ends_the_waiting() -> None:
    """The rule. A gate that ran the whole window with nobody answering is
    evidence about the USER, not about that gate, so it carries to the rest of
    the run."""
    budget = ConfirmBudget()
    assert budget.approval_is_unreachable() is False

    budget.record_raised()
    budget.record_expiry(300.2)
    assert budget.approval_is_unreachable() is True
    assert budget.wasted_s == pytest.approx(300.2)


def test_a_gate_still_in_flight_proves_nothing() -> None:
    """⚠ Reads ``expired``, never ``raised`` — and the difference is load
    bearing.

    The SDK runs a turn's tool calls CONCURRENTLY: the live table shows three
    ``EXPORT`` gates minted inside the same second. If ``raised`` gated the
    decision, the first of those three would silence its own siblings — and a
    present user, looking right at three cards, would find two of them dead.
    Each concurrent gate deserves its own window; what is bounded is the batch
    AFTER one comes back empty.
    """
    budget = ConfirmBudget()
    budget.record_raised()
    budget.record_raised()
    budget.record_raised()
    assert budget.approval_is_unreachable() is False


def test_an_answered_gate_never_counts_against_the_run() -> None:
    """Deny is an ANSWER. A user who reads a card and says no is present, and
    the next gate must still get its full window — otherwise declining one
    write silently disarms approval for the rest of the turn."""
    budget = ConfirmBudget()
    for _ in range(5):
        budget.record_raised()
        budget.record_answered()
    assert budget.approval_is_unreachable() is False
    assert budget.wasted_s == 0.0


def test_the_ceiling_and_the_docstring_cannot_drift() -> None:
    """The module says "one". Pin it, so raising the constant is a deliberate
    edit to a test rather than a quiet change of behaviour."""
    assert RUN_EXPIRED_GATE_CEILING == 1


def test_the_summary_names_the_wasted_seconds() -> None:
    """``wasted_s`` is the only place this cost is visible. Every other clock
    in the run is paused for exactly that window by design — the stall
    watchdog's ``active`` accumulator most of all — so an approval stall is
    invisible in ``watchdog_active_s``, invisible in tool counts, and
    invisible in token spend."""
    budget = ConfirmBudget()
    budget.record_raised()
    budget.record_expiry(300.2)
    budget.record_raised()
    budget.record_skipped()
    assert budget.summary() == {
        "raised": 2,
        "answered": 0,
        "expired": 1,
        "skipped": 1,
        "wasted_s": 300.2,
    }


# ── The wait path itself ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_second_gate_of_a_silent_run_returns_immediately() -> None:
    """⚠ End to end, against the real ``_wait_for_confirmation``, with a real
    clock.

    A unit test of ``ConfirmBudget`` alone cannot see this: the whole defect is
    that the WAIT does not consult the budget. Both sides or neither
    (gotcha 28).

    The first gate is given a tiny timeout so the test can afford to let it
    genuinely expire; the second must then come back without waiting at all.
    """
    token = bind_confirm_budget()
    try:
        started = asyncio.get_event_loop().time()
        first = await agent_tools._wait_for_confirmation("gate-1", timeout=0.05)
        second = await agent_tools._wait_for_confirmation("gate-2", timeout=30.0)
        elapsed = asyncio.get_event_loop().time() - started

        assert first == (False, None)
        assert second == (False, None)
        # The second gate's 30s timeout must not have been entered. Generous
        # bound: the point is 30s vs ~0s, not the precise figure.
        assert elapsed < 5.0, f"the second gate waited — {elapsed:.1f}s total"

        budget = current_confirm_budget()
        assert budget is not None
        assert budget.expired == 1, "the first gate must be recorded as a real expiry"
        assert budget.skipped == 1, "the second must be recorded as skipped, not expired"
        assert budget.raised == 2
    finally:
        reset_confirm_budget(token)


@pytest.mark.asyncio
async def test_the_first_gate_still_waits_for_a_real_answer() -> None:
    """⚠ The guard against fixing this by simply never waiting.

    The cheapest way to make the test above pass is to stop blocking — which
    would delete the write confirmation gate, the one structural control that
    makes an irreversible Buildium write require a human. So: an approval that
    arrives must still reach a waiting run and must still return True.
    """
    token = bind_confirm_budget()
    try:

        async def _answer() -> None:
            await asyncio.sleep(0.05)
            await agent_tools.resolve_confirmation("gate-live", approved=True)

        answerer = asyncio.create_task(_answer())
        approved, _edits = await agent_tools._wait_for_confirmation("gate-live", timeout=30.0)
        await answerer

        assert approved is True, "an approved gate must still execute its write"
        budget = current_confirm_budget()
        assert budget is not None
        assert budget.answered == 1
        assert budget.expired == 0
        assert budget.approval_is_unreachable() is False
    finally:
        reset_confirm_budget(token)


@pytest.mark.asyncio
async def test_concurrent_gates_each_get_their_own_window() -> None:
    """⚠ The behaviour ``test_a_gate_still_in_flight_proves_nothing`` asserts
    on the dataclass, proven through the real wait path.

    Three gates minted in the same second — the live shape — must all wait and
    all expire. If the skip were keyed on ``raised``, two of the three would
    return instantly and a user staring at three cards could only answer one.
    """
    token = bind_confirm_budget()
    try:
        results = await asyncio.gather(
            *(agent_tools._wait_for_confirmation(f"gate-p{i}", timeout=0.05) for i in range(3))
        )
        assert results == [(False, None)] * 3
        budget = current_confirm_budget()
        assert budget is not None
        assert budget.expired == 3, f"all three should have waited, got {budget.summary()}"
        assert budget.skipped == 0
    finally:
        reset_confirm_budget(token)


@pytest.mark.asyncio
async def test_the_budget_survives_the_as_tool_boundary() -> None:
    """⚠ A nested specialist run is an asyncio Task, and a Task COPIES the
    context — so a ``ContextVar.set`` inside it is invisible out here
    (gotcha 42). The budget crosses the boundary only because it is MUTATED by
    reference, and a refactor to "rebind a fresh one" would silently restore
    the unbounded behaviour inside every ``.as_tool()`` consult, which is
    exactly where the Drive gates were raised.
    """
    token = bind_confirm_budget()
    try:

        async def _nested_consult() -> None:
            # Stands in for a specialist sub-run raising its own gate.
            await agent_tools._wait_for_confirmation("gate-nested", timeout=0.05)

        await asyncio.create_task(_nested_consult())

        budget = current_confirm_budget()
        assert budget is not None
        assert budget.expired == 1, (
            "a gate raised inside a nested Task did not reach the parent's budget — "
            "the specialist consults would each get an unbounded 300s again"
        )
    finally:
        reset_confirm_budget(token)
