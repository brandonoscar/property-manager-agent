"""Unit tests for the agent_flow eval harness mechanism.

Like the orchestrator_routing harness tests, these exercise the
mechanical pieces (sequenced mock construction, tool-call trail
extraction, assertion engine, dispatch error paths) so a regression
that breaks the mechanism still fails CI even when the real-LLM
agent_flow cases (060-062) SKIP for lack of a Gemini key.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.agents.browser_tools import (
    BrowserNavigateResult,
    BrowserReadResult,
)
from helixis.evals.agent_flow import (
    AgentFlowRunResult,
    _build_navigate_return,
    _build_read_page_return,
    _build_sequenced_patches,
    _check_agent_flow_assertions,
    _extract_tool_call_trail,
    _SequencedToolMock,
)

# ── _SequencedToolMock — queue mechanics ────────────────────────


def test_sequenced_mock_drains_one_at_a_time() -> None:
    entries = [
        {"return": "first", "repeat": 1},
        {"return": "second", "repeat": 1},
    ]
    mock = _SequencedToolMock("test", entries)
    assert mock() == "first"
    assert mock() == "second"
    assert mock.call_count == 2


def test_sequenced_mock_handles_repeat() -> None:
    """``repeat: 3`` short-hands "next 3 calls return this same value." """
    entries = [
        {"return": "A", "repeat": 3},
        {"return": "B", "repeat": 1},
    ]
    mock = _SequencedToolMock("test", entries)
    assert [mock() for _ in range(4)] == ["A", "A", "A", "B"]


def test_sequenced_mock_raises_when_exhausted() -> None:
    """Beyond the queue length, the mock raises so the test author
    knows their queue was too short. The harness translates this
    into a case-level error rather than a silent pass."""
    mock = _SequencedToolMock("test", [{"return": "only", "repeat": 1}])
    mock()  # consume the one entry
    with pytest.raises(RuntimeError, match="exhausted"):
        mock()


# ── return-value factories — typed Pydantic models ──────────────


def test_navigate_factory_builds_typed_result() -> None:
    spec = {
        "status": "ok",
        "url": "https://example.com",
        "final_url": "https://example.com",
        "deny_category": "allowed",
        "step_count": 1,
        "warnings": [],
    }
    result = _build_navigate_return(spec)
    assert isinstance(result, BrowserNavigateResult)
    assert result.status == "ok"
    assert result.final_url == "https://example.com"


def test_read_page_factory_builds_typed_result() -> None:
    spec = {
        "status": "ok",
        "url": "https://x.test",
        "text": "page text",
        "truncated": False,
        "query": "foo",
        "step_count": 2,
        "warnings": [],
    }
    result = _build_read_page_return(spec)
    assert isinstance(result, BrowserReadResult)
    assert result.text == "page text"


def test_factory_rejects_non_dict_spec() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        _build_navigate_return("not a dict")
    with pytest.raises(ValueError, match="must be a dict"):
        _build_read_page_return(["list", "not", "dict"])


# ── mock-patch construction ─────────────────────────────────────


def test_build_patches_for_known_tools() -> None:
    tool_mocks = {
        "navigate": [{"return": {"status": "ok", "url": "https://x.test"}}],
        "escalate_to_user": [{"return": {"status": "escalated"}}],
    }
    patches = _build_sequenced_patches(tool_mocks)
    assert len(patches) == 2


def test_build_patches_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        _build_sequenced_patches({"not_a_real_tool": [{"return": {}}]})


def test_build_patches_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        _build_sequenced_patches({"navigate": []})


def test_build_patches_rejects_entry_without_return_key() -> None:
    with pytest.raises(ValueError, match="'return' key"):
        _build_sequenced_patches({"navigate": [{"oops": "missing return"}]})


# ── tool-call trail extraction ──────────────────────────────────


def _make_tool_call_item(name: str) -> Any:
    """Stand-in for an SDK ``ToolCallItem`` with the tool name on
    ``raw_item.name`` — same shape the SDK produces during runs."""
    from agents.items import ToolCallItem

    raw = MagicMock()
    raw.name = name
    item = MagicMock(spec=ToolCallItem)
    item.__class__ = ToolCallItem
    item.raw_item = raw
    return item


def test_extract_trail_returns_tool_names_in_order() -> None:
    run_result = MagicMock()
    run_result.new_items = [
        _make_tool_call_item("navigate"),
        _make_tool_call_item("read_page"),
        _make_tool_call_item("escalate_to_user"),
    ]
    trail = _extract_tool_call_trail(run_result)
    assert trail == ["navigate", "read_page", "escalate_to_user"]


def test_extract_trail_skips_non_tool_items() -> None:
    from agents.items import ToolCallItem

    tool_item = _make_tool_call_item("navigate")
    other = MagicMock()  # not a ToolCallItem
    assert not isinstance(other, ToolCallItem)

    run_result = MagicMock()
    run_result.new_items = [other, tool_item, other]
    assert _extract_tool_call_trail(run_result) == ["navigate"]


# ── assertion engine ────────────────────────────────────────────


def _result_with(
    *,
    trail: list[str] | None = None,
    final_output: str = "ok",
) -> AgentFlowRunResult:
    return AgentFlowRunResult(
        final_output=final_output,
        tool_call_trail=trail or [],
    )


def test_trail_must_contain_passes_when_tool_present() -> None:
    result = _result_with(trail=["navigate", "escalate_to_user"])
    _check_agent_flow_assertions(
        result=result,
        expected={"tool_call_trail_must_contain": ["navigate", "escalate_to_user"]},
    )
    assert all(a.passed for a in result.assertions)


def test_trail_must_contain_fails_when_tool_absent() -> None:
    result = _result_with(trail=["navigate"])
    _check_agent_flow_assertions(
        result=result,
        expected={"tool_call_trail_must_contain": ["escalate_to_user"]},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1
    assert "escalate_to_user" in failures[0].reason


def test_trail_must_not_contain_catches_forbidden_tool() -> None:
    """The deny-list case (060) asserts read_page is NEVER called
    after a hard-block. This is the assertion shape that catches it."""
    result = _result_with(trail=["navigate", "read_page", "escalate_to_user"])
    _check_agent_flow_assertions(
        result=result,
        expected={"tool_call_trail_must_not_contain": ["read_page"]},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1
    assert "read_page" in failures[0].reason


def test_tool_call_count_min_max_enforced() -> None:
    """Case 062 (budget) asserts read_page is called at least once
    AND at most 15 times. Both bounds tested here."""
    result = _result_with(trail=["navigate"] + ["read_page"] * 14 + ["escalate_to_user"])

    # min satisfied (14 ≥ 1), max satisfied (14 ≤ 15)
    _check_agent_flow_assertions(
        result=result,
        expected={
            "tool_call_count_min": {"read_page": 1},
            "tool_call_count_max": {"read_page": 15, "click": 0},
        },
    )
    assert all(a.passed for a in result.assertions)


def test_tool_call_count_max_catches_over_budget() -> None:
    """Asserting click max=0 catches a regression where the agent
    starts clicking forms despite the read-only rule."""
    result = _result_with(trail=["navigate", "click", "escalate_to_user"])
    _check_agent_flow_assertions(
        result=result,
        expected={"tool_call_count_max": {"click": 0}},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1
    assert "click" in failures[0].name
    assert "over cap" in failures[0].reason


def test_reply_assertions_case_insensitive() -> None:
    result = _result_with(final_output="THIS REPLY MENTIONS BUDGET clearly.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_contain": ["budget", "this"]},
    )
    assert all(a.passed for a in result.assertions)


def test_reply_must_not_contain_catches_identity_leak() -> None:
    result = _result_with(final_output="Found it. Task #12345 has status...")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_not_contain": ["Task #"]},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1


def test_reply_should_not_contain_is_quality_non_gating() -> None:
    # A present "should not" fails the assertion but is critical=False (quality),
    # so it never hard-gates — e.g. an agent that refused an injection yet echoed
    # the attacker's address back to the operator (transparency, not exfil).
    result = _result_with(final_output="I refused, but the email named collector@evil.test.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_should_not_contain": ["collector@evil.test"]},
    )
    failing = [a for a in result.assertions if not a.passed]
    assert len(failing) == 1
    assert failing[0].critical is False


def test_reply_should_not_contain_passes_when_absent() -> None:
    result = _result_with(final_output="The tenant asked about parking and gym hours.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_should_not_contain": ["collector@evil.test"]},
    )
    assert all(a.passed for a in result.assertions)


def test_reply_must_match_regex_works() -> None:
    result = _result_with(final_output="May 12 at 7:30 PM")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_match_regex": [r"\b\d{1,2}:\d{2}\s*[APap][Mm]\b"]},
    )
    assert all(a.passed for a in result.assertions)


# ── reverse_report assertion family ─────────────────────────────

import json as _json  # noqa: E402

_GOOD_REPORT = _json.dumps(
    {
        "observed": [
            "user said: 'cancel it' [user_message@turn_1]",
            "work_order.id = 9183 status 'Open' [buildium_tools.list_work_orders#412]",
        ],
        "inferred": [],
        "unknown": ["Which work order did the user mean?"],
        "confidence": 0.7,
        "next_action": {"kind": "ask_user", "why": "ambiguous [from #2]"},
    }
)

_BAD_REPORT = _json.dumps(
    {
        "observed": ["user wants to cancel a work order"],
        "inferred": ["probably the most recent one"],
        "unknown": [],
        "confidence": 0.9,
        "next_action": {"kind": "tool_call", "why": "cancel"},
    }
)


def test_reverse_report_good_passes_all_wired_assertions() -> None:
    result = _result_with(final_output=_GOOD_REPORT, trail=["buildium_list_work_orders"])
    _check_agent_flow_assertions(
        result=result,
        expected={
            "reverse_report_observed_min": 1,
            "reverse_report_observed_cited": True,
            "reverse_report_unknown_blocks_writes": True,
            "reverse_report_confidence_calibrated": True,
        },
    )
    assert result.assertions and all(a.passed for a in result.assertions)
    # The reverse_report family is kind=QUALITY (re-rollable): it checks
    # LLM-output-shape (did the model emit a well-formed, cited self-report),
    # which varies run to run. The genuine "must not call a forbidden write"
    # safety bound lives in the tool_call_*_must_not_contain family, not here.
    assert not any(a.critical for a in result.assertions)


def test_reverse_report_bad_fails_cited_and_calibration() -> None:
    result = _result_with(final_output=_BAD_REPORT)
    _check_agent_flow_assertions(
        result=result,
        expected={
            "reverse_report_observed_cited": True,
            "reverse_report_confidence_calibrated": True,
        },
    )
    failed = {a.name for a in result.assertions if not a.passed}
    assert "reverse_report_observed_cited" in failed
    assert "reverse_report_confidence_calibrated" in failed


def test_reverse_report_unknown_blocks_write_in_trail() -> None:
    result = _result_with(final_output=_GOOD_REPORT, trail=["buildium_create_to_do_task"])
    _check_agent_flow_assertions(
        result=result,
        expected={"reverse_report_unknown_blocks_writes": True},
    )
    failed = [a for a in result.assertions if not a.passed]
    assert len(failed) == 1 and failed[0].name == "reverse_report_unknown_blocks_writes"


def test_reverse_report_missing_block_is_a_parse_failure() -> None:
    result = _result_with(final_output="I just replied with prose, no report.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reverse_report_observed_min": 1},
    )
    assert len(result.assertions) == 1
    assert result.assertions[0].name == "reverse_report_parse"
    # parse failure is now quality (re-rollable), not a hard gate — see the
    # reverse_report family comment in agent_flow.py.
    assert not result.assertions[0].passed and not result.assertions[0].critical


def test_reverse_report_keys_absent_adds_no_assertions() -> None:
    result = _result_with(final_output="anything")
    _check_agent_flow_assertions(result=result, expected={"reply_must_contain": ["any"]})
    # No reverse_report_* keys ⇒ no reverse_report assertions appended.
    assert not any(a.name.startswith("reverse_report") for a in result.assertions)


# ── harness extension (Slice 3 prerequisites) ───────────────────


def test_flow_settings_threads_reverse_report_flag() -> None:
    from helixis.evals.agent_flow import _flow_settings

    assert _flow_settings().agent.reverse_report is False
    assert _flow_settings(reverse_report=True).agent.reverse_report is True


def test_buildium_writes_are_mockable_in_flow() -> None:
    from helixis.evals.agent_flow import _TOOL_MOCK_TARGETS, _build_sequenced_patches

    for write in ("buildium_create_to_do_task", "buildium_create_work_order"):
        assert write in _TOOL_MOCK_TARGETS
    # A buildium mock spec (string return) builds patches without error.
    patches = _build_sequenced_patches(
        {"buildium_create_work_order": [{"return": "Created #9001"}]}
    )
    assert len(patches) == 1


def test_buildium_mock_rejects_non_string_return() -> None:
    import pytest as _pytest

    from helixis.evals.agent_flow import _build_sequenced_patches

    with _pytest.raises(ValueError, match="must be a string"):
        _build_sequenced_patches({"buildium_create_work_order": [{"return": {"not": "a string"}}]})


def test_all_specialists_registered_as_flow_factories() -> None:
    from helixis.evals.agent_flow import _AGENT_FACTORIES

    for name in ("Browser Agent", "Buildium Agent", "Web Agent", "Document Agent"):
        assert name in _AGENT_FACTORIES


# ── dispatch error paths ────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_flow_returns_error_when_user_message_missing() -> None:
    from helixis.evals.agent_flow import run_agent_flow_case

    case = {
        "id": "test_missing_input",
        "case_type": "agent_flow",
        "input": {"agent": "Browser Agent"},
        "expected": {},
    }
    result = await run_agent_flow_case(case)
    assert result.error is not None
    assert "user_message" in result.error


@pytest.mark.asyncio
async def test_agent_flow_returns_error_on_unknown_agent() -> None:
    from helixis.evals.agent_flow import run_agent_flow_case

    case = {
        "id": "test_unknown_agent",
        "case_type": "agent_flow",
        "input": {"user_message": "hi", "agent": "Not An Agent"},
        "expected": {},
    }
    result = await run_agent_flow_case(case)
    assert result.error is not None
    assert "Not An Agent" in result.error


@pytest.mark.asyncio
async def test_agent_flow_returns_error_on_unknown_tool() -> None:
    from helixis.evals.agent_flow import run_agent_flow_case

    case = {
        "id": "test_unknown_tool",
        "case_type": "agent_flow",
        "input": {
            "user_message": "hi",
            "agent": "Browser Agent",
            "tool_mocks": {"bogus_tool": [{"return": {}}]},
        },
        "expected": {},
    }
    result = await run_agent_flow_case(case)
    assert result.error is not None
    assert "unknown tool" in result.error


@pytest.mark.asyncio
async def test_runner_dispatches_agent_flow_when_env_missing() -> None:
    """Top-level runner SKIPs agent_flow cases when requires_env is
    missing — same path as orchestrator_routing."""
    import os

    from helixis.evals.runner import run_case

    case = {
        "id": "test_skip",
        "case_type": "agent_flow",
        "requires_env": ["DEFINITELY_UNSET_AGENT_FLOW_ENV"],
        "input": {"user_message": "hi", "agent": "Browser Agent"},
        "expected": {},
    }
    os.environ.pop("DEFINITELY_UNSET_AGENT_FLOW_ENV", None)
    result = await run_case(case)
    assert result.skipped is True
    assert "DEFINITELY_UNSET_AGENT_FLOW_ENV" in (result.skip_reason or "")


# ── #271 citation-graph + budget assertions (deterministic) ──────


def _report_json(observed: list[str]) -> str:
    return json.dumps(
        {
            "observed": observed,
            "inferred": [],
            "unknown": [],
            "confidence": 0.7,
            "next_action": {"kind": "reply", "why": "answer"},
        }
    )


def test_citations_grounded_passes_when_cited_tool_was_called() -> None:
    result = _result_with(
        trail=["buildium_get_property"],
        final_output=_report_json(["Maple Court [buildium_tools.get_property#property:88421]"]),
    )
    _check_agent_flow_assertions(
        result=result, expected={"reverse_report_citations_grounded": True}
    )
    a = next(x for x in result.assertions if x.name == "reverse_report_citations_grounded")
    assert a.passed
    assert a.critical is False  # quality-class: deterministic but model-cites-vary


def test_citations_grounded_fails_on_fabricated_citation() -> None:
    """Cited buildium_tools but the trail is empty → a fact from training
    data wearing a fake citation. This is the #271 failure the check exists for."""
    result = _result_with(
        trail=[],
        final_output=_report_json(["Maple Court [buildium_tools.get_property#property:88421]"]),
    )
    _check_agent_flow_assertions(
        result=result, expected={"reverse_report_citations_grounded": True}
    )
    a = next(x for x in result.assertions if x.name == "reverse_report_citations_grounded")
    assert not a.passed
    assert "no matching tool call" in a.reason


