"""Property-intel backfill — populate ``helixis_property_intel``.

Mirrors :mod:`helixis.geo.backfill`'s shape: runs OFF the webhook hot path
(the daily mirror re-sync calls :func:`enrich_company_properties` right after
the geocode backfill), touches only geocoded properties that don't have intel
rows yet, and is best-effort throughout — a failed source or property is
skipped, never raised.

Work-list sentinel: a property is "pending" while it has no ``flood`` row.
Flood is the one source that ALWAYS writes on a successful probe (even the
"not in a mapped flood zone" answer is persisted), so the work-list drains;
the keyed/partial sources (``fmr`` without a HUD key, ``radon`` for a county
outside the bundled table) may legitimately never write a row, which is why
they cannot be the sentinel — the same property would be re-crawled forever.

The static sources only (flood / wildfire / radon / superfund / fmr) — NWS
weather is live-alert data and belongs to the weather detector, not a cache.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from helixis.geo import intel, jurisdiction
from helixis.geo.intel import SOURCE_FLOOD, SOURCE_SUPERFUND, SOURCE_WILDFIRE, IntelRecord
from helixis.geo.intel_freshness import RECRAWL_AFTER
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: ⚠ `i.buildium_property_id IS NULL` ALONE meant a property left this list the
#: first time it was enriched and never came back. Measured on helixis-test
#: 2026-09-10, cross-tenant because a refresh cadence is a product property:
#: 109 rows across two sources, every one stamped 2026-08-17..08-21, nothing
#: re-fetched in the twenty days since — and by construction nothing ever would
#: be. `fetched_at` was written by the upsert below and read by nothing, which
#: is gotcha 88's `handled_at` arriving in a second table.
#:
#: The stale arm makes it load-bearing. Flood stays the SENTINEL for the
#: never-crawled case (the docstring above says why the keyed sources cannot
#: be), and its own row's age is what says a property is due again — one pass
#: re-queries every source, so `RECRAWL_AFTER` is the shortest window of the
#: set and is computed from it.
#:
#: ⚠ **Never-crawled sorts FIRST, and that is not cosmetic.** Refreshes and
#: first-time enrichments share one work-list under a LIMIT, so without the
#: leading sort key a company with many ageing properties would spend its whole
#: budget re-crawling rows that already have an answer while a newly added
#: property waited behind them — a fix for staleness that starves the case it
#: was built on. `false` (no row) orders before `true` in Postgres.
#:
#: ⚠ **And a source that failed is tried again, not left for 180 days.** The
#: stale arm alone meant a property whose wildfire or Superfund lookup failed
#: (both did, on every property, until 2026-10-03: gotcha 193) kept its flood
#: row and so stayed off the list until `RECRAWL_AFTER`. A source in
#: `ALWAYS_WRITTEN` writes a row on EVERY successful probe ("not mapped" and "no
#: sites" are rows), so a missing one means its last try failed, and the
#: property is due again once its flood row is `RETRY_MISSING_AFTER` old. FMR
#: (needs a key) and radon (a county outside the bundled table) can
#: legitimately write nothing, so they are not in it: a property missing them
#: would be re-crawled on every pass, forever.
_PENDING_SQL = """
SELECT p.buildium_id,
       ST_Y(p.geog::geometry) AS lat,
       ST_X(p.geog::geometry) AS lng,
       p.postal_code
  FROM public.helixis_properties p
  LEFT JOIN public.helixis_property_intel i
    ON i.company_id = p.company_id
   AND i.buildium_property_id = p.buildium_id
   AND i.source = 'flood'
 WHERE p.company_id = $1::uuid
   AND p.geog IS NOT NULL
   AND (i.buildium_property_id IS NULL
        OR i.fetched_at IS NULL
        OR i.fetched_at < now() - $3::interval
        OR (i.fetched_at < now() - $4::interval
            AND (SELECT count(*)
                   FROM public.helixis_property_intel x
                  WHERE x.company_id = p.company_id
                    AND x.buildium_property_id = p.buildium_id
                    AND x.source = ANY($5::text[])) < cardinality($5::text[])))
 ORDER BY (i.buildium_property_id IS NOT NULL), p.updated_at DESC
 LIMIT $2
