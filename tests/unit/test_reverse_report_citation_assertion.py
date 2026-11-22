"""Deterministic test of the citation-graph assertion through the agent_flow
harness (#271).

The underlying functions (citation_source / _source_grounded /
ungrounded_observations) are unit-tested in test_reverse_report.py. This covers
the next layer up: the ``reverse_report_citations_grounded`` golden-case
assertion, end-to-end through ``_check_reverse_report_assertions`` — parse a
REVERSE_REPORT out of the agent's final output, then verify every CITED
observation traces to a tool actually in the trail. This is the deterministic
"real fix" for #271: a fabricated citation fails the assertion regardless of
model wording, so case 070 no longer rides solely on flaky presence checks.

No live model — the report text and trail are hand-authored.
"""

from __future__ import annotations

import json

from helixis.evals.agent_flow import AgentFlowRunResult, _check_reverse_report_assertions

# A schema-valid REVERSE_REPORT whose single observation cites buildium_tools.
_REPORT = {
    "observed": ["property.id = 88421 [buildium_tools.buildium_get_property#maple]"],
    "inferred": [],
    "unknown": [],
    "confidence": 0.7,
    "next_action": {"kind": "ask_user", "why": "share the property details"},
}


def _grounded_assertion(result: AgentFlowRunResult):
    return next(a for a in result.assertions if a.name == "reverse_report_citations_grounded")


def test_citations_grounded_passes_when_cited_tool_is_in_trail() -> None:
    result = AgentFlowRunResult(
        final_output=json.dumps(_REPORT),
        tool_call_trail=["buildium_get_property"],
    )
    _check_reverse_report_assertions(
        result=result, expected={"reverse_report_citations_grounded": True}
    )
    assert _grounded_assertion(result).passed is True


def test_citations_grounded_fails_on_fabricated_citation() -> None:
    # Same report, but the cited buildium tool was never called → the citation
    # is fabricated. This is the exact failure mode REVERSE_REPORT exists to
    # catch, and it's caught deterministically (no dependence on model wording).
    result = AgentFlowRunResult(
        final_output=json.dumps(_REPORT),
        tool_call_trail=[],
    )
    _check_reverse_report_assertions(
        result=result, expected={"reverse_report_citations_grounded": True}
    )
    assert _grounded_assertion(result).passed is False


def test_unparseable_report_fails_parse_not_grounding() -> None:
    result = AgentFlowRunResult(final_output="I couldn't produce a report.", tool_call_trail=[])
    _check_reverse_report_assertions(
        result=result, expected={"reverse_report_citations_grounded": True}
    )
    # No report → the parse assertion fires; the grounding assertion isn't reached.
    assert any(a.name == "reverse_report_parse" and not a.passed for a in result.assertions)
