"""The cost gate's missing producer, and the both-sides checks it needs.

`cost_budgets.check_measurement(task_class, ...)` takes a class name, and until
now **nothing in the product produced one** — a grep for the seven names in
`config/eval_budgets.yaml` found zero non-test references. That is the reason
the gate had no caller, and it is exactly gotcha 28's shape: a string crossing
a producer/consumer boundary with only one side written. So these guards read
BOTH sides — the YAML's keys against the classifier's outputs, and the
classifier's write set against the negative-safety corpus's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from helixis.evals.cost_budgets import METRICS, check_all, load_budgets
from helixis.evals.cost_drift import (
    CARD_TOOLS,
    CHAT_READ,
    RunRecord,
    answered,
    classify_task_class,
    summarise,
    write_tool_names,
)

_BUDGETS = Path(__file__).resolve().parents[2] / "config" / "eval_budgets.yaml"


def _read(latency: int = 10_000, tools: tuple[str, ...] = (), prose: bool = True) -> RunRecord:
    return RunRecord(latency_ms=latency, tool_names=tools, has_prose=prose)


# ── both sides: the class name the producer emits must exist in the YAML ──


def test_every_class_the_classifier_can_emit_exists_in_the_budget_file() -> None:
    """The gap this module closes. A producer emitting a name the budget file
    does not carry would look like it was working — `baseline_for` returns {}
    and `check_measurement` skips every metric, so the gate stays silent and
    nothing fails."""
    declared = set((yaml.safe_load(_BUDGETS.read_text(encoding="utf-8")) or {})["task_classes"])
    assert CHAT_READ in declared, (
        f"cost_drift emits {CHAT_READ!r} and eval_budgets.yaml has no such class — "
        "the measurement would be silently discarded"
    )


def test_the_populated_class_is_gated_on_every_metric_the_telemetry_records() -> None:
    """All four metrics are recordable for `chat_read`, so all four are gated.

    ⚠ This test first asserted the OPPOSITE — that `tokens`/`usd` must stay
    null because "no per-run source exists". It was wrong, and it would have
    locked the mistake in: `llm/turn_cost.py` writes cost and token counts into
    `orchestrator_runs.raw->'usage'` on every run since 2026-08-26. The error
    came from checking the column list and `helixis_llm_usage` and never
    opening `raw` — absence concluded from an incomplete lookup (gotcha 47).
    A guard written from a wrong premise does not fail; it defends the premise.
    """
    budgets = load_budgets(_BUDGETS)
    base = budgets.baseline_for(CHAT_READ)
    for metric in METRICS:
        assert base[metric] and base[metric] > 0, (
            f"{CHAT_READ}.{metric} has no baseline, but the run record carries it — "
            "an ungated metric is one a regression passes through silently"
        )


def test_the_six_write_classes_stay_ungated_while_they_are_unobservable() -> None:
    """Not tidiness — a baseline on a class nothing can measure gates nothing
    and reads as coverage. 1 write tool call in 1,620 recorded calls, whole
    history: writes run inside specialist sub-runs the column does not record.
    """
    budgets = load_budgets(_BUDGETS)
    for tc in budgets.baselines:
        if tc == CHAT_READ:
            continue
        assert all(budgets.baseline_for(tc)[m] is None for m in METRICS), (
            f"{tc} has a baseline but no run can be classified into it — either the "
            "run record now carries specialist tool calls (update cost_drift and this "
            "test), or the number is a guess"
        )


# ── both sides: the write set must match the negative-safety corpus's ──


def test_the_write_set_matches_the_negative_safety_corpus() -> None:
    """`classify_task_class` calls a turn `chat_read` when it sees no write
    tool, so a write this module does not recognise is filed as a read — a
    silent misclassification, never an error. The 32x negative-safety suite
    derives the authoritative set from the same live registries; if the two
    drift, one of them is wrong about what a write is."""
    from tests.unit.test_negative_should_not_act import WRITE_TOOLS

    assert write_tool_names() == frozenset(WRITE_TOOLS), (
        "cost_drift's write set has drifted from the negative-safety corpus's\n"
        f"  only in cost_drift: {sorted(write_tool_names() - set(WRITE_TOOLS))}\n"
        f"  only in the corpus: {sorted(set(WRITE_TOOLS) - write_tool_names())}"
    )


def test_a_turn_that_called_a_write_is_not_classified_at_all() -> None:
    """Declining beats guessing (gotcha 55). Filing a write turn under
    `chat_read` would drag a write's expensive latency into the read baseline;
    filing it under a guessed write class would invent an observation."""
    a_write = sorted(write_tool_names())[0]
    assert classify_task_class(_read(tools=(a_write,))) is None
    assert classify_task_class(_read(tools=("unpaid_bills",))) == CHAT_READ


# ── the answered rule, which decides the sample ──


def test_a_card_only_turn_counts_as_answered() -> None:
    """gotcha 56: cards are not in `final_text`, so counting prose alone drops
    card-heavy turns out of the sample and biases the median downward — the
    fast turns are exactly the ones most likely to be card-only."""
    card = sorted(CARD_TOOLS)[0]
    assert answered(_read(tools=(card,), prose=False)) is True
    assert answered(_read(tools=("unpaid_bills",), prose=False)) is False
    assert answered(_read(prose=True)) is True


def test_an_unanswered_run_never_reaches_the_median() -> None:
    """A run that produced nothing is a deadline death (gotcha 37), not a cheap
    turn. Letting one in makes the latency baseline look better the more often
    the product fails."""
    out = summarise([_read(latency=1_000, prose=False), _read(latency=9_000, prose=True)])
    assert out[CHAT_READ]["wall_clock_ms"] == 9_000


def test_a_missing_cost_is_skipped_not_counted_as_zero() -> None:
    """Runs before 2026-08-26 carry no `usage` object, and a floor-priced run
    is dropped on purpose. Reading either as 0 would drag the median toward
    zero in proportion to how much old data sits in the window — a cost metric
    that IMPROVES the further back you look, which is the most flattering
    possible way to be wrong (gotcha 57's unknown-must-not-collapse-into-zero).
    """
    priced = RunRecord(latency_ms=9_000, has_prose=True, usd=0.20, tokens=100_000)
    unpriced = RunRecord(latency_ms=9_000, has_prose=True)
    out = summarise([priced, unpriced, unpriced])
    assert out[CHAT_READ]["usd"] == 0.20, "an unpriced run was averaged in as zero"
    assert out[CHAT_READ]["tokens"] == 100_000

    none_priced = summarise([unpriced, unpriced])
    assert "usd" not in none_priced[CHAT_READ], (
        "usd is present with no priced run behind it — check_measurement would "
        "compare a fabricated number against the baseline"
    )
    assert "tokens" not in none_priced[CHAT_READ]


def test_a_class_with_no_qualifying_run_is_absent_not_zero() -> None:
    """A fabricated zero reads as an enormous improvement and can never trip
    the gate — the same decline-to-assert rule as gotcha 57's null count.

    ⚠ The empty-input cases below are not enough on their own, and mutation
    proved it: injecting `work_order_create: {wall_clock_ms: 0}` alongside a
    real result left all twelve green. It is invisible TODAY only because the
    write baselines are null and a null baseline is skipped — the day one is
    populated, that zero is a permanent free pass. So assert the RESULT'S
    SHAPE: only classes that actually had a qualifying run may appear.
    """
    assert summarise([]) == {}
    assert summarise([RunRecord(latency_ms=None, tool_names=(), has_prose=True)]) == {}

    only_reads = summarise([_read(latency=9_000), _read(latency=11_000)])
    assert set(only_reads) == {CHAT_READ}, (
        f"summarise invented classes with no qualifying run: {sorted(set(only_reads))} — "
        "a zero here can never exceed a baseline, so the class would be permanently "
        "un-gateable once someone fills it in"
    )


# ── the comparison actually gates ──


def test_a_window_median_well_over_the_baseline_is_flagged() -> None:
    """End to end against the SHIPPED file, so a future edit that nulls the
    baseline back out fails here rather than going quiet."""
    budgets = load_budgets(_BUDGETS)
    baseline = budgets.baseline_for(CHAT_READ)["wall_clock_ms"]
    assert baseline is not None
    over = int(baseline * 2)
    violations = check_all(summarise([_read(latency=over)] * 3), budgets)
    assert any(v.metric == "wall_clock_ms" for v in violations), (
        f"a window median of {over}ms against a {baseline:g}ms baseline was not flagged"
    )


@pytest.mark.parametrize("factor", [0.5, 1.0, 1.2])
def test_a_window_at_or_below_the_baseline_is_not_flagged(factor: float) -> None:
    """The other direction. 1.2x is inside the 25% threshold deliberately — a
    gate that fires on ordinary variance is one people switch off."""
    budgets = load_budgets(_BUDGETS)
    baseline = budgets.baseline_for(CHAT_READ)["wall_clock_ms"]
    assert baseline is not None
    measured = summarise([_read(latency=int(baseline * factor))] * 3)
    assert [v for v in check_all(measured, budgets) if v.metric == "wall_clock_ms"] == []
