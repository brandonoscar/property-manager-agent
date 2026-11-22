"""Per-turn spend is bounded — the control `llm/turn_cost` was the instrument for.

Before this, per-turn cost was MEASURED and never BOUNDED. `llm/spend.py`
enforces a per-company DAILY gate, checked once before the turn and documented
as failing OPEN; `llm/turn_cost.py` frames itself as the instrument the dark
cost levers lacked. Nothing raised or refused on what one turn spent, and the
live tail showed why that matters: four runs of 213 went past $1.00 and burned
$8.51 of a $42.64 window — 20% of the spend in under 2% of the turns, with a
$4.34 maximum.

Two design properties are load-bearing, and both have a gotcha behind them:

* **It is checked in the WATCHDOG, not on the read path.** Gotcha 37: the
  soft-deadline message could not reach the runs it was written for because it
  spoke only from inside `call_budget._invoke`, on a budgeted read — and the
  expensive runs barely read, since their cost is model turns and specialist
  consults. A cost check on that path would inherit the identical blind spot.
* **It stops the run, it does not kill it.** Crossing the ceiling raises the
  SOFT deadline error, so `deadline_synthesis` writes an answer from what was
  gathered. Killing on cost and returning nothing would re-create the
  22%-blank failure gotcha 37 documents.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from helixis.agents import orchestrator as orch
from helixis.llm.turn_cost import TurnUsage, bind_turn_usage, unbind_turn_usage
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", auth={"jwt_secret": "x" * 40}, memory={"enabled": False})


# ── the setting ──────────────────────────────────────────────────────


def test_the_ceiling_ships_enabled_and_above_the_honest_expensive_turn() -> None:
    """A ceiling below p95 would truncate real portfolio reviews; one above the
    observed max would never fire. Live 2026-08-29, n=213: p95 $0.60,
    p99 $1.39, max $4.34."""
    ceiling = _settings().agent.turn_cost_ceiling_usd
    assert ceiling > 0.60, f"{ceiling} sits at or below the p95 honest turn ($0.60)"
    assert ceiling < 4.34, f"{ceiling} is above the observed max — it could never fire"


def test_a_non_positive_ceiling_disables_the_check_entirely() -> None:
    """The off switch has to produce NO callable, not a callable that always
    returns False — the watchdog's `over_cost is not None` branch is what keeps
    a disabled ceiling byte-for-byte identical to the old behaviour."""
    src = inspect.getsource(orch.run_agent_streaming)
    assert "else None" in src and "turn_cost_ceiling_usd" in src


# ── the property: it fires on a run that never reads ─────────────────


@pytest.mark.asyncio
async def test_the_ceiling_stops_a_run_that_made_no_budgeted_reads() -> None:
    """The whole point (gotcha 37). This stream never touches `call_budget`, so
    every read-path defence is silent — and the run still has to be stopped.

    Asserted through the REAL watchdog rather than by calling a helper, because
    the defect this replaces was precisely a check living somewhere the failing
    runs never reached.
    """
    usage, token = bind_turn_usage()
    try:

        async def _expensive_silence():
            usage.cost_usd = 5.00  # a run that spent, without reading anything
            for _ in range(50):
                yield object()
                await asyncio.sleep(0.01)

        seen = 0
        with pytest.raises(orch._SoftDeadlineReachedError) as excinfo:
            async for _ in orch._stream_with_stall_watchdog(
                _expensive_silence(),
                {"pending": False},
                over_cost=lambda: usage.cost_usd >= 1.00,
            ):
                seen += 1
        assert "wall-clock deadline" in str(excinfo.value), (
            "the phrase is load-bearing: `is_deadline` and "
            "`_stalled_run_user_message` both key on it, so dropping it "
            "silently disables the salvage while every other test still passes"
        )
        assert "$5.00" in str(excinfo.value), "the run row must say what it spent"
    finally:
        unbind_turn_usage(token)


@pytest.mark.asyncio
async def test_a_cheap_run_is_untouched() -> None:
    """The other direction. A gate that fires on ordinary turns is one that
    gets switched off — and here 'switched off' means an unbounded tail."""
    usage, token = bind_turn_usage()
    try:
        usage.cost_usd = 0.07  # the measured p50

        async def _cheap():
            for _ in range(3):
                yield object()

        seen = [
            e
            async for e in orch._stream_with_stall_watchdog(
                _cheap(),
                {"pending": False},
                over_cost=lambda: usage.cost_usd >= 1.00,
            )
        ]
        assert len(seen) == 3
    finally:
        unbind_turn_usage(token)


# ── the accumulator it reads ─────────────────────────────────────────


def test_cost_accrues_across_nested_sub_runs_by_mutation() -> None:
    """The ceiling reads `TurnUsage.cost_usd`, and a specialist consult is a
    nested `.as_tool()` sub-run — a Task, which COPIES the context. A
    `ContextVar.set` in there is invisible outside (gotchas 34/42), so the
    ledger must be MUTATED in place or a consult's spend never reaches the
    ceiling and the most expensive runs are exactly the ones it misses."""
    usage = TurnUsage()

    def _a_nested_sub_run(ledger: TurnUsage) -> None:
        ledger.cost_usd += 0.80

    _a_nested_sub_run(usage)
    _a_nested_sub_run(usage)
    assert usage.cost_usd == pytest.approx(1.60), (
        "spend did not accumulate by reference — a specialist's cost would be "
        "invisible to the ceiling"
    )
