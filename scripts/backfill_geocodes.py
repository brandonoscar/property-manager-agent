#!/usr/bin/env python3
"""Backfill ``helixis_properties.geog`` by geocoding property addresses.

The daily mirror re-sync already geocodes new properties
(:mod:`helixis.geo.backfill`); this script runs the SAME pass on demand —
useful right after applying migration 0053 to a DB that already has synced
properties, or after setting ``HELIXIS_MAPS__API_KEY`` for the first time.

Only touches rows whose ``geog IS NULL`` and that carry an address, so it is
idempotent: re-running once everything is geocoded is a no-op (zero Maps API
calls). Best-effort — a single failed lookup is skipped, never fatal.

Required env (loaded the same way the app does):
  HELIXIS_SUPABASE__DB_URL   postgresql://… (direct DB access)
  HELIXIS_MAPS__API_KEY      Google/Mapbox key (blank ⇒ nothing to do)

Usage:
  python3 scripts/backfill_geocodes.py               # every connected company
  python3 scripts/backfill_geocodes.py --company <uuid>
  python3 scripts/backfill_geocodes.py --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import asyncpg

from helixis.geo.backfill import geocode_company_properties
from helixis.geo.geocode import geocoding_supported
from helixis.settings import get_settings

logger = logging.getLogger("backfill_geocodes")


async def _companies(pool: asyncpg.Pool, only: str | None) -> list[str]:
    if only:
        return [only]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT company_id FROM public.helixis_properties WHERE geog IS NULL"
        )
    return [str(r["company_id"]) for r in rows]


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Geocode ungeocoded property addresses.")
    parser.add_argument("--company", help="Only backfill this company_id (UUID).")
    parser.add_argument("--limit", type=int, default=500, help="Max properties per company.")
    args = parser.parse_args()

    settings = get_settings()
    # ⚠ The census DEFAULT is keyless, so "no key" is not "cannot geocode"
    # (gotcha 107). This refused to run on the configuration the product
    # ships, and told the operator to set a key nothing needs.
    if not geocoding_supported(settings):
        sys.stderr.write(
            f"backfill_geocodes: provider={settings.maps.provider!r} needs "
            "HELIXIS_MAPS__API_KEY and none is set — nothing to do.\n"
        )
        return 2
    db_url = settings.supabase.db_url
    if not db_url:
        sys.stderr.write("backfill_geocodes: HELIXIS_SUPABASE__DB_URL is not set.\n")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        companies = await _companies(pool, args.company)
        if not companies:
            logger.info("no companies with ungeocoded properties — nothing to do")
            return 0
        total = 0
        for company_id in companies:
            n = await geocode_company_properties(
                pool, settings=settings, company_id=company_id, limit=args.limit
            )
            logger.info("company=%s geocoded=%d", company_id, n)
            total += n
        logger.info("done: %d properties geocoded across %d company(ies)", total, len(companies))
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
