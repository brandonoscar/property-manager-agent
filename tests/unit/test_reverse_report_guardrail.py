"""Slice 2b tests: the REVERSE_REPORT tool-input guardrail.

The guardrail only fires on live model runs, so these tests exercise it
directly with synthetic ``ToolInputGuardrailData`` — no SDK run, no model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from helixis.agents import reverse_report_guardrail as g
from helixis.agents.reverse_report_guardrail import (
    REVERSE_REPORT_TOOL_GUARDRAIL,
    attach_reverse_report_guardrail,
    extract_latest_reverse_report,
)

_GOOD = json.dumps(
    {
        "observed": ["work_order.id = 9183 status 'Open' [buildium_tools.list_work_orders#412]"],
        "inferred": [],
        "unknown": [],
        "confidence": 0.85,
        "next_action": {"kind": "tool_call", "why": "cancel wo [from #1]"},
    }
)
_BAD_UNKNOWN = json.dumps(
    {
        "observed": ["wo 9183 [buildium_tools.list_work_orders#412]"],
        "inferred": [],
        "unknown": ["which work order?"],
        "confidence": 0.85,
        "next_action": {"kind": "tool_call", "why": "cancel"},
    }
)


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _data(tool_name: str, turn_input: list) -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(tool_name=tool_name, turn_input=turn_input),
        agent=None,
    )


def _enable(monkeypatch, *, on: bool) -> None:
    monkeypatch.setattr(
        g, "get_settings", lambda: SimpleNamespace(agent=SimpleNamespace(reverse_report=on))
    )


# ── flag gate ────────────────────────────────────────────────────


def test_flag_off_allows_even_a_bare_write(monkeypatch):
    _enable(monkeypatch, on=False)
    out = g._evaluate(_data("buildium_create_to_do_task", []))
    assert out.behavior["type"] == "allow"


def test_flag_on_read_tool_is_never_gated(monkeypatch):
    _enable(monkeypatch, on=True)
    out = g._evaluate(_data("buildium_list_tenants", []))
    assert out.behavior["type"] == "allow"


# ── enforcement ──────────────────────────────────────────────────


def test_flag_on_good_report_allows_write(monkeypatch):
    _enable(monkeypatch, on=True)
    out = g._evaluate(_data("buildium_create_to_do_task", [_assistant(_GOOD)]))
    assert out.behavior["type"] == "allow"


def test_flag_on_unknown_present_rejects_write(monkeypatch):
    _enable(monkeypatch, on=True)
    out = g._evaluate(_data("buildium_create_to_do_task", [_assistant(_BAD_UNKNOWN)]))
    assert out.behavior["type"] == "reject_content"
    assert any("rule 3" in r for r in out.output_info["reasons"])


def test_flag_on_missing_report_rejects_write(monkeypatch):
    _enable(monkeypatch, on=True)
    out = g._evaluate(_data("buildium_create_work_order", [_assistant("just prose, no JSON")]))
    assert out.behavior["type"] == "reject_content"


def test_buildium_write_below_080_rejected(monkeypatch):
    _enable(monkeypatch, on=True)
    report = json.dumps(
        {
            "observed": ["wo 9183 [buildium_tools.list_work_orders#412]"],
            "inferred": [],
            "unknown": [],
            "confidence": 0.7,  # clears 0.6 default, not Buildium's 0.80
            "next_action": {"kind": "tool_call", "why": "cancel [from #1]"},
        }
    )
    out = g._evaluate(_data("buildium_create_to_do_task", [_assistant(report)]))
    assert out.behavior["type"] == "reject_content"


def test_guardrail_fails_open_on_internal_error(monkeypatch):
    _enable(monkeypatch, on=True)
    monkeypatch.setattr(
        g, "evaluate_write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = g._evaluate(_data("buildium_create_to_do_task", [_assistant(_GOOD)]))
    # A guardrail bug must never break a write.
    assert out.behavior["type"] == "allow"
    assert "guardrail_error" in out.output_info


# ── report extraction from turn input ────────────────────────────


def test_extract_picks_latest_assistant_report():
    older = _assistant(json.dumps({**json.loads(_GOOD), "confidence": 0.61}))
    newer = _assistant(_GOOD)
    report, err = extract_latest_reverse_report([{"role": "user", "content": "hi"}, older, newer])
    assert err is None and report is not None
    assert report.confidence == 0.85  # the newer one


def test_extract_handles_content_parts_list():
    item = {"role": "assistant", "content": [{"type": "output_text", "text": _GOOD}]}
    report, err = extract_latest_reverse_report([item])
    assert err is None and report is not None


def test_extract_ignores_user_and_system_items():
    report, err = extract_latest_reverse_report(
        [{"role": "user", "content": _GOOD}, {"role": "system", "content": _GOOD}]
    )
    assert report is None and err


# ── attach helper ────────────────────────────────────────────────


def test_attach_is_idempotent_and_write_only():
    write = SimpleNamespace(name="buildium_create_to_do_task", tool_input_guardrails=None)
    read = SimpleNamespace(name="buildium_list_tenants", tool_input_guardrails=None)
    tools = [write, read]
    attach_reverse_report_guardrail(tools)
    attach_reverse_report_guardrail(tools)  # idempotent
    assert write.tool_input_guardrails.count(REVERSE_REPORT_TOOL_GUARDRAIL) == 1
    assert not (read.tool_input_guardrails or [])


# ── factory wiring (Slice 2c: browser + Composio) ────────────────


def _settings():
    from helixis.settings import Settings

    return Settings(env="test", memory={"enabled": False}, composio={"api_key": ""})


def _guarded(tool) -> bool:
    return REVERSE_REPORT_TOOL_GUARDRAIL in (getattr(tool, "tool_input_guardrails", None) or [])


def test_browser_factory_gates_submit_form_only():
    from helixis.agents.browser_agent import create_browser_agent

    agent = create_browser_agent(_settings())
    by_name = {getattr(t, "name", ""): t for t in agent.tools}
    assert _guarded(by_name["submit_form"])  # side-effectful write
    assert not _guarded(by_name["read_page"])  # read-only


def test_gmail_factory_gates_composio_write_not_read():
    from helixis.agents.gmail_agent import create_gmail_agent

    send = SimpleNamespace(name="GMAIL_SEND_EMAIL", tool_input_guardrails=None)
    fetch = SimpleNamespace(name="GMAIL_FETCH_EMAILS", tool_input_guardrails=None)
    create_gmail_agent(_settings(), gmail_tools=[send, fetch])
    assert _guarded(send)
    assert not _guarded(fetch)


def test_composio_factory_no_tools_is_safe():
    from helixis.agents.calendar_agent import create_calendar_agent
    from helixis.agents.drive_agent import create_drive_agent

    # Not-connected path (no tools) must not error.
    create_calendar_agent(_settings())
    create_drive_agent(_settings())
