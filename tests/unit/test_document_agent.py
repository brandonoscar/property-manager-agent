"""Unit tests for the document_agent specialist and orchestrator wiring.

Mirrors the test pattern from ``test_web_agent.py``: verifies the
specialist constructs cleanly, carries the right tool, has the
terminal-behavior rules the audit follow-up #2 required, and is
registered as a handoff target on the orchestrator. Heavier
LLM-driven routing assertions are out of scope here — they need a
Runner-driven harness (PR #143 audit follow-up #1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from helixis.agents import document_agent as da
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
    )


def test_document_agent_constructs_with_its_tools() -> None:
    agent = da.create_document_agent(_settings())
    assert agent.name == "document_agent"
    tool_names = [getattr(t, "name", "") for t in agent.tools]
    # #533 — the specialist now carries the free-form image assessor alongside
    # the structured document extractor.
    assert "extract_document_fields" in tool_names
    assert "assess_image" in tool_names
    assert len(agent.tools) == 2


def test_document_agent_prompt_carries_terminal_rules() -> None:
    """Carry-over #1: the system prompt must hard-rule every terminal
    failure path. The eval harness can't verify LLM compliance, but
    we can at least lock the prompt text shape."""
    text = da.DOCUMENT_AGENT_INSTRUCTIONS
    # Hard-rule language present
    assert "NEVER" in text
    assert "STOP" in text
    # Each terminal warning class explicitly mentioned
    for warning in (
        "attachment_unavailable",
        "attachment_too_large",
        "low_confidence",
        "doc_type_uncertain",
        "invalid_extraction",
        "llm_unavailable",
        "llm_unauthorized",
        "empty_document",
    ):
        assert warning in text, f"system prompt does not mention {warning}"
    # Identity rule
    assert "Buildium IDs" in text
    # Terminal-behavior section named
    assert "Terminal behavior" in text


def test_orchestrator_routing_text_mentions_document_agent() -> None:
    """The handoff prompt must teach the orchestrator when to route
    to document_agent."""
    text = ORCHESTRATOR_INSTRUCTIONS_API_ONLY
    assert "Document Agent" in text
    assert "extract the invoice" in text or "extract" in text.lower()
    # Document Agent must appear in the browser-enabled variant too
    assert "Document Agent" in ORCHESTRATOR_INSTRUCTIONS


def test_orchestrator_registers_document_agent_as_handoff() -> None:
    """Construct the orchestrator with stubbed specialists and assert
    ``document_agent`` is in the registered handoff list, before
    ``web_agent`` (web is the fall-through; document is in-domain)."""
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
            "helixis.agents.orchestrator.create_document_agent",
            return_value=_stub_named("document_agent"),
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
    assert "document_agent" in handoff_names
    # PR #153 — Browser Agent registered LAST (tool of last resort).
    # Web Agent sits before Browser Agent (snippet preferred over
    # real navigation). Document Agent still before Web Agent.
    assert handoff_names[-1] == "browser_agent"
    assert handoff_names.index("document_agent") < handoff_names.index("web_agent")
    assert handoff_names.index("web_agent") < handoff_names.index("browser_agent")
