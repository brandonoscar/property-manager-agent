"""Reconciliation engine (roadmap §7 rank 4, Phase 1) — rules + ledger + routes.

Pins the trust contract: flags carry the exact numbers they were computed
from (evidence), the conservative-gap math never overstates the leak,
upserts dedupe on (company, rule, fingerprint), healed conditions resolve,
and the PM-facing routes are company-scoped with engine-owned statuses
protected.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.reconciliation import scan_company
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _a_buildium_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests describe a Buildium company, which fills every mirror table.

    The detector cycle resolves the company's PMS strictly before running anything;
    the fake pools here were written before that lookup existed and cannot
    answer it. Stating the PMS is what keeps them testing what they were
    written to test.
    """
    from helixis.detectors import runner

    monkeypatch.setattr(runner, "company_pms_strict", AsyncMock(return_value="buildium"))


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _rent_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "lease_id": 4001,
        "property_id": 77,
        "unit_id": 12,
        "unit_number": "4B",
        "rent": 1000.0,
        "charged_m0": 950.0,
        "charged_m1": 950.0,
        "months_elapsed": 7,
    }
    base.update(overrides)
    return base


def _bill_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bill_a": 9001,
        "bill_b": 9002,
        "vendor_id": 55,
        "vendor_name": "Rodriguez Plumbing",
        "amount": 480.0,
        "date_a": date(2026, 6, 20),
        "date_b": date(2026, 6, 27),
        "paid_a": True,
        "paid_b": True,
    }
    base.update(overrides)
    return base


def _fetch_by_rule(*positional: list) -> Any:
    """A fetch stub that survives a new detector joining the cycle.

    These tests pin the two RECONCILIATION rules, but ``scan_company`` runs
    every enabled state-rule detector, and a positional ``side_effect`` list
    silently hands rule N's rows to rule N-1 the moment the enabled set
    changes. ``rent_not_collected`` going live (2026-08-21) did exactly that.
    Dispatch on the query instead: anything that isn't one of the two rules
    under test answers empty.
    """
    remaining = list(positional)

    async def _fetch(sql: str, *_args: Any) -> list:
        # ``helixis_activity_log`` appears in exactly one enabled detector's
        # query — rent_not_collected's neglect gate. A looser marker (the
        # month truncation) matches rent_under_charged too and starves the
        # rule under test.
        if "helixis_activity_log" in sql:  # rent_not_collected
            return []
        return remaining.pop(0) if remaining else []

    return _fetch


def _upserts(conn: Any) -> list[tuple]:
    """Args of the finding UPSERT calls, in order.

    Positional indexing into ``execute.await_args_list`` broke the moment a
    third detector joined the cycle and issued its own resolve-stale first.
    Select by what the statement IS, not where it landed.
    """
    return [c.args for c in conn.execute.await_args_list if "ON CONFLICT" in c.args[0]]


def _resolve_for(conn: Any, rule_key: str) -> tuple:
    """Args of the resolve-stale sweep for one rule."""
    return next(
        c.args
        for c in conn.execute.await_args_list
        if "SET status = 'resolved'" in c.args[0] and c.args[2] == rule_key
    )


# ── engine ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rent_rule_flags_the_hidden_gap_with_evidence() -> None:
    """The founding example: lease says $1,000, charges say $950, month 7."""
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch_by_rule([_rent_row()], []))
    conn.execute = AsyncMock()

    counts = await scan_company(_pool(conn), COMPANY_ID)

    assert counts["rent_under_charged"] == 1
    assert counts["duplicate_vendor_bill"] == 0
    # first execute = the upsert for the rent flag
    upsert = _upserts(conn)[0]
    assert "ON CONFLICT (company_id, rule_key, fingerprint)" in upsert[0]
    assert upsert[1] == COMPANY_ID
    assert upsert[2] == "rent_under_charged"
    assert upsert[3] == "lease:4001"
    assert upsert[4] == "warning"
    assert "Rent charged below the lease on unit 4B" in upsert[5]
    assert "$50/mo short" in upsert[6]
    assert "≈$350" in upsert[6]  # 7 months × $50 — the out-of-sight leak, priced
    assert upsert[7] == 50.0
    evidence = json.loads(upsert[11])
    assert evidence["lease_rent"] == 1000.0
    assert evidence["monthly_gap"] == 50.0
    assert evidence["estimated_cumulative"] == 350.0


@pytest.mark.asyncio
async def test_rent_rule_conservative_gap_and_never_charged_severity() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=_fetch_by_rule(
            [
                # months differ → gap uses the HIGHER month (never overstates)
                _rent_row(lease_id=1, charged_m0=900.0, charged_m1=950.0),
                # rent never charged at all → critical
                _rent_row(lease_id=2, unit_number=None, charged_m0=0.0, charged_m1=0.0),
            ],
            [],
        )
    )
    conn.execute = AsyncMock()

    await scan_company(_pool(conn), COMPANY_ID)

    first = _upserts(conn)[0]
    assert first[7] == 50.0  # 1000 - max(900, 950)
    second = _upserts(conn)[1]
    assert second[4] == "critical"
    assert "No rent charged" in second[5]
    assert "lease #2" in second[5]  # no unit number → falls back to lease id


@pytest.mark.asyncio
async def test_duplicate_bill_rule_flags_paid_pairs_as_critical() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch_by_rule([], [_bill_row()]))
    conn.execute = AsyncMock()

    counts = await scan_company(_pool(conn), COMPANY_ID)

    assert counts["duplicate_vendor_bill"] == 1
    upsert = _upserts(conn)[0]
    assert upsert[2] == "duplicate_vendor_bill"
    assert upsert[3] == "bills:9001:9002"
    assert upsert[4] == "critical"  # both paid → paid twice
    assert "Rodriguez Plumbing" in upsert[5]
    assert "paid twice" in upsert[6]
    assert upsert[7] == 480.0


