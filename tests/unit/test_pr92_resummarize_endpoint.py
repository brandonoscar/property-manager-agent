"""PR #92 — POST /api/v1/buildium/events/{event_id}/resummarize.

Mirrors test_pr89_replan_endpoint.py since the endpoint shape is
symmetric. Covers:

  - happy path: 200 + summary_status='pending' in the response,
    queue helper called with the right kwargs.
  - 404 on missing row / cross-tenant access.
  - 400 on malformed UUID.
  - 400 on unsupported source.
  - 503 on UndefinedColumnError (migration not applied).
  - 403 on no-company-membership.
  - default body (source=buildium) works.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import buildium as buildium_routes


def _make_app() -> tuple[FastAPI, AuthenticatedUser]:
    app = FastAPI()
    app.include_router(buildium_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-pr92"],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return app, user


def test_resummarize_happy_path_returns_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_summary_for_resummarize",
        reset_mock,
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{event_id}/resummarize", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["event_id"] == event_id
    assert body["summary_status"] == "pending"
    assert "queued" in body["message"].lower()

    reset_mock.assert_awaited_once()
    kwargs = reset_mock.await_args.kwargs
    assert kwargs["company_id"] == "company-pr92"
    assert kwargs["table_name"] == "buildium_events"


def test_resummarize_404_when_row_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _user = _make_app()
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_summary_for_resummarize",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{uuid.uuid4()}/resummarize", json={})
    assert resp.status_code == 404


def test_resummarize_400_on_malformed_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same UUID-at-the-boundary discipline as ``/replan`` (PR #89
    hotfix). A non-UUID path-param must surface as 400 BEFORE the
    queue helper is reached."""
    app, _user = _make_app()
    reset_mock = AsyncMock(side_effect=AssertionError("should not reach"))
    monkeypatch.setattr("helixis.webhooks.queue.reset_summary_for_resummarize", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post("/api/v1/buildium/events/not-a-uuid/resummarize", json={})
    assert resp.status_code == 400
    assert "UUID" in resp.json()["detail"]
    reset_mock.assert_not_awaited()


def test_resummarize_400_on_unsupported_source(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _user = _make_app()
    reset_mock = AsyncMock(side_effect=AssertionError("should not reach"))
    monkeypatch.setattr("helixis.webhooks.queue.reset_summary_for_resummarize", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/buildium/events/{uuid.uuid4()}/resummarize",
        json={"source": "leadsimple"},
    )
    assert resp.status_code == 400
    assert "leadsimple" in resp.json()["detail"]


def test_resummarize_503_when_column_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-migration deploy: summary_status column doesn't exist yet
    → 503 with a hint pointing at migration 0018."""
    app, _user = _make_app()
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_summary_for_resummarize",
        AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedColumnError("summary_status does not exist")
        ),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{uuid.uuid4()}/resummarize", json={})
    assert resp.status_code == 503
    assert "0018_buildium_events_summary.sql" in resp.json()["detail"]


def test_resummarize_403_when_no_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """No company memberships → 403 before reaching the queue helper."""
    app = FastAPI()
    app.include_router(buildium_routes.router, prefix="/api/v1")
    orphan = AuthenticatedUser(
        user_id="user-orphan",
        email="orphan@example.com",
        company_ids=[],
    )
    app.dependency_overrides[get_current_user] = lambda: orphan

    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_summary_for_resummarize",
        AsyncMock(side_effect=AssertionError("should not reach")),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{uuid.uuid4()}/resummarize", json={})
    assert resp.status_code == 403


def test_resummarize_default_source_is_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty body → source defaults to 'buildium' → table=buildium_events."""
    app, _user = _make_app()
    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("helixis.webhooks.queue.reset_summary_for_resummarize", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{uuid.uuid4()}/resummarize")
    assert resp.status_code == 200
    assert reset_mock.await_args.kwargs["table_name"] == "buildium_events"
