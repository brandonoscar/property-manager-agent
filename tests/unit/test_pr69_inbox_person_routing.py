"""#69 — per-user inbox filter (person-routing last mile).

``GET /buildium/recent-events?assignee=me`` narrows the feed to the rows the
webhook router assigned to the CALLER, so a shared 10-person account gives
each teammate a "My work" view instead of one undifferentiated queue. The
caller's Buildium staff id is resolved by email against the ``buildium_staff``
cache, and the filter runs on the indexed
``routing_decision->>'assigned_buildium_user_id'`` predicate (migration 0016).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import buildium as buildium_routes


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(buildium_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="user-1",
        email="robert@redbrick.com",
        company_ids=["company-1"],
    )
    return app


def _client(monkeypatch: pytest.MonkeyPatch, pool: MagicMock) -> TestClient:
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: pool)
    return TestClient(_make_app())


def _events_query_call(pool: MagicMock) -> tuple:
    """The recent-events handler fires TWO pool.fetch calls — the main
    ``buildium_events`` query AND the staff-name-map decoration. Return the
    args of the one against ``buildium_events``."""
    for call in pool.fetch.await_args_list:
        if "FROM buildium_events" in call.args[0]:
            return call.args
    raise AssertionError("no buildium_events query was issued")


def test_assignee_me_filters_on_routed_buildium_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller resolves to buildium_user_id 77 → the query gains the indexed
    routing_decision filter with '77' as a bound param."""
    pool = MagicMock()
    # _resolve_caller_buildium_user_id → the caller's staff row.
    pool.fetchrow = AsyncMock(return_value={"buildium_user_id": 77})
    pool.fetch = AsyncMock(return_value=[])

    client = _client(monkeypatch, pool)
    resp = client.get(
        "/api/v1/buildium/recent-events"
        "?include=action_surface,enrichment,executed_action_index&assignee=me&limit=20"
    )
    assert resp.status_code == 200

    query, *params = _events_query_call(pool)
    assert "routing_decision->>'assigned_buildium_user_id'" in query
    # The resolved id is bound as text (routing_decision->> yields text).
    assert "77" in params


def test_assignee_me_returns_empty_when_caller_has_no_buildium_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email doesn't match any active staff row → we cannot attribute any work
    to the caller, so return [] rather than leaking the whole team's queue
    under a 'My work' label. The main list query never runs."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)  # no staff match
    pool.fetch = AsyncMock(return_value=[{"should": "not be returned"}])

    client = _client(monkeypatch, pool)
    resp = client.get("/api/v1/buildium/recent-events?include=action_surface&assignee=me&limit=20")
    assert resp.status_code == 200
    assert resp.json() == []
    pool.fetch.assert_not_awaited()


def test_no_assignee_returns_whole_company_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``assignee=me`` the query carries no per-user filter — the
    shared 'All' view (default)."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"buildium_user_id": 77})
    pool.fetch = AsyncMock(return_value=[])

    client = _client(monkeypatch, pool)
    resp = client.get("/api/v1/buildium/recent-events?include=action_surface,enrichment&limit=20")
    assert resp.status_code == 200

    query, *_params = _events_query_call(pool)
    assert "routing_decision->>'assigned_buildium_user_id'" not in query
    # Identity resolution is skipped entirely when the caller didn't ask for it.
    pool.fetchrow.assert_not_awaited()
