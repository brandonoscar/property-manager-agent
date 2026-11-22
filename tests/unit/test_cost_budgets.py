"""Unit tests for the cost/latency budget scaffold (evals/cost_budgets.py)."""

from __future__ import annotations

from pathlib import Path

from helixis.evals.cost_budgets import (
    METRICS,
    Budgets,
    check_all,
    check_measurement,
    load_budgets,
    suggest_baseline,
)

_SHIPPED = Path(__file__).resolve().parents[2] / "config" / "eval_budgets.yaml"


def _budgets(baseline: dict, *, threshold: float = 0.25) -> Budgets:
    return Budgets(over_threshold=threshold, baselines={"work_order_create": baseline})


def test_shipped_budget_file_parses_and_gates_nothing_yet() -> None:
    b = load_budgets(_SHIPPED)
    assert b.over_threshold == 0.25
    assert "work_order_create" in b.baselines
    # Every shipped baseline is null → no metric gates yet.
    measured = {"tokens": 1_000_000, "tool_calls": 999, "wall_clock_ms": 999_999, "usd": 99.0}
    assert check_measurement("work_order_create", measured, b) == []


def test_null_baseline_is_not_gated() -> None:
    b = _budgets({"tokens": None, "tool_calls": None, "wall_clock_ms": None, "usd": None})
    assert check_measurement("work_order_create", {"tokens": 10_000}, b) == []


def test_within_threshold_no_violation() -> None:
    b = _budgets({"tokens": 1000})
    # 25% over exactly is allowed (strictly greater than fails).
    assert check_measurement("work_order_create", {"tokens": 1250}, b) == []
    assert check_measurement("work_order_create", {"tokens": 900}, b) == []


def test_over_threshold_flags() -> None:
    b = _budgets({"tokens": 1000})
    v = check_measurement("work_order_create", {"tokens": 1300}, b)
    assert len(v) == 1
    assert v[0].metric == "tokens" and v[0].measured == 1300 and v[0].baseline == 1000
    assert abs(v[0].over_by_pct - 0.3) < 1e-9


def test_missing_measured_metric_skipped() -> None:
    b = _budgets({"tokens": 1000, "usd": 0.5})
    # Only tokens measured; usd baseline present but unmeasured → skipped.
    v = check_measurement("work_order_create", {"tokens": 2000}, b)
    assert [x.metric for x in v] == ["tokens"]


def test_unknown_task_class_no_baseline() -> None:
    b = _budgets({"tokens": 1000})
    assert check_measurement("nonexistent", {"tokens": 10_000}, b) == []


def test_check_all_sorted_most_over_first() -> None:
    budgets = Budgets(
        over_threshold=0.25,
        baselines={
            "a": {"tokens": 1000, "tool_calls": None, "wall_clock_ms": None, "usd": None},
            "b": {"tokens": 1000, "tool_calls": None, "wall_clock_ms": None, "usd": None},
        },
    )
    out = check_all({"a": {"tokens": 1400}, "b": {"tokens": 2000}}, budgets)
    assert [v.task_class for v in out] == ["b", "a"]  # 100% over before 40% over


def test_custom_threshold() -> None:
    b = _budgets({"tokens": 1000}, threshold=0.5)
    assert check_measurement("work_order_create", {"tokens": 1400}, b) == []  # under 50%
    assert len(check_measurement("work_order_create", {"tokens": 1600}, b)) == 1


def test_suggest_baseline_rounds() -> None:
    got = suggest_baseline(
        {"tokens": 1234.7, "usd": 0.012345, "tool_calls": 3.0, "wall_clock_ms": 850.6}
    )
    assert got == {"tokens": 1235, "usd": 0.0123, "tool_calls": 3, "wall_clock_ms": 851}


def test_violation_str_is_readable() -> None:
    v = check_measurement("work_order_create", {"tokens": 1500}, _budgets({"tokens": 1000}))[0]
    assert "work_order_create.tokens" in str(v) and "50%" in str(v)


def test_metrics_constant_matches_file_rows() -> None:
    b = load_budgets(_SHIPPED)
    for row in b.baselines.values():
        assert set(row) == set(METRICS)
