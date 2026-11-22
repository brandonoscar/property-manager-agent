"""Tests for the PostGIS property-proximity query — mocked pool, no live DB.

Asserts the SQL shape (company scoping, ST_DWithin radius filter, nearest-first
KNN order, ungeocoded exclusion) and the row -> dataclass mapping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest

from helixis.db.repositories.geo import (
    NearbyProperty,
    ResolvedProperty,
    UngeocodedProperty,
    list_ungeocoded,
    properties_near,
    resolve_property,
    set_property_point,
)


def _make_pool_with_conn(conn: MagicMock) -> MagicMock:
    pool = MagicMock(spec=asyncpg.Pool)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = cm
    return pool


@pytest.mark.asyncio
async def test_properties_near_maps_rows_and_scopes_by_company():
    company_id = str(uuid4())
    row = {
        "id": uuid4(),
        "buildium_id": 4610,
        "name": "Birch Lane",
        "address_line1": "88 Birch Lane",
        "city": "Queens",
        "state": "NY",
        "postal_code": "11378",
        "distance_meters": 1234.5,
        "lat": 40.7213,
        "lng": -73.9012,
    }
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    pool = _make_pool_with_conn(conn)

    out = await properties_near(
        pool, company_id=company_id, lat=40.7128, lng=-74.006, radius_meters=8000
    )

    assert out == [
        NearbyProperty(
            id=str(row["id"]),
            buildium_id=4610,
            name="Birch Lane",
            address_line1="88 Birch Lane",
            city="Queens",
            state="NY",
            postal_code="11378",
            distance_meters=1234.5,
            lat=40.7213,
            lng=-73.9012,
        )
    ]

    sql, args = conn.fetch.await_args.args[0], conn.fetch.await_args.args[1:]
    # Spatial contract: within-radius filter, nearest-first KNN, exclude ungeocoded.
    assert "ST_DWithin(geog" in sql
    assert "geog <-> ST_MakePoint" in sql  # KNN order
    assert "geog IS NOT NULL" in sql
    assert "company_id = $1::uuid" in sql
    # ST_MakePoint($3, $2) => (x=lng, y=lat): param order is company, lat, lng, radius, limit.
    assert args == (company_id, 40.7128, -74.006, 8000, 25)


@pytest.mark.asyncio
async def test_properties_near_empty_result():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = _make_pool_with_conn(conn)
    out = await properties_near(pool, company_id=str(uuid4()), lat=0.0, lng=0.0, radius_meters=1000)
    assert out == []


def test_ungeocoded_property_address_joins_present_parts():
    assert (
        UngeocodedProperty(1, "88 Birch Lane", "Queens", "NY", "11378").address
        == "88 Birch Lane, Queens, NY, 11378"
    )
    # Missing parts are dropped, not rendered as blanks/commas.
    assert UngeocodedProperty(2, None, "Queens", "NY", None).address == "Queens, NY"
    assert UngeocodedProperty(3, None, None, None, None).address == ""


@pytest.mark.asyncio
async def test_list_ungeocoded_scopes_and_filters_nullgeog():
    company_id = str(uuid4())
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "buildium_id": 4610,
                "address_line1": "88 Birch Lane",
                "city": "Queens",
                "state": "NY",
                "postal_code": "11378",
            }
        ]
    )
    pool = _make_pool_with_conn(conn)
    out = await list_ungeocoded(pool, company_id=company_id, limit=50)
    assert out == [UngeocodedProperty(4610, "88 Birch Lane", "Queens", "NY", "11378")]
    sql, args = conn.fetch.await_args.args[0], conn.fetch.await_args.args[1:]
    assert "geog IS NULL" in sql
    assert "company_id = $1::uuid" in sql
    assert args == (company_id, 50)


@pytest.mark.asyncio
async def test_resolve_property_by_name_reads_point_and_address():
    company_id = str(uuid4())
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "buildium_id": 700,
                "name": "Garden Row",
                "address_line1": "1 Garden Way",
                "city": "Austin",
                "state": "TX",
                "postal_code": "78701",
                "lat": 30.26,
                "lng": -97.74,
            }
        ]
    )
    pool = _make_pool_with_conn(conn)
    out = await resolve_property(pool, company_id=company_id, query="garden")
    assert out == [
        ResolvedProperty(700, "Garden Row", "1 Garden Way, Austin, TX, 78701", 30.26, -97.74)
    ]
    sql, args = conn.fetch.await_args.args[0], conn.fetch.await_args.args[1:]
    assert "name ILIKE '%' || $2 || '%'" in sql
    assert args == (company_id, "garden")


@pytest.mark.asyncio
async def test_resolve_property_by_id_and_null_point():
    company_id = str(uuid4())
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "buildium_id": 701,
                "name": "74 Grove",
                "address_line1": "74 Grove",
                "city": None,
                "state": None,
                "postal_code": None,
                "lat": None,
                "lng": None,
            }
        ]
    )
    pool = _make_pool_with_conn(conn)
    out = await resolve_property(pool, company_id=company_id, query="701")
    assert out == [ResolvedProperty(701, "74 Grove", "74 Grove", None, None)]
    sql, args = conn.fetch.await_args.args[0], conn.fetch.await_args.args[1:]
    assert "buildium_id = $2" in sql
    assert args == (company_id, 701)


@pytest.mark.asyncio
async def test_resolve_property_blank_query_returns_empty():
    pool = _make_pool_with_conn(MagicMock())
    assert await resolve_property(pool, company_id=str(uuid4()), query="  ") == []


@pytest.mark.asyncio
async def test_set_property_point_writes_makepoint_lng_lat():
    company_id = str(uuid4())
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = _make_pool_with_conn(conn)
    await set_property_point(
        pool, company_id=company_id, buildium_id=4610, lat=40.7128, lng=-74.006
    )
    sql, args = conn.execute.await_args.args[0], conn.execute.await_args.args[1:]
    # ST_MakePoint($3, $2) => (x=lng, y=lat); geocoded_at stamped.
    assert "ST_MakePoint($3, $2)::geography" in sql
    assert "geocoded_at = now()" in sql
    assert args == (company_id, 40.7128, -74.006, 4610)
