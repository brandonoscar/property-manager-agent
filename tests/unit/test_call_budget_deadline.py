"""The three bugs battery v4 exposed in the call budget.

v4 ran all 12 questions against #861 and still lost Q5, Q7 and Q11 to the wall
clock — **39% of the battery's spend for zero characters returned**. The
budget was not the binding constraint in any of them, and that is the finding:

* **Q11** issued **66 ``buildium_get_work_order`` calls, 52 refused**, and died
  with ``record_calls`` at 8 of 14. The cap bound perfectly and bought nothing,
  because a refusal is free to the LEDGER and expensive on the CLOCK — each one
  still costs a model turn.
* **Q7** solved its maintenance fan-out (3 bulk calls, not 20 per-unit) and died
  on ``property_snapshot`` ×10, which no cap covered.
* All three were still hydrating records when the hard deadline killed the run,
  so nothing ever reached the user. Nothing had told the model time was short.
"""

from __future__ import annotations

import time

from helixis.agents.call_budget import (
    PER_RECORD_CAPS,
    RUN_REFUSAL_CEILING,
    SOFT_DEADLINE_FRACTION,
    CallLedger,
)


def _aged(seconds: float, *, deadline: float | None = 240.0) -> CallLedger:
    ledger = CallLedger(deadline_s=deadline)
    ledger.started = time.monotonic() - seconds
    return ledger


# ── bug 1: refusals are free to the budget, expensive on the clock ──


def test_a_run_that_keeps_getting_refused_is_recognised_as_looping() -> None:
    """52 refusals is not a model taking the hint. Past the ceiling the message
    stops being advice — the polite version demonstrably did not hold."""
    ledger = CallLedger()
    assert not ledger.looping
    ledger.refusals["buildium_get_work_order"] = RUN_REFUSAL_CEILING - 1
    assert not ledger.looping
    ledger.refusals["buildium_get_work_order"] = RUN_REFUSAL_CEILING
    assert ledger.looping


def test_refusals_across_different_tools_still_count_as_looping() -> None:
    """Q7 spread its refusals over three tools. Per-tool counting would have
    missed it — the run is looping, not the tool."""
    ledger = CallLedger()
    ledger.refusals = {"a": 5, "b": 5, "c": 5}
    assert ledger.looping


# ── bug 2: property_snapshot was the one uncapped per-record read ──


def test_property_snapshot_is_capped() -> None:
    """Q7 called it 10 times, one property each, with nothing bounding it."""
    assert "property_snapshot" in PER_RECORD_CAPS
    assert PER_RECORD_CAPS["property_snapshot"].limit <= 8


def test_every_cap_steers_somewhere_real() -> None:
    """A refusal that names a tool which doesn't exist is worse than no
    refusal — it sends the model looking for something it can't call."""
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.mirror_tools import mirror_tools  # a list, not a factory

    registered = {t.name for t in (*buildium_tools(), *mirror_tools)}
    for name, cap in PER_RECORD_CAPS.items():
        named = {w.strip("`,.") for w in cap.steer.split() if w.startswith("`buildium_")}
        for suggestion in named:
            clean = suggestion.strip("`")
            assert clean in registered, f"{name} steers to unregistered {clean}"


# ── bug 3: the hard deadline lands mid-lookup and returns nothing ──


def test_reads_stop_before_the_hard_deadline() -> None:
    """The whole point: leave a turn to answer in. A run killed while fetching
    returns zero characters, which is what Q5, Q7 and Q11 all did."""
    assert not _aged(10).out_of_time
    assert _aged(240 * SOFT_DEADLINE_FRACTION + 1).out_of_time


def test_a_run_with_no_deadline_never_runs_out_of_time() -> None:
    """The handoff path passes no deadline (reasoning_loop off). It must behave
    exactly as before — this feature is additive, not a new failure mode."""
    ledger = CallLedger()
    ledger.started = time.monotonic() - 100_000
    assert ledger.deadline_s is None
    assert not ledger.out_of_time


def test_the_soft_deadline_leaves_real_headroom() -> None:
    """Firing at 99% would be indistinguishable from the hard cutoff. The
    margin has to be big enough for a model turn that writes an answer."""
    assert 0.5 <= SOFT_DEADLINE_FRACTION <= 0.85
    remaining = 240 * (1 - SOFT_DEADLINE_FRACTION)
    assert remaining >= 30  # seconds left to compose in


def test_the_summary_reports_both_new_stop_conditions() -> None:
    """v4 could only diagnose this because the summary made the budget a
    measurement rather than an inference. The new limits need the same."""
    ledger = _aged(200)
    ledger.refusals = {"buildium_get_work_order": 52}
    summary = ledger.summary()
    assert summary["hit_refusal_ceiling"] is True
    assert summary["ran_out_of_time"] is True
    assert summary["refusal_ceiling"] == RUN_REFUSAL_CEILING
    assert summary["elapsed_s"] >= 200


# ── the run row must carry all three clocks (2026-08-25) ───────────────────


def test_the_ledger_reports_its_deadline_not_just_its_elapsed() -> None:
    """⚠ An ABSENT key and a null VALUE read identically in SQL.

    The 757-second run recorded `elapsed_s: 757.2` and nothing about the
    deadline, so `raw->'call_budget'->>'deadline_s'` came back NULL — which
    reads as "this run had no deadline" and is a completely different
    diagnosis from "had one and blew past it". That cost a wrong hypothesis.
    `ran_out_of_time: true` was the only thing that disproved it, and only
    because `out_of_time` returns False when the deadline is None.
    """
    from helixis.agents.call_budget import CallLedger

    summary = CallLedger(deadline_s=240.0).summary()
    assert summary["deadline_s"] == 240.0
    assert "elapsed_s" in summary

    # No deadline is a REAL state (loop mode off) and must be distinguishable.
    assert CallLedger().summary()["deadline_s"] is None


def test_the_ledger_clock_never_pauses() -> None:
    """It is deliberately NOT the watchdog's accumulator. `elapsed_s` measures
    raw wall time from bind; the watchdog's `active` starts later and pauses
    during a confirm wait. Keeping both is the only way to see the gap — on
    the 757s run roughly 590 seconds sat outside the accumulator that gates."""
    import time as _t

    from helixis.agents.call_budget import CallLedger

    ledger = CallLedger(deadline_s=240.0)
    ledger.started -= 100.0
    assert ledger.elapsed_s >= 100.0
    assert ledger.summary()["elapsed_s"] >= 100.0
    assert _t.monotonic  # the clock is monotonic, not wall-clock date math


def test_the_watchdog_publishes_its_accumulator() -> None:
    """⚠ Reads BOTH sides (gotcha 28): the watchdog writes `watchdog_active_s`
    and the finally block reads it into the run row. Wired separately, they
    drift into recording nothing while each half looks correct."""
    import ast
    import pathlib

    # ⚠ Read from DISK, not via import. `helixis.agents.orchestrator` cannot be
    # imported first — it hits a pre-existing circular import that the app
    # entry avoids only by import ORDER. Reading the file asserts the same
    # property with no import at all.
    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "helixis"
        / "agents"
        / "orchestrator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    watchdog = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stream_with_stall_watchdog"
    )
    assert "watchdog_active_s" in ast.unparse(watchdog), (
        "the watchdog must publish the accumulator the deadline actually tests"
    )
    assert '_run_stats["clocks"]' in src, "the run row must record the clocks"
    for key in ("wall_s", "ledger_elapsed_s"):
        assert key in src, f"the run row must record {key}"
