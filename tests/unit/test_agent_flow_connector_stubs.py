"""Harness wiring for the Composio connector agents in agent_flow (#297/#298).

Composio is disabled in evals, so Gmail/Calendar/Drive normally boot with
zero tools. These tests pin the eval-stub plumbing that makes connector
``agent_flow`` cases writable — all offline, no LLM:

1. The stub slugs are registered in ``_TOOL_MOCK_TARGETS`` pointing at the
   patchable ``*_impl`` coroutines, so cases can mock them.
2. ``_build_flow_agent`` injects the stub tools for a happy-path connector
   case, and yields a zero-tool reconnect agent when ``connection_expired``.
3. ``_build_sequenced_patches`` accepts a connector mock spec.
"""

from __future__ import annotations

import pytest

from helixis.evals.agent_flow import (
    _AGENT_FACTORIES,
    _TOOL_MOCK_TARGETS,
    _build_flow_agent,
    _build_sequenced_patches,
    _flow_settings,
)
from helixis.evals.connector_stub_tools import CONNECTOR_EVAL_TOOLS

_CONNECTOR_SLUGS = {
    "Gmail Agent": "GMAIL_FETCH_EMAILS",
    "Calendar Agent": "GOOGLECALENDAR_EVENTS_LIST",
    "Drive Agent": "GOOGLEDRIVE_FIND_FILE",
}


def test_connector_slugs_registered_as_mock_targets() -> None:
    for slug in _CONNECTOR_SLUGS.values():
        assert slug in _TOOL_MOCK_TARGETS, f"{slug} not mockable"
        target_path, _factory = _TOOL_MOCK_TARGETS[slug]
        assert target_path.startswith("helixis.evals.connector_stub_tools.")
        assert target_path.endswith("_impl")


def test_connector_agents_are_registered_factories() -> None:
    for agent_name in _CONNECTOR_SLUGS:
        assert agent_name in _AGENT_FACTORIES
        assert agent_name in CONNECTOR_EVAL_TOOLS


@pytest.mark.parametrize(("agent_name", "slug"), list(_CONNECTOR_SLUGS.items()))
def test_happy_path_agent_gets_stub_tool(agent_name: str, slug: str) -> None:
    settings = _flow_settings()
    agent = _build_flow_agent(agent_name, _AGENT_FACTORIES[agent_name], settings, {})
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert slug in tool_names, f"{agent_name} missing stub tool {slug}: {tool_names}"


@pytest.mark.parametrize("agent_name", list(_CONNECTOR_SLUGS))
def test_connection_expired_agent_has_zero_tools_and_reconnect_prompt(agent_name: str) -> None:
    settings = _flow_settings()
    agent = _build_flow_agent(
        agent_name,
        _AGENT_FACTORIES[agent_name],
        settings,
        {"connection_expired": True},
    )
    assert not agent.tools, f"{agent_name} expired path should carry no tools"
    assert "reconnect" in agent.instructions.lower()


def test_sequenced_patches_accept_connector_mock() -> None:
    patches = _build_sequenced_patches(
        {"GMAIL_FETCH_EMAILS": [{"return": "1 email from Sarah Chen — subject: Lease renewal"}]}
    )
    assert len(patches) == 1


def test_non_connector_agent_built_with_settings_only() -> None:
    # Browser Agent is not a connector — _build_flow_agent must not inject stubs.
    settings = _flow_settings()
    agent = _build_flow_agent("Browser Agent", _AGENT_FACTORIES["Browser Agent"], settings, {})
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "GMAIL_FETCH_EMAILS" not in tool_names


# ── #359 / #386 additions: write tools + Outlook specialist ──────────────

import json  # noqa: E402
from pathlib import Path  # noqa: E402

