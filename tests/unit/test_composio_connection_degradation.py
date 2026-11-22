"""Composio connection-degradation handling (#183).

When the May 2026 key-rotation incident orphaned users' Composio grants, the
Gmail/Calendar/Drive connections went expired/dropped. ``get_tools`` only loads
tools for ACTIVE connections, so a lapsed grant looked identical to
never-connected and the specialists told users to "Connect" (as if new) — or
just failed vaguely.

These tests cover the graceful-degradation pieces:
  * ``ComposioClient.connection_health`` / ``degraded_toolkits`` — distinguish a
    lapsed grant (present, non-active) from never-connected (absent);
  * the specialist agents surface a RECONNECT message (not a first-time connect)
    when their toolkit is degraded.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.drive_agent import create_drive_agent
from helixis.agents.gmail_agent import create_gmail_agent
from helixis.connectors.composio_client import ComposioClient
from helixis.settings import Settings


def _client_with(connections: list[dict[str, Any]]) -> ComposioClient:
    """A ComposioClient whose list_connections returns canned rows (no SDK)."""
    client = ComposioClient(Settings(env="test", memory={"enabled": False}))
    client.list_connections = lambda user_id: connections  # type: ignore[assignment]
    return client


# ── connection_health / degraded_toolkits ────────────────────────────────


def test_connection_health_maps_toolkit_to_status() -> None:
    client = _client_with(
        [
            {"toolkit": "gmail", "status": "active"},
            {"toolkit": "googlecalendar", "status": "expired"},
        ]
    )
    assert client.connection_health("u-1") == {"GMAIL": "active", "GOOGLECALENDAR": "expired"}


def test_connection_health_active_wins_over_dropped() -> None:
    """The orphan incident left multiple rows per toolkit; an active row must
    not be masked by a dropped one."""
    client = _client_with(
        [
            {"toolkit": "gmail", "status": "dropped"},
            {"toolkit": "gmail", "status": "active"},
        ]
    )
    assert client.connection_health("u-1")["GMAIL"] == "active"


def test_degraded_toolkits_only_lapsed_grants() -> None:
    client = _client_with(
        [
            {"toolkit": "gmail", "status": "active"},  # healthy → excluded
            {"toolkit": "googlecalendar", "status": "expired"},  # degraded
            {"toolkit": "googledrive", "status": "dropped"},  # degraded
            # slack absent entirely → never-connected, NOT degraded
        ]
    )
    assert client.degraded_toolkits("u-1") == {"GOOGLECALENDAR", "GOOGLEDRIVE"}


def test_degraded_toolkits_empty_when_all_active() -> None:
    client = _client_with([{"toolkit": "gmail", "status": "connected"}])
    assert client.degraded_toolkits("u-1") == set()


# ── specialist reconnect vs first-time-connect messaging ──────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
    )


def test_gmail_agent_says_reconnect_when_expired(settings: Settings) -> None:
    agent = create_gmail_agent(settings, gmail_tools=None, connection_expired=True)
    instructions = agent.instructions.lower()
    assert "reconnect" in instructions
    assert "expired" in instructions
    assert "isn't connected yet" not in instructions


def test_gmail_agent_says_connect_when_never_connected(settings: Settings) -> None:
    agent = create_gmail_agent(settings, gmail_tools=None, connection_expired=False)
    instructions = agent.instructions.lower()
    assert "isn't connected yet" in instructions
    assert "connection has expired" not in instructions


def test_calendar_agent_says_reconnect_when_expired(settings: Settings) -> None:
    agent = create_calendar_agent(settings, calendar_tools=None, connection_expired=True)
    assert "reconnect" in agent.instructions.lower()
    assert "isn't connected yet" not in agent.instructions.lower()


def test_drive_agent_says_reconnect_when_expired(settings: Settings) -> None:
    agent = create_drive_agent(settings, drive_tools=None, connection_expired=True)
    assert "reconnect" in agent.instructions.lower()
    assert "isn't connected yet" not in agent.instructions.lower()


def test_expired_flag_ignored_when_tools_present(settings: Settings) -> None:
    """With live tools the agent must show NEITHER degradation notice, even if
    the flag is set — tools mean the connection is working."""
    from agents import FunctionTool

    async def _inner(_ctx: Any, _payload: str) -> str:
        return "{}"

    tool = FunctionTool(
        name="GMAIL_LIST_MESSAGES",
        description="stub",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=_inner,
        strict_json_schema=False,
    )
    agent = create_gmail_agent(settings, gmail_tools=[tool], connection_expired=True)
    instructions = agent.instructions.lower()
    assert "connection has expired" not in instructions
    assert "isn't connected yet" not in instructions