@pytest.mark.asyncio
async def test_portfolio_guard_collapses_a_wholesale_charge_gap_to_one_flag() -> None:
    """19 of 20 active leases short at once = missing charge data, not 19
    leaks — one data-gap flag replaces the flood, and the stale sweep
    (fingerprint list = just the portfolio flag) resolves the per-lease
    flags a previous sweep may have written."""
    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=_fetch_by_rule([_rent_row(lease_id=4000 + i) for i in range(19)], [])
    )
    conn.fetchval = AsyncMock(return_value=20)  # eligible denominator
    conn.execute = AsyncMock()

    counts = await scan_company(_pool(conn), COMPANY_ID)

    assert counts["rent_under_charged"] == 1
    upsert = _upserts(conn)[0]
    assert upsert[3] == "portfolio:charge_gap"
    assert upsert[4] == "warning"
    assert "charge data isn't reaching Occupella" in upsert[6]
    evidence = json.loads(upsert[11])
    assert evidence == {"affected_leases": 19, "eligible_leases": 20, "share": 0.95}
    resolve = _resolve_for(conn, "rent_under_charged")
    assert resolve[3] == ["portfolio:charge_gap"]


@pytest.mark.asyncio
async def test_portfolio_guard_stays_out_of_the_way_for_real_leaks() -> None:
    """6 short leases in a 30-lease book (20%) = individual flags; and a
    small trip count never even consults the denominator."""
    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=_fetch_by_rule([_rent_row(lease_id=4000 + i) for i in range(6)], [])
    )
    conn.fetchval = AsyncMock(return_value=30)
    conn.execute = AsyncMock()

    counts = await scan_company(_pool(conn), COMPANY_ID)
    assert counts["rent_under_charged"] == 6  # guard consulted, not triggered

    conn2 = MagicMock()
    conn2.fetch = AsyncMock(side_effect=_fetch_by_rule([_rent_row(), _rent_row(lease_id=4002)], []))
    conn2.fetchval = AsyncMock()
    conn2.execute = AsyncMock()

    counts = await scan_company(_pool(conn2), COMPANY_ID)
    assert counts["rent_under_charged"] == 2
    conn2.fetchval.assert_not_awaited()  # below MIN_AFFECTED → no denominator query


@pytest.mark.asyncio
async def test_healed_conditions_resolve_and_clean_books_touch_nothing() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch_by_rule([], []))
    conn.execute = AsyncMock()

    counts = await scan_company(_pool(conn), COMPANY_ID)

    assert counts["rent_under_charged"] == 0
    assert counts["duplicate_vendor_bill"] == 0
    # No upserts — but stale flags of the rules under test resolve (empty
    # current set). Asserted by rule rather than by COUNT: every enabled
    # detector sweeps, so a count pins the size of the cycle, not the
    # behaviour this test is about.
    resolves = [c.args for c in conn.execute.await_args_list]
    assert not _upserts(conn)
    assert {"rent_under_charged", "duplicate_vendor_bill"} <= {r[2] for r in resolves}
    for sql, cid, rule_key, fingerprints in resolves:
        assert "SET status = 'resolved'" in sql
        assert cid == COMPANY_ID
        assert fingerprints == []


# ── routes ───────────────────────────────────────────────────────────


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


def _route_env(settings: Settings, conn: Any):
    return (
        patch("helixis.api.routes.reports.get_pool", return_value=_pool(conn)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def test_list_flags_filters_and_rejects_unknown_status() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    with ExitStack() as stack:
        for cm in _route_env(settings, conn):
            stack.enter_context(cm)
        app = create_app(settings)
        with TestClient(app) as c:
            headers = {"Authorization": f"Bearer {_token()}"}
            assert c.get("/api/v1/reports/risk-flags", headers=headers).status_code == 200
            sql = conn.fetch.await_args.args[0]
            assert "status IN ('new', 'confirmed')" in sql  # default = actionable set
            assert (
                c.get("/api/v1/reports/risk-flags?status=dismissed", headers=headers).status_code
                == 200
            )
            assert (
                c.get("/api/v1/reports/risk-flags?status=bogus", headers=headers).status_code == 400
            )


def test_patch_flag_confirms_dismisses_and_protects_engine_statuses() -> None:
    settings = _settings()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="11111111-1111-1111-1111-111111111111")
    with ExitStack() as stack:
        for cm in _route_env(settings, conn):
            stack.enter_context(cm)
        app = create_app(settings)
        with TestClient(app) as c:
            headers = {"Authorization": f"Bearer {_token()}"}
            ok = c.patch(
                "/api/v1/reports/risk-flags/11111111-1111-1111-1111-111111111111",
                json={"status": "dismissed"},
                headers=headers,
            )
            assert ok.status_code == 200 and ok.json()["status"] == "dismissed"
            # company scoping is in the UPDATE itself
            assert "company_id = $2::uuid" in conn.fetchval.await_args.args[0]

            # 'resolved'/'new' are engine-owned — a client may not set them
            bad = c.patch(
                "/api/v1/reports/risk-flags/11111111-1111-1111-1111-111111111111",
                json={"status": "resolved"},
                headers=headers,
            )
            assert bad.status_code == 400

            conn.fetchval = AsyncMock(return_value=None)
            gone = c.patch(
                "/api/v1/reports/risk-flags/22222222-2222-2222-2222-222222222222",
                json={"status": "confirmed"},
                headers=headers,
            )
            assert gone.status_code == 404
