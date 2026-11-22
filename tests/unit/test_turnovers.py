"""Turnover checklist (migration 0046) — repo semantics + property-page routes.

Pins: the one-row-per-lease upsert (dismissed reactivates, done stays done),
the step allowlist (unknown step never reaches SQL), the done-recompute from
the three step timestamps, and the route guards (bad step → 400 before the
repo, missing row → 404, completion writes exactly one activity entry).
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.db.repositories import turnovers as turnovers_repo
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
TURNOVER_ID = "00000000-0000-0000-0000-00000000beef"


# ── repository ────────────────────────────────────────────────────────


class _Conn:
    def __init__(
        self,
        fetch_rows: list | None = None,
        fetchrow_results: list[Any] | None = None,
    ) -> None:
        self.fetch = AsyncMock(return_value=fetch_rows or [])
        self.fetchrow = AsyncMock(side_effect=fetchrow_results or [None])
        self.execute = AsyncMock()


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": TURNOVER_ID,
        "lease_buildium_id": 5678,
        "property_id": 700,
        "unit_id": 55,
        "move_out_date": date(2026, 8, 31),
        "status": "active",
        "inspection_done_at": None,
        "work_orders_done_at": None,
        "listed_done_at": None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_open_turnover_upserts_and_reactivates_dismissed() -> None:
    conn = _Conn()
    await turnovers_repo.open_turnover(
        _pool(conn),
        company_id=COMPANY_ID,
        lease_buildium_id=5678,
        property_id=700,
        unit_id=55,
        move_out_date=date(2026, 8, 31),
    )
    sql = conn.execute.await_args.args[0]
    assert "ON CONFLICT (company_id, lease_buildium_id)" in sql
    # A new move-out reactivates a dismissed row; a done row stays done.
    assert "WHEN helixis_turnovers.status = 'dismissed'" in sql
    assert "THEN 'active' ELSE helixis_turnovers.status" in sql


@pytest.mark.asyncio
async def test_set_step_rejects_unknown_step_before_sql() -> None:
    conn = _Conn()
    with pytest.raises(ValueError):
        await turnovers_repo.set_step(
            _pool(conn),
            company_id=COMPANY_ID,
            turnover_id=TURNOVER_ID,
            step="demolish",
            done=True,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_step_returns_none_for_missing_or_dismissed_row() -> None:
    conn = _Conn(fetchrow_results=[None])
    out = await turnovers_repo.set_step(
        _pool(conn),
        company_id=COMPANY_ID,
        turnover_id=TURNOVER_ID,
        step="inspection",
        done=True,
    )
    assert out is None
    # The step update refuses dismissed rows; the recompute never ran.
    assert "status <> 'dismissed'" in conn.fetchrow.await_args.args[0]
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_set_step_recomputes_done_from_all_three_timestamps() -> None:
    now = datetime(2026, 8, 20, 12, 0, 0)
    conn = _Conn(
        fetchrow_results=[
            {"id": TURNOVER_ID},
            _record(
                status="done",
                inspection_done_at=now,
                work_orders_done_at=now,
                listed_done_at=now,
            ),
        ]
    )
    out = await turnovers_repo.set_step(
        _pool(conn),
        company_id=COMPANY_ID,
        turnover_id=TURNOVER_ID,
        step="listed",
        done=True,
    )
    assert out is not None and out.status == "done"
    step_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "listed_done_at" in step_sql
    recompute_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "THEN 'done' ELSE 'active'" in recompute_sql
    assert "COALESCE(completed_at, now())" in recompute_sql


# ── routes ────────────────────────────────────────────────────────────


def _make_token() -> str:
    return jwt.encode(
        {
            "sub": ME_USER_ID,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _turnover(**over: Any) -> turnovers_repo.Turnover:
    base: dict[str, Any] = {
        "id": TURNOVER_ID,
        "lease_buildium_id": 5678,
        "property_id": 700,
        "unit_id": 55,
        "unit_number": "3C",
        "move_out_date": date(2026, 8, 31),
        "status": "active",
        "inspection_done_at": None,
        "work_orders_done_at": None,
        "listed_done_at": None,
        "open_work_orders": 2,
    }
    base.update(over)
    return turnovers_repo.Turnover(**base)


def _route_env(settings: Settings):
    return (
        patch("helixis.api.routes.turnovers.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.turnovers.write_activity", AsyncMock()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def _request(app: Any, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    with TestClient(app) as c:
        return c.request(
            method,
            f"/api/v1/properties/700/turnovers{path}",
            json=body,
            headers={"Authorization": f"Bearer {_make_token()}"},
        )


def test_route_lists_turnovers_with_step_flags() -> None:
    settings = _settings()
    now = datetime(2026, 8, 20, 12, 0, 0)
    with ExitStack() as stack:
        for cm in _route_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.turnovers.turnovers_repo.list_turnovers",
                AsyncMock(return_value=[_turnover(inspection_done_at=now)]),
            )
        )
        app = create_app(settings)
        resp = _request(app, "GET", "")
    assert resp.status_code == 200
    (row,) = resp.json()["turnovers"]
    assert row["unit_number"] == "3C"
    assert row["inspection_done"] is True
    assert row["work_orders_done"] is False
    assert row["open_work_orders"] == 2


def test_route_rejects_unknown_step_before_repo() -> None:
    settings = _settings()
    with ExitStack() as stack:
        for cm in _route_env(settings):
            stack.enter_context(cm)
        set_step = stack.enter_context(
            patch("helixis.api.routes.turnovers.turnovers_repo.set_step", AsyncMock())
        )
        app = create_app(settings)
        resp = _request(app, "POST", f"/{TURNOVER_ID}/steps/demolish", {"done": True})
    assert resp.status_code == 400
    set_step.assert_not_awaited()


def test_route_step_completion_writes_one_activity_entry() -> None:
    settings = _settings()
    now = datetime(2026, 8, 20, 12, 0, 0)
    done = _turnover(
        status="done",
        inspection_done_at=now,
        work_orders_done_at=now,
        listed_done_at=now,
    )
    with ExitStack() as stack:
        for cm in _route_env(settings):
            stack.enter_context(cm)
        activity = stack.enter_context(
            patch("helixis.api.routes.turnovers.write_activity", AsyncMock())
        )
        stack.enter_context(
            patch(
                "helixis.api.routes.turnovers.turnovers_repo.set_step",
                AsyncMock(return_value=done),
            )
        )
        app = create_app(settings)
        resp = _request(app, "POST", f"/{TURNOVER_ID}/steps/listed", {"done": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    activity.assert_awaited_once()
    entry = activity.await_args.args[1]
    assert entry.action_type == "turnover_completed"
    assert "3C" in entry.description


def test_route_missing_turnover_is_404() -> None:
    settings = _settings()
    with ExitStack() as stack:
        for cm in _route_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.turnovers.turnovers_repo.dismiss",
                AsyncMock(return_value=None),
            )
        )
        app = create_app(settings)
        resp = _request(app, "POST", f"/{TURNOVER_ID}/dismiss")
    assert resp.status_code == 404
