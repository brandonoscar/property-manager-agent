"""Runner-level tests for the deterministic ``reverse_report_enforcement`` case
type (#271). These guard the harness wiring itself — that the case type drives
``evaluate_write`` / ``ungrounded_observations`` and that its assertions really
gate (a wrong expectation FAILS, not vacuously passes), model-free.
"""

from __future__ import annotations

import pytest

from helixis.evals.runner import run_case

_GROUNDED_LOW_CONF = {
    "id": "t_low_conf",
    "case_type": "reverse_report_enforcement",
    "input": {
        "report": {
            "observed": ["WO 5501 open [buildium_tools.get_work_order#wo:5501]"],
            "inferred": [],
            "unknown": [],
            "confidence": 0.4,
            "next_action": {"kind": "tool_call", "why": "close it"},
        },
        "tool_call_trail": ["buildium_get_work_order"],
        "min_confidence": 0.6,
    },
}


@pytest.mark.asyncio
async def test_low_confidence_block_passes() -> None:
    case = {**_GROUNDED_LOW_CONF, "expected": {"write_allowed": False}}
    res = await run_case(case)
    assert res.passed and not res.skipped


@pytest.mark.asyncio
async def test_wrong_expectation_fails_as_safety() -> None:
    # Same fixture, but assert the (incorrect) opposite verdict — must fail,
    # and as a safety-class failure since the assertion is critical.
    case = {**_GROUNDED_LOW_CONF, "expected": {"write_allowed": True}}
    res = await run_case(case)
    assert not res.passed
    assert res.failure_kind == "safety"


@pytest.mark.asyncio
async def test_citation_graph_flags_ungrounded() -> None:
    case = {
        "id": "t_ungrounded",
        "case_type": "reverse_report_enforcement",
        "input": {
            "report": {
                "observed": ["Owner prefers email [buildium_tools.get_owner#owner:88]"],
                "inferred": [],
                "unknown": [],
                "confidence": 0.9,
                "next_action": {"kind": "reply", "why": "answer"},
            },
            "tool_call_trail": ["web_search"],
        },
        "expected": {"ungrounded_contains": ["buildium_tools.get_owner#owner:88"]},
    }
    res = await run_case(case)
    assert res.passed


@pytest.mark.asyncio
async def test_clean_report_allows_write() -> None:
    case = {
        "id": "t_clean",
        "case_type": "reverse_report_enforcement",
        "input": {
            "report": {
                "observed": ["WO 5501 open [buildium_tools.get_work_order#wo:5501]"],
                "inferred": [],
                "unknown": [],
                "confidence": 0.9,
                "next_action": {"kind": "tool_call", "why": "update status"},
            },
            "tool_call_trail": ["buildium_get_work_order"],
        },
        "expected": {"write_allowed": True, "ungrounded_empty": True},
    }
    res = await run_case(case)
    assert res.passed