# slug the case exercises -> the agent that must carry it as a stub tool.
_NEW_STUB_SLUGS = {
    "GMAIL_SEND_EMAIL": "Gmail Agent",
    "GOOGLECALENDAR_CREATE_EVENT": "Calendar Agent",
    # Sonnet resolves an event id via FIND_EVENT (real Composio slug) rather
    # than EVENTS_LIST — missing stub crashed case 058 on the 2026-07-22 merge.
    "GOOGLECALENDAR_FIND_EVENT": "Calendar Agent",
    "OUTLOOK_LIST_MESSAGES": "Outlook Agent",
}
_GOLDEN_DIR = Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals" / "golden"


def test_new_connector_slugs_registered_as_mock_targets() -> None:
    # Write tools (#359) + Outlook read (#386) must be mockable from JSON.
    for slug in _NEW_STUB_SLUGS:
        assert slug in _TOOL_MOCK_TARGETS, f"{slug} not mockable"
        target_path, _factory = _TOOL_MOCK_TARGETS[slug]
        assert target_path.startswith("helixis.evals.connector_stub_tools.")
        assert target_path.endswith("_impl")


def test_outlook_agent_registered_and_gets_read_stub() -> None:
    # #386 — the Outlook specialist joins the flow harness with a read stub.
    assert "Outlook Agent" in _AGENT_FACTORIES
    assert "Outlook Agent" in CONNECTOR_EVAL_TOOLS
    settings = _flow_settings()
    agent = _build_flow_agent("Outlook Agent", _AGENT_FACTORIES["Outlook Agent"], settings, {})
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "OUTLOOK_LIST_MESSAGES" in tool_names, tool_names


@pytest.mark.parametrize(("slug", "agent_name"), list(_NEW_STUB_SLUGS.items()))
def test_new_stub_present_on_its_agent(slug: str, agent_name: str) -> None:
    # The Gmail/Calendar happy-path agents now carry BOTH read and write stubs.
    settings = _flow_settings()
    agent = _build_flow_agent(agent_name, _AGENT_FACTORIES[agent_name], settings, {})
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert slug in tool_names, f"{agent_name} missing stub {slug}: {tool_names}"


def test_buildium_read_surface_slugs_are_mockable() -> None:
    # #410 — the GL-balance + resident-request read tools must be mockable.
    for slug in ("buildium_get_gl_account_balances", "buildium_list_resident_requests"):
        assert slug in _TOOL_MOCK_TARGETS, f"{slug} not mockable"
        target_path, _factory = _TOOL_MOCK_TARGETS[slug]
        assert target_path.startswith("helixis.agents.buildium_tools.")


@pytest.mark.parametrize(
    "case_id",
    # new agent_flow cases (#386/#410/#359) + the #410 continuation (090-097).
    # ⚠ The range ENDED at 098 until 2026-09-22, when that case
    # (098_list_appliances_flow) was deleted with the appliance surface it
    # exercised. `next(glob(...))` raises StopIteration on a missing id, so a
    # deleted case has to leave this list — the range is not decorative.
    ["049", "054", "055", "056", "057"] + [f"{n:03d}" for n in range(90, 98)],
)
def test_new_agent_flow_case_mock_slugs_all_resolve(case_id: str) -> None:
    """Every tool_mocks slug in a new agent_flow case must be a registered
    mock target — _build_sequenced_patches raises ValueError otherwise, so
    this catches a typo'd or unregistered slug deterministically (no LLM)."""
    path = next(_GOLDEN_DIR.glob(f"{case_id}_*.json"))
    case = json.loads(path.read_text())
    tool_mocks = case["input"].get("tool_mocks") or {}
    patches = _build_sequenced_patches(tool_mocks)
    assert len(patches) == len(tool_mocks)


def test_email_planner_case_enrichment_is_valid_and_email_shaped() -> None:
    """#366 — case 006's enrichment must validate as an EnrichedEvent AND be
    detected as an email event so SYSTEM_PROMPT_EMAIL runs."""
    from helixis.agent.enrichment.models import EnrichedEvent
    from helixis.agent.planner.action_planner import _is_email_event

    path = next(_GOLDEN_DIR.glob("006_*.json"))
    case = json.loads(path.read_text())
    enriched = EnrichedEvent.model_validate(case["input"]["enrichment"])
    assert _is_email_event(enriched)
