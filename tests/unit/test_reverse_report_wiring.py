"""Slice 2a wiring test: the REVERSE_REPORT contract reaches every agent's
instructions when the flag is on, and is absent when off (the prod default).

This guards against a factory being forgotten or the flag being ignored.
"""

from __future__ import annotations

import pytest

from helixis.agents.browser_agent import create_browser_agent
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.document_agent import create_document_agent
from helixis.agents.drive_agent import create_drive_agent
from helixis.agents.gmail_agent import create_gmail_agent
from helixis.agents.orchestrator import create_orchestrator
from helixis.agents.web_agent import create_web_agent
from helixis.settings import Settings

_CONTRACT_MARKER = "REVERSE_REPORT contract (required)"


def _settings(*, reverse_report: bool) -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        composio={"api_key": ""},
        agent={"reverse_report": reverse_report},
    )


def _build_all(settings: Settings) -> dict[str, str]:
    """Return ``{agent_name: instructions}`` for every agent."""
    agents = [
        create_orchestrator(settings, composio_client=None),
        create_buildium_agent(settings),
        create_gmail_agent(settings),
        create_calendar_agent(settings),
        create_drive_agent(settings),
        create_web_agent(settings),
        create_document_agent(settings),
        create_browser_agent(settings),
    ]
    return {a.name: str(a.instructions) for a in agents}


def test_flag_off_leaves_every_agent_contract_free() -> None:
    for name, instructions in _build_all(_settings(reverse_report=False)).items():
        assert _CONTRACT_MARKER not in instructions, f"{name} leaked the contract with flag off"


def test_flag_on_injects_contract_into_every_agent() -> None:
    for name, instructions in _build_all(_settings(reverse_report=True)).items():
        assert _CONTRACT_MARKER in instructions, f"{name} missing the contract with flag on"


@pytest.mark.parametrize(
    ("factory_name", "addendum_fragment"),
    [
        ("buildium_agent", "numeric ID"),
        ("drive_agent", "file NAME is not its CONTENTS"),
        ("web_agent", "observation of THE"),
        ("document_agent", "page/section anchor"),
    ],
)
def test_specialist_addendum_lands_on_the_right_agent(
    factory_name: str, addendum_fragment: str
) -> None:
    settings = _settings(reverse_report=True)
    builders = {
        "buildium_agent": create_buildium_agent,
        "drive_agent": create_drive_agent,
        "web_agent": create_web_agent,
        "document_agent": create_document_agent,
    }
    agent = builders[factory_name](settings)
    assert addendum_fragment in str(agent.instructions)
