"""Slice 2a tests: prompt-contract assembly + the evaluate_write decision core.

No live model — the contract assembly is pure string work and
``evaluate_write`` is a pure function over a parsed report.
"""

from __future__ import annotations

import json

from helixis.agents.reverse_report import (
    BUILDIUM_WRITE_MIN_CONFIDENCE,
    evaluate_write,
    parse_reverse_report,
)
from helixis.agents.reverse_report_prompt import (
    REVERSE_REPORT_CONTRACT,
    SPECIALIST_ADDENDA,
    apply_reverse_report_contract,
)

# ── contract assembly ────────────────────────────────────────────


def test_contract_disabled_returns_instructions_unchanged():
    base = "You are the Buildium specialist."
    assert apply_reverse_report_contract(base, "buildium_agent", enabled=False) == base


def test_contract_enabled_appends_contract_and_specialist_addendum():
    base = "You are the Buildium specialist."
    out = apply_reverse_report_contract(base, "buildium_agent", enabled=True)
    assert out.startswith(base)
    assert "REVERSE_REPORT contract (required)" in out
    assert SPECIALIST_ADDENDA["buildium_agent"] in out
    # The contract block itself is present verbatim.
    assert REVERSE_REPORT_CONTRACT in out


def test_contract_enabled_without_specialist_addendum():
    base = "Generic agent."
    out = apply_reverse_report_contract(base, "no_such_agent", enabled=True)
    assert REVERSE_REPORT_CONTRACT in out
    # No addendum appended for an unknown specialist key.
    assert out == base + "\n\n" + REVERSE_REPORT_CONTRACT


def test_every_specialist_has_an_addendum_key():
    # Guards against typo drift between factory keys and the addenda map.
    for key in (
        "orchestrator",
        "buildium_agent",
        "gmail_agent",
        "calendar_agent",
        "drive_agent",
        "web_agent",
        "document_agent",
        "browser_agent",
    ):
        assert key in SPECIALIST_ADDENDA


# ── evaluate_write decision core ─────────────────────────────────


def _report(**overrides):
    data = {
        "observed": ["work_order.id = 9183 status 'Open' [buildium_tools.list_work_orders#412]"],
        "inferred": [],
        "unknown": [],
        "confidence": 0.85,
        "next_action": {"kind": "tool_call", "why": "cancel the work order [from #1]"},
    }
    data.update(overrides)
    report, err = parse_reverse_report(json.dumps(data))
    assert err is None and report is not None
    return report


def test_evaluate_write_allows_clean_grounded_write():
    decision = evaluate_write(_report(), confirm_present=True)
    assert decision.allowed
    assert decision.reasons == []


def test_evaluate_write_blocks_when_no_report():
    decision = evaluate_write(None)
    assert not decision.allowed
    assert "no parseable REVERSE_REPORT" in decision.reasons[0]


def test_evaluate_write_blocks_on_unknown_present():
    decision = evaluate_write(_report(unknown=["which work order?"]), confirm_present=True)
    assert not decision.allowed
    assert any("rule 3" in r for r in decision.reasons)


def test_evaluate_write_blocks_on_uncited_observation():
    decision = evaluate_write(_report(observed=["the work order on 4B"]), confirm_present=True)
    assert not decision.allowed
    assert any("uncited" in r for r in decision.reasons)


def test_evaluate_write_blocks_below_confidence_floor():
    decision = evaluate_write(_report(confidence=0.5), confirm_present=True)
    assert not decision.allowed
    assert any("rule 4" in r for r in decision.reasons)


def test_evaluate_write_blocks_inferred_without_confirmation():
    rep = _report(inferred=["likely the leak one [from #1]"], confidence=0.7)
    # With confirmation → allowed; without → blocked on rule 2.
    assert evaluate_write(rep, confirm_present=True).allowed
    blocked = evaluate_write(rep, confirm_present=None)
    assert not blocked.allowed
    assert any("rule 2" in r for r in blocked.reasons)


def test_evaluate_write_blocks_overclaimed_confidence():
    # inferred present caps calibration at 0.79; 0.95 over-claims.
    rep = _report(inferred=["guess [from #1]"], confidence=0.95)
    decision = evaluate_write(rep, confirm_present=True)
    assert not decision.allowed
    assert any("over-claimed" in r for r in decision.reasons)


def test_buildium_write_needs_080_confidence():
    # 0.7 clears the default 0.6 floor but not Buildium's 0.80.
    rep = _report(confidence=0.7)
    assert evaluate_write(rep, confirm_present=True).allowed  # default floor
    strict = evaluate_write(rep, confirm_present=True, min_confidence=BUILDIUM_WRITE_MIN_CONFIDENCE)
    assert not strict.allowed
    assert any("0.8" in r for r in strict.reasons)
