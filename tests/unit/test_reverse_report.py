"""Unit tests for the REVERSE_REPORT core (Slice 1).

Anchored on the contract's Appendix A (good) and Appendix B (bad, caught
by the enforcer) so the parser/validator provably agrees with the spec.
"""

from __future__ import annotations

import json

import pytest

from helixis.agents.reverse_report import (
    citation_source,
    confidence_is_calibrated,
    inferred_requires_confirm_ok,
    is_cited,
    is_side_effectful_tool,
    max_allowed_confidence,
    parse_reverse_report,
    report_violations,
    ungrounded_observations,
    unknown_blocks_writes_ok,
)

# ── spec fixtures ────────────────────────────────────────────────

APPENDIX_A_GOOD = {
    "observed": [
        "user said: 'cancel the open work order on Unit 4B at Maple Heights' [user_message@turn_1]",
        "property.id = 41 with name 'Maple Heights' [buildium_tools.search_properties#q_maple]",
        "unit.id = 412 on property #41 with label '4B' [buildium_tools.list_units#41]",
        "work_order.id = 9183 status 'Open' [buildium_tools.list_work_orders#412]",
        "work_order.id = 9184 status 'Open' [buildium_tools.list_work_orders#412]",
    ],
    "inferred": [],
    "unknown": ["Which of the two open work orders on Unit 4B (#9183 or #9184) did the user mean?"],
    "confidence": 0.72,
    "next_action": {
        "kind": "ask_user",
        "why": "Two open work orders match; cannot cancel without disambiguation [from #4, #5].",
    },
}

APPENDIX_B_BAD = {
    "observed": ["user wants to cancel a work order on Unit 4B"],
    "inferred": ["it's probably the most recent one"],
    "unknown": [],
    "confidence": 0.9,
    "next_action": {"kind": "tool_call", "why": "cancel work order"},
}


# ── parsing ──────────────────────────────────────────────────────


def test_parse_appendix_a_good():
    report, err = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    assert err is None
    assert report is not None
    assert len(report.observed) == 5
    assert report.confidence == 0.72
    assert report.next_action.kind == "ask_user"


def test_parse_tolerates_surrounding_prose_and_fence():
    text = (
        "Here is my reasoning.\n\n```json\n"
        + json.dumps(APPENDIX_A_GOOD)
        + "\n```\n\nNow the reply to the user."
    )
    report, err = parse_reverse_report(text)
    assert err is None
    assert report is not None and len(report.observed) == 5


def test_parse_picks_report_object_past_a_stray_object():
    text = 'noise {"foo": 1} more ' + json.dumps(APPENDIX_A_GOOD)
    report, err = parse_reverse_report(text)
    assert err is None and report is not None


def test_parse_malformed_json_returns_error():
    report, err = parse_reverse_report("{ observed: [unquoted] ")
    assert report is None
    assert err and "no JSON object" in err or "not valid JSON" in err


def test_parse_empty_returns_error():
    report, err = parse_reverse_report("")
    assert report is None and err == "empty model output"


def test_parse_confidence_out_of_range_fails_schema():
    bad = {**APPENDIX_A_GOOD, "confidence": 1.7}
    report, err = parse_reverse_report(json.dumps(bad))
    assert report is None
    assert err and "schema validation" in err


def test_parse_unknown_next_action_kind_fails_schema():
    bad = {**APPENDIX_A_GOOD, "next_action": {"kind": "yolo", "why": "x"}}
    report, err = parse_reverse_report(json.dumps(bad))
    assert report is None and err and "schema validation" in err


# ── citation discipline ──────────────────────────────────────────


def test_is_cited_accepts_dotted_buildium_path():
    assert is_cited("property.id = 41 [buildium_tools.search_properties#q_maple]")


def test_is_cited_accepts_mem0_and_user_message_tokens():
    assert is_cited("memory: 'Unit 4B tenant is Sarah Chen' [mem0:m_9f2c]")
    assert is_cited("user said: 'cancel it' [user_message@turn_4]")


def test_is_cited_rejects_uncited_and_unknown_source():
    assert not is_cited("user wants to cancel a work order on Unit 4B")
    assert not is_cited("something [random_tool#1]")


# ── confidence calibration ───────────────────────────────────────


def test_calibration_clean_cited_chain_allows_high():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    assert max_allowed_confidence(report) == 1.0
    assert confidence_is_calibrated(report)  # 0.72 ≤ 1.0


def test_calibration_inferred_caps_at_079():
    data = {**APPENDIX_A_GOOD, "inferred": ["likely the leak one [from #4]"], "confidence": 0.79}
    report, _ = parse_reverse_report(json.dumps(data))
    assert max_allowed_confidence(report) == 0.79
    assert confidence_is_calibrated(report)
    over = {**data, "confidence": 0.85}
    report_over, _ = parse_reverse_report(json.dumps(over))
    assert not confidence_is_calibrated(report_over)


def test_calibration_no_cited_observation_caps_at_039():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_B_BAD))
    assert max_allowed_confidence(report) == 0.39
    assert not confidence_is_calibrated(report)  # 0.9 ≫ 0.39


# ── report_violations: Appendix A clean, Appendix B flagged ───────


def test_appendix_a_has_no_internal_violations():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    assert report_violations(report) == []


def test_appendix_b_flags_uncited_and_miscalibrated():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_B_BAD))
    violations = report_violations(report)
    joined = " | ".join(violations)
    assert "uncited" in joined
    assert "calibration ceiling" in joined


