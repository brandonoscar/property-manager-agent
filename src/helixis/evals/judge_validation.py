"""LLM-as-judge validation harness (release-gate scoring discipline / #12) — scaffold.

The spec forbids trusting an LLM judge until it's validated against **≥50 hand-labeled
traces** with **TPR > 90% AND TNR > 90%** (measured separately); otherwise its scores
are advisory only. This module is the deterministic plumbing — load labeled traces,
run a judge over them, build the confusion matrix, compute TPR/TNR, and render the
pass/advisory verdict — all pure and unit-testable. The ONE part that needs Gemini
is the judge itself; it's an injected callable, and the live implementation is left
as an explicit stub to be wired in the state-based-scoring pass.

Convention: a trace's boolean ``label`` is human ground truth for the judged property
(``True`` == the property holds). The judge predicts the same boolean.
  TPR = TP / (TP + FN)   — of the true-positives, the fraction the judge caught
  TNR = TN / (TN + FP)   — of the true-negatives, the fraction the judge caught
Both must clear the bar so a judge can't pass by always answering one class.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MIN_TRACES = 50
MIN_RATE = 0.90


@dataclass(frozen=True)
class LabeledTrace:
    id: str
    content: str
    label: bool  # human ground truth for the judged property


def load_labeled_traces(path: Path | str) -> list[LabeledTrace]:
    """Parse a JSONL file of ``{"id", "content", "label"}`` rows. Blank lines are
    skipped; a missing field raises so a malformed label set fails loudly."""
    traces: list[LabeledTrace] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        traces.append(
            LabeledTrace(id=str(row["id"]), content=str(row["content"]), label=bool(row["label"]))
        )
    return traces


@dataclass(frozen=True)
class ConfusionMatrix:
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def positives(self) -> int:
        return self.tp + self.fn

    @property
    def negatives(self) -> int:
        return self.tn + self.fp

    @property
    def tpr(self) -> float | None:
        """Sensitivity. None when there are no positive labels to measure."""
        return self.tp / self.positives if self.positives else None

    @property
    def tnr(self) -> float | None:
        """Specificity. None when there are no negative labels to measure."""
        return self.tn / self.negatives if self.negatives else None


def confusion_matrix(labels: list[bool], predictions: list[bool]) -> ConfusionMatrix:
    if len(labels) != len(predictions):
        raise ValueError(f"labels ({len(labels)}) and predictions ({len(predictions)}) differ")
    tp = fp = tn = fn = 0
    for label, pred in zip(labels, predictions, strict=True):
        if label and pred:
            tp += 1
        elif label and not pred:
            fn += 1
        elif not label and pred:
            fp += 1
        else:
            tn += 1
    return ConfusionMatrix(tp=tp, fp=fp, tn=tn, fn=fn)


@dataclass(frozen=True)
class JudgeValidation:
    cm: ConfusionMatrix
    min_traces: int
    min_rate: float
    is_valid: bool
    reasons: tuple[str, ...]  # why it's advisory-only, empty when valid

    @property
    def tpr(self) -> float | None:
        return self.cm.tpr

    @property
    def tnr(self) -> float | None:
        return self.cm.tnr

    def summary(self) -> str:
        tpr = "n/a" if self.tpr is None else f"{self.tpr:.1%}"
        tnr = "n/a" if self.tnr is None else f"{self.tnr:.1%}"
        verdict = "VALIDATED (gating)" if self.is_valid else "ADVISORY-ONLY"
        return f"judge {verdict}: n={self.cm.n} TPR={tpr} TNR={tnr}"


def validate(
    labels: list[bool],
    predictions: list[bool],
    *,
    min_traces: int = MIN_TRACES,
    min_rate: float = MIN_RATE,
) -> JudgeValidation:
    """Build the confusion matrix and decide whether the judge clears the bar
    (≥ ``min_traces`` labeled, TPR AND TNR strictly > ``min_rate``). Any shortfall
    makes the judge advisory-only, with a reason per failed condition."""
    cm = confusion_matrix(labels, predictions)
    reasons: list[str] = []
    if cm.n < min_traces:
        reasons.append(f"only {cm.n} labeled traces (need ≥{min_traces})")
    if cm.positives == 0 or cm.negatives == 0:
        reasons.append("labels lack both classes — TPR/TNR not both measurable")
    if cm.tpr is not None and cm.tpr <= min_rate:
        reasons.append(f"TPR {cm.tpr:.1%} ≤ {min_rate:.0%}")
    if cm.tnr is not None and cm.tnr <= min_rate:
        reasons.append(f"TNR {cm.tnr:.1%} ≤ {min_rate:.0%}")
    is_valid = not reasons
    return JudgeValidation(
        cm=cm,
        min_traces=min_traces,
        min_rate=min_rate,
        is_valid=is_valid,
        reasons=tuple(reasons),
    )


def run_judge(traces: list[LabeledTrace], judge_fn: Callable[[str], bool]) -> list[bool]:
    """Apply ``judge_fn`` (the predictor) to each trace's content, in order. The
    live judge is an LLM call — inject it here; a stub predictor keeps this
    deterministic for tests."""
    return [judge_fn(t.content) for t in traces]


def validate_judge_over_traces(
    traces: list[LabeledTrace],
    judge_fn: Callable[[str], bool],
    *,
    min_traces: int = MIN_TRACES,
    min_rate: float = MIN_RATE,
) -> JudgeValidation:
    """End-to-end: run ``judge_fn`` over labeled traces and validate it."""
    predictions = run_judge(traces, judge_fn)
    return validate(
        [t.label for t in traces], predictions, min_traces=min_traces, min_rate=min_rate
    )


def live_judge_stub(_content: str) -> bool:
    """The live judge now EXISTS — inject it, don't call this.

    ``evals.rubric_judge.compliance_predicate(settings)`` returns the real binary
    ``judge_fn`` to pass to :func:`run_judge` / :func:`validate_judge_over_traces`
    (see ``scripts/validate_judge.py``). This module stays pure/deterministic (no
    LLM import), so the live judge is injected at the call site, never imported
    here. This shim only remains so an accidental *unwired* validation run raises
    loudly instead of silently certifying an empty judge."""
    raise NotImplementedError(
        "Do not call live_judge_stub — inject rubric_judge.compliance_predicate(settings) "
        "into validate_judge_over_traces. See scripts/validate_judge.py."
    )


__all__ = [
    "MIN_RATE",
    "MIN_TRACES",
    "ConfusionMatrix",
    "JudgeValidation",
    "LabeledTrace",
    "confusion_matrix",
    "live_judge_stub",
    "load_labeled_traces",
    "run_judge",
    "validate",
    "validate_judge_over_traces",
]
