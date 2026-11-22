"""The worker tick for the connectors built on ``helixis.pms`` (Rent Manager,
Propertyware). Rentvine keeps its own (``rentvine/sync_cycle.py``, design P1).

Each tick, for every such system whose deployment switch is on
(``pms.registry.enabled``):

* the connector's own pass is found lazily, as ``<package>.sync.sync_company``
  (``(pool, company_id) -> pms.mirror.SyncResult``), so a connector never edits
  this file. A system switched on whose connector is not built yet logs that
  and does nothing (fail closed);
* each company holding that system's credentials is synced when its interval
  (``Settings.<key>.sync_interval_hours``) has lapsed;
* one run per company at a time IN THIS PROCESS (:func:`claim`), shared with
  any on-demand sync a connector's route starts in the same process. ⚠ The web
  (Sync now) and the worker each keep their own claims, so a click during a
  worker pass runs a second pass beside it, each honouring every 429 on its
  own. A shared claim needs a lease table: Supabase's pooler is in
  transaction mode (``db/engine.py``), so a session advisory lock would not
  hold (design/connector-shared-layer.md, Risks: per-process state);
* at most :data:`COMPANIES_PER_TICK` companies per system per tick, so one
  tick's length is bounded in code (rule 13), not by how many companies exist.

A connector's routes start a sync for a page through :func:`start_now` (Sync
now, and the first sync after a passed test) and read it back through
:func:`sync_status`, with Rentvine's rules (``rentvine/sync_cycle.py``): a
click while a sync is running waits on that run, and how the last run ended is
kept until the process restarts.

Same posture as every worker cycle: per-company isolation, loud logs, never
raises. A mirror is an enhancement; it must never keep the queues from draining.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from helixis.db.company_population import with_pms_credentials
from helixis.geo.backfill import geocode_company_properties
from helixis.geo.intel_backfill import enrich_company_properties
from helixis.pms import registry
from helixis.pms.mirror import SyncResult

logger = logging.getLogger(__name__)

#: The most companies one system syncs in one tick. The rest wait for the next.
COMPANIES_PER_TICK = 5

#: ``{(pms, company): time.monotonic()}`` of the last start. Per process.
_last_synced: dict[tuple[str, str], float] = {}


@dataclass
class SyncRun:
    """One sync of one company in THIS process."""

    pms: str
    started_at: datetime
    finished_at: datetime | None = None
    result: SyncResult | None = None


#: The run going now for ``(pms, company)``, if any. Per process.
_running: dict[tuple[str, str], SyncRun] = {}

#: The last run that ENDED for ``(pms, company)`` in this process, so the page
#: that started a sync can read how it went. A restart forgets it, and the page
#: says so rather than guessing (Rentvine's ``_last_finished``).
_last_finished: dict[tuple[str, str], SyncRun] = {}

#: Syncs started for a page, held until they end: the event loop keeps only a
#: weak reference to a task, so an unheld one can vanish half-way.
_background: set[asyncio.Task[SyncResult | None]] = set()


def systems() -> tuple[str, ...]:
    """The systems this cycle drives: every mirror-only one but Rentvine."""
    return tuple(
        key
        for key, found in registry.PMS.items()
        if not found.live_tools and key != registry.RENTVINE
    )


def switched_on() -> tuple[str, ...]:
    """The systems this cycle drives whose deployment switch is on, read now.
    The worker's gate and each tick's list: one definition, so the two cannot
    disagree about which systems exist."""
    return tuple(pms for pms in systems() if registry.enabled(pms))


def claim(pms: str, company_id: str) -> SyncRun | None:
    """Take the company's sync slot, or ``None`` when a sync already holds it.
    Synchronous on purpose: held before any await, so two callers cannot both
    see it free."""
    key = (pms, company_id)
    if key in _running:
        return None
    run = SyncRun(pms=pms, started_at=datetime.now(UTC))
    _running[key] = run
    return run


def release(company_id: str, run: SyncRun) -> None:
    """Free the slot ``run`` holds, and keep it as the last run that ended."""
    run.finished_at = datetime.now(UTC)
    _last_finished[(run.pms, company_id)] = run
    if _running.get((run.pms, company_id)) is run:
        del _running[(run.pms, company_id)]


def claim_or_join(pms: str, company_id: str) -> tuple[SyncRun, bool]:
    """``(a new run, True)`` with the slot taken, or ``(the run already going,
    False)``. A second click, or a click during the first sync, waits on the
    running one instead of starting a second crawl of the same account.
    Synchronous, like :func:`claim`."""
    key = (pms, company_id)
    running = _running.get(key)
    if running is not None:
        return running, False
    run = SyncRun(pms=pms, started_at=datetime.now(UTC))
    _running[key] = run
    return run, True


def sync_status(pms: str, company_id: str) -> SyncRun | None:
    """The run going now, else the last one that ended here, else ``None``."""
    return _running.get((pms, company_id)) or _last_finished.get((pms, company_id))


async def run_claimed(pool: Any, pms: str, company_id: str, run: SyncRun) -> SyncResult | None:
    """Run a claimed sync to the end and free the slot. :func:`sync_one` never
    raises; the ``finally`` is for cancellation (a deploy stopping the
    process), which must not leave the slot held."""
    try:
        run.result = await sync_one(pool, pms, company_id)
    finally:
        release(company_id, run)
    return run.result


def start_now(pool: Any, pms: str, company_id: str) -> SyncRun:
    """Start one company's sync in the background for a page, unless one is
    already running here, and return the run to report.

    ``finished_at`` is ``None`` while it runs. When the background task cannot
    even be created, the slot is freed and the run reads as one that ended
    without a result, so a later click is not left waiting on a run that never
    ends (Rentvine's ``abandon_claim``).
    """
    run, started = claim_or_join(pms, company_id)
    if not started:
        return run
    work = run_claimed(pool, pms, company_id, run)
    try:
        task = asyncio.create_task(work)
    except Exception:  # noqa: BLE001 — the run reports how it ended
        work.close()
        logger.warning("%s sync: could not start a sync for company=%s", pms, company_id)
        release(company_id, run)
        return run
    _background.add(task)
    task.add_done_callback(_background.discard)
    return run


def _due(pms: str, company_id: str, interval_s: float) -> bool:
    last = _last_synced.get((pms, company_id))
    return last is None or (time.monotonic() - last) >= interval_s


async def sync_one(pool: Any, pms: str, company_id: str) -> SyncResult | None:
    """Run the connector's pass for one company and log what it found.
    ``None`` when nothing ran. Never raises.

    ⚠ Checks the switch again, though the tick already did: this is not the
    only caller (a connector's on-demand sync comes here too), and a refusal
    here is what keeps a switched-off system from syncing by another path.
    """
    if not registry.enabled(pms):
        logger.info("%s sync: switched off in this deployment; skipping", pms)
        return None
    run_pass = registry.connector_attr(pms, "sync", "sync_company")
    if run_pass is None:
        logger.warning("%s sync: switched on but the connector is not built; skipping", pms)
        return None
    try:
        result = await run_pass(pool, company_id)
    except (Exception, SystemExit):
        logger.exception("%s sync failed for company=%s — continuing", pms, company_id)
        return None
    if not isinstance(result, SyncResult):
        logger.error(
            "%s sync for company=%s returned %r, not a SyncResult", pms, company_id, result
        )
        return None
    logger.info(
        "%s sync company=%s: %d rows across %d tables",
        pms,
        company_id,
        result.total_rows,
        len(result.rows_by_table),
    )
    if result.failed:
        logger.warning(
            "%s sync company=%s: %d table(s) STALE, not empty — %s",
            pms,
            company_id,
            len(result.failed),
            "; ".join(f"{t}: {why}" for t, why in sorted(result.failed.items())),
        )
    # ⚠ ERROR when nothing answered: the STALE line above is a WARNING, and
    # Sentry turns only ERRORs into events, so a pass failing every table every
    # day would leave no trace (the Buildium daily pull left none from
    # 2026-09-10, when Buildium began refusing the saved keys, to 2026-10-05).
    # Table names only: the vendor's reasons stay in the WARNING.
    if result.fetched_nothing:
        logger.error(
            "%s sync company=%s fetched nothing: every table failed (%s)",
            pms,
            company_id,
            ", ".join(sorted(result.failed)),
        )
    if result.skipped_no_id:
        logger.warning(
            "%s sync company=%s: rows with no usable id were NOT written — %s",
            pms,
            company_id,
            ", ".join(f"{t}={n}" for t, n in sorted(result.skipped_no_id.items())),
        )
    if result.pruned:
        logger.info(
            "%s sync company=%s: pruned rows the vendor no longer has — %s",
            pms,
            company_id,
            ", ".join(f"{t}={n}" for t, n in sorted(result.pruned.items())),
        )
    return result


async def run_pms_sync_cycle(pool: Any) -> dict[tuple[str, str], SyncResult]:
    """One tick over every switched-on system. Returns what ran. Never raises."""
    results: dict[tuple[str, str], SyncResult] = {}
    # ⚠ The switch is read BEFORE the population query, so a dark deployment
    # spends nothing per tick (Rentvine's order).
    for pms in switched_on():
        try:
            switches = getattr(_settings(), pms)
            interval_s = float(switches.sync_interval_hours) * 3600
            company_ids = await with_pms_credentials(pool, pms)
        except (Exception, SystemExit):
            logger.exception("%s sync: listing connected companies failed — skipping", pms)
            continue
        budget = COMPANIES_PER_TICK
        for company_id in company_ids:
            if budget <= 0:
                logger.info("%s sync: tick budget spent; the rest wait for the next tick", pms)
                break
            if not _due(pms, company_id, interval_s):
                continue
            run = claim(pms, company_id)
            if run is None:
                continue
            budget -= 1
            _last_synced[(pms, company_id)] = time.monotonic()
            try:
                run.result = await sync_one(pool, pms, company_id)
            finally:
                release(company_id, run)
            if run.result is not None:
                results[(pms, company_id)] = run.result
                await enrich_places(pool, pms, company_id)
    return results


async def enrich_places(pool: Any, pms: str, company_id: str) -> None:
    """After a pass: geocode the properties that carry an address and no
    point, then the flood / FMR intel and the jurisdiction stack that
    local-law answers read. Rentvine's ``enrich_places``, which this cycle
    never ran: no Rent Manager or Propertyware property had a point, so
    "what's within 5 miles" answered "none", and every hazard and local-law
    question said to check back after the next sync.

    Here and not in :func:`sync_one`, as for Rentvine: Sync now shares that
    path, and a page should not wait on public map services. Both steps are
    cached, so a steady state costs one work-list query each. Never raises.
    """
    app = _settings()
    try:
        await geocode_company_properties(pool, settings=app, company_id=company_id)
    except (Exception, SystemExit):
        logger.exception("%s sync: geocode backfill failed for company=%s", pms, company_id)
    try:
        await enrich_company_properties(pool, company_id, settings=app)
    except (Exception, SystemExit):
        logger.exception("%s sync: intel backfill failed for company=%s", pms, company_id)


def _settings() -> Any:
    from helixis.settings import get_settings

    return get_settings()


__all__ = [
    "COMPANIES_PER_TICK",
    "SyncRun",
    "claim",
    "claim_or_join",
    "enrich_places",
    "release",
    "run_claimed",
    "run_pms_sync_cycle",
    "start_now",
    "switched_on",
    "sync_one",
    "sync_status",
    "systems",
]
