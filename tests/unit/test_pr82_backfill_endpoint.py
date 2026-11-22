"""PR #82 — backfill endpoint for historical ``action_surface = NULL`` rows.

Brandon's live audit on the ``team`` company surfaced 2
``Task.Created`` rows that landed before the PR #82 wiring and never
got an ``action_surface``. The endpoint re-runs ``route_event`` +
``build_action_surface_from_routing`` for any such row scoped to the
caller's company. One-shot — parked for removal after the cleanup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import admin as admin_routes

from ._pg_bind_contract import reject_unbindable


def _make_app() -> tuple[FastAPI, AuthenticatedUser]:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-pr82"],
    )

    def _stub() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _stub
    return app, user


def test_backfill_requires_auth() -> None:
    """Without a bearer the endpoint must NOT execute the backfill."""
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")
    client = TestClient(app)
    resp = client.post("/api/v1/admin/diag/backfill_action_surface", json={})
    assert resp.status_code in (401, 403)


def test_backfill_rejects_cross_company_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller belongs to ``company-pr82``. Passing
    ``company_id=other-company`` in the body → 403, never touches
    the pool."""
    app, _user = _make_app()
    # If the pool helper was ever called, the test would explode —
    # use a Mock that raises if reached.
    monkeypatch.setattr(
        admin_routes, "_get_pool_for_admin", lambda: pytest.fail("should not reach")
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "other-company"},
    )
    assert resp.status_code == 403


def test_backfill_patches_null_surface_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed 3 events for the company (2 with NULL surface, 1 with
    surface). POST → response should report ``patched=2 skipped=1``
    (skipped because the 1 already had a surface, so the row's
    filter excludes it from the SELECT)."""
    app, _user = _make_app()

    # Pool stub: ``_fetch_null_surface_rows`` calls ``pool.fetch``
    # with the SELECT. Return 2 NULL-surface rows. The third row is
    # filtered out by the WHERE clause so it never reaches the
    # response counter — that's the point.
    null_rows = [
        {
            "id": uuid.uuid4(),
            "event_name": "Task.Created",
            "payload": {
                "EventName": "Task.Created",
                "TaskId": 17434,
                "Title": "Loud Crashing",
            },
            "action_surface": None,
        },
        {
            "id": uuid.uuid4(),
            "event_name": "Task.Created",
            "payload": {
                "EventName": "Task.Created",
                "TaskId": 17435,
                "Title": "AC unit broken",
            },
            "action_surface": None,
        },
    ]
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=null_rows)
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)

    routing_decision_mock = MagicMock()
    routing_decision_mock.action_type = "assign_to_user"
    routing_decision_mock.assigned_buildium_user_id = 42
    routing_decision_mock.assigned_helixis_user_id = "admin-uuid"
    routing_decision_mock.surface = "inbox"
    routing_decision_mock.reason = "test"

    monkeypatch.setattr(
        admin_routes,
        "route_event",
        AsyncMock(return_value=routing_decision_mock),
    )
    monkeypatch.setattr(
        admin_routes,
        "load_staff_cache",
        AsyncMock(return_value={}),
    )
    # PR #83 — backfill now also runs the enrichment handler. This
    # PR #82 test exercises the route/floor/store flow only, so
    # patch the handler lookup to None (no-op enrichment, same as
    # an unregistered event type).
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: None)
    store_routing_mock = AsyncMock()
    monkeypatch.setattr(admin_routes, "store_routing_decision", store_routing_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr82"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["company_id"] == "company-pr82"
    assert body["patched"] == 2
    assert body["skipped"] == 0
    assert body["errors"] == 0
    # store_routing_decision was called once per row.
    assert store_routing_mock.await_count == 2
    # Each call carried a real surface dict.
    for call in store_routing_mock.await_args_list:
        assert call.kwargs["deterministic_surface"] is not None
        assert "summary" in call.kwargs["deterministic_surface"]


def test_backfill_handles_string_payload_via_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some asyncpg JSONB configurations return the payload as a
    JSON string (no codec registered). The endpoint must json-decode
    it before passing to ``route_event`` — pin that the helper
    tolerates both shapes."""
    app, _user = _make_app()
    import json as _json

    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid.uuid4(),
                "event_name": "Task.Created",
                "payload": _json.dumps({"EventName": "Task.Created", "TaskId": 1, "Title": "x"}),
                "action_surface": None,
            }
        ]
    )
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)
    rd = MagicMock()
    rd.action_type = "assign_to_user"
    rd.assigned_buildium_user_id = None
    rd.assigned_helixis_user_id = None
    rd.surface = "inbox"
    rd.reason = "stub"
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock(return_value=rd))
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: None)
    monkeypatch.setattr(admin_routes, "store_routing_decision", AsyncMock())

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr82"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["patched"] == 1
    assert body["errors"] == 0


def test_backfill_scopes_query_to_company_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SELECT MUST filter by ``company_id``. Other companies
    must stay untouched even when the caller's company is empty."""
    app, _user = _make_app()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock())
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: None)
    monkeypatch.setattr(admin_routes, "store_routing_decision", AsyncMock())

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr82"},
    )
    assert resp.status_code == 200
    sql = pool.fetch.await_args.args[0]
    assert "company_id = $1::uuid" in sql
    assert pool.fetch.await_args.args[1] == "company-pr82"


def test_backfill_passes_since_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    """``since`` narrows the SELECT to events created after the timestamp —
    used to avoid scanning ancient history.

    ⚠ **THIS TEST WAS RIGHT ABOUT THE PROPERTY AND PINNED TO THE DEFECT.** It
    asserted the bound argument was the literal ``"2026-05-16T00:00:00Z"`` —
    i.e. that a ``str`` reaches a ``$2::timestamptz`` parameter, which asyncpg
    refuses outright (``DataError``). The bare ``MagicMock`` pool accepted it,
    so the endpoint's one piece of coverage held the bug in place while reading
    as proof it worked (gotchas 89/93/95/103/110 — re-point, never delete; and
    gotcha 96's fake-pool trap, which is why ``reject_unbindable`` is wired in
    below). The property is "the window the caller asked for reaches the
    query", and that is what it asserts now.
    """
    app, _user = _make_app()
    pool = MagicMock()

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        reject_unbindable(sql, args)
        return []

    pool.fetch = AsyncMock(side_effect=_fetch)
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock())
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: None)
    monkeypatch.setattr(admin_routes, "store_routing_decision", AsyncMock())

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr82", "since": "2026-05-16T00:00:00Z"},
    )
    assert resp.status_code == 200
    sql = pool.fetch.await_args.args[0]
    assert "created_at >= $2::timestamptz" in sql
    assert pool.fetch.await_args.args[2] == datetime(2026, 5, 16, tzinfo=UTC)


def test_backfill_counts_errors_when_route_event_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose backfill explodes counts toward ``errors``, not
    ``patched``. The endpoint does NOT raise — the response carries
    the count so Brandon can see partial-success runs."""
    app, _user = _make_app()
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid.uuid4(),
                "event_name": "Task.Created",
                "payload": {"EventName": "Task.Created", "TaskId": 1, "Title": "x"},
                "action_surface": None,
            }
        ]
    )
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(admin_routes, "route_event", _boom)
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: None)
    monkeypatch.setattr(admin_routes, "store_routing_decision", AsyncMock())

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr82"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["patched"] == 0
    assert body["errors"] == 1
