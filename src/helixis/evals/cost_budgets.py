"""Per-task-class cost / latency budgets (release-gate Tier-3 #14) — deterministic.

Loads ``config/eval_budgets.yaml`` (a baseline per metric per task class) and flags
a measured run that exceeds its baseline by more than the configured threshold
(spec: >25%). A ``null`` baseline is "not yet measured" and is NOT gated, so the
budget file ships as a scaffold and starts gating each metric only once a real
median is wired in from a live measured run. Pure — no model, no network — so the
threshold logic is fully unit-testable now.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_BUDGET_PATH = Path(__file__).resolve().parents[3] / "config" / "eval_budgets.yaml"
METRICS: tuple[str, ...] = ("tokens", "tool_calls", "wall_clock_ms", "usd")


@dataclass(frozen=True)
class Budgets:
    over_threshold: float
    baselines: dict[str, dict[str, float | None]]

    def baseline_for(self, task_class: str) -> dict[str, float | None]:
        return self.baselines.get(task_class, {})


@dataclass(frozen=True)
class BudgetViolation:
    task_class: str
    metric: str
    measured: float
    baseline: float
    over_by_pct: float  # e.g. 0.42 == 42% over the baseline

    def __str__(self) -> str:
        return (
            f"{self.task_class}.{self.metric}: {self.measured:g} exceeds baseline "
            f"{self.baseline:g} by {self.over_by_pct * 100:.0f}%"
        )


def load_budgets(path: Path | str | None = None) -> Budgets:
    """Parse the budgets YAML. Missing/empty file → a zero-baseline scaffold that
    gates nothing."""
    p = Path(path) if path is not None else _DEFAULT_BUDGET_PATH
    if not p.exists():
        return Budgets(over_threshold=0.25, baselines={})
    raw = yaml.safe_load(p.read_text()) or {}
    threshold = float(raw.get("over_budget_threshold", 0.25))
    task_classes = raw.get("task_classes") or {}
    baselines: dict[str, dict[str, float | None]] = {}
    for tc, metrics in task_classes.items():
        row: dict[str, float | None] = {}
        for m in METRICS:
            v = (metrics or {}).get(m)
            row[m] = None if v is None else float(v)
        baselines[str(tc)] = row
    return Budgets(over_threshold=threshold, baselines=baselines)


def check_measurement(
    task_class: str,
    measured: dict[str, float],
    budgets: Budgets,
) -> list[BudgetViolation]:
    """Flag every metric where ``measured`` exceeds the task class's baseline by
    more than ``budgets.over_threshold``. A ``null`` baseline (not yet measured) or
    a metric absent from ``measured`` is skipped — never a false gate."""
    baseline = budgets.baseline_for(task_class)
    limit = 1.0 + budgets.over_threshold
    violations: list[BudgetViolation] = []
    for metric in METRICS:
        base = baseline.get(metric)
        if base is None or base <= 0:
            continue  # not yet measured → not gated
        actual = measured.get(metric)
        if actual is None:
            continue
        if actual > base * limit:
            violations.append(
                BudgetViolation(
                    task_class=task_class,
                    metric=metric,
                    measured=float(actual),
                    baseline=float(base),
                    over_by_pct=(float(actual) - base) / base,
                )
            )
    return violations


def check_all(
    measurements: dict[str, dict[str, float]],
    budgets: Budgets,
) -> list[BudgetViolation]:
    """Check a ``{task_class: measured-metrics}`` map, most-over first."""
    out: list[BudgetViolation] = []
    for task_class, measured in measurements.items():
        out.extend(check_measurement(task_class, measured, budgets))
    return sorted(out, key=lambda v: v.over_by_pct, reverse=True)


def suggest_baseline(measured: dict[str, float]) -> dict[str, float]:
    """Round a measured run into a baseline to paste into the YAML (helper for
    wiring real numbers later — never called by the gate)."""
    out: dict[str, float] = {}
    for metric in METRICS:
        if metric in measured and measured[metric] is not None:
            v = float(measured[metric])
            out[metric] = round(v, 4) if metric == "usd" else round(v)
    return out


__all__ = [
    "METRICS",
    "BudgetViolation",
    "Budgets",
    "check_all",
    "check_measurement",
    "load_budgets",
    "suggest_baseline",
]
