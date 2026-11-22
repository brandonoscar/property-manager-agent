"""Operations page routes — auth, company scoping, dataclass → wire mapping.

Same convention as test_cockpit_routes.py: the SQL lives in
``db/repositories/operations.py`` and runs against the real schema when the
mirror is populated; here the repo functions are mocked and the tests pin
the route contract. The repo's row→dataclass mapping is pinned separately
with a fake pool returning record-shaped dicts.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import operations as ops_routes
from helixis.db.repositories import operations as ops_repo
from helixis.db.repositories.operations import TeamOpsRow, TenantOpsRow, VendorOpsRow


def _make_app(*, authed: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(ops_routes.router, prefix="/api/v1")
    if authed:
        user = AuthenticatedUser(
            user_id="user-1", email="brandon@example.com", company_ids=["company-1"]
        )
        app.dependency_overrides[get_current_user] = lambda: user
    return app


# ── auth ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/vendors", "/tenants", "/team"])
def test_endpoints_require_auth(path: str) -> None:
    resp = TestClient(_make_app(authed=False)).get(f"/api/v1/operations{path}")
    assert resp.status_code in (401, 403)


@pytest.mark.parametrize("path", ["/vendors", "/tenants", "/team"])
def test_endpoints_403_without_company(path: str) -> None:
    app = FastAPI()
    app.include_router(ops_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(user_id="user-1", email="a@b.c", company_ids=[])
    app.dependency_overrides[get_current_user] = lambda: user
    resp = TestClient(app).get(f"/api/v1/operations{path}")
    assert resp.status_code == 403


# ── route mapping ─────────────────────────────────────────────────────


def test_vendors_route_maps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    row = VendorOpsRow(
        buildium_id=41,
        name="Rapid Plumbing Co",
        category_name="Plumbing",
        is_active=True,
        phone="555-0101",
        email="dispatch@rapidplumbing.example",
        open_work_orders=2,
        total_work_orders=19,
        jobs_90d=6,
        avg_days_to_complete=2.4,
        spend_12m=18240.0,
        bill_count_12m=17,
        last_bill_date=date(2026, 8, 2),
    )
    fetch = AsyncMock(return_value=[row])
    monkeypatch.setattr(ops_routes.ops_repo, "list_vendor_ops", fetch)
    monkeypatch.setattr(ops_routes, "get_pool", lambda: object())

    resp = TestClient(_make_app()).get("/api/v1/operations/vendors")
    assert resp.status_code == 200
    vendors = resp.json()["vendors"]
    assert vendors[0]["name"] == "Rapid Plumbing Co"
    assert vendors[0]["avg_days_to_complete"] == 2.4
    assert vendors[0]["spend_12m"] == 18240.0
    assert vendors[0]["last_bill_date"] == "2026-08-02"
    # Scoped to the caller's company, always.
    assert fetch.await_args.args[1] == "company-1"


def test_tenants_route_maps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    row = TenantOpsRow(
        tenant_buildium_id=9001,
        first_name="Maria",
        last_name="Alvarez",
        email="maria@example.com",
        phone="555-0102",
        property_buildium_id=700,
        property_name="Maple Court",
        unit_number="4B",
        lease_buildium_id=555,
        lease_to_date=date(2026, 11, 30),
        rent=1850.0,
        balance=850.0,
        oldest_bucket="31-60",
    )
    fetch = AsyncMock(return_value=[row])
    monkeypatch.setattr(ops_routes.ops_repo, "list_tenant_ops", fetch)
    monkeypatch.setattr(ops_routes, "get_pool", lambda: object())

    resp = TestClient(_make_app()).get("/api/v1/operations/tenants")
    assert resp.status_code == 200
    t = resp.json()["tenants"][0]
    assert t["balance"] == 850.0
    assert t["oldest_bucket"] == "31-60"
    assert t["lease_to_date"] == "2026-11-30"
    assert fetch.await_args.args[1] == "company-1"


def test_team_route_maps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    row = TeamOpsRow(
        buildium_user_id=77,
        first_name="Dana",
        last_name="Whitfield",
        email="dana@example.com",
        user_role_name="Property Manager",
        is_active=True,
        open_work_orders=4,
        open_tasks=7,
        completed_tasks_7d=12,
    )
    fetch = AsyncMock(return_value=[row])
    monkeypatch.setattr(ops_routes.ops_repo, "list_team_ops", fetch)
    monkeypatch.setattr(ops_routes, "get_pool", lambda: object())

    resp = TestClient(_make_app()).get("/api/v1/operations/team")
    assert resp.status_code == 200
    m = resp.json()["members"][0]
    assert m["open_work_orders"] == 4
    assert m["completed_tasks_7d"] == 12
    assert fetch.await_args.args[1] == "company-1"


# ── repo row mapping (fake pool → dataclasses) ────────────────────────


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.seen_sql: str | None = None
        self.seen_args: tuple | None = None

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.seen_sql = " ".join(sql.split())
        self.seen_args = args
        return self._rows

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _FakeConn(rows)

    def acquire(self) -> _FakeConn:
        return self.conn


@pytest.mark.asyncio
async def test_vendor_repo_maps_numerics_and_scopes() -> None:
    from decimal import Decimal

    pool = _FakePool(
        [
            {
                "buildium_id": 41,
                "name": "Rapid Plumbing Co",
                "category_name": "Plumbing",
                "is_active": True,
                "phone": None,
                "email": None,
                "open_work_orders": 2,
                "total_work_orders": 19,
                "jobs_90d": 6,
                # asyncpg yields Decimal for numeric/AVG columns — the repo
                # must coerce, or JSON serialization downstream chokes.
                "avg_days_to_complete": Decimal("2.4"),
                "spend_12m": Decimal("18240.00"),
                "bill_count_12m": 17,
                "last_bill_date": date(2026, 8, 2),
            }
        ]
    )
    rows = await ops_repo.list_vendor_ops(pool, "company-1")  # type: ignore[arg-type]
    assert rows[0].avg_days_to_complete == pytest.approx(2.4)
    assert isinstance(rows[0].spend_12m, float)
    # Company scoping is in the SQL itself, parameterized.
    assert pool.conn.seen_args == ("company-1",)
    assert "company_id = $1::uuid" in (pool.conn.seen_sql or "")
    # The vendor link is the raw-jsonb extraction (no typed column exists).
    assert "raw->>'VendorId'" in (pool.conn.seen_sql or "")


@pytest.mark.asyncio
async def test_tenant_repo_maps_bucket_and_balance() -> None:
    from decimal import Decimal

    pool = _FakePool(
        [
            {
                "tenant_buildium_id": 9001,
                "first_name": "Maria",
                "last_name": "Alvarez",
                "email": None,
                "phone": None,
                "property_buildium_id": 700,
                "property_name": "Maple Court",
                "unit_number": "4B",
                "lease_buildium_id": 555,
                "lease_to_date": date(2026, 11, 30),
                "rent": Decimal("1850.00"),
                "balance": Decimal("850.00"),
                "oldest_bucket": "31-60",
            }
        ]
    )
    rows = await ops_repo.list_tenant_ops(pool, "company-1")  # type: ignore[arg-type]
    assert rows[0].balance == 850.0
    assert rows[0].rent == 1850.0
    assert rows[0].oldest_bucket == "31-60"
    # Only ACTIVE leases feed the tab.
    assert "l.status = 'Active'" in (pool.conn.seen_sql or "")


@pytest.mark.asyncio
async def test_team_repo_filters_to_staff() -> None:
    pool = _FakePool(
        [
            {
                "buildium_user_id": 77,
                "first_name": "Dana",
                "last_name": "Whitfield",
                "email": None,
                "user_role_name": "Property Manager",
                "is_active": True,
                "open_work_orders": 4,
                "open_tasks": 7,
                "completed_tasks_7d": 12,
            }
        ]
    )
    rows = await ops_repo.list_team_ops(pool, "company-1")  # type: ignore[arg-type]
    assert rows[0].open_tasks == 7
    sql = pool.conn.seen_sql or ""
    assert "'Staff' = ANY(s.user_types)" in sql
    # Open-task vocabulary matches cockpit.py exactly.
    assert "NOT IN ('Completed', 'Closed', 'Cancelled')" in sql
