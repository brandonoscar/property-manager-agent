"""The reminder sweep — runs every producer and reconciles the desk.

DB-local SQL first (no Buildium API calls), so the cycle is cheap enough
to run every few hours: snoozes reopen promptly, rent-day cards appear
the morning they're due, and self-clears keep the Inbox honest between
webhook pushes. v1.5 adds ONE live signal: the Gmail-outbound checker,
consulted only by outbound-aware producers and only for candidates that
survived every DB gate — a clean sweep still costs zero API calls. Same
failure posture as the worker's other cycles: per-producer and
per-company isolation, loud logs, never raises.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from helixis.common.company_clock import company_timezone, today_in
from helixis.db import company_population
from helixis.db.company_pms import company_pms_strict
from helixis.db.pms_coverage import BUILDIUM, blind_tables, unlanded_tables
from helixis.db.repositories import reminders as reminders_repo
from helixis.db.repositories.mirror_sync_state import get_freshness
from helixis.pms import sync_cycle as pms_sync_cycle
from helixis.reminders.outbound import build_outbound_checker
from helixis.reminders.producers import PRODUCER_READS, PRODUCERS, dark_producer_ids

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_HOURS = 6

# Producers that accept the Gmail-outbound checker (their neglect gate
# extends to the team's own sent mail). The rest are called unchanged.
# ``rent_missing`` was here until it was retired for the rent_not_collected
# detector (2026-08-21). ⚠ That swap LOSES the Gmail sent-mail gate: the
# detector is pure SQL over the mirror and cannot ask Gmail, so it suppresses
# on the activity log (ported into its query) but not on a reply the team sent
# from their own inbox. Re-adding that gate means teaching the detector layer
# to take an outbound checker — a real change, not an oversight.
_OUTBOUND_AWARE = frozenset({"lead_cold"})

# company_id → monotonic time of last sweep (in-memory, same rationale as
# mirror_sync._last_synced: a worker restart sweeping everyone is desired).
_last_swept: dict[str, float] = {}

#: Deliberately separate from ``_last_swept`` — see ``_reconcile_orphaned_cards``.
_last_cleanup: dict[str, float] = {}


def _due(company_id: str, interval_seconds: float) -> bool:
    last = _last_swept.get(company_id)
    return last is None or (time.monotonic() - last) >= interval_seconds


async def run_reminder_sweep(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    outbound: Any = None,
    blind: set[str] | None = None,
) -> dict[str, int]:
    """Run all producers for one company. Returns producer → candidate count.

    A producer that raises is skipped (its existing cards stay untouched —
    NOT cleared, so a transient query failure can't wipe the desk).
    """
    counts: dict[str, int] = {}
    # Clear cards belonging to producers that no longer exist. ``sync_producer``
    # self-clears only within a producer that still RUNS, so unregistering one
    # would strand its open cards in the Inbox forever — retiring the
    # ``rent_missing`` producer would have left 41 of them sitting there. This
    # makes retirement a one-line change instead of a manual cleanup.
    try:
        await reminders_repo.clear_unknown_producers(
            pool, company_id=company_id, known=tuple(PRODUCERS)
        )
    except (Exception, SystemExit):
        logger.exception("retired-producer cleanup failed for company=%s — continuing", company_id)
    # ⚠ Resolved ONCE, here, and handed to every producer — the container is
    # UTC and that is nobody's calendar (migration 0098). Doing it per producer
    # would be five reads of one row and, worse, five chances for a new
    # producer to forget; `today` is a REQUIRED keyword argument on all of them
    # so forgetting is not expressible. Gotcha 88's reasoning, one level up
    # from the aging ladder.
    #
    # ⚠ Unreadable falls back to the default rather than skipping the company:
    # the fallback IS the date the sweep produced before this existed, so a
    # blip costs a customisation, not an Inbox.
    today = today_in(await company_timezone(pool, company_id))
    # ⚠ A producer reading a table this company's PMS never fills is not run
    # (``db/pms_coverage.py``). Its existing cards are left as-is, the same
    # posture as a producer that raised. STRICT lookup: a failed read skips the
    # company for this sweep rather than guessing Buildium.
    try:
        pms = await company_pms_strict(pool, company_id)
    except (Exception, SystemExit):
        logger.exception("reminder sweep: PMS lookup failed for company=%s — skipping", company_id)
        return counts
    # A Rentvine table whose stage never landed is not known to be complete
    # (``pms_coverage.unlanded_tables``); same rule as the detector cycle, and
    # for every system but Buildium (Rent Manager, Propertyware).
    landed = await get_freshness(pool, company_id) if pms != BUILDIUM else {}
    for name, produce in PRODUCERS.items():
        reads = PRODUCER_READS.get(name)
        if reads is None:
            # Undeclared reads can't be checked, so they're treated as blind.
            logger.error("reminder producer %s declares no PRODUCER_READS — not run", name)
        if reads is None or blind_tables(pms, reads) or unlanded_tables(pms, reads, landed):
            if blind is not None:
                blind.add(name)
            continue
        try:
            if outbound is not None and name in _OUTBOUND_AWARE:
                candidates = await produce(pool, company_id, today=today, outbound=outbound)
            else:
                candidates = await produce(pool, company_id, today=today)
        except (Exception, SystemExit):
            logger.exception(
                "reminder producer %s failed for company=%s — cards left as-is", name, company_id
            )
            continue
        try:
            await reminders_repo.sync_producer(
                pool, company_id=company_id, producer=name, candidates=candidates
            )
            counts[name] = len(candidates)
        except (Exception, SystemExit):
            logger.exception(
                "reminder sync %s failed for company=%s — continuing", name, company_id
            )
    return counts


async def _companies_with_mirror(pool: asyncpg.Pool, settings: Any = None) -> list[str]:
    """Companies with a connected PMS whose mirror the producers can read.

    Buildium companies always. Rentvine, Rent Manager and Propertyware
    companies only while that connector is enabled on this deployment: with it
    off, nothing refreshes their mirror, so a card built from it describes the
    day the flag was last on. A caller without ``settings`` gets Buildium
    only, which is what this returned before Rentvine existed.

    Delegates to ``db/company_population`` rather than restating the query.
    Five cycles each carried their own copy and nobody could see they
    disagreed; this one's disagreement with the detectors' population is what
    stranded that workspace's cards (see ``_reconcile_orphaned_cards``).
    """
    companies = await company_population.with_buildium_credentials(pool)
    rentvine = getattr(settings, "rentvine", None)
    if rentvine is not None and getattr(rentvine, "enabled", False) is True:
        companies += await company_population.with_rentvine_credentials(pool)
    # The connectors built on helixis.pms (Rent Manager, Propertyware), by
    # Rentvine's rule: only while that system's switch is on. Their companies
    # got no Inbox reminder at all before, not even the lead card, which reads
    # no PMS data.
    for key in pms_sync_cycle.systems():
        switch = getattr(settings, key, None)
        if switch is not None and getattr(switch, "enabled", False) is True:
            companies += await company_population.with_pms_credentials(pool, key)
    return companies


async def _reconcile_orphaned_cards(pool: asyncpg.Pool, produced_for: set[str]) -> None:
    """Clear retired-producer cards in companies the production sweep skips.

    ⚠ ``clear_unknown_producers`` is documented as making producer retirement
    safe — "retiring a producer must not strand its cards" — and that guarantee
    was FALSE at the one boundary that matters. It runs per company, inside the
    per-company sweep, over the credentialed population only. A company with
    mirrored books and no credentials row is outside it, so its cards can never
    be revisited by anything.

    Live proof, 2026-08-29: a test workspace holds three
    open cards last touched 2026-07-26, one of them from a ``turnover``
    producer that is not in ``PRODUCERS`` at all. That is also a Buildium
    ToS §18 concern — ``helixis_reminders`` IS in ``PURGE_TABLES``, so those
    rows reached a credential-less company by a route that bypassed the purge.

    Cleanup ONLY. Producing cards here would need Buildium, which by definition
    this company cannot answer.
    """
    try:
        candidates = await company_population.for_reminder_cleanup(pool)
    except (Exception, SystemExit):
        logger.exception("orphaned-card reconciliation: listing companies failed — skipping")
        return
    for company_id in candidates:
        if company_id in produced_for:
            continue  # already reconciled by its own sweep
        # ⚠ A SEPARATE clock from ``_last_swept``. Sharing it would let a
        # cleanup-only visit mark the company swept, so the first PRODUCTION
        # sweep after it gains credentials would be suppressed for a full
        # interval — trading a stranded card for a silent Inbox.
        last = _last_cleanup.get(company_id)
        if last is not None and (time.monotonic() - last) < SWEEP_INTERVAL_HOURS * 3600:
            continue
        _last_cleanup[company_id] = time.monotonic()
        try:
            cleared = await reminders_repo.clear_unknown_producers(
                pool, company_id=company_id, known=tuple(PRODUCERS)
            )
            if cleared:
                logger.info(
                    "cleared %d orphaned reminder card(s) for company=%s "
                    "(has cards, not in the production population)",
                    cleared,
                    company_id,
                )
        except (Exception, SystemExit):
            logger.exception("orphaned-card cleanup failed for company=%s — continuing", company_id)


async def run_reminder_sweep_cycle(
    pool: asyncpg.Pool, composio: Any = None, settings: Any = None
) -> dict[str, dict[str, int]]:
    """One worker tick: sweep every connected company whose interval lapsed.

    ``composio`` (the worker's shared client, None when email ingest is
    dark) enables the Gmail-outbound suppression; without it the sweep is
    exactly the v1 pure-SQL behavior. ``settings`` (the worker's Settings,
    None in settings-less callers/tests) enables the post-sweep manager SMS
    digest (``sms_digest.py``) — dark unless ``twilio.enabled``, and every
    send rides the Stage-1 ``send_sms`` guards. A digest failure never
    fails the sweep.
    """
    interval_seconds = SWEEP_INTERVAL_HOURS * 3600
    results: dict[str, dict[str, int]] = {}
    try:
        company_ids = await _companies_with_mirror(pool, settings)
    except (Exception, SystemExit):
        logger.exception("reminder sweep: listing companies failed — skipping tick")
        return results
    for company_id in company_ids:
        if not _due(company_id, interval_seconds):
            continue
        _last_swept[company_id] = time.monotonic()
        outbound = None
        if composio is not None:
            try:
                outbound = await build_outbound_checker(pool, composio, company_id)
            except (Exception, SystemExit):
                logger.exception("outbound checker init failed — sweeping without it")
        blind: set[str] = set()
        counts = await run_reminder_sweep(pool, company_id, outbound=outbound, blind=blind)
        results[company_id] = counts
        fired = sum(counts.values())
        # ⚠ Logged on EVERY sweep, including the quiet ones. This used to be
        # `if fired:`, so a cycle where every producer returned nothing wrote
        # no line at all — which made "the reminder subsystem ran and found
        # nothing" indistinguishable from "the reminder subsystem is dead",
        # the exact condition under which a promoted detector and a registered
        # producer both reached production emitting nothing and nobody
        # noticed. One line per company per six hours is not noise; a silence
        # that means two different things is.
        #
        # `dark` names the producers that never ran because the env allowlist
        # does not list them, so an operator can tell a switched-off producer
        # from a switched-on empty one without reading the source.
        # `blind` names the producers skipped because this company's PMS never
        # fills a table they read.
        dark = sorted(dark_producer_ids())
        logger.info(
            "reminder sweep company=%s fired=%d counts=%s dark=%s blind=%s",
            company_id,
            fired,
            counts,
            dark or "-",
            sorted(blind) or "-",
        )
        # Post-sweep manager SMS digest (Stage 3) — internally guarded
        # (twilio flag, consent roster, per-day cap, send_sms's compliance
        # gates) and never allowed to fail the sweep.
        if settings is not None and fired:
            try:
                from helixis.reminders.sms_digest import send_reminder_digests

                await send_reminder_digests(pool, settings, company_id, counts)
            except (Exception, SystemExit):
                logger.exception("sms digest failed for company=%s — sweep unaffected", company_id)
    # Companies OUTSIDE the production population can still be holding cards
    # (see the function's docstring for the live case). Runs after the main
    # loop and is cleanup-only, so it cannot affect what was produced above.
    await _reconcile_orphaned_cards(pool, produced_for=set(company_ids))
    return results


__all__ = [
    "SWEEP_INTERVAL_HOURS",
    "run_reminder_sweep",
    "run_reminder_sweep_cycle",
]
