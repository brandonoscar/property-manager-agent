"""A Rentvine property is placed where Rentvine says it is, and enriched.

Measured 2026-10-03 (shape only): all 13 sandbox properties carry
``latitude``/``longitude`` as decimal strings inside the US, 0 had a stored
point and 0 had any property intel. Nothing read the coordinates, and the
Rentvine cycle never ran the geocode and intel steps Buildium's daily sync runs,
so a local-law, flood or "near" question had nothing to work from (gotcha 188).

The sync test drives the real ``sync.sync_company`` over an HTTP mock. All rows
are synthetic.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import httpx
import pytest

from helixis.rentvine import mapper, sync, sync_cycle
from helixis.rentvine.sync import SyncResult

from .test_rentvine_source_prune import CO, _app, _complete, _pool
from .test_rentvine_sync import PROPERTY, _client, export_leases, lease_route, wire

HERE = {"latitude": "39.73923580", "longitude": "-104.99025100"}


# ── reading the point ────────────────────────────────────────────────────────


def test_a_measured_point_reads_as_lat_then_lng() -> None:
    assert mapper.property_point(HERE) == (39.7392358, -104.990251)
    assert mapper.property_point({"latitude": 0, "longitude": 5.5}) == (0.0, 5.5)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"latitude": "39.7"},
        {"latitude": "", "longitude": ""},
        {"latitude": "north", "longitude": "-104.9"},
        {"latitude": "nan", "longitude": "-104.9"},
        {"latitude": "39.7", "longitude": "inf"},
        {"latitude": "91", "longitude": "-104.9"},
        {"latitude": "39.7", "longitude": "-181"},
        {"latitude": "0", "longitude": "0.0"},
    ],
)
def test_a_point_that_cannot_be_trusted_is_none(fields: dict[str, Any]) -> None:
    assert mapper.property_point(fields) is None


# ── storing it ───────────────────────────────────────────────────────────────


def _recording_pool(status: Any = "UPDATE 2") -> tuple[Any, Any]:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=status)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.mark.asyncio
async def test_the_update_is_scoped_ordered_and_clears_a_moved_jurisdiction(caplog) -> None:
    pool, conn = _recording_pool()
    with caplog.at_level(logging.INFO, logger="helixis.rentvine.sync"):
        moved = await sync._store_property_points(pool, CO, {7: (39.5, -104.5), 8: (26.1, -81.7)})
    assert moved == 2
    sql, company, ids, lats, lngs, tolerance = conn.execute.await_args.args
    flat = " ".join(sql.split())
    # This company's Rentvine rows only (rule 11; gotcha 112).
    assert "WHERE p.company_id = $1::uuid AND p.pms = 'rentvine' AND p.buildium_id = v.id" in flat
    # PostGIS takes (x=lng, y=lat).
    assert "SET geog = ST_MakePoint(v.lng, v.lat)::geography" in flat
    assert "unnest($2::bigint[], $3::float8[], $4::float8[]) AS v(id, lat, lng)" in flat
    assert (company, ids, lats, lngs) == (CO, [7, 8], [39.5, 26.1], [-104.5, -81.7])
    # An unmoved point is not rewritten, so its stack is not re-resolved.
    assert "p.geog IS NULL OR NOT ST_DWithin(" in flat and tolerance == sync.POINT_UNCHANGED_M
    for column in ("state_fips", "county_fips", "place_fips", "county_name", "place_name"):
        assert f"{column} = NULL" in flat
    assert "jurisdiction_resolved_at = NULL" in flat
    assert "placed 2 propert(ies)" in caplog.text


@pytest.mark.asyncio
async def test_no_points_runs_no_statement() -> None:
    pool, conn = _recording_pool()
    assert await sync._store_property_points(pool, CO, {}) == 0
    pool.acquire.assert_not_called()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_database_error_is_logged_and_never_raised(caplog) -> None:
    """The properties are already written; a point is an enhancement."""
    pool, conn = _recording_pool()
    conn.execute = AsyncMock(side_effect=asyncpg.UndefinedColumnError("no geog"))
    with caplog.at_level(logging.WARNING, logger="helixis.rentvine.sync"):
        assert await sync._store_property_points(pool, CO, {7: (39.5, -104.5)}) == 0
    assert "storing property points failed" in caplog.text


def _handler(*properties: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/properties"):
            return httpx.Response(
                200, json=wire("GET /properties", *properties), headers=_complete(list(properties))
            )
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler


def _point_updates(pool: Any) -> list[Any]:
    return [
        c
        for c in pool._conn.execute.await_args_list
        if c.args[0].lstrip().startswith("UPDATE helixis_properties p")
    ]


@pytest.mark.asyncio
async def test_the_sync_stores_the_coordinates_it_read() -> None:
    placed = {**PROPERTY, **HERE}
    unplaced = {**PROPERTY, "propertyID": 990000777}
    pool, log = _pool()
    await sync.sync_company(pool, CO, settings=_app(), client=_client(_handler(placed, unplaced)))
    (update,) = _point_updates(pool)
    _sql, company, ids, lats, lngs, _tolerance = update.args
    assert (company, ids, lats, lngs) == (CO, [PROPERTY["propertyID"]], [39.7392358], [-104.990251])
    # After the properties are written, so the UPDATE has rows to land on.
    inserts = [i for i, s in enumerate(log) if s.startswith("INSERT INTO helixis_properties")]
    point = next(i for i, s in enumerate(log) if s.lstrip().startswith("UPDATE helixis_properties"))
    assert inserts and max(inserts) < point


# ── enriching after the pass ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_clock():
    sync_cycle._last_synced.clear()
    yield
    sync_cycle._last_synced.clear()


def _cycle(monkeypatch: pytest.MonkeyPatch, *, synced: bool) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=[CO]))
    if synced:
        monkeypatch.setattr(
            sync_cycle, "sync_company", AsyncMock(return_value=SyncResult(company_id=CO))
        )
    else:
        monkeypatch.setattr(sync_cycle, "sync_company", AsyncMock(side_effect=RuntimeError("x")))
    geocode, enrich = AsyncMock(return_value=0), AsyncMock(return_value=0)
    monkeypatch.setattr(sync_cycle, "geocode_company_properties", geocode)
    monkeypatch.setattr(sync_cycle, "enrich_company_properties", enrich)
    return geocode, enrich


@pytest.mark.asyncio
async def test_a_pass_is_followed_by_the_geocode_and_intel_steps(monkeypatch) -> None:
    geocode, enrich = _cycle(monkeypatch, synced=True)
    app, pool = _app(), MagicMock()
    out = await sync_cycle.run_rentvine_sync_cycle(pool, app)
    assert list(out) == [CO]
    geocode.assert_awaited_once_with(pool, settings=app, company_id=CO)
    enrich.assert_awaited_once_with(pool, CO, settings=app)


@pytest.mark.asyncio
async def test_a_failed_pass_enriches_nothing(monkeypatch) -> None:
    geocode, enrich = _cycle(monkeypatch, synced=False)
    assert await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _app()) == {}
    geocode.assert_not_awaited()
    enrich.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_step_never_stops_the_other_or_the_cycle(monkeypatch) -> None:
    geocode, enrich = _cycle(monkeypatch, synced=True)
    geocode.side_effect = RuntimeError("census down")
    out = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _app())
    assert list(out) == [CO]
    enrich.assert_awaited_once()