def test_tool_call_total_max_passes_and_fails() -> None:
    ok = _result_with(trail=["a", "b"])
    _check_agent_flow_assertions(result=ok, expected={"tool_call_total_max": 2})
    assert all(x.passed for x in ok.assertions)

    over = _result_with(trail=["a", "b", "c"])
    _check_agent_flow_assertions(result=over, expected={"tool_call_total_max": 2})
    failures = [x for x in over.assertions if not x.passed]
    assert len(failures) == 1
    assert failures[0].critical is False
    assert "total tool call" in failures[0].reason


def test_reply_max_words_passes_and_fails() -> None:
    ok = _result_with(final_output="one two three")
    _check_agent_flow_assertions(result=ok, expected={"reply_max_words": 3})
    assert all(x.passed for x in ok.assertions)

    over = _result_with(final_output="one two three four five")
    _check_agent_flow_assertions(result=over, expected={"reply_max_words": 3})
    failures = [x for x in over.assertions if not x.passed]
    assert len(failures) == 1
    assert failures[0].critical is False
    assert "5 words" in failures[0].reason


# ── reply_must_not_match_regex (forbidden-pattern guard) ─────────────
# Regression: this key was only honored for orchestrator_routing cases,
# so agent_flow cases relying on it (e.g. release cases 100/401) passed it
# vacuously. It must now fail — and gate as SAFETY (critical=True).


def test_reply_must_not_match_regex_fails_on_forbidden_pattern() -> None:
    result = _result_with(final_output="Sure — her preference is email, based on what we know.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_not_match_regex": ["(?i)(her preference is|based on what we know)"]},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1
    # SAFETY: a forbidden-pattern match must hard-gate (not a quality flake).
    assert failures[0].critical is True
    assert "forbidden regex" in failures[0].reason


def test_reply_must_not_match_regex_passes_when_absent() -> None:
    result = _result_with(final_output="I don't have any record of that tenant on file.")
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_not_match_regex": ["(?i)(her preference is|based on what we know)"]},
    )
    assert all(a.passed for a in result.assertions)


def test_reply_must_not_match_regex_catches_invented_date() -> None:
    # Case 401's intent: when the tool returned nothing, the agent must not
    # fabricate a concrete lease date.
    result = _result_with(final_output="The lease ends 2027-06-30.")
    iso_date = r"20\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    _check_agent_flow_assertions(
        result=result,
        expected={"reply_must_not_match_regex": [iso_date]},
    )
    failures = [a for a in result.assertions if not a.passed]
    assert len(failures) == 1 and failures[0].critical is True
