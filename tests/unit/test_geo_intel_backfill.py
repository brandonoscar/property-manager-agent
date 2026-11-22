"""Tests for the property-intel backfill (geo/intel_backfill.py).

Mocks the pool + the intel clients — no live DB, no live APIs. Pins the
best-effort contract: pending-only work-list, one upsert per source per
property, keyed sources contribute nothing when dark, per-property failures
skip without aborting the batch, and a missing migration degrades to 0."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.geo import intel_backfill
from helixis.geo.intel import IntelRecord
from helixis.geo.intel_freshness import RECRAWL_AFTER


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(rows: list[dict] | None = None) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    return pool, conn


def _row(buildium_id: int = 700, postal_code: str | None = "70115") -> dict:
    return {"buildium_id": buildium_id, "lat": 29.95, "lng": -90.07, "postal_code": postal_code}


def _rec(source: str, code: str = "X") -> IntelRecord:
    return IntelRecord(source=source, code=code, label=f"{source} label", detail={"k": "v"})


@pytest.fixture(autouse=True)
def _no_live_jurisdiction():
    """The jurisdiction pass rides the same enrichment call (migration 0067);
    default it to "couldn't resolve" so the intel tests stay offline and
    deterministic. Jurisdiction-specific tests re-patch inside themselves."""
    with patch.object(intel_backfill.jurisdiction, "resolve_stack", AsyncMock(return_value=None)):
        yield


def _patch_clients(
    flood=None,
    wildfire=None,
    radon=None,
    fmr=None,
    superfund=None,
):
    return (
        patch.object(intel_backfill.intel, "flood_zone", AsyncMock(return_value=flood)),
        patch.object(intel_backfill.intel, "wildfire_hazard", AsyncMock(return_value=wildfire)),
        patch.object(intel_backfill.intel, "radon_zone", AsyncMock(return_value=radon)),
        patch.object(intel_backfill.intel, "fmr_for_zip", AsyncMock(return_value=fmr)),
        patch.object(intel_backfill.intel, "superfund_sites", AsyncMock(return_value=superfund)),
    )


@pytest.mark.asyncio
async def test_upserts_one_row_per_source() -> None:
    pool, conn = _pool([_row()])
    p1, p2, p3, p4, p5 = _patch_clients(
        flood=_rec("flood", "AE"),
        wildfire=_rec("wildfire", "2"),
        radon=_rec("radon", "1"),
        fmr=_rec("fmr", "1420"),
        superfund=[{"name": "S", "npl_status": "Currently on the Final NPL", "distance_km": 1.1}],
    )
    with p1, p2, p3, p4, p5:
        n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock())
    assert n == 5
    # First fetch is the work-list; every execute is the upsert with the
    # conflict-update shape.
    assert conn.execute.await_count == 5
    sources = [call.args[3] for call in conn.execute.await_args_list]
    assert sources == ["flood", "wildfire", "radon", "fmr", "superfund"]
    sql = conn.execute.await_args_list[0].args[0]
    assert "ON CONFLICT (company_id, buildium_property_id, source) DO UPDATE" in sql
    # The superfund list is wrapped into a per-source record with a count code.
    superfund_call = conn.execute.await_args_list[4]
    assert superfund_call.args[4] == "1"
    assert "1 Superfund (NPL) site within 2 km" in superfund_call.args[5]


@pytest.mark.asyncio
async def test_dark_sources_write_nothing() -> None:
    """None from a client (no key / no data / failed) contributes no row;
    a failed superfund check (None) is NOT recorded as clean."""
    pool, conn = _pool([_row()])
    p1, p2, p3, p4, p5 = _patch_clients(flood=_rec("flood"), superfund=None)
    with p1, p2, p3, p4, p5:
        n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock())
    assert n == 1
    assert [c.args[3] for c in conn.execute.await_args_list] == ["flood"]


@pytest.mark.asyncio
async def test_superfund_clean_is_recorded() -> None:
    pool, conn = _pool([_row()])
    p1, p2, p3, p4, p5 = _patch_clients(superfund=[])
    with p1, p2, p3, p4, p5:
        n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock())
    assert n == 1
    call = conn.execute.await_args_list[0]
    assert call.args[3] == "superfund"
    assert call.args[4] == "0"
    assert "No Superfund" in call.args[5]


@pytest.mark.asyncio
async def test_property_failure_skips_but_batch_continues() -> None:
    pool, conn = _pool([_row(1), _row(2)])
    flood = AsyncMock(side_effect=[RuntimeError("boom"), _rec("flood")])
    p1, p2, p3, p4, p5 = _patch_clients()
    with patch.object(intel_backfill.intel, "flood_zone", flood), p2, p3, p4, p5:
        n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock())
    # Property 1's crash is isolated; property 2 still lands its flood row.
    assert n == 1
    assert conn.execute.await_args_list[0].args[1:4] == ("co-1", 2, "flood")


@pytest.mark.asyncio
async def test_missing_migration_degrades_to_zero() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("relation helixis_property_intel does not exist"))
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock())
    assert n == 0


@pytest.mark.asyncio
async def test_worklist_selects_pending_only_with_limit() -> None:
    pool, conn = _pool([])
    n = await intel_backfill.enrich_company_properties(pool, "co-1", settings=MagicMock(), limit=7)
    assert n == 0
    # First fetch = the intel work-list; second = the jurisdiction work-list
    # (migration 0067) riding the same pass.
    sql = conn.fetch.await_args_list[0].args[0]
    # Geocoded rows missing the sentinel 'flood' source, capped by limit.
    assert "geog IS NOT NULL" in sql
    assert "i.source = 'flood'" in sql
    assert "i.buildium_property_id IS NULL" in sql
    # ⚠ This asserted `== ("co-1", 7)` and broke the moment the query gained a
    # refresh interval — the gate working, not a regression (gotcha 89's
    # fixed-arg-count fixture). The cadence is asserted rather than dropped:
    # without it the stale arm below can never match and the work list goes
    # back to enriching each property exactly once, forever.
    assert conn.fetch.await_args_list[0].args[1:3] == ("co-1", 7)
    assert conn.fetch.await_args_list[0].args[3] == RECRAWL_AFTER
    assert "i.fetched_at < now() - $3::interval" in sql
    # The retry arm (gotcha 193): a property missing a row from a source that
    # writes on every success is due again after the back-off, not 180 days on.
    assert conn.fetch.await_args_list[0].args[4] == intel_backfill.RETRY_MISSING_AFTER
    assert conn.fetch.await_args_list[0].args[5] == list(intel_backfill.ALWAYS_WRITTEN)
    flat = " ".join(sql.split())
    assert "OR (i.fetched_at < now() - $4::interval" in flat
    assert "x.source = ANY($5::text[])) < cardinality($5::text[])" in flat
    juris_sql = conn.fetch.await_args_list[1].args[0]
    assert "state_fips IS NULL" in juris_sql
    assert conn.fetch.await_args_list[1].args[1:] == ("co-1", 7)


def test_only_sources_that_always_write_are_retried() -> None:
    """A source that can legitimately write nothing (FMR without a HUD key,
    radon for a county outside the bundled table) would put its property back
    on the list on every pass, forever (gotcha 193)."""
    assert set(intel_backfill.ALWAYS_WRITTEN) == {"flood", "wildfire", "superfund"}
    assert intel_backfill.RETRY_MISSING_AFTER.total_seconds() >= 3600
