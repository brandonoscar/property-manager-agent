"""Unit tests for the single-run-gate quarantine (gotcha 10).

A golden case flagged ``single_run_gate: false`` must not red-gate the
single-run gate (`test_evals_pass`) on a QUALITY-only failure, but a SAFETY
failure (any ``critical=True`` assertion) must STILL gate even when quarantined.
These exercise the pure classifiers without touching the LLM-driven runner.
"""

from __future__ import annotations

from helixis.evals.runner import (
    AssertionResult,
    CaseResult,
    _is_quarantined,
    _quarantined_nongating,
)

_QUARANTINED = {"070_x": {"id": "070_x", "single_run_gate": False}}
# Deterministic type → gates fully. (A case with NO case_type defaults to
# planner_summarizer — model-driven, quarantined — matching the dispatcher.)
_GATED = {"040_x": {"id": "040_x", "case_type": "tool_invocation"}}


def _result(case_id: str, *, quality_fail: bool = False, safety_fail: bool = False) -> CaseResult:
    assertions = [AssertionResult(name="ok", passed=True, critical=False)]
    if quality_fail:
        assertions.append(AssertionResult(name="q", passed=False, critical=False))
    if safety_fail:
        assertions.append(AssertionResult(name="s", passed=False, critical=True))
    return CaseResult(case_id=case_id, assertions=assertions)


def test_is_quarantined_reads_flag() -> None:
    assert _is_quarantined(_QUARANTINED, "070_x") is True
    assert _is_quarantined(_GATED, "040_x") is False
    assert _is_quarantined({}, "missing") is False  # absent → gated by default


def test_quarantined_quality_failure_is_nongating() -> None:
    r = _result("070_x", quality_fail=True)
    assert r.failure_kind == "quality"
    assert _quarantined_nongating(_QUARANTINED, r) is True


def test_quarantined_safety_failure_still_gates() -> None:
    # A quarantined case keeps its safety teeth: a critical assertion failing
    # is kind=safety and must NOT be downgraded to non-gating.
    r = _result("070_x", quality_fail=True, safety_fail=True)
    assert r.failure_kind == "safety"
    assert _quarantined_nongating(_QUARANTINED, r) is False


def test_non_quarantined_quality_failure_still_gates() -> None:
    r = _result("040_x", quality_fail=True)
    assert _quarantined_nongating(_GATED, r) is False


def test_passed_and_skipped_are_never_nongating_failures() -> None:
    passed = _result("070_x")
    assert passed.passed is True
    assert _quarantined_nongating(_QUARANTINED, passed) is False

    skipped = CaseResult(case_id="070_x", skipped=True, skip_reason="no key")
    assert _quarantined_nongating(_QUARANTINED, skipped) is False


# ── quarantine-by-default for model-driven case types (#350 follow-up) ──

_MODEL_DRIVEN = {"m": {"id": "m", "case_type": "agent_flow"}}  # no flag
_ROUTING = {"r": {"id": "r", "case_type": "orchestrator_routing"}}  # no flag
_DETERMINISTIC = {"d": {"id": "d", "case_type": "tool_invocation"}}  # no flag
_OPTED_IN = {"m": {"id": "m", "case_type": "agent_flow", "single_run_gate": True}}


def test_model_driven_case_types_quarantined_by_default() -> None:
    # No explicit flag → agent_flow / orchestrator_routing default to quarantined
    # (quality doesn't gate); safety still gates via _quarantined_nongating.
    assert _is_quarantined(_MODEL_DRIVEN, "m") is True
    assert _is_quarantined(_ROUTING, "r") is True


def test_deterministic_case_type_gates_by_default() -> None:
    # tool_invocation isn't model-driven → not quarantined → gates fully.
    assert _is_quarantined(_DETERMINISTIC, "d") is False


def test_missing_case_type_defaults_to_planner_summarizer_quarantine() -> None:
    # The six 00x planner cases omit case_type entirely — the dispatcher and
    # _is_model_driven both default it to planner_summarizer, so quarantine
    # must too. Without the shared default, one Haiku wording miss on 005
    # quality-GATED an entire run (2026-07-21).
    assert _is_quarantined({"p": {"id": "p"}}, "p") is True


def test_explicit_gate_true_opts_model_driven_case_back_into_gating() -> None:
    # A dogfood-proven model-driven case can opt INTO quality-gating.
    assert _is_quarantined(_OPTED_IN, "m") is False


def test_model_driven_quality_failure_is_nongating_without_flag() -> None:
    r = _result("m", quality_fail=True)
    assert r.failure_kind == "quality"
    assert _quarantined_nongating(_MODEL_DRIVEN, r) is True


def test_model_driven_safety_failure_still_gates_without_flag() -> None:
    r = _result("m", safety_fail=True)
    assert r.failure_kind == "safety"
    assert _quarantined_nongating(_MODEL_DRIVEN, r) is False
