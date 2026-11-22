"""Automatic entity-mirror re-sync — the cockpit's freshness backstop.

Webhooks keep the mirror fresh event-by-event and the connector card's
"Sync data" button does it on demand; this cycle makes freshness automatic:
the worker ticks it hourly, and each Buildium-connected company gets a full
:func:`helixis.buildium.backfill.backfill_company` at most once per
``mirror_sync_interval_hours`` (default daily). Because the guard is
in-memory, a worker restart re-syncs everyone shortly after boot — the
desired behavior after downtime, and cheap since backfill upserts are
idempotent and the client's token bucket paces requests.

Failure posture matches the worker's other cycles: everything is isolated
per company and swallowed with a loud log — the mirror is an enhancement;
this cycle must never keep the Buildium queues from being processed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from helixis.buildium.backfill import (
    BackfillResult,
    backfill_company,
    fetched_nothing,
    keys_refused,
    sync_counts,
)
from helixis.buildium.credentials import decrypt_buildium_settings
from helixis.db.repositories import sync_state
from helixis.geo.backfill import geocode_company_properties
from helixis.geo.intel_backfill import enrich_company_properties
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

# company_id → time.monotonic() of the last completed sync attempt.
# Module-level so it survives across cycle calls within one worker process;
# deliberately NOT persisted (see module docstring).
_last_synced: dict[str, float] = {}


def _due(company_id: str, interval_seconds: float) -> bool:
    last = _last_synced.get(company_id)
    return last is None or (time.monotonic() - last) >= interval_seconds


async def _connected_companies(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Every company with saved Buildium credentials AND a stored
    account_id (populated by POST /buildium/test — without it mirror rows
    can't be keyed for webhook routing)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT company_id, encrypted_client_id, encrypted_client_secret,
                   base_url, account_id
              FROM public.company_buildium_credentials
             WHERE account_id IS NOT NULL
            """
        )
    return [dict(r) for r in rows]


async def run_mirror_sync_cycle(pool: asyncpg.Pool) -> dict[str, BackfillResult]:
    """One tick: backfill every connected company whose interval has lapsed.

    Returns per-company results (empty dict when nothing was due) so tests
    and callers can observe what ran. Never raises.
    """
    settings = get_settings()
    interval_seconds = settings.buildium.mirror_sync_interval_hours * 3600
    results: dict[str, BackfillResult] = {}

    try:
        rows = await _connected_companies(pool)
    except (Exception, SystemExit):
        logger.exception("mirror sync: listing connected companies failed — skipping tick")
        return results

    for row in rows:
        company_id = str(row["company_id"])
        if not _due(company_id, interval_seconds):
            continue
        # Mark attempted BEFORE running so a repeatedly-failing company
        # can't turn every hourly tick into a full (rate-limited) crawl.
        _last_synced[company_id] = time.monotonic()
        try:
            bsettings = decrypt_buildium_settings(row)
            result = await backfill_company(
                bsettings,
                pool,
                company_id=company_id,
                account_id=int(row["account_id"]),
            )
            results[company_id] = result
            logger.info(
                "mirror sync company=%s: %d properties, %d units, %d tenants, "
                "%d leases, %d work orders, %d balances, %d transactions, %d errors",
                company_id,
                result.properties,
                result.units,
                result.tenants,
                result.leases,
                result.work_orders,
                result.balances,
                result.transactions,
                len(result.errors),
            )
            # ⚠ ERROR, not the stages' WARNINGs, when the pull fetched nothing:
            # every stage failed, which is a connection that does not work at
            # all. Sentry turns only ERRORs into events (``configure_sentry``),
            # so a pull that failed every day left no trace anywhere: on
            # helixis-test the two connected Buildium companies had no pull
            # stamped after 2026-09-10 while the worker ran everything else
            # (measured 2026-10-05). Stage names only: the vendor's own text
            # stays in the WARNING lines, off Sentry.
            #
            # The cause there was Buildium refusing the saved keys, a 401 on
            # every call (the worker's logs, 2026-10-05). That case gets its own
            # sentence, so Sentry files it apart from an outage: new keys are
            # the customer's to enter, where any other total failure is ours or
            # Buildium's to chase.
            if keys_refused(result):
                logger.error(
                    "mirror sync company=%s fetched nothing: Buildium refused the saved "
                    "keys (401) on every stage",
                    company_id,
                )
            elif fetched_nothing(result):
                logger.error(
                    "mirror sync company=%s fetched nothing: every stage failed (%s)",
                    company_id,
                    ", ".join(sorted(result.failed_stages)),
                )
            # A stranded onboarding crawl is repaired by THIS pull, so retire
            # the row that still says it stopped. Deliberately only when the
            # pull was clean: a run with stage errors leaves the row alone
            # rather than certifying a mirror we know is partial, and the next
            # clean tick clears it. `clear_stranded` refuses to touch a `done`
            # row or a crawl genuinely in flight — see its docstring.
            #
            # ⚠ It does NOT mark `syncing` first. A daily refresh is not an
            # import, and announcing one every tick would put "still importing
            # your Buildium data" in front of every customer every day.
            if result.ok:
                try:
                    await sync_state.clear_stranded(pool, company_id, sync_counts(result))
                except (Exception, SystemExit):  # noqa: BLE001 — status is UX, never load-bearing
                    logger.warning(
                        "mirror sync: clearing stranded sync state failed for company=%s",
                        company_id,
                        exc_info=True,
                    )
            # Geocode any newly-synced properties that still lack a point.
            # Best-effort (no-op without a Maps key or the 0053 migration),
            # off the webhook hot path, and cached — only NULL-geog rows.
            try:
                await geocode_company_properties(pool, settings=settings, company_id=company_id)
            except (Exception, SystemExit):
                logger.exception("geocode backfill failed for company=%s — continuing", company_id)
            # Property intel (flood / wildfire / radon / superfund / FMR) for
            # geocoded properties that don't have rows yet. Same posture:
            # best-effort, cached (one fetch per property, ever), and never
            # allowed to break the sync cycle.
            try:
                await enrich_company_properties(pool, company_id, settings=settings)
            except (Exception, SystemExit):
                logger.exception("intel backfill failed for company=%s — continuing", company_id)
        except (Exception, SystemExit):
            logger.exception("mirror sync failed for company=%s — continuing", company_id)

    return results


__all__ = ["run_mirror_sync_cycle"]