def test_empty_observed_must_ask_or_tool_call():
    data = {
        "observed": [],
        "inferred": [],
        "unknown": [],
        "confidence": 0.3,
        "next_action": {"kind": "reply", "why": "answer"},
    }
    report, _ = parse_reverse_report(json.dumps(data))
    assert any("empty observed" in v for v in report_violations(report))
    # ask_user is allowed even with empty observed.
    ok = {**data, "next_action": {"kind": "ask_user", "why": "need info"}}
    report_ok, _ = parse_reverse_report(json.dumps(ok))
    assert not any("empty observed" in v for v in report_violations(report_ok))


def test_low_confidence_must_ask_or_escalate():
    data = {
        "observed": ["x [user_message@turn_1]"],
        "inferred": [],
        "unknown": [],
        "confidence": 0.5,
        "next_action": {"kind": "reply", "why": "answer"},
    }
    report, _ = parse_reverse_report(json.dumps(data))
    assert any("< 0.6" in v for v in report_violations(report))


# ── side-effectful classification ────────────────────────────────


def test_is_side_effectful_tool():
    assert is_side_effectful_tool("buildium_create_to_do_task")
    assert is_side_effectful_tool("submit_form")
    assert is_side_effectful_tool("GMAIL_SEND_EMAIL")  # verb heuristic
    assert is_side_effectful_tool("GOOGLECALENDAR_CREATE_EVENT")
    assert not is_side_effectful_tool("buildium_list_work_orders")
    assert not is_side_effectful_tool("read_page")


# ── trail-dependent rules ────────────────────────────────────────


def test_unknown_blocks_writes():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))  # unknown non-empty
    assert unknown_blocks_writes_ok(report, ["buildium_list_work_orders"])  # read ok
    assert not unknown_blocks_writes_ok(report, ["buildium_create_to_do_task"])  # write blocked


def test_unknown_empty_allows_writes():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_B_BAD))  # unknown empty
    assert unknown_blocks_writes_ok(report, ["buildium_create_to_do_task"])


def test_inferred_requires_confirm():
    report, _ = parse_reverse_report(json.dumps(APPENDIX_B_BAD))  # inferred non-empty
    trail = ["buildium_create_to_do_task"]
    assert inferred_requires_confirm_ok(report, trail, confirm_present=True) is True
    assert inferred_requires_confirm_ok(report, trail, confirm_present=False) is False
    # Undetermined until Slice-2 confirm-trace capture.
    assert inferred_requires_confirm_ok(report, trail, confirm_present=None) is None
    # No write tool ⇒ no confirmation needed.
    assert (
        inferred_requires_confirm_ok(report, ["buildium_list_units"], confirm_present=None) is True
    )
    # No inference ⇒ rule does not apply.
    clean, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    assert inferred_requires_confirm_ok(clean, trail, confirm_present=None) is True


# ── citation grounding (#271 citation-graph core, deterministic) ──


def test_citation_source_extracts_module():
    assert (
        citation_source("property.id = 41 [buildium_tools.search_properties#q_maple]")
        == "buildium_tools"
    )
    assert citation_source("user said x [user_message@turn_1]") == "user_message"
    assert citation_source("memory note [mem0:m_9f2c]") == "mem0"
    assert citation_source("a bare fact with no citation") is None


def test_grounding_empty_when_every_cited_source_was_called():
    """Appendix A cites buildium_tools (×4) + user_message; with the buildium
    tools actually in the trail, nothing is ungrounded."""
    report, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    trail = ["buildium_search_properties", "buildium_list_units", "buildium_list_work_orders"]
    assert ungrounded_observations(report, trail) == []


def test_grounding_flags_fabricated_citation():
    """The headline failure: cites buildium_tools but no Buildium tool ran →
    the 4 buildium-cited facts are fabricated; the user_message fact is fine."""
    report, _ = parse_reverse_report(json.dumps(APPENDIX_A_GOOD))
    ungrounded = ungrounded_observations(report, tool_call_trail=[])
    assert len(ungrounded) == 4
    assert all("buildium_tools" in obs for obs in ungrounded)


def test_grounding_user_message_always_grounded():
    data = {**APPENDIX_A_GOOD, "observed": ["user said 'x' [user_message@turn_1]"]}
    report, _ = parse_reverse_report(json.dumps(data))
    assert ungrounded_observations(report, tool_call_trail=[]) == []


def test_grounding_skips_uncited_observations():
    """Uncited facts are NOT flagged here — that's observed_cited's job."""
    data = {**APPENDIX_A_GOOD, "observed": ["a bare fact with no citation"]}
    report, _ = parse_reverse_report(json.dumps(data))
    assert ungrounded_observations(report, tool_call_trail=[]) == []


@pytest.mark.parametrize(
    "token,trail,grounded",
    [
        ("[gmail_tools.fetch#1]", ["GMAIL_FETCH_EMAILS"], True),
        ("[gmail_tools.fetch#1]", ["buildium_get_property"], False),
        ("[calendar_tools.list#1]", ["GOOGLECALENDAR_EVENTS_LIST"], True),
        ("[drive_tools.find#1]", ["GOOGLEDRIVE_FIND_FILE"], True),
        ("[web_tools.search#1]", ["web_search"], True),
        ("[browser_tools.nav#1]", ["navigate"], True),
        ("[mem0:m_1]", ["search_memory"], True),
        ("[mem0:m_1]", [], False),
    ],
)
def test_grounding_classifier_per_source(token: str, trail: list[str], grounded: bool):
    data = {**APPENDIX_A_GOOD, "observed": [f"fact {token}"]}
    report, _ = parse_reverse_report(json.dumps(data))
    assert (ungrounded_observations(report, trail) == []) is grounded
