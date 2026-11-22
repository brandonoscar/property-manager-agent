"""Past the deadline, a consult costs everything and returns nothing.

One live run, 2026-08-25: **757 seconds, four ``buildium_agent`` consults, zero
data returned.** What the specialists actually said —

    "I attempted to pull the vendor list, but the tool call didn't return
     usable vendor data — it came back with an interrupted/incomplete response."

    "If you'd like, my first move would be to pull the full active vendor list…"

Neither is carelessness. Both are the honest reply to a message aimed at
somebody else. The call ledger is a ``ContextVar``, so it propagates INTO
``.as_tool()`` sub-runs and refuses a specialist's reads with
*"the next thing you produce must be the ANSWER"* — an instruction only the
orchestrator can carry out. A specialist has no user and writes no final
answer, so it reports our own deadline as a provider fault (the orchestrator
retries) or asks permission from a program that cannot grant it.

And nothing stopped the dispatch, because ``specialist_budget`` knew about a
cap and knew nothing about the clock.

Two fixes, both needed. Refusing the consult stops the waste; wording the read
refusal for its actual reader stops the retry loop for consults already in
flight when the deadline passes.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents.call_budget import (
    CallLedger,
    _ledger_var,
    in_specialist_consult,
    specialist_out_of_time,
)
from helixis.agents.specialist_budget import _invoke_as_specialist, cap_specialist_tool


def _expired() -> CallLedger:
    """A ledger whose soft deadline has already passed."""
    ledger = CallLedger(deadline_s=100.0)
    ledger.started -= 99.0  # 99s of a 100s deadline; soft fires at 70
    assert ledger.out_of_time, "fixture is wrong — the ledger is not past the soft deadline"
    return ledger


def _fresh() -> CallLedger:
    ledger = CallLedger(deadline_s=100.0)
    assert not ledger.out_of_time
    return ledger


# ── refusing the consult ────────────────────────────────────────────────────


def test_no_ledger_means_no_opinion() -> None:
    """An unbound run behaves byte-for-byte as before."""
    assert specialist_out_of_time() is None


def test_a_fresh_run_may_consult() -> None:
    token = _ledger_var.set(_fresh())
    try:
        assert specialist_out_of_time() is None
    finally:
        _ledger_var.reset(token)


def test_past_the_deadline_a_consult_is_refused() -> None:
    token = _ledger_var.set(_expired())
    try:
        refusal = specialist_out_of_time()
        assert refusal is not None
        assert "NOT CONSULTED" in refusal
        # A bare "no" invites a retry that lands in the same place, so the
        # refusal has to say what to do instead (gotcha 32).
        assert "what you already have" in refusal
        assert "refused identically" in refusal, (
            "must say a retry is pointless, or the orchestrator reads this as transient"
        )
    finally:
        _ledger_var.reset(token)


class _Tool:
    """Minimal stand-in for a FunctionTool clone."""

    def __init__(self) -> None:
        self.name = "buildium_agent"
        self.description = "consult"
        self.params_json_schema: dict[str, object] = {}
        self.strict_json_schema = True
        self.calls = 0

    async def on_invoke_tool(self, ctx: object, payload: str) -> str:
        self.calls += 1
        return "specialist result"


@pytest.mark.asyncio
async def test_the_capped_tool_does_not_dispatch_past_the_deadline() -> None:
    """⚠ Reads BOTH sides (gotcha 28): the deadline predicate AND the wrapper
    that consults it. The predicate returning a string is worth nothing if the
    tool still opens the nested run."""
    pytest.importorskip("agents")
    tool = _Tool()
    capped = cap_specialist_tool(tool)
    token = _ledger_var.set(_expired())
    try:
        result = await capped.on_invoke_tool(None, "{}")
    finally:
        _ledger_var.reset(token)
    assert tool.calls == 0, "the nested agent run must never start"
    assert "NOT CONSULTED" in result


@pytest.mark.asyncio
async def test_a_fresh_run_still_dispatches() -> None:
    """The narrowness, as an executable claim — this must not become a cap on
    consults generally."""
    pytest.importorskip("agents")
    tool = _Tool()
    capped = cap_specialist_tool(tool)
    token = _ledger_var.set(_fresh())
    try:
        result = await capped.on_invoke_tool(None, "{}")
    finally:
        _ledger_var.reset(token)
    assert tool.calls == 1
    assert result == "specialist result"


# ── wording the read refusal for its actual reader ──────────────────────────


@pytest.mark.asyncio
async def test_the_marker_is_true_for_the_whole_await_not_just_the_dispatch() -> None:
    """⚠ The subtlety that would make this silently useless.

    The ledger is consulted on every read the sub-run makes, and those reads
    happen while the consult coroutine is SUSPENDED. A flag set and cleared
    around the dispatch would be false for the entire window it exists to
    describe — present in the source, absent in effect.
    """
    seen: list[bool] = []

    async def _inner(ctx: object, payload: str) -> str:
        seen.append(in_specialist_consult())  # a read at the start
        await asyncio.sleep(0)  # …and one after a suspension
        seen.append(in_specialist_consult())
        return "ok"

    assert in_specialist_consult() is False
    await _invoke_as_specialist(_inner, None, "{}")
    assert seen == [True, True], "the marker must hold across suspension points"
    assert in_specialist_consult() is False, "and must not leak past the consult"


@pytest.mark.asyncio
async def test_a_failed_consult_still_clears_the_marker() -> None:
    async def _boom(ctx: object, payload: str) -> str:
        raise RuntimeError("specialist exploded")

    with pytest.raises(RuntimeError):
        await _invoke_as_specialist(_boom, None, "{}")
    assert in_specialist_consult() is False


@pytest.mark.asyncio
async def test_a_consult_does_not_mark_a_concurrent_orchestrator_read() -> None:
    """ContextVars are copied into each Task, so a consult running beside an
    orchestrator read must not reword the orchestrator's refusal. If this ever
    fails, the two messages have started bleeding into each other."""

    async def _slow_consult(ctx: object, payload: str) -> str:
        await asyncio.sleep(0.02)
        return "ok"

    async def _orchestrator_read() -> bool:
        await asyncio.sleep(0.01)
        return in_specialist_consult()

    consult = asyncio.create_task(_invoke_as_specialist(_slow_consult, None, "{}"))
    outside = asyncio.create_task(_orchestrator_read())
    _, saw_marker = await asyncio.gather(consult, outside)
    assert saw_marker is False


def test_the_two_refusals_say_different_things_to_different_readers() -> None:
    """⚠ The whole point, as one assertion.

    The orchestrator is told to write the answer. A specialist CANNOT write the
    answer — telling it to is what produced "if you'd like, my first move would
    be…", a request for permission from a program. It is told to report instead,
    and told the cut-off is ours rather than the provider's, because "the call
    was interrupted" is what made the orchestrator retry.
    """
    from helixis.agents.call_budget import _OUT_OF_TIME, _OUT_OF_TIME_SPECIALIST

    assert "must be the ANSWER" in _OUT_OF_TIME
    assert "must be the ANSWER" not in _OUT_OF_TIME_SPECIALIST

    assert "Report NOW" in _OUT_OF_TIME_SPECIALIST
    assert "NOT a provider failure" in _OUT_OF_TIME_SPECIALIST
    assert "not by a person" in _OUT_OF_TIME_SPECIALIST
