"""First operations scan (roadmap §7 rank 1) — repository math + route.

Pins: every report number is deterministic mirror SQL (one round trip),
``synced`` flips only when entity rows exist, and the route is company-
scoped with a 503 (not a crash) when the pool is down.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.db.repositories.first_scan import (
    EXPIRING_WINDOW_DAYS,
    STALLED_AFTER_DAYS,
    run_first_scan,
)
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
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


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.fixture(autouse=True)
def _a_buildium_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests describe a Buildium company, which fills lease balances.

    The scan now asks which PMS the company is on before stating delinquency;
    the fake connection here answers every query with the scan row, so it
    cannot answer that one. Tests that need another PMS override this.
    """
    from datetime import UTC, datetime

    from helixis.db.repositories import first_scan
    from helixis.db.repositories.mirror_sync_state import StageSync

    monkeypatch.setattr(first_scan, "company_pms_strict", AsyncMock(return_value="buildium"))
    # And whose balances stage has landed (2026-10-01: the scan reads the
    # stamp before stating delinquency; the fake connection cannot answer it).
    stamped = StageSync(synced_at=datetime.now(UTC), row_count=2)
    monkeypatch.setattr(first_scan, "get_stage_sync", AsyncMock(return_value=stamped))


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "properties": 0,
        "units": 0,
        "tenants": 0,
        "active_leases": 0,
        "open_work_orders": 0,
        "stalled_work_orders": 0,
        "expiring_leases": 0,
        "delinquent_leases": 0,
        "delinquent_total": 0,
        "pending_promises": 0,
    }
    base.update(overrides)
    return base


# ── repository ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_maps_row_and_flags_synced() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_row(
            properties=12,
            units=483,
            tenants=390,
            active_leases=402,
            open_work_orders=71,
            stalled_work_orders=12,
            expiring_leases=3,
            delinquent_leases=9,
            delinquent_total=18340.50,
            pending_promises=2,
        )
    )

    scan = await run_first_scan(_pool(conn), COMPANY_ID)

    assert scan.synced is True
    assert (scan.open_work_orders, scan.stalled_work_orders) == (71, 12)
    assert (scan.expiring_leases, scan.expiring_window_days) == (3, EXPIRING_WINDOW_DAYS)
    assert (scan.delinquent_leases, scan.delinquent_total) == (9, 18340.50)
    assert scan.pending_promises == 2
    # one round trip, company-scoped, with the window constants bound
    args = conn.fetchrow.await_args.args
    assert args[1:] == (COMPANY_ID, EXPIRING_WINDOW_DAYS, STALLED_AFTER_DAYS)


@pytest.mark.asyncio
async def test_scan_empty_mirror_reports_not_synced() -> None:
    """Right after the wizard's connect step the backfill may still be
    running — the scan must say 'not synced yet', never a wall of zeros
    presented as truth."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_row())

    scan = await run_first_scan(_pool(conn), COMPANY_ID)

    assert scan.synced is False
    assert scan.open_work_orders == 0


# ── route ────────────────────────────────────────────────────────────


def _env(settings: Settings, conn: Any):
    return (
        patch("helixis.api.routes.reports.get_pool", return_value=_pool(conn)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def _get(app: Any) -> Any:
    with TestClient(app) as c:
        return c.get("/api/v1/reports/first-scan", headers={"Authorization": f"Bearer {_token()}"})


def test_route_returns_scan_payload() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_row(properties=4, open_work_orders=7, units=30))
    with ExitStack() as stack:
        for cm in _env(settings, conn):
            stack.enter_context(cm)
        app = create_app(settings)
        resp = _get(app)

    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] is True
    assert body["open_work_orders"] == 7
    assert body["expiring_window_days"] == EXPIRING_WINDOW_DAYS
    assert "generated_at" in body


def test_route_503_when_pool_unavailable() -> None:
    settings = _settings()
    with ExitStack() as stack:
        stack.enter_context(
            patch("helixis.api.routes.reports.get_pool", side_effect=RuntimeError("no pool"))
        )
        stack.enter_context(
            patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID])
        )
        stack.enter_context(patch("helixis.api.auth.get_settings", return_value=settings))
        stack.enter_context(patch("helixis.main.get_settings", return_value=settings))
        app = create_app(settings)
        resp = _get(app)

    assert resp.status_code == 503


def test_route_403_without_company() -> None:
    settings = _settings()
    with ExitStack() as stack:
        stack.enter_context(
            patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [])
        )
        stack.enter_context(patch("helixis.api.auth.get_settings", return_value=settings))
        stack.enter_context(patch("helixis.main.get_settings", return_value=settings))
        app = create_app(settings)
        resp = _get(app)

    assert resp.status_code == 403


# ── a PMS that does not fill lease balances ──────────────────────────


class _OneRow:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def fetchrow(self, *_a: Any) -> dict[str, Any]:
        return self.row


async def test_a_rentvine_scan_states_no_delinquency_rather_than_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-pointed 2026-10-01: Rentvine lease balances sync now (from the lease
    export), so a Rentvine scan whose balances stage landed states what is
    owed, and one whose stage never landed says nothing (next test)."""
    from helixis.db.repositories import first_scan

    monkeypatch.setattr(first_scan, "company_pms_strict", AsyncMock(return_value="rentvine"))
    row = _row(properties=12, active_leases=8, delinquent_leases=2, delinquent_total=900)
    scan = await run_first_scan(_pool(_OneRow(row)), COMPANY_ID)
    assert scan.delinquent_leases == 2
    assert scan.delinquent_total == 900
    assert scan.pms == "rentvine"
    # Everything Rentvine does fill is still reported.
    assert scan.properties == 12 and scan.active_leases == 8 and scan.synced


@pytest.mark.parametrize("pms", ["buildium", "rentvine"])
async def test_a_balances_stage_that_never_landed_states_no_delinquency(
    monkeypatch: pytest.MonkeyPatch, pms: str
) -> None:
    """An empty balances table reads "nobody owes anything" whether or not the
    snapshot was ever written; without the stage's stamp, say nothing."""
    from helixis.db.repositories import first_scan

    monkeypatch.setattr(first_scan, "company_pms_strict", AsyncMock(return_value=pms))
    monkeypatch.setattr(first_scan, "get_stage_sync", AsyncMock(return_value=None))
    scan = await run_first_scan(_pool(_OneRow(_row(properties=3, active_leases=8))), COMPANY_ID)
    assert scan.delinquent_leases is None and scan.delinquent_total is None
    assert scan.properties == 3
    first_scan.get_stage_sync.assert_awaited_with(ANY, COMPANY_ID, "balances")


async def test_a_failed_pms_lookup_declines_delinquency_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from helixis.db.repositories import first_scan

    monkeypatch.setattr(
        first_scan, "company_pms_strict", AsyncMock(side_effect=ConnectionError("down"))
    )
    row = _row(properties=3, delinquent_leases=2, delinquent_total=900)
    scan = await run_first_scan(_pool(_OneRow(row)), COMPANY_ID)
    assert scan.delinquent_leases is None and scan.delinquent_total is None
    assert scan.pms is None
    assert scan.properties == 3


async def test_a_buildium_scan_still_states_what_is_owed() -> None:
    """The other direction: declining for everyone would hide real arrears."""
    row = _row(properties=3, delinquent_leases=2, delinquent_total=900)
    scan = await run_first_scan(_pool(_OneRow(row)), COMPANY_ID)
    assert scan.delinquent_leases == 2 and scan.delinquent_total == 900.0
    assert scan.pms == "buildium"
