"""Unit tests for the web_agent specialist and orchestrator wiring.

Heavier orchestrator routing assertions (the LLM actually picking
web_agent vs not) would need a real OpenAI Agents Runner and live LLM
— out of scope for this unit suite. These tests verify the structural
wiring: the specialist constructs cleanly, carries the web_search
tool, and is registered as a handoff target on the orchestrator with
the routing-guidance text live in the prompt.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from helixis.agents import web_agent as wa
from helixis.agents.orchestrator import (
    ORCHESTRATOR_INSTRUCTIONS,
    ORCHESTRATOR_INSTRUCTIONS_API_ONLY,
    create_orchestrator,
)
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        web={"tavily_api_key": "tvly-fake"},
    )


def test_web_agent_constructs_with_search_and_table_tools() -> None:
    agent = wa.create_web_agent(_settings())
    assert agent.name == "web_agent"
    # The agent carries web_search plus show_table (so multi-result searches
    # can render as a table) — no Composio tools, no confirmation gate, no
    # memory tools.
    tool_names = [getattr(t, "name", "") for t in agent.tools]
    assert "web_search" in tool_names
    assert "show_table" in tool_names
    assert len(agent.tools) == 2


def test_web_agent_prompt_carries_grounding_rules() -> None:
    """System prompt must explicitly require source citation, refuse
    fabrication on unavailability, and forbid internal Buildium IDs.
    These are the safety properties the eval cases lock down."""
    text = wa.WEB_AGENT_INSTRUCTIONS
    assert "source.url" in text
    assert "search_unavailable" in text
    assert "search_unauthorized" in text
    assert "Buildium IDs" in text
    assert "no in-domain specialist" in text


def test_web_agent_prompt_has_stop_if_nothing_grounded_rule() -> None:
    """Audit follow-up #2 — the web_agent prompt must include a hard
    terminal rule for the 'every claim would have to be omitted' case:
    STOP, reply with the no-grounding sentence, hand back."""
    text = wa.WEB_AGENT_INSTRUCTIONS
    assert "STOP" in text
    assert "can't ground an answer" in text
    assert "training data" in text
    # The rule must be a hard rule, not a soft suggestion
    assert "Do NOT freelance" in text or "do not freelance" in text.lower()


def test_orchestrator_routing_text_mentions_web_agent() -> None:
    """The handoff prompt must teach the orchestrator when to route
    to web_agent. Both the API-only and browser-enabled variants
    inherit from the same base text — assert against API-only."""
    text = ORCHESTRATOR_INSTRUCTIONS_API_ONLY
    assert "Web Agent" in text
    assert "no in-domain specialist" in text
    # Don't fire when an in-domain specialist covers it
    assert "Buildium" in text and "Gmail" in text
    # browser-enabled variant is API-only + browser fallback section
    assert "Web Agent" in ORCHESTRATOR_INSTRUCTIONS


def test_orchestrator_mixed_query_language_is_tightened() -> None:
    """Audit follow-up #2 — replace 'optionally hand off' with stricter
    phrasing. The orchestrator must only chain Web Agent on a mixed
    query when web context would materially change the answer."""
    text = ORCHESTRATOR_INSTRUCTIONS_API_ONLY
    # The old soft phrasing is gone
    assert "optionally hand off to the Web Agent" not in text
    # The new gating language is present
    assert "MATERIALLY change" in text or "materially change" in text.lower()
    # The in-domain-first rule is still explicit
    assert "in-domain part first" in text


def test_orchestrator_registers_web_agent_as_handoff() -> None:
    """Construct the orchestrator with stubbed specialists and assert
    ``web_agent`` is in the registered handoff list, last in order."""
    settings = _settings()

    def _stub_named(name: str) -> MagicMock:
        m = MagicMock()
        m.name = name
        return m

    with (
        patch(
            "helixis.agents.orchestrator.create_buildium_agent",
            return_value=_stub_named("buildium_agent"),
        ),
        patch(
            "helixis.agents.orchestrator.create_calendar_agent",
            return_value=_stub_named("calendar_agent"),
        ),
        patch(
            "helixis.agents.orchestrator.create_gmail_agent",
            return_value=_stub_named("gmail_agent"),
        ),
        patch(
            "helixis.agents.orchestrator.create_drive_agent",
            return_value=_stub_named("drive_agent"),
        ),
        patch(
            "helixis.agents.orchestrator.create_web_agent",
            return_value=_stub_named("web_agent"),
        ),
        patch(
            "helixis.agents.orchestrator.create_browser_agent",
            return_value=_stub_named("browser_agent"),
        ),
    ):
        orchestrator = create_orchestrator(settings, composio_client=None)

    handoff_names = [getattr(h, "name", "") for h in orchestrator.handoffs]
    assert "web_agent" in handoff_names
    # PR #153 — Browser Agent is now LAST in the chain (tool of
    # last resort); Web Agent sits just before it as the snippet
    # fall-through.
    assert handoff_names[-1] == "browser_agent"
    assert handoff_names.index("web_agent") < handoff_names.index("browser_agent")
