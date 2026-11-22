"""Aggregate real turns into the per-class measurement `cost_budgets` wants.

`evals/cost_budgets.py` has been able to flag a cost regression since it landed
and has never flagged one, because **nothing produced the two things it needs**:
a task-class string and a measurement. Every baseline in
`config/eval_budgets.yaml` is null, and — the deeper gap — a grep for the seven
class names finds ZERO non-test references anywhere in the product. So
`check_measurement(task_class, ...)` was uncallable regardless of baselines.
This module is the missing producer. `scripts/cost_drift_check.py` is the glue
that runs it against the live table.

WHAT THE TELEMETRY CAN AND CANNOT SEE (measured 2026-08-29, whole history)
-------------------------------------------------------------------------
The budget file asks for four metrics across seven classes. Today's telemetry
supports **two metrics on one class**, and the reasons are structural rather
than "nobody has run the battery yet":

* **`tokens` and `usd` ARE recordable — but only since 2026-08-26.**
  ⚠ This module first shipped saying the opposite ("not recordable per run;
  the only spend table is `helixis_llm_usage`, keyed `(company_id, day)`").
  That was wrong, and wrong in the way gotcha 47 describes: absence concluded
  from an incomplete lookup. `helixis_llm_usage` and the column list were both
  checked; `orchestrator_runs.raw` never was. `llm/turn_cost.py` writes a
  `usage` object there — `cost_usd`, `prompt_tokens`, `completion_tokens`,
  `calls`, `cached_tokens`, `cache_write_tokens`, `cost_is_floor` — on 213 of
  213 runs since it landed. The real limit is HISTORY, not schema: runs before
  2026-08-26 carry no usage, so the sample is short and a missing value is
  skipped rather than counted as zero.
* **Six of the seven classes are unobservable.** They are named for WRITES, and
  writes happen inside specialist sub-runs while `orchestrator_runs.tool_calls`
  records the ORCHESTRATOR's calls only: **1 write tool call in 1,620**, across
  every run ever recorded. A classifier reading that column cannot see a write
  that happened, so it must not claim one did not.

Hence `classify_task_class` returns `chat_read` or **None**, never a write
class — declining to name what it cannot see (gotcha 55's rule: a guard that
cannot see must decline to act, never report emptiness). The six write classes
stay null in the YAML with that reason recorded, so the next reader does not
spend a paid battery run on a number the schema cannot hold.

WHY AGGREGATE, NOT PER-RUN
--------------------------
`check_measurement` compares one measurement against a baseline. Feeding it a
SINGLE run's latency against a p50 baseline fires on roughly half of all runs —
noise, not a gate. Feeding it the WINDOW's p50 against the baseline p50 is the
comparison that catches the regression this exists for, and it is the one
gotcha 37 describes: a 3x cost change that hid for a month because the number
was read off one day.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from helixis.common.answered import CARD_TOOLS as _CARD_TOOLS
from helixis.common.answered import turn_answered

#: The class for a turn that called no write tool. The only one today's
#: telemetry can support — see the module docstring.
CHAT_READ = "chat_read"

#: Card tools. A run that emitted one ANSWERED even with an empty `final_text`
#: (gotcha 56: cards are not in that column), so counting only prose would
#: silently drop card-heavy turns out of the sample and bias the median.
#:
#: ⚠ RE-EXPORTED, not defined here. The same rule decides whether a turn is
#: BILLABLE, and while it had two homes the billing copy was the weaker one —
#: see `common/answered.py` for what that cost. Keeping the name importable
#: from here so existing readers (and `__all__`) keep working.
CARD_TOOLS = _CARD_TOOLS


def write_tool_names() -> frozenset[str]:
    """The registered write tools, derived from the LIVE registries.

    Same derivation as `test_negative_should_not_act.WRITE_TOOLS`, and
    `test_cost_drift` asserts the two agree — a hardcoded copy here would drift
    the day a new write verb lands, and its failure mode is silent: an
    unrecognised write reads as a read and the turn is counted as `chat_read`.
    """
    from helixis.agents.tool_registries import write_registry_tools

    verbs = (
        "create",
        "update",
        "add",
        "upload",
        "share",
        "close",
        "post",
        "record",
        "send",
        "text",
        "undo",
    )
    # ⚠ EVERY write registry, not just Buildium's. A second PMS's writes live in
    # their own registry (the 32x cases pin the Buildium one by name), and a
    # registry missing from this set fails SILENTLY in the expensive
    # direction: `classify_task_class` sees no write tool and files the turn as
    # `chat_read`, so a Rentvine write's latency and cost land in the read
    # baseline and quietly move it. Found by the full-suite stash diff, not by
    # the working selection — this file carries neither "rentvine" nor "write
    # tool" in a name anyone would grep for while building one. The registries
    # are listed in ONE place now (agents/tool_registries.py), which also
    # finds each toolkit connector's tools by itself.
    return frozenset(t.name for t in write_registry_tools() if any(v in t.name for v in verbs))


@dataclass(frozen=True)
class RunRecord:
    """One `orchestrator_runs` row, reduced to what a budget cares about."""

    latency_ms: int | None
    tool_names: Sequence[str] = field(default_factory=tuple)
    has_prose: bool = False
    #: From ``raw->'usage'`` (llm/turn_cost). None on any run before
    #: 2026-08-26, and on any run whose cost LiteLLM could only floor — a
    #: partial total presented as a total is gotcha 25's $0.00 bill, so it is
    #: dropped rather than averaged in.
    tokens: int | None = None
    usd: float | None = None


def classify_task_class(record: RunRecord, writes: frozenset[str] | None = None) -> str | None:
    """`chat_read` for a turn that called no write tool; None otherwise.

    ⚠ None means "cannot classify", NOT "some other class". Six of the seven
    classes are write-shaped and invisible here (module docstring), so guessing
    one would attribute a read turn's cheap latency to a write baseline and
    make the gate lie in the safe-looking direction.
    """
    if writes is None:
        writes = write_tool_names()
    return None if any(name in writes for name in record.tool_names) else CHAT_READ


def answered(record: RunRecord) -> bool:
    """Did the user get an answer — prose OR a card (gotcha 56).

    Delegates so the latency sample and the billing meter cannot disagree
    about what "answered" means; `common/answered.py` records why that
    mattered.
    """
    return turn_answered(has_prose=record.has_prose, tool_names=record.tool_names)


def summarise(records: Iterable[RunRecord]) -> dict[str, dict[str, float]]:
    """`{task_class: {metric: window median}}`, ready for `check_all`.

    Only ANSWERED, classifiable runs with a latency contribute. A class with no
    qualifying run is ABSENT from the result rather than present with a zero —
    `check_measurement` skips a missing metric, and a fabricated zero would read
    as an enormous improvement and could never trip the gate.
    """
    buckets: dict[str, list[RunRecord]] = {}
    writes = write_tool_names()
    for rec in records:
        if rec.latency_ms is None or not answered(rec):
            continue
        tc = classify_task_class(rec, writes)
        if tc is None:
            continue
        buckets.setdefault(tc, []).append(rec)

    out: dict[str, dict[str, float]] = {}
    for tc, rows in buckets.items():
        if not rows:
            continue
        metrics: dict[str, float] = {
            "wall_clock_ms": float(statistics.median(r.latency_ms or 0 for r in rows)),
            "tool_calls": float(statistics.median(len(r.tool_names) for r in rows)),
        }
        # ⚠ Median over the rows that HAVE the figure, and omit the metric
        # entirely when none does. Runs before 2026-08-26 carry no usage, so
        # treating a missing cost as 0 would drag the median toward zero in
        # exact proportion to how much old data is in the window — a cost
        # metric that improves as you widen the lookback.
        priced = [r.usd for r in rows if r.usd is not None]
        counted = [r.tokens for r in rows if r.tokens is not None]
        if priced:
            metrics["usd"] = float(statistics.median(priced))
        if counted:
            metrics["tokens"] = float(statistics.median(counted))
        out[tc] = metrics
    return out


__all__ = [
    "CARD_TOOLS",
    "CHAT_READ",
    "RunRecord",
    "answered",
    "classify_task_class",
    "summarise",
    "write_tool_names",
]
