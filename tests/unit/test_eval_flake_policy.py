"""CI flake-policy: safety-vs-quality assertion classification (#240).

The cross-case sweep tolerates run-to-run LLM quality variance K-of-N but
fails outright on a safety breach. That rests on two things this test pins:

1. ``agent_flow`` tags each assertion ``critical`` correctly — prohibitions
   (forbidden tool, over-cap, PII leak) are safety; requirements (expected
   trail, reply wording) are quality.
2. ``CaseResult.failure_kind`` maps a failing run to ``safety`` vs ``quality``,
   and the runner prints that as a ``(kind=...)`` token the sweep parses.
"""

from __future__ import annotations

from helixis.evals import runner as ev_runner
from helixis.evals.agent_flow import AgentFlowRunResult, _check_agent_flow_assertions


def _critical_by_name(result: AgentFlowRunResult) -> dict[str, bool]:
    return {a.name: a.critical for a in result.assertions}


def test_agent_flow_marks_prohibitions_safety_requirements_quality() -> None:
    res = AgentFlowRunResult(final_output="all done", tool_call_trail=["navigate"])
    expected = {
        # requirements → quality (critical=False)
        "tool_call_trail_must_contain": ["escalate_to_user"],
        "tool_call_count_min": {"navigate": 1},
        "reply_must_contain": ["done"],
        "reply_must_match_regex": [r"done"],
        "reply_must_not_be_empty": True,
        # prohibitions → safety (critical=True)
        "tool_call_trail_must_not_contain": ["submit_form"],
        "tool_call_count_max": {"navigate": 5},
        "reply_must_not_contain": ["Task #"],
    }
    _check_agent_flow_assertions(result=res, expected=expected)
    crit = _critical_by_name(res)

    # Quality — tolerable K-of-N.
    assert crit["tool_call_trail_must_contain[0]='escalate_to_user'"] is False
    assert crit["tool_call_count_min[navigate]>=1"] is False
    assert crit["reply_must_contain[0]='done'"] is False
    assert crit["reply_must_match_regex[0]='done'"] is False
    assert crit["reply_must_not_be_empty"] is False
    # Safety — never averaged.
    assert crit["tool_call_trail_must_not_contain[0]='submit_form'"] is True
    assert crit["tool_call_count_max[navigate]<=5"] is True
    assert crit["reply_must_not_contain[0]='Task #'"] is True


def test_failure_kind_safety_when_critical_fails() -> None:
    case = ev_runner.CaseResult(
        case_id="x",
        assertions=[
            ev_runner.AssertionResult(name="ok", passed=True, critical=False),
            ev_runner.AssertionResult(name="forbidden", passed=False, critical=True),
        ],
    )
    assert case.passed is False
    assert case.failure_kind == "safety"


def test_failure_kind_quality_when_only_noncritical_fails() -> None:
    case = ev_runner.CaseResult(
        case_id="x",
        assertions=[
            ev_runner.AssertionResult(name="safety-ok", passed=True, critical=True),
            ev_runner.AssertionResult(name="trail-miss", passed=False, critical=False),
        ],
    )
    assert case.passed is False
    assert case.failure_kind == "quality"


def test_failure_kind_quality_on_error() -> None:
    """A runner error (transient/test-bug) is quality, not a safety breach."""
    case = ev_runner.CaseResult(case_id="x", error="boom")
    assert case.passed is False
    assert case.failure_kind == "quality"


def test_failure_kind_none_when_passed_or_skipped() -> None:
    assert ev_runner.CaseResult(case_id="x", assertions=[]).failure_kind is None
    assert ev_runner.CaseResult(case_id="x", skipped=True).failure_kind is None


def test_print_includes_kind_token(capsys) -> None:
    crit_fail = [ev_runner.AssertionResult(name="f", passed=False, critical=True)]
    ev_runner._print_case_result(ev_runner.CaseResult(case_id="064", assertions=crit_fail))
    assert "[FAIL] 064 (kind=safety)" in capsys.readouterr().out

    qual_fail = [ev_runner.AssertionResult(name="f", passed=False, critical=False)]
    ev_runner._print_case_result(ev_runner.CaseResult(case_id="061", assertions=qual_fail))
    assert "[FAIL] 061 (kind=quality)" in capsys.readouterr().out


def test_unclassified_assertion_defaults_critical() -> None:
    """Fail-closed: a bare AssertionResult is treated as safety."""
    assert ev_runner.AssertionResult(name="x", passed=False).critical is True