"""

#: Sources that write a row on every successful probe. A property missing one
#: of them is retried (see `_PENDING_SQL`).
ALWAYS_WRITTEN: tuple[str, ...] = (SOURCE_FLOOD, SOURCE_WILDFIRE, SOURCE_SUPERFUND)

#: How long after its last pass a property missing an `ALWAYS_WRITTEN` row is
#: retried. The callers bound it further: the Rentvine cycle enriches a company
#: every 4 hours and Buildium's mirror sync once a day, so a source that stays
#: down costs one retry per property per pass, never a loop.
RETRY_MISSING_AFTER = timedelta(hours=1)

_UPSERT_SQL = """
INSERT INTO public.helixis_property_intel
    (company_id, buildium_property_id, source, code, label, detail, fetched_at)
VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, now())
ON CONFLICT (company_id, buildium_property_id, source) DO UPDATE SET
    code       = EXCLUDED.code,
    label      = EXCLUDED.label,
    detail     = EXCLUDED.detail,
    fetched_at = now()
"""


# Jurisdiction stack (migration 0067) — geocoded properties whose FIPS stack
# hasn't been resolved yet. state_fips is the sentinel: resolve_stack always
# returns a state on success (place_fips=None is a real "unincorporated"
# answer, persisted alongside jurisdiction_resolved_at), so the list drains.
_JURISDICTION_PENDING_SQL = """
SELECT p.buildium_id,
       ST_Y(p.geog::geometry) AS lat,
       ST_X(p.geog::geometry) AS lng
  FROM public.helixis_properties p
 WHERE p.company_id = $1::uuid
   AND p.geog IS NOT NULL
   AND (
        p.state_fips IS NULL
        -- Migration 0099 added county_name/place_name, which cannot be
        -- backfilled from a FIPS code (no bundled gazetteer). A row resolved
        -- before that migration carries a county FIPS and no name, so it is
        -- re-resolved once to pick the names up off the same Census response.
        -- Self-draining: resolve_stack always returns a county name when it
        -- returns a county FIPS, so a row leaves this list on its first pass.
        OR (p.county_fips IS NOT NULL AND p.county_name IS NULL)
   )
 -- ⚠ NEVER-RESOLVED FIRST. Both arms share one LIMIT, so without this a
 -- company with many name-less rows spends its whole budget re-resolving
 -- properties that already answer while a brand-new property waits behind
 -- them — a staleness fix starving the case it was built for (gotcha 93).
 ORDER BY (p.state_fips IS NOT NULL), p.updated_at DESC
 LIMIT $2
"""

_JURISDICTION_UPDATE_SQL = """
UPDATE public.helixis_properties
   SET state_fips = $3,
       county_fips = $4,
       place_fips = $5,
       county_name = $6,
       place_name = $7,
       jurisdiction_resolved_at = now()
 WHERE company_id = $1::uuid
   AND buildium_id = $2
