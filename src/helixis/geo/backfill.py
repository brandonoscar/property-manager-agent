"""Geocode-on-sync backfill — populate ``helixis_properties.geog``.

Geocoding runs OFF the webhook hot path: the daily mirror re-sync
(:mod:`helixis.webhooks.mirror_sync`) calls :func:`geocode_company_properties`
after a company's backfill, and ``scripts/backfill_geocodes.py`` runs it on
demand. Both only touch rows whose ``geog IS NULL`` — an address is geocoded
ONCE and cached, so a steady state makes zero Maps API calls.

⚠ **That cache has no invalidation, and it is a KNOWN gap rather than an
oversight.** ``upsert_property`` refreshes ``address_line1``/``city``/
``state``/``postal_code`` from Buildium on every sync while leaving ``geog``
alone, so a property whose address is CORRECTED in Buildium keeps the point
derived from the old one — and the jurisdiction snapshot then answers which
state's law applies from a location the account no longer claims. Closing it
needs a stored "the address this point came from" column to compare against;
nothing here records one today. See CLAUDE.md gotcha 107.

Best-effort, same discipline as the rest of the geo module: a blank Maps key
or a single failed geocode is skipped, never raised — a missing point just
excludes that property from spatial results until the next pass.
"""

from __future__ import annotations

import logging

import asyncpg

from helixis.db.repositories import geo as geo_repo
from helixis.geo.geocode import geocode, geocoding_supported
from helixis.settings import Settings

logger = logging.getLogger(__name__)


async def geocode_company_properties(
    pool: asyncpg.Pool,
    *,
    settings: Settings,
    company_id: str,
    limit: int = 100,
) -> int:
    """Geocode up to ``limit`` of the company's ungeocoded properties and
    store their points. Returns the number newly geocoded (0 when the Maps
    key is unset, the work-list is empty, or every lookup missed). Never
    raises — geocoding is an enhancement, never load-bearing."""
    # ⚠ NOT `if not settings.maps.api_key`. The DEFAULT provider is census,
    # which is KEYLESS — so the raw-key form returned 0 before looking at
    # anything, and the geocode layer had been dark on every keyless install
    # since the 2026-08-17 census migration. Everything downstream keys off the
    # point this writes: flood zone, Fair Market Rent, and which state's
    # landlord-tenant law the jurisdiction snapshot answers from.
    if not geocoding_supported(settings):
        return 0
    try:
        pending = await geo_repo.list_ungeocoded(pool, company_id=company_id, limit=limit)
    except Exception:  # noqa: BLE001 — no 0053 migration / no geog column yet
        logger.warning("geocode backfill: work-list query failed (migration 0053?)", exc_info=True)
        return 0

    geocoded = 0
    for prop in pending:
        address = prop.address
        if not address:
            continue
        point = await geocode(address, settings=settings)
        if point is None:
            continue
        try:
            await geo_repo.set_property_point(
                pool,
                company_id=company_id,
                buildium_id=prop.buildium_id,
                lat=point.lat,
                lng=point.lng,
            )
            geocoded += 1
        except Exception:  # noqa: BLE001 — best-effort; skip this row, keep going
            logger.warning(
                "geocode backfill: store failed for buildium_id=%s", prop.buildium_id, exc_info=True
            )
    if geocoded:
        logger.info(
            "geocode backfill company=%s: %d/%d properties geocoded",
            company_id,
            geocoded,
            len(pending),
        )
    return geocoded


__all__ = ["geocode_company_properties"]
