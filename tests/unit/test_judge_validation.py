"""Unit tests for the judge-validation harness (evals/judge_validation.py)."""

from __future__ import annotations

import pytest

from helixis.evals.judge_validation import (
    MIN_RATE,
    MIN_TRACES,
    LabeledTrace,
    confusion_matrix,
    live_judge_stub,
    load_labeled_traces,
    run_judge,
    validate,
    validate_judge_over_traces,
)


def test_confusion_matrix_counts() -> None:
    labels = [True, True, False, False]
    preds = [True, False, True, False]
    cm = confusion_matrix(labels, preds)
    assert (cm.tp, cm.fn, cm.fp, cm.tn) == (1, 1, 1, 1)
    assert cm.n == 4 and cm.positives == 2 and cm.negatives == 2
    assert cm.tpr == 0.5 and cm.tnr == 0.5


def test_confusion_matrix_length_mismatch() -> None:
    with pytest.raises(ValueError):
        confusion_matrix([True], [True, False])


def test_tpr_tnr_none_when_class_absent() -> None:
    cm = confusion_matrix([True, True], [True, True])  # no negatives
    assert cm.tpr == 1.0 and cm.tnr is None


def test_validate_rejects_too_few_traces() -> None:
    # Perfect predictions but only 4 traces → advisory.
    labels = [True, False, True, False]
    v = validate(labels, labels)
    assert not v.is_valid
    assert any("labeled traces" in r for r in v.reasons)


def test_validate_rejects_low_tpr() -> None:
    # 50 traces, 25 pos / 25 neg. TNR perfect, TPR = 22/25 = 88% → below 90%.
    labels = [True] * 25 + [False] * 25
    preds = ([True] * 22 + [False] * 3) + [False] * 25
    v = validate(labels, preds)
    assert not v.is_valid
    assert any("TPR" in r for r in v.reasons)
    assert v.tnr == 1.0


def test_validate_passes_at_bar() -> None:
    # 60 traces, 30/30. TPR = 29/30 ≈ 96.7%, TNR = 30/30 = 100%.
    labels = [True] * 30 + [False] * 30
    preds = ([True] * 29 + [False]) + [False] * 30
    v = validate(labels, preds)
    assert v.is_valid and v.reasons == ()
    assert v.tpr is not None and v.tpr > MIN_RATE
    assert "VALIDATED" in v.summary()


def test_validate_needs_both_classes() -> None:
    labels = [True] * 60
    v = validate(labels, labels)
    assert not v.is_valid
    assert any("both classes" in r for r in v.reasons)


def test_run_judge_applies_predictor() -> None:
    traces = [LabeledTrace(id="1", content="good", label=True), LabeledTrace("2", "bad", False)]
    preds = run_judge(traces, lambda c: c == "good")
    assert preds == [True, False]


def test_validate_over_traces_end_to_end() -> None:
    traces = [LabeledTrace(str(i), "p" if i < 30 else "n", i < 30) for i in range(60)]
    # A perfect judge on this labeling.
    v = validate_judge_over_traces(traces, lambda c: c == "p")
    assert v.is_valid and v.tpr == 1.0 and v.tnr == 1.0


def test_live_judge_stub_raises() -> None:
    with pytest.raises(NotImplementedError):
        live_judge_stub("anything")


def test_load_labeled_traces(tmp_path) -> None:
    p = tmp_path / "traces.jsonl"
    p.write_text(
        '{"id": "a", "content": "hello", "label": true}\n'
        "\n"  # blank line skipped
        '{"id": "b", "content": "world", "label": false}\n'
    )
    traces = load_labeled_traces(p)
    assert [t.id for t in traces] == ["a", "b"]
    assert traces[0].label is True and traces[1].label is False


def test_defaults_match_spec() -> None:
    assert MIN_TRACES == 50 and MIN_RATE == 0.90
