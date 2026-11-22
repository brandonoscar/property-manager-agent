"""The /agent/run activation event (audit Q3).

A reliable, server-side "agent run started" PostHog event — with the real
company_id + user_id — is the activation funnel's value step. Frontend
"task submitted" can be lost to an ad-blocker or a page-close; this one can't.
This calls the route handler directly with mocked collaborators (no SSE, no real
agent) and asserts the event fires with the right shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.api.auth import AuthenticatedUser
from helixis.api.routes import agent as agent_route
from helixis.common.types import AgentRequest


async def _empty_sse():  # noqa: ANN202 — trivial async generator stand-in
    if False:
        yield b""  # pragma: no cover


def _request() -> MagicMock:
    req = MagicMock()
    req.app.state.settings = MagicMock()
    req.app.state.event_bus = MagicMock()
    req.app.state.composio_client = MagicMock()
    return req


@pytest.mark.asyncio
async def test_run_emits_server_side_activation_event() -> None:
    user = AuthenticatedUser(user_id="user-123", email="pm@x.com", company_ids=["co-abc"])
    body = AgentRequest(task="how many work orders are open?", company_id="co-abc")

    with (
        patch.object(agent_route, "require_company_access"),
        patch.object(agent_route.agent_run_limiter, "check_by_user", new=AsyncMock()),
        patch.object(agent_route.spend, "budget_exceeded", new=AsyncMock(return_value=False)),
        patch.object(agent_route, "run_agent_streaming", new=AsyncMock()),
        patch.object(agent_route, "sse_event_generator", return_value=_empty_sse()),
        patch.object(agent_route, "ph_capture") as ph,
    ):
        await agent_route.run_agent(_request(), body, user)

    ph.assert_called_once()
    kwargs = ph.call_args.kwargs
    assert kwargs["event"] == "agent run started"
    assert kwargs["distinct_id"] == "user-123"
    # The REAL company group (not a placeholder) — powers per-company activation.
    assert kwargs["company_id"] == "co-abc"
    assert kwargs["properties"]["entry_point"] == "chat"  # default source


@pytest.mark.asyncio
async def test_run_activation_falls_back_to_membership_company() -> None:
    # If the body omits company_id, fall back to the user's first membership so
    # the event is still company-grouped (never an empty group silently).
    user = AuthenticatedUser(user_id="u", email=None, company_ids=["member-co"])
    body = AgentRequest(task="hi", company_id="")

    with (
        patch.object(agent_route, "require_company_access"),
        patch.object(agent_route.agent_run_limiter, "check_by_user", new=AsyncMock()),
        patch.object(agent_route.spend, "budget_exceeded", new=AsyncMock(return_value=False)),
        patch.object(agent_route, "run_agent_streaming", new=AsyncMock()),
        patch.object(agent_route, "sse_event_generator", return_value=_empty_sse()),
        patch.object(agent_route, "ph_capture") as ph,
    ):
        await agent_route.run_agent(_request(), body, user)

    assert ph.call_args.kwargs["company_id"] == "member-co"
