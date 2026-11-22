"""Routing-trail extraction across both topologies (W1 slice 3).

The routing-eval harness asserts on a "routing trail" — which specialists the
orchestrator engaged. Under the proven **handoff** topology that trail comes
from ``HandoffCallItem``s and ``RunResult.last_agent``. Under the **reasoning
loop** (specialists as ``.as_tool()``), there are no handoffs and last_agent
stays the orchestrator, so the trail must instead come from the specialist TOOL
calls. This locks both extractors and the single-source-of-truth specialist set.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents import Agent
from agents.items import HandoffCallItem, ToolCallItem

from helixis.agents.orchestrator import (
    _SPECIALIST_TOOL_DESCRIPTIONS,
    SPECIALIST_AGENT_NAMES,
    create_orchestrator,
)
from helixis.evals.orchestrator_routing import (
    _extract_handoff_trail,
    _extract_specialist_tool_trail,
)
from helixis.settings import Settings

_AGENT = Agent(name="Helixis", instructions="x")


def _run_result(items: list) -> SimpleNamespace:
    return SimpleNamespace(new_items=items, last_agent=_AGENT)


def _tool_call(name: str) -> ToolCallItem:
    return ToolCallItem(agent=_AGENT, raw_item=SimpleNamespace(name=name))


def _tool_call_dict_raw(name: str) -> ToolCallItem:
    # The live SDK's raw_item is sometimes a TypedDict — extractor must handle it.
    return ToolCallItem(agent=_AGENT, raw_item={"name": name})


def _handoff(name: str) -> HandoffCallItem:
    return HandoffCallItem(agent=_AGENT, raw_item=SimpleNamespace(name=name))


# ── single source of truth ───────────────────────────────────────


def test_specialist_names_match_descriptions_and_orchestrator() -> None:
    # The frozenset is derived from the descriptions, and both cover exactly the
    # 9 specialists the orchestrator wires (handoff-mode handoffs == names).
    assert SPECIALIST_AGENT_NAMES == frozenset(_SPECIALIST_TOOL_DESCRIPTIONS)
    settings = Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "k"},
        memory={"enabled": False},
    )
    handoff_names = {h.name for h in create_orchestrator(settings).handoffs}
    assert handoff_names == set(SPECIALIST_AGENT_NAMES)


# ── handoff-mode extractor (unchanged behavior) ──────────────────


def test_handoff_trail_strips_transfer_prefix_in_order() -> None:
    items = [_handoff("transfer_to_buildium_agent"), _handoff("transfer_to_gmail_agent")]
    assert _extract_handoff_trail(_run_result(items)) == ["buildium_agent", "gmail_agent"]


def test_handoff_trail_ignores_non_handoff_items() -> None:
    items = [_tool_call("buildium_agent"), _handoff("transfer_to_web_agent")]
    # Only the HandoffCallItem counts here.
    assert _extract_handoff_trail(_run_result(items)) == ["web_agent"]


# ── tool-mode extractor (reasoning loop) ─────────────────────────


def test_specialist_tool_trail_collects_specialist_calls_in_order() -> None:
    items = [
        _tool_call("buildium_agent"),
        _tool_call("document_agent"),
        _tool_call("gmail_agent"),
    ]
    assert _extract_specialist_tool_trail(_run_result(items)) == [
        "buildium_agent",
        "document_agent",
        "gmail_agent",
    ]


def test_specialist_tool_trail_ignores_base_tools() -> None:
    # search_memory / show_table are ordinary tools, NOT specialist consultations.
    items = [
        _tool_call("search_memory"),
        _tool_call("buildium_agent"),
        _tool_call("show_table"),
    ]
    assert _extract_specialist_tool_trail(_run_result(items)) == ["buildium_agent"]


def test_specialist_tool_trail_handles_dict_raw_item() -> None:
    items = [_tool_call_dict_raw("web_agent")]
    assert _extract_specialist_tool_trail(_run_result(items)) == ["web_agent"]


def test_specialist_tool_trail_empty_without_specialist_calls() -> None:
    items = [_tool_call("search_memory"), _handoff("transfer_to_gmail_agent")]
    # No specialist TOOL call → empty (the handoff item is not a tool call here).
    assert _extract_specialist_tool_trail(_run_result(items)) == []
