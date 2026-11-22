"""Specialist ``show_table`` wiring.

The orchestrator has always carried ``show_table``; this suite locks in that
the list-producing specialists (Buildium, Web, and the connected Composio
agents) carry it too — and, crucially, that a NOT-connected Composio agent does
NOT (it has no data to tabulate, and its prompt should stay focused on telling
the user to connect). Structural wiring only — no live LLM.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.drive_agent import create_drive_agent
from helixis.agents.gmail_agent import create_gmail_agent
from helixis.agents.outlook_agent import create_outlook_agent
from helixis.agents.web_agent import create_web_agent
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        web={"tavily_api_key": "tvly-fake"},
    )


def _tool_names(agent: Any) -> list[str]:
    return [getattr(t, "name", "") for t in agent.tools]


def _fake_connector_tool(name: str) -> MagicMock:
    """A stand-in Composio FunctionTool — attach_reverse_report_guardrail only
    reads attributes, so a MagicMock with a name is enough for wiring tests."""
    m = MagicMock()
    m.name = name
    return m


def test_buildium_agent_carries_show_table() -> None:
    agent = create_buildium_agent(_settings())
    assert "show_table" in _tool_names(agent)


def test_web_agent_carries_show_table() -> None:
    agent = create_web_agent(_settings())
    assert "show_table" in _tool_names(agent)


def test_connected_composio_agents_carry_show_table() -> None:
    s = _settings()
    gmail = create_gmail_agent(s, gmail_tools=[_fake_connector_tool("GMAIL_FETCH_EMAILS")])
    cal = create_calendar_agent(s, calendar_tools=[_fake_connector_tool("CAL_LIST")])
    drive = create_drive_agent(s, drive_tools=[_fake_connector_tool("DRIVE_LIST")])
    outlook = create_outlook_agent(s, outlook_tools=[_fake_connector_tool("OUTLOOK_FETCH")])
    for agent in (gmail, cal, drive, outlook):
        assert "show_table" in _tool_names(agent), agent.name


def test_not_connected_composio_agents_omit_show_table() -> None:
    """No connection → no data to tabulate → no table tool (and the prompt
    stays the connect/reconnect notice, not the table hint)."""
    s = _settings()
    for agent in (
        create_gmail_agent(s, gmail_tools=None),
        create_calendar_agent(s, calendar_tools=None),
        create_drive_agent(s, drive_tools=None),
        create_outlook_agent(s, outlook_tools=None, connection_expired=True),
    ):
        assert "show_table" not in _tool_names(agent), agent.name
        assert "Answering with a table" not in str(agent.instructions), agent.name