"""


@dataclass(frozen=True)
class _PendingProperty:
    buildium_id: int
    lat: float
    lng: float
    postal_code: str | None


async def _upsert(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_id: int,
    record: IntelRecord,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _UPSERT_SQL,
            company_id,
            buildium_id,
            record.source,
            record.code,
            record.label,
            json.dumps(record.detail),
        )


async def _records_for(prop: _PendingProperty, settings: Settings) -> list[IntelRecord]:
    """Query every static source for one property; a source that returns
    ``None`` (failed / no key / no data) contributes no record."""
    records: list[IntelRecord] = []
    for rec in (
        await intel.flood_zone(prop.lat, prop.lng, settings=settings),
        await intel.wildfire_hazard(prop.lat, prop.lng, settings=settings),
        await intel.radon_zone(prop.lat, prop.lng, settings=settings),
        await intel.fmr_for_zip(prop.postal_code or "", settings=settings),
    ):
        if rec is not None:
            records.append(rec)
    sites = await intel.superfund_sites(prop.lat, prop.lng, settings=settings)
    if sites is not None:  # None = check failed; [] = verified clean
        n = len(sites)
        records.append(
            IntelRecord(
                source=SOURCE_SUPERFUND,
                code=str(n),
                label=(
                    f"{n} Superfund (NPL) site{'s' if n != 1 else ''} within 2 km"
                    if n
                    else "No Superfund (NPL) sites within 2 km"
                ),
                detail={"radius_km": 2.0, "sites": sites},
            )
        )
    return records


async def _backfill_jurisdiction(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    settings: Settings,
    limit: int,
) -> int:
    """Resolve + persist the jurisdiction FIPS stack for geocoded properties
    that don't have one yet (migration 0067). Same failure-isolation style as
    the intel pass: a failed property is skipped, never raised; a pre-0067 DB
    degrades to 0. A ``None`` from :func:`resolve_stack` (transient service
    failure) writes nothing, so the property is retried on the next pass."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_JURISDICTION_PENDING_SQL, company_id, limit)
    except Exception:  # noqa: BLE001 — migration 0067 not applied yet
        logger.warning(
            "jurisdiction backfill: work-list query failed (migration 0067?)", exc_info=True
        )
        return 0

    resolved = 0
    for row in rows:
        try:
            stack = await jurisdiction.resolve_stack(
                float(row["lat"]), float(row["lng"]), settings=settings
            )
            if stack is None:
                continue  # couldn't resolve — leave state_fips NULL for retry
            async with pool.acquire() as conn:
                await conn.execute(
                    _JURISDICTION_UPDATE_SQL,
                    company_id,
                    row["buildium_id"],
                    stack.state_fips,
                    stack.county_fips,
                    stack.place_fips,
                    # ⚠ Already on `stack` from the same Census response the
                    # FIPS codes came from, and dropped here until 2026-09-10.
                    # A FIPS code cannot be turned back into a name without a
                    # gazetteer this product does not bundle, so discarding
                    # them made the jurisdiction unnameable rather than merely
                    # unnamed (gotchas 41/50/51's shape, seventh instance).
                    stack.county_name,
                    stack.place_name,
                )
            resolved += 1
        except Exception:  # noqa: BLE001 — best-effort; skip this property, keep going
            logger.warning(
                "jurisdiction backfill: property buildium_id=%s failed",
                row["buildium_id"],
                exc_info=True,
            )
    if resolved:
        logger.info(
            "jurisdiction backfill company=%s: %d properties resolved", company_id, resolved
        )
    return resolved


async def enrich_company_properties(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    settings: Settings,
    limit: int = 25,
) -> int:
    """Fetch intel for up to ``limit`` of the company's geocoded-but-not-yet-
    enriched properties; upsert one row per source per property. Returns the
    number of rows written (0 when nothing is pending or the migration isn't
    applied). Never raises — intel is an enhancement, never load-bearing."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _PENDING_SQL,
                company_id,
                limit,
                RECRAWL_AFTER,
                RETRY_MISSING_AFTER,
                list(ALWAYS_WRITTEN),
            )
    except Exception:  # noqa: BLE001 — no 0066 migration / no geog column yet
        logger.warning("intel backfill: work-list query failed (migration 0066?)", exc_info=True)
        return 0

    written = 0
    for row in rows:
        prop = _PendingProperty(
            buildium_id=row["buildium_id"],
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            postal_code=row["postal_code"],
        )
        try:
            for record in await _records_for(prop, settings):
                await _upsert(
                    pool, company_id=company_id, buildium_id=prop.buildium_id, record=record
                )
                written += 1
        except Exception:  # noqa: BLE001 — best-effort; skip this property, keep going
            logger.warning(
                "intel backfill: property buildium_id=%s failed", prop.buildium_id, exc_info=True
            )
    if written:
        logger.info(
            "intel backfill company=%s: %d rows across %d properties",
            company_id,
            written,
            len(rows),
        )
    # Jurisdiction stack (migration 0067) rides the same enrichment pass but
    # keeps its own work list + failure isolation; it never affects the intel
    # row count above.
    await _backfill_jurisdiction(pool, company_id, settings=settings, limit=limit)
    return written


__all__ = ["enrich_company_properties"]
