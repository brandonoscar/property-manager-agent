"""PR #89 — POST /api/v1/buildium/events/{event_id}/replan tests.

The replan endpoint resets ``planner_status='pending'`` (clearing
``planner_error`` + ``planner_attempts``) so the worker re-runs the
planner on the next claim pass. User-triggered from the inbox
"Replan" pill when the previous run timed out or failed.

Covers:
  - Happy path: returns 200 with planner_status='pending', UPDATE
    fires with the right WHERE clause.
  - 404 on missing row.
  - 404 on cross-tenant access (don't disambiguate from missing).
  - 400 on unsupported source string.
  - 503 when the planner_status column is missing (migration not
    yet applied).
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
        company_ids=["company-pr89"],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return app, user


def test_replan_happy_path_returns_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful reset → 200 with planner_status='pending' and the
    affirmative message."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    # Stub the queue helper directly — the SQL contract is pinned
    # separately in test_pr89_planner_worker.py.
    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_planner_for_replan",
        reset_mock,
    )
    # connectors_limiter.check_by_user uses the pool indirectly via
    # the rate-limit backend; patch the limiter to no-op so we don't
    # touch a real DB.
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/buildium/events/{event_id}/replan",
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["event_id"] == event_id
    assert body["planner_status"] == "pending"
    assert "queued" in body["message"].lower()

    reset_mock.assert_awaited_once()
    kwargs = reset_mock.await_args.kwargs
    assert kwargs["company_id"] == "company-pr89"
    assert kwargs["table_name"] == "buildium_events"


def test_replan_404_when_row_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset_planner_for_replan`` returns False (row not found OR
    wrong tenant) → 404. We do NOT distinguish the two cases — the
    user can't tell whether the event exists or just isn't theirs."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_planner_for_replan",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{event_id}/replan", json={})
    assert resp.status_code == 404


def test_replan_400_on_malformed_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path-param event_id that isn't a UUID must surface as a 400
    with a clear message, NOT a 500 from ``UUID(...)`` blowing up in
    the queue helper.

    PR #89 CI hotfix: the endpoint validates the UUID at the boundary
    before passing it to ``reset_planner_for_replan`` (whose signature
    is strict-typed ``UUID``). Without this guard the call site
    coerces ``str → UUID`` inline and a malformed input bubbles to
    FastAPI's broad 500 handler."""
    app, _user = _make_app()

    # Reset helper must NOT be reached — the malformed UUID check
    # runs before the queue call.
    reset_mock = AsyncMock(side_effect=AssertionError("should not reach"))
    monkeypatch.setattr("helixis.webhooks.queue.reset_planner_for_replan", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post("/api/v1/buildium/events/not-a-uuid/replan", json={})
    assert resp.status_code == 400
    assert "UUID" in resp.json()["detail"]
    reset_mock.assert_not_awaited()


def test_replan_400_on_unsupported_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """``source`` must be in ``_PLANNER_SOURCE_TABLES``. Unknown
    sources → 400 rather than 404 because the error is the user's
    request, not the row state."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    # If the endpoint reached the reset helper, this would raise.
    reset_mock = AsyncMock(side_effect=AssertionError("should not reach"))
    monkeypatch.setattr("helixis.webhooks.queue.reset_planner_for_replan", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/buildium/events/{event_id}/replan",
        json={"source": "leadsimple"},  # not registered yet
    )
    assert resp.status_code == 400
    assert "leadsimple" in resp.json()["detail"]
    reset_mock.assert_not_awaited()


def test_replan_503_when_column_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-migration deploy: planner_status column doesn't exist yet
    → 503 with a hint pointing at the migration file. Better UX than
    a generic 500."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    # Simulate the column-missing branch by raising
    # UndefinedColumnError from the reset helper.
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_planner_for_replan",
        AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedColumnError("planner_status does not exist")
        ),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{event_id}/replan", json={})
    assert resp.status_code == 503
    assert "0017_buildium_events_planner_status.sql" in resp.json()["detail"]


def test_replan_default_source_is_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty body → source defaults to 'buildium' → looks up the
    buildium_events table. Existing frontend posts no body, so this
    is the path the inbox actually hits."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("helixis.webhooks.queue.reset_planner_for_replan", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    # No body at all — body=None branch in the endpoint.
    resp = client.post(f"/api/v1/buildium/events/{event_id}/replan")
    assert resp.status_code == 200
    assert reset_mock.await_args.kwargs["table_name"] == "buildium_events"


def test_replan_explicit_source_buildium_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit source='buildium' is the same as no body."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("helixis.webhooks.queue.reset_planner_for_replan", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{event_id}/replan", json={"source": "buildium"})
    assert resp.status_code == 200


def test_replan_403_when_no_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user with no company memberships gets 403 from
    ``_require_company`` before reaching the row. Mirrors the same
    pattern as every other endpoint in this router."""
    app = FastAPI()
    app.include_router(buildium_routes.router, prefix="/api/v1")
    no_company_user = AuthenticatedUser(
        user_id="user-orphan",
        email="orphan@example.com",
        company_ids=[],
    )
    app.dependency_overrides[get_current_user] = lambda: no_company_user

    # Reset helper must not be reached.
    monkeypatch.setattr(
        "helixis.webhooks.queue.reset_planner_for_replan",
        AsyncMock(side_effect=AssertionError("should not reach")),
    )
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(f"/api/v1/buildium/events/{uuid.uuid4()}/replan", json={})
    assert resp.status_code == 403


def test_replan_handler_passes_table_name_to_queue_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward-compat smoke: when the registry grows (e.g.
    'leadsimple' → 'leadsimple_events'), the endpoint must thread the
    looked-up table name into the queue helper, not hardcode
    'buildium_events'."""
    app, _user = _make_app()
    event_id = str(uuid.uuid4())

    # Pretend a leadsimple source has been registered for this test
    # so we exercise the table-name lookup path with a non-default
    # value.
    monkeypatch.setitem(
        buildium_routes._PLANNER_SOURCE_TABLES,
        "leadsimple",
        "leadsimple_events",
    )
    # Allow the SQL-injection allow-list in queue.py to accept the
    # synthetic table name too. We patch the frozenset.
    from helixis.webhooks import queue as queue_module

    monkeypatch.setattr(
        queue_module,
        "_PLANNER_QUEUE_TABLES",
        frozenset({"buildium_events", "leadsimple_events"}),
    )

    reset_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("helixis.webhooks.queue.reset_planner_for_replan", reset_mock)
    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/buildium/events/{event_id}/replan",
        json={"source": "leadsimple"},
    )
    assert resp.status_code == 200
    assert reset_mock.await_args.kwargs["table_name"] == "leadsimple_events"
