"""Spatial reads over the property mirror (Lane A — no external API call).

These answer "which of MY properties are near X / closest / within R" as
PostGIS SQL over ``helixis_properties.geog`` — deterministic and free (runs in
our own Postgres, never touches a Maps API). Points are populated by the
geocode-on-sync path; a property with no geocode (``geog IS NULL``) is excluded.

Requires migration ``0053_property_geocoding.sql`` (PostGIS extension + the
``geog`` column). ``ST_MakePoint`` takes (x=lng, y=lat) — order matters.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class NearbyProperty:
    id: str
    buildium_id: int
    name: str | None
    address_line1: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    distance_meters: float
    # Stored point (always present here — proximity excludes ungeocoded rows),
    # so callers can plot the property on a static map.
    lat: float
    lng: float


@dataclass(frozen=True)
class UngeocodedProperty:
    buildium_id: int
    address_line1: str | None
    city: str | None
    state: str | None
    postal_code: str | None

    @property
    def address(self) -> str:
        """A single-line address string for the geocoder, blank if there is
        nothing to geocode."""
        return ", ".join(
            p for p in (self.address_line1, self.city, self.state, self.postal_code) if p
        )


@dataclass(frozen=True)
class ResolvedProperty:
    buildium_id: int
    name: str | None
    address: str
    lat: float | None
    lng: float | None


async def resolve_property(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    query: str,
) -> list[ResolvedProperty]:
    """Fuzzy-match the company's properties by name fragment (or exact
    buildium_id) and return each match with its single-line address and its
    stored point (``lat``/``lng`` are ``None`` when the row isn't geocoded
    yet). Used by the Lane-B places tool to anchor an external search on a
    property. Company-scoped."""
    query = (query or "").strip()
    if not query:
        return []
    async with pool.acquire() as conn:
        if query.isdigit():
            rows = await conn.fetch(
                """
                SELECT buildium_id, name, address_line1, city, state, postal_code,
                       ST_Y(geog::geometry) AS lat, ST_X(geog::geometry) AS lng
                  FROM public.helixis_properties
                 WHERE company_id = $1::uuid AND buildium_id = $2
                """,
                company_id,
                int(query),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT buildium_id, name, address_line1, city, state, postal_code,
                       ST_Y(geog::geometry) AS lat, ST_X(geog::geometry) AS lng
                  FROM public.helixis_properties
                 WHERE company_id = $1::uuid AND name ILIKE '%' || $2 || '%'
                 ORDER BY name
                 LIMIT 10
                """,
                company_id,
                query,
            )
    return [
        ResolvedProperty(
            buildium_id=r["buildium_id"],
            name=r["name"],
            address=", ".join(
                p for p in (r["address_line1"], r["city"], r["state"], r["postal_code"]) if p
            ),
            lat=float(r["lat"]) if r["lat"] is not None else None,
            lng=float(r["lng"]) if r["lng"] is not None else None,
        )
        for r in rows
    ]


async def list_ungeocoded(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    limit: int = 100,
) -> list[UngeocodedProperty]:
    """The company's properties that still have no stored point but DO carry a
    street address — the backfill work-list. Rows with no address at all are
    skipped (nothing to geocode). Company-scoped."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT buildium_id, address_line1, city, state, postal_code
              FROM public.helixis_properties
             WHERE company_id = $1::uuid
               AND geog IS NULL
               AND COALESCE(NULLIF(TRIM(address_line1), ''),
                            NULLIF(TRIM(city), '')) IS NOT NULL
             ORDER BY updated_at DESC
             LIMIT $2
            """,
            company_id,
            limit,
        )
    return [
        UngeocodedProperty(
            buildium_id=r["buildium_id"],
            address_line1=r["address_line1"],
            city=r["city"],
            state=r["state"],
            postal_code=r["postal_code"],
        )
        for r in rows
    ]


async def set_property_point(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_id: int,
    lat: float,
    lng: float,
) -> None:
    """Store a resolved point on the property mirror row and stamp
    ``geocoded_at``. ``ST_MakePoint`` takes (x=lng, y=lat) — order matters."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.helixis_properties
               SET geog        = ST_MakePoint($3, $2)::geography,
                   geocoded_at = now()
             WHERE company_id = $1::uuid
               AND buildium_id = $4
            """,
            company_id,
            lat,
            lng,
            buildium_id,
        )


async def properties_near(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lat: float,
    lng: float,
    radius_meters: float,
    limit: int = 25,
) -> list[NearbyProperty]:
    """The company's geocoded properties within ``radius_meters`` of
    ``(lat, lng)``, nearest first. Company-scoped; ungeocoded rows excluded."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, buildium_id, name, address_line1, city, state, postal_code,
                   ST_Distance(geog, ST_MakePoint($3, $2)::geography) AS distance_meters,
                   ST_Y(geog::geometry) AS lat, ST_X(geog::geometry) AS lng
              FROM public.helixis_properties
             WHERE company_id = $1::uuid
               AND geog IS NOT NULL
               AND ST_DWithin(geog, ST_MakePoint($3, $2)::geography, $4)
             ORDER BY geog <-> ST_MakePoint($3, $2)::geography
             LIMIT $5
            """,
            company_id,
            lat,
            lng,
            radius_meters,
            limit,
        )
    return [
        NearbyProperty(
            id=str(r["id"]),
            buildium_id=r["buildium_id"],
            name=r["name"],
            address_line1=r["address_line1"],
            city=r["city"],
            state=r["state"],
            postal_code=r["postal_code"],
            distance_meters=float(r["distance_meters"]),
            lat=float(r["lat"]),
            lng=float(r["lng"]),
        )
        for r in rows
    ]


__all__ = [
    "NearbyProperty",
    "ResolvedProperty",
    "UngeocodedProperty",
    "list_ungeocoded",
    "properties_near",
    "resolve_property",
    "set_property_point",
]
