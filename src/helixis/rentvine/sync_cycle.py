"""The nightly Rentvine mirror refresh — the worker tick that calls the sync.

``sync.sync_company`` is the cycle itself and until this module existed it had
**no production caller**: ``grep -rn sync_company src/ scripts/`` found its own
``__all__``, one docstring, and ``scripts/rentvine_phase8.py``. So a company
could hold credentials, the deployment flag could be on, and the mirror would
still never refresh on its own — the ``comms_triage`` shape (gotcha 62) at the
scale of a background job. The webhook receiver's ``refresh_one`` was the only
wired path, and it refused every delivery until the signature scheme was known
(``rentvine/signature.py``), which is what made this poll primary rather than
an optimisation. The scheme was measured on 2026-10-04 and production accepts
deliveries now, but they cover four object types, so the poll still refreshes
the rest.

⚠ **THIS SHIPS INERT AND THAT IS THE POINT.** ``rentvine.enabled`` defaults
False and ``render.yaml`` declares it False on web AND worker, so nothing here
runs today. It is the wiring that has to exist before that flag can be flipped,
and the flag should not be flipped before Phase 8 is green
(``docs/rentvine/PROGRESS.md``) — a mirror nobody has reconciled against a live
account is what puts a confident wrong number in front of a property manager.

Failure posture matches every other worker cycle: per-company isolation, loud
logs, never raises. The mirror is an enhancement; this must never keep the
Buildium queues from draining.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from helixis.db.company_population import with_rentvine_credentials
from helixis.geo.backfill import geocode_company_properties
from helixis.geo.intel_backfill import enrich_company_properties
from helixis.rentvine.sync import PAGED_TABLES, RentvineSyncDisabled, SyncResult, sync_company
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: company_id → ``time.monotonic()`` of the last completed attempt. Module-level
#: so it survives across ticks within one worker process, deliberately NOT
#: persisted: a restart re-syncs everyone shortly after boot, which is the
#: wanted behaviour after downtime and cheap because the upserts are idempotent.
#:
#: ⚠ **Its own dict, not the Buildium cycle's.** Sharing one would let a
#: Buildium sync mark a company swept for Rentvine and vice versa — gotcha 62's
#: separate-clock reasoning, and here the two populations are disjoint by
#: construction (migration 0100's ``assert_one_pms_per_company``), so a shared
#: clock would be wrong in a way no test would produce.
_last_synced: dict[str, float] = {}


def _due(company_id: str, interval_seconds: float) -> bool:
    last = _last_synced.get(company_id)
    return last is None or (time.monotonic() - last) >= interval_seconds


def _unconfirmed_paged_sources(result: SyncResult) -> list[str]:
    """Paged collections whose row count is NOT claimed to be the total.

    ⚠ **Derived from :data:`~helixis.rentvine.sync.PAGED_TABLES`, and both
    subtractions are load-bearing.** ``confirmed_complete``'s own docstring
    names three causes for absence and only one is a failure:

    * ``helixis_units`` can never be confirmed — Rentvine cannot page it per
      property. Naming it every night is a caveat that is always true and
      therefore worthless, which is how a reader learns to discount the ones
      that matter (gotchas 57/93), so it is not in ``PAGED_TABLES``. Leases
      come from the paged export and are either confirmed or FAILED (an
      export with no headers is refused), so they never land here either.
    * A table already in ``failed`` is stale rather than under-claimed. It is
      reported as failed; reporting the same fact twice under a second name
      makes one problem read as two.

    What is left is the signal: a collection that SHOULD have carried
    ``pagination-total-pages`` and did not, which is a finding about the
    vendor's spec rather than a sync fault (gotcha 149).
    """
    return sorted(PAGED_TABLES - result.confirmed_complete - set(result.failed))


async def run_rentvine_sync_cycle(
    pool: asyncpg.Pool,
    settings: Settings | None = None,
) -> dict[str, SyncResult]:
    """One tick: mirror every Rentvine company whose interval has lapsed.

    Returns per-company results (empty when nothing was due, the connector is
    dark, or the population could not be read) so a caller can observe what
    ran. Never raises.
    """
    app = settings or get_settings()
    results: dict[str, SyncResult] = {}

    # ⚠ The flag is checked BEFORE the population query, so a dark deployment —
    # which is every deployment today — spends nothing per tick. It is also
    # checked again inside ``sync_company``; that is not redundant, because this
    # cycle is not the only caller and the refusal there is what makes a forged
    # flag impossible (``scripts/rentvine_phase8.py`` names the variable rather
    # than flipping it for exactly this reason).
    if not app.rentvine.enabled:
        return results

    interval_seconds = app.rentvine.sync_interval_hours * 3600

    try:
        company_ids = await with_rentvine_credentials(pool)
    except (Exception, SystemExit):
        logger.exception("rentvine sync: listing connected companies failed — skipping tick")
        return results

    for company_id in company_ids:
        if not _due(company_id, interval_seconds):
            continue
        # Mark attempted BEFORE running, so a company that fails every time
        # cannot turn each hourly tick into a full rate-limited crawl.
        _last_synced[company_id] = time.monotonic()
        result = await sync_one(pool, company_id, app)
        if result is not None:
            results[company_id] = result
            await enrich_places(pool, company_id, app)

    return results


async def enrich_places(pool: asyncpg.Pool, company_id: str, app: Settings) -> None:
    """After a pass: geocode any property Rentvine sent no point for, then the
    flood / FMR intel and the jurisdiction stack local-law answers read.

    The same two steps, in the same order and with the same posture, as
    Buildium's daily mirror sync (``webhooks/mirror_sync.py``), which the
    Rentvine cycle never ran: no Rentvine property had a jurisdiction, so every
    local-law question went unanswered (gotcha 188). Both are cached, so a
    steady state costs one work-list query each. No Rentvine call is made.

    Here and not in ``sync_one``: the web Re-sync and the first sync share that
    path, and like Buildium's ``/backfill`` they should not wait on public map
    services. Never raises.
    """
    try:
        await geocode_company_properties(pool, settings=app, company_id=company_id)
    except (Exception, SystemExit):
        logger.exception("rentvine sync: geocode backfill failed for company=%s", company_id)
    try:
        await enrich_company_properties(pool, company_id, settings=app)
    except (Exception, SystemExit):
        logger.exception("rentvine sync: intel backfill failed for company=%s", company_id)


async def sync_one(pool: asyncpg.Pool, company_id: str, app: Settings) -> SyncResult | None:
    """Sync one company and log what it found. ``None`` when nothing ran.

    The ONE path both callers take, so the honesty logging (stale tables,
    unconfirmed totals, skipped rows) cannot reach one and not the other.
    Never raises.
    """
    try:
        result = await sync_company(pool, company_id, settings=app)
    except RentvineSyncDisabled as exc:
        # ⚠ NOT an exception log. The reachable cases are races — the
        # credentials row was deleted between the population query and this
        # call, or removed or replaced while the pass ran (the pass stops:
        # ``sync._check_login``) — and a company simply not being a Rentvine
        # customer is the ordinary state of almost every company in the
        # deployment. Logging it as a fault is how a canary nobody reads gets
        # born (gotcha 84), which is the same reasoning that kept ``Disabled``
        # out of the exception's name.
        logger.info("rentvine sync skipped company=%s: %s", company_id, exc)
        return None
    except (Exception, SystemExit):
        logger.exception("rentvine sync failed for company=%s — continuing", company_id)
        return None

    logger.info(
        "rentvine sync company=%s: %d rows across %d tables",
        company_id,
        result.total_rows,
        len(result.rows_by_table),
    )
    # ⚠ BOTH honesty fields reach the log, because a field computed and
    # read by nobody is what gotchas 147/149 both record in this very
    # package. ``failed`` says which part of the mirror is STALE rather
    # than empty (gotcha 106); the unconfirmed list says which counts are
    # not claimed to be totals.
    if result.failed:
        logger.warning(
            "rentvine sync company=%s: %d table(s) STALE, not empty — %s",
            company_id,
            len(result.failed),
            "; ".join(f"{table}: {reason}" for table, reason in sorted(result.failed.items())),
        )
    # ⚠ ERROR when nothing answered: the STALE line above is a WARNING, and
    # Sentry turns only ERRORs into events, so a pass failing every table every
    # day would leave no trace (the Buildium daily pull left none from
    # 2026-09-10, when Buildium began refusing the saved keys, to 2026-10-05).
    # Table names only: the vendor's reasons stay in the WARNING.
    if result.fetched_nothing:
        logger.error(
            "rentvine sync company=%s fetched nothing: every table failed (%s)",
            company_id,
            ", ".join(sorted(result.failed)),
        )
    unconfirmed = _unconfirmed_paged_sources(result)
    if unconfirmed:
        logger.warning(
            "rentvine sync company=%s: %s mirrored but the row count is NOT "
            "claimed complete — the server sent no pagination headers",
            company_id,
            ", ".join(unconfirmed),
        )
    if result.skipped_no_id:
        logger.warning(
            "rentvine sync company=%s: rows with no usable id were NOT written — %s",
            company_id,
            ", ".join(f"{table}={n}" for table, n in sorted(result.skipped_no_id.items())),
        )
    # A deletion is the one thing a sync does that a re-sync cannot undo, so
    # every one is said, even when it removed nothing (that is still a claim).
    if result.pruned:
        logger.info(
            "rentvine sync company=%s: pruned rows Rentvine no longer has — %s",
            company_id,
            ", ".join(f"{table}={n}" for table, n in sorted(result.pruned.items())),
        )
    if result.owner_links is not None:
        logger.info(
            "rentvine sync company=%s: owner-to-property links rewritten for %d owner(s); "
            "%d propert(ies) have no owner on record",
            company_id,
            result.owner_links,
            result.owners_unlinked_properties,
        )
    return result


@dataclass
class SyncRun:
    """One sync of one company in THIS process: when it began and how it ended."""

    started_at: datetime
    #: ``None`` while it runs.
    finished_at: datetime | None = None
    #: ``None`` after it ends means it did not finish (``sync_one`` logged why).
    result: SyncResult | None = None


#: Companies whose sync is running in THIS process. A re-test, a second click,
#: or a click during the first sync must not start a second crawl of the same
#: account. The web service runs one uvicorn worker on one instance
#: (``Dockerfile`` ``--workers 1``), so for the web routes this is the whole
#: picture; the worker's own tick is a separate process.
_initial_running: dict[str, SyncRun] = {}

#: The last run that ENDED for each company in this process, so the page that
#: started it can read how it went (``GET /rentvine/sync``). A restart forgets
#: it, and the page says so rather than guessing.
_last_finished: dict[str, SyncRun] = {}


def claim_sync(company_id: str) -> tuple[SyncRun, bool]:
    """Take the company's sync slot: ``(new run, True)``, or ``(the run already
    going, False)``. Synchronous on purpose: the slot is held before any await,
    so two requests cannot both see it free."""
    running = _initial_running.get(company_id)
    if running is not None:
        return running, False
    run = SyncRun(started_at=datetime.now(UTC))
    _initial_running[company_id] = run
    return run, True


async def run_claimed_sync(pool: asyncpg.Pool, company_id: str, run: SyncRun) -> SyncResult | None:
    """Run a claimed sync to the end, record how it ended, and free the slot.

    ``sync_one`` never raises; the ``finally`` is for cancellation (a deploy
    stopping the process), which must not leave the slot held."""
    try:
        run.result = await sync_one(pool, company_id, get_settings())
    finally:
        _finish(company_id, run)
    return run.result


def abandon_claim(company_id: str, run: SyncRun) -> None:
    """Free a slot whose sync never started, recorded as one that did not finish."""
    _finish(company_id, run)


def _finish(company_id: str, run: SyncRun) -> None:
    run.finished_at = datetime.now(UTC)
    _last_finished[company_id] = run
    if _initial_running.get(company_id) is run:
        del _initial_running[company_id]


def sync_status(company_id: str) -> SyncRun | None:
    """The run going now, else the last one that ended here, else ``None``."""
    return _initial_running.get(company_id) or _last_finished.get(company_id)


async def sync_now(pool: asyncpg.Pool, company_id: str) -> tuple[bool, SyncResult | None]:
    """Mirror one company now, unless a sync for it is already running here.

    Returns ``(ran, result)``. ``ran`` is False when another sync for the
    company holds the slot; ``result`` is ``None`` when the sync ran and did
    not finish (``sync_one`` logged why). The first-sync kick and the
    Operations page's Re-sync button share the slot, so two clicks, or a click
    during the first sync, cannot run two syncs of one company side by side.
    """
    run, started = claim_sync(company_id)
    if not started:
        return False, None
    return True, await run_claimed_sync(pool, company_id, run)


async def start_initial_sync(pool: asyncpg.Pool, company_id: str) -> bool:
    """Mirror a company right after its Rentvine credentials validate.

    Without this, the first sync waited for the worker's hourly tick. The
    wizard's launch screen polls the mirror for about a minute, so it timed out
    on an empty mirror and handed the customer an app with no data in it for
    up to an hour. Buildium's ``/test`` has always kicked its initial sync for
    the same reason.

    Returns False when a sync for this company is already running here (the
    caller does nothing). Never raises: a failed first sync is logged, and the
    hourly tick retries it.
    """
    ran, _result = await sync_now(pool, company_id)
    return ran


__all__ = [
    "SyncRun",
    "abandon_claim",
    "claim_sync",
    "enrich_places",
    "run_claimed_sync",
    "run_rentvine_sync_cycle",
    "start_initial_sync",
    "sync_now",
    "sync_one",
    "sync_status",
]
