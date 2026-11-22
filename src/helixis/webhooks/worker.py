"""Buildium webhook worker loop.

Polls ``buildium_events`` for pending rows, dispatches to the handler for
each EventName, and retries transient failures with exponential backoff.
After ``worker_max_attempts`` failures the row is moved to status=failed
and a warning is logged (attach real alerting later).

Run as a separate process:

    python -m helixis.webhooks.worker

or programmatically via ``run_worker()``.

Graceful shutdown
-----------------
Render sends ``SIGTERM`` and waits up to **30 seconds** before ``SIGKILL``.
``main()`` installs handlers for ``SIGTERM`` / ``SIGINT`` that set an
``asyncio.Event``; ``run_worker`` checks the event at the top of every
loop iteration AND uses it to interrupt the inter-poll sleep, so the
worker stops claiming new batches as soon as the signal arrives.

Any batch that's already in flight runs to completion (so each event
either lands in ``done`` or ``pending`` for retry — never stuck in
``running``). The hold-off is bounded by ``worker_max_attempts`` *
handler latency, but in practice handlers are fast (<1s) so the worker
exits well inside the 30s grace window.

If a handler is genuinely slow and exceeds the window, Render
``SIGKILL``s the process and the in-flight rows stay in
``running`` — until the stale-``running`` janitor
(``queue.reset_stale_running``, swept every ``_JANITOR_INTERVAL_SECONDS``
and once at boot) requeues them, so a worker death never silently
loses an event.

Crash resilience
----------------
The loop body is guarded: a transient DB/pooler failure in the claim
path logs, backs off exponentially (capped at 60s), and continues —
it must never exit the process (Render alert "exited with status 1",
2026-06-30). ``SystemExit`` is included everywhere per gotcha 7
(optional ML deps can ``sys.exit`` at runtime). Boot-time
``init_pool`` retries with backoff so a deploy racing a briefly
unavailable DB doesn't crash-loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agent.planner import PLANNER_PROMPT_VERSION, PlannerError
from helixis.agent.planner.action_planner import PLANNER_WORST_CASE_SECONDS
from helixis.agent.summarizer import SUMMARY_PROMPT_VERSION, Summarizer, SummarizerError
from helixis.analytics import capture as ph_capture
from helixis.analytics import configure_posthog, shutdown_posthog
from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.db.company_pms import company_pms
from helixis.db.engine import get_pool, init_pool
from helixis.db.repositories import orchestrator_runs
from helixis.db.repositories.action_idempotency import PgIdempotencyStore
from helixis.llm.router import configure_litellm_retries
from helixis.observability import (
    configure_langfuse,
    configure_sentry,
    shutdown_langfuse,
    shutdown_sentry,
    tracing,
)
from helixis.observability.tracing import configure_tracer
from helixis.settings import assert_no_openai_default, get_settings, weakened_safety_defaults
from helixis.webhooks.handlers import dispatch
from helixis.webhooks.handlers.enrichment_registry import get_enrichment_handler
from helixis.webhooks.mirror_context import gather_mirror_context
from helixis.webhooks.queue import (
    ClaimedEvent,
    PlannerClaimedRow,
    SummaryClaimedRow,
    claim_batch,
    claim_planner_batch,
    claim_summary_batch,
    coalesce_action_surface,
    mark_dead,
    mark_done,
    mark_failed_for_retry,
    mark_planner_failed,
    mark_planner_succeeded,
    mark_planner_timed_out,
    mark_summary_failed,
    mark_summary_retrying,
    mark_summary_succeeded,
    reset_stale_running,
    utc_now,
)

logger = logging.getLogger(__name__)

# Matches Buildium's own delivery schedule (1 min → 10 min → 1 hour → drop)
# so transient backend issues look similar regardless of which side retries.
_RETRY_DELAYS = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=24),
]


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _retry_delay(attempt: int) -> timedelta:
    idx = min(max(0, attempt - 1), len(_RETRY_DELAYS) - 1)
    return _RETRY_DELAYS[idx]


# PR #89 — planner-claim cap. After 5 attempts a row stops being
# claimed; the worker flips it to ``planner_status='failed'`` so the
# inbox shows the Replan affordance rather than spinning forever.
_PLANNER_MAX_ATTEMPTS = 5

# PR #89 — cooldown between re-claims on a stuck ``in_progress`` row.
# Protects against a SIGKILL'd worker leaving a row claimed and
# stalling siblings; another worker waits this long before reclaiming.
#
# ⚠ DERIVED, not chosen. A row mid-plan and a row abandoned mid-plan are
# indistinguishable — same status, same timestamp — so elapsed time is the
# ONLY separator, and a cooldown shorter than a legitimate plan makes a
# sibling worker reclaim live work: double LLM spend and two writers racing
# one action surface. This was 30 against a 61 s worst case, which was safe
# only because ``in_progress`` was never reclaimed at all. Making the reclaim
# real is what makes the number load-bearing. 3x the planner's own worst case
# leaves room for the enrichment and mirror-context work that precedes it.
_PLANNER_RECLAIM_COOLDOWN_SECONDS = PLANNER_WORST_CASE_SECONDS * 3

# PR #92 — same caps for the summarizer queue. Independent of the
# planner — a row can have ``planner_status='succeeded'`` and
# ``summary_status='retrying'`` at the same time. Tuned identically
# because the cost / latency / failure shape is similar enough that
# diverging would just be guesswork.
_SUMMARY_MAX_ATTEMPTS = 5
_SUMMARY_RECLAIM_COOLDOWN_SECONDS = 30

# Email-ingest poll cadence. The worker checks each connected user's Gmail
# this often (not every tick — that'd hammer Composio). Opt-in via the
# HELIXIS_EMAIL_INGEST_ENABLED env var so the feature stays dark until a
# deploy explicitly turns it on.
_EMAIL_POLL_INTERVAL_SECONDS = 120

# CRM email-lead poll cadence. Same rationale as the Inbox poll; opt-in via
# HELIXIS_CRM__EMAIL_INGEST_ENABLED (+ the CRM master flag).
_CRM_POLL_INTERVAL_SECONDS = 120

# Company-knowledge daily-profile tick (ADR 0006). We CHECK hourly, but the
# generator's per-company ``generated_on = today`` guard makes actual
# regeneration at-most-once-per-day — the hourly check just lets a worker that
# restarted (monotonic clock reset) catch up promptly without re-summarizing a
# company already done today. Opt-in via HELIXIS_COMPANY_KNOWLEDGE__ENABLED.
_KNOWLEDGE_TICK_INTERVAL_SECONDS = 3600

# Entity-mirror re-sync tick (cockpit freshness backstop). Same shape as the
# knowledge tick: we CHECK hourly, and the cycle's own per-company guard
# (``mirror_sync_interval_hours``, default 24h) makes an actual backfill
# at-most-once-per-interval. Opt-out via HELIXIS_BUILDIUM__MIRROR_SYNC_ENABLED.
_MIRROR_SYNC_TICK_INTERVAL_SECONDS = 3600
# Rentvine mirror refresh. Same shape as the Buildium tick above — CHECK
# hourly, and the cycle's own per-company `sync_interval_hours` guard decides
# whether anything is actually fetched. ⚠ For Rentvine the poll is PRIMARY, not
# a backstop: `rentvine/signature.py` refuses every webhook delivery until the
# signature scheme is known, so nothing else refreshes that mirror. Gated on
# HELIXIS_RENTVINE__ENABLED, which render.yaml declares false on web AND worker.
_RENTVINE_SYNC_TICK_INTERVAL_SECONDS = 3600
# The helixis.pms connectors (Rent Manager, Propertyware): the same hourly
# check, with a per-company interval inside the cycle. Each system has its own
# deployment switch, all off (design/connector-shared-layer.md, P4).
_PMS_SYNC_TICK_INTERVAL_SECONDS = 3600
_REMINDER_SWEEP_TICK_INTERVAL_SECONDS = 3600
# A2P registration sweep. We CHECK every 15 minutes; the per-row backoff in
# ``a2p_pipeline.due`` (6h while pending, 30m after a failure) is what decides
# whether anything is actually called. Checking often matters for the SHORT
# steps — a freshly-submitted registration should start the carrier chain in
# minutes rather than sitting until the next hour — while the long ones cost
# nothing extra, because campaign vetting takes 10-15 DAYS and the backoff
# skips them.
_A2P_TICK_INTERVAL_SECONDS = 900
# Hourly. Seat drift only happens when a membership change coincided with a
# Stripe failure, so this is a repair pass and not the primary mechanism —
# `sync_company_seats` runs inline on every invite and removal. Sweeping more
# often would spend queries on a fleet that is almost always already correct.
_SEATS_TICK_INTERVAL_SECONDS = 3600
# Reconciliation rules are cheap company-scoped SQL; every 6h keeps the
# risk-flag ledger fresh without re-scanning on every queue iteration.
_RECON_TICK_INTERVAL_SECONDS = 6 * 3600
# The Phase-2 analyst costs real tokens — nightly, and only when
# HELIXIS_RECON__ANALYST_ENABLED is on.
_ANALYST_TICK_INTERVAL_SECONDS = 24 * 3600

# Standing agents due-check cadence. The check itself is one cheap SELECT;
# actual LLM runs happen only for rows whose (hour_utc, last_run_at) say
# they're due, so a 5-minute tick keeps delivery within minutes of each
# user's chosen hour without idle spend.
_STANDING_TICK_INTERVAL_SECONDS = 300


def _email_ingest_enabled() -> bool:
    return os.getenv("HELIXIS_EMAIL_INGEST_ENABLED", "").strip().lower() in ("1", "true", "yes")


# Stale-``running`` janitor cadence (see ``queue.reset_stale_running``).
# Legacy-queue handlers finish in seconds, so 10 minutes of ``running``
# unambiguously means the claiming worker died. Swept every 60s.
_JANITOR_INTERVAL_SECONDS = 60
_STALE_RUNNING_SECONDS = 600

# Loop-guard backoff cap. A transient DB/network failure in the claim
# path backs off exponentially (2s, 4s, … capped here) instead of
# killing the process — a worker that logs and retries beats a Render
# "exited with status 1" alert plus a cold restart.
_LOOP_ERROR_BACKOFF_CAP_SECONDS = 60

# Boot-time pool init retry. Render deploys can race a briefly
# unavailable DB / pooler; failing the whole boot for a 5s blip just
# trades a clean retry for a crash-loop alert.
_BOOT_INIT_ATTEMPTS = 5


async def _init_pool_with_retry(
    db_url: str, stop_event: asyncio.Event, *, max_size: int = 10
) -> None:
    """``init_pool`` with exponential backoff (2s → 32s, 5 attempts).

    Raises the last error if every attempt fails — a genuinely wrong
    DB URL should still crash loudly rather than retry forever.
    """
    for attempt in range(1, _BOOT_INIT_ATTEMPTS + 1):
        try:
            await init_pool(db_url, max_size=max_size)
            return
        except (Exception, SystemExit):
            if attempt >= _BOOT_INIT_ATTEMPTS or stop_event.is_set():
                raise
            delay = 2**attempt
            logger.exception(
                "init_pool failed at boot (attempt %d/%d) — retrying in %ds",
                attempt,
                _BOOT_INIT_ATTEMPTS,
                delay,
            )
            await _interruptible_sleep(delay, stop_event)


async def _process_planner_row(row: PlannerClaimedRow) -> None:
    """Run the planner against one claimed row and persist the result.

    Lifecycle (one of three branches):

    1. Success: ``planner.plan`` returns a valid ``ActionSurface`` →
       ``coalesce_action_surface`` writes it over the deterministic
       floor → ``planner_status='succeeded'``.
    2. A TRANSIENT upstream failure — ``PlannerError(transient=True)``,
       raised when the planner exhausts its own internal retries against
       a timeout / connection error / 5xx / rate limit:
       ``planner_status='timed_out'``, which the claim query reclaims
       after the cooldown. The deterministic floor stays on the row; the
       inbox shows the Replan pill.
    3. ``PlannerError`` (invalid LLM output) OR any other exception
       OR ``planner_attempts >= max``: ``planner_status='failed'``.

    ⚠ Branch 2 said ``litellm.Timeout`` until 2026-08-29 and was
    unreachable: ``plan()`` wraps every failure into ``PlannerError``
    before it leaves, so the bare-timeout arm never fired and every
    provider blip took branch 3 on its first attempt. Measured on
    helixis-test: 14 rows at ``failed`` reading "Planner exhausted
    transient retries: litellm.Timeout", every failed row at
    ``planner_attempts = 1``, and ``timed_out`` written zero times ever.

    Importantly: we NEVER let an exception escape past this function.
    ``run_worker``'s ``gather(..., return_exceptions=False)`` shape
    would mean one bad row kills the whole batch otherwise.
    """
    import litellm

    pool = get_pool()
    handler = get_enrichment_handler(row.event_name)

    # No handler → no planner. Mark as failed so the inbox stops
    # showing this row as "pending" indefinitely; in practice the
    # enrichment registry covers everything the receiver enqueues
    # planner work for, so this branch is defensive.
    if handler is None:
        await mark_planner_failed(
            pool,
            row.id,
            error=f"no enrichment handler registered for event_name={row.event_name!r}",
        )
        return

    # Reconstruct the EnrichedEvent for the planner. Three branches:
    #
    # 1. PR #83+ row: ``enrichment`` was persisted at receive time —
    #    just validate it back into the Pydantic model. Fast path,
    #    no Buildium round-trips.
    # 2. Pre-PR-#83 legacy row: ``enrichment IS NULL`` because the
    #    receiver of the day did NOT persist it. PR #90 self-heals
    #    by re-running the enricher in the worker using the
    #    persisted ``payload`` — mirrors the receiver's call at
    #    ``router.py:208`` exactly so future schema changes only
    #    have to update one site. We also persist the result for
    #    free so subsequent replans hit the fast path.
    # 3. Re-enrichment itself fails: surface as ``planner_status='failed'``
    #    with the enricher exception. That's a real bug worth
    #    flagging (Buildium 502 / schema drift / etc.).
    if row.enrichment is None:
        logger.info(
            "PR #90 re-enriching legacy row: id=%s event_name=%s "
            "(pre-PR-#83 receiver did not persist enrichment)",
            row.id,
            row.event_name,
        )
        try:
            enriched = await handler.enricher.enrich(row.payload, row.company_id)
        except Exception as exc:  # noqa: BLE001 — re-enrichment is best-effort
            logger.exception(
                "PR #90 re-enrichment failed for legacy row id=%s",
                row.id,
            )
            await mark_planner_failed(
                pool,
                row.id,
                error=f"re-enrichment failed for legacy row: {exc!r}",
            )
            return

        # Persist the freshly-computed enrichment so subsequent
        # replans skip the re-enrich. Best-effort — if the UPDATE
        # fails (e.g. transient DB hiccup) the in-memory ``enriched``
        # is what we actually need to continue to the planner, so we
        # log and press on.
        #
        # TODO PR-when-LeadSimple-ships: the table name is hardcoded
        # to ``buildium_events`` because ``claim_planner_batch`` only
        # claims from there today. When a second source registers,
        # parameterize via ``row.table_name`` (not yet on
        # ``PlannerClaimedRow``) or thread the table name through.
        try:
            await pool.execute(
                "UPDATE buildium_events SET enrichment = $1::jsonb WHERE id = $2",
                json.dumps(enriched.model_dump(mode="json")),
                row.id,
            )
        except Exception:  # noqa: BLE001 — persistence is a nice-to-have
            logger.warning(
                "PR #90 could not persist re-enrichment for id=%s; "
                "continuing to planner with in-memory enrichment",
                row.id,
                exc_info=True,
            )
    else:
        try:
            enriched = EnrichedEvent.model_validate(row.enrichment)
        except Exception as exc:  # noqa: BLE001 — defensive against schema drift
            await mark_planner_failed(
                pool,
                row.id,
                error=f"could not reconstruct EnrichedEvent from enrichment column: {exc!r}",
            )
            return

    # Cross-record grounding for "What Helixis noticed" — local mirror
    # SQL only, best-effort: no facts is fine, a failure never blocks
    # planning. For email events the sender's address lets the gatherer
    # resolve their lease so money/lease facts light up there too.
    mirror_facts: list[str] = []
    try:
        sender = row.payload.get("From") if isinstance(row.payload, dict) else None
        lease_tenants = enriched.active_lease.tenants if enriched.active_lease else []
        tenant_emails = [t.email for t in lease_tenants if t.email]
        if sender and not tenant_emails:
            tenant_emails = [str(sender)]
        mirror_facts = await gather_mirror_context(
            pool,
            company_id=row.company_id,
            property_id=enriched.unit.property_id if enriched.unit else None,
            unit_id=enriched.unit.id if enriched.unit else None,
            lease_id=enriched.active_lease.id if enriched.active_lease else None,
            tenant_emails=tenant_emails,
            exclude_task_id=enriched.task.id,
        )
    except (Exception, SystemExit):
        logger.exception("mirror context failed for id=%s — planning without it", row.id)

    try:
        # The company's PMS decides which write tools the card may offer: a
        # Rentvine card offering a Buildium write is a button that can only
        # fail (gotcha 183). Fails open to Buildium, and the Inbox read path
        # and the executor each refuse such an action again.
        action_surface = await handler.planner.plan(
            enriched,
            row.company_id,
            mirror_facts=mirror_facts,
            pms=await company_pms(pool, row.company_id),
        )
    # ⚠ ``PlannerError`` FIRST, and it carries the transient verdict. There
    # used to be an ``except litellm.Timeout`` arm above this one holding the
    # attempts-aware retry — and it was unreachable, because ``plan()`` wraps
    # every failure into a ``PlannerError`` before it leaves (including the
    # timeout it names in its own docstring). Measured on helixis-test
    # 2026-08-29: 14 rows at ``planner_status='failed'`` reading "Planner
    # exhausted transient retries: litellm.Timeout", EVERY failed row at
    # ``planner_attempts = 1``, and ``timed_out`` written zero times ever. A
    # provider blip cost the card permanently.
    #
    # The flag comes from the raise site rather than a string match on the
    # message, so the two layers cannot drift into disagreeing (gotcha 46).
    except (litellm.Timeout, PlannerError) as exc:
        transient = isinstance(exc, litellm.Timeout) or getattr(exc, "transient", False)
        if not transient:
            logger.warning(
                "Planner returned invalid output for event=%s id=%s: %s",
                row.event_name,
                row.id,
                exc,
            )
            await mark_planner_failed(pool, row.id, error=repr(exc))
            return
        logger.warning(
            "Planner hit a transient upstream failure for event=%s id=%s (attempts=%d): %s",
            row.event_name,
            row.id,
            row.planner_attempts,
            exc,
        )
        if row.planner_attempts >= _PLANNER_MAX_ATTEMPTS:
            await mark_planner_failed(
                pool,
                row.id,
                error=f"planner failed transiently after {row.planner_attempts} attempts: {exc!r}",
            )
        else:
            await mark_planner_timed_out(pool, row.id, error=repr(exc))
        return
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — diagnostic catch-all;
        # SystemExit included per the house rule (gotcha 7): an optional
        # ML dep can sys.exit(1) at runtime, which slips past
        # ``except Exception`` and kills the whole worker process.
        logger.exception(
            "Planner crashed unexpectedly for event=%s id=%s",
            row.event_name,
            row.id,
        )
        await mark_planner_failed(pool, row.id, error=repr(exc))
        return

    # Happy path. coalesce_action_surface picks the LLM surface over
    # the floor when valid (PR #82 contract); we already know it's
    # valid because plan() validated before returning.
    try:
        await coalesce_action_surface(
            pool,
            row.id,
            llm_surface=action_surface.model_dump(mode="json"),
            deterministic_surface=row.deterministic_surface,
            enrichment=row.enrichment,
        )
        await mark_planner_succeeded(pool, row.id)
        logger.info(
            "Planner succeeded for event=%s id=%s (actions=%d)",
            row.event_name,
            row.id,
            len(action_surface.actions),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Planner output persistence failed for event=%s id=%s",
            row.event_name,
            row.id,
        )
        await mark_planner_failed(
            pool,
            row.id,
            error=f"action_surface persistence failed: {exc!r}",
        )


async def _process_summary_row(row: SummaryClaimedRow) -> None:
    """Run the summarizer against one claimed row and persist the
    text. Lifecycle mirrors ``_process_planner_row`` but maps onto
    the summary_* columns:

    1. Success: ``mark_summary_succeeded`` with the text + prompt
       version.
    2. Transient LLM error (Timeout / 5xx / RateLimit), attempts
       under cap: ``mark_summary_retrying`` so the claim loop picks
       it back up after the cooldown.
    3. Transient error, attempts AT cap: ``mark_summary_failed`` —
       inbox shows the Resummarize affordance.
    4. Non-retryable error (sanitizer rejection, schema drift, etc.):
       ``mark_summary_failed`` immediately.

    Never lets an exception escape — ``run_worker``'s
    ``gather(..., return_exceptions=False)`` would kill the whole
    batch otherwise.
    """

    pool = get_pool()

    # ISO-format the event_time so the prompt has a stable timestamp
    # to anchor natural-date phrasing on ("yesterday evening", "on
    # May 12", etc.). asyncpg returns timestamptz as a
    # timezone-aware datetime so ``isoformat()`` is sufficient.
    event_time_iso = row.event_time.isoformat() if row.event_time is not None else None

    summarizer = Summarizer()

    try:
        result = await summarizer.summarize(
            row.payload,
            row.enrichment,
            row.company_id,
            event_time_iso=event_time_iso,
            session_id=f"webhook-{row.id}",
        )
    except SummarizerError as exc:
        # Distinguish between transient (retry) and terminal
        # (failed) based on the underlying cause. The summarizer
        # already retried internally for transient errors; if we
        # see a SummarizerError here it means either:
        #   - litellm transient retries exhausted → retry the row
        #     UNLESS we're at the attempts cap (then fail)
        #   - sanitizer rejection / schema error → fail immediately
        is_transient = isinstance(exc.__cause__, _TRANSIENT_FOR_SUMMARY)
        if is_transient and row.summary_attempts < _SUMMARY_MAX_ATTEMPTS:
            logger.warning(
                "Summarizer transient failure for event=%s id=%s (attempts=%d/%d): %s",
                row.event_name,
                row.id,
                row.summary_attempts,
                _SUMMARY_MAX_ATTEMPTS,
                exc,
            )
            await mark_summary_retrying(pool, row.id, error=repr(exc))
        else:
            logger.warning(
                "Summarizer terminal failure for event=%s id=%s: %s",
                row.event_name,
                row.id,
                exc,
            )
            await mark_summary_failed(pool, row.id, error=repr(exc))
        return
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — diagnostic catch-all;
        # SystemExit per gotcha 7 — see _process_planner_row.
        logger.exception(
            "Summarizer crashed unexpectedly for event=%s id=%s",
            row.event_name,
            row.id,
        )
        await mark_summary_failed(pool, row.id, error=repr(exc))
        return

    try:
        await mark_summary_succeeded(
            pool,
            row.id,
            text=result.text,
            prompt_version=result.prompt_version,
        )
        logger.info(
            "Summarizer succeeded for event=%s id=%s (chars=%d, duration_ms=%d)",
            row.event_name,
            row.id,
            len(result.text),
            result.duration_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Summary persistence failed for event=%s id=%s",
            row.event_name,
            row.id,
        )
        await mark_summary_failed(pool, row.id, error=f"persistence failed: {exc!r}")


# Pulled out so ``_process_summary_row`` can introspect the cause
# class without importing the full litellm module at function-call
# time. Same shape as ``action_planner._TRANSIENT_LLM_EXCEPTIONS``.
def _build_transient_for_summary() -> tuple[type[Exception], ...]:
    import litellm as _litellm

    return (
        _litellm.Timeout,
        _litellm.APIConnectionError,
        _litellm.InternalServerError,
        _litellm.RateLimitError,
    )


_TRANSIENT_FOR_SUMMARY: tuple[type[Exception], ...] = _build_transient_for_summary()


async def _process_event(
    event: ClaimedEvent,
    api: BuildiumApi,
    max_attempts: int,
) -> None:
    handler = dispatch(event.event_name)
    pool = get_pool()
    # Prefer the real Helixis company UUID for analytics attribution; only fall
    # back to the Buildium AccountId when the row predates company_id backfill.
    # (Using str(account_id) when company_id is present mis-attributes the
    # worker's PostHog events vs the receiver, which uses the UUID.)
    company_id = event.company_id or (str(event.account_id) if event.account_id else "")
    try:
        await handler(event, api)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — we want to catch everything,
        # including a library sys.exit (gotcha 7) — a poison event must
        # land in retry/dead, never take the worker process down.
        logger.exception(
            "Handler failed for event=%s id=%s (attempt %d/%d)",
            event.event_name,
            event.id,
            event.attempts,
            max_attempts,
        )
        ph_capture(
            distinct_id="system",
            event="webhook failed",
            properties={
                "webhook_type": event.event_name,
                "error": str(exc)[:200],
                "attempt": event.attempts,
            },
            company_id=company_id,
        )
        if event.attempts >= max_attempts:
            await mark_dead(pool, event.id, error=str(exc))
            return
        next_at = utc_now() + _retry_delay(event.attempts)
        await mark_failed_for_retry(pool, event.id, error=str(exc), next_available_at=next_at)
        return

    await mark_done(pool, event.id)
    logger.info("Processed event=%s id=%s", event.event_name, event.id)
    ph_capture(
        distinct_id="system",
        event="webhook processed",
        properties={
            "webhook_type": event.event_name,
            "success": True,
        },
        company_id=company_id,
    )


#: How long the worker waits, at shutdown, for a cancelled background cycle to
#: unwind (roll back its open transaction) before the pool closes under it.
_BACKGROUND_STOP_GRACE_SECONDS = 5.0


class _SingleFlight:
    """Runs a sync cycle as a background task, one at a time, off the claim
    loop.

    ⚠ The worker awaited every cycle inline, so the loop claimed no webhook
    event while a cycle ran. A Rent Manager or Propertyware pass reads for
    minutes under its call budget, five companies a tick, so one tick could
    hold every claim for up to an hour. Started here instead, a cycle runs
    beside the claims; a tick that finds the last cycle still running starts
    nothing (single flight), so two passes never read one company at once.

    Never raises: a cycle's failure is logged, as the inline guard did. On
    leaving the ``async with`` (shutdown, or the loop returning) a running
    cycle is cancelled and given a moment to roll back before the pool
    closes. The pass it interrupted is not clean, so no watermark moved, and
    the next pass reads it again.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._task: asyncio.Task[None] | None = None

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, cycle: Callable[[], Awaitable[Any]]) -> bool:
        """Start ``cycle`` unless the last one is still running. Returns
        whether it started."""
        if self.busy():
            return False
        self._task = asyncio.create_task(self._run(cycle), name=self.name)
        return True

    async def _run(self, cycle: Callable[[], Awaitable[Any]]) -> None:
        try:
            await cycle()
        except (Exception, SystemExit):
            # SystemExit too: an optional dependency can sys.exit (gotcha 7),
            # and in a task it would otherwise stop the whole event loop.
            logger.exception("%s failed — continuing", self.name)

    async def __aenter__(self) -> _SingleFlight:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=_BACKGROUND_STOP_GRACE_SECONDS)
        if not done:
            logger.warning(
                "%s did not stop within %.0fs of shutdown",
                self.name,
                _BACKGROUND_STOP_GRACE_SECONDS,
            )


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep up to ``seconds``, but return early if ``stop_event`` is set.

    Implemented via ``wait_for(stop_event.wait(), timeout=seconds)``: if
    the timeout fires we just continue polling; if the event fires first
    we return immediately and the caller's loop-top check will exit.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run_worker(
    *,
    stop_after_empty_polls: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Main loop.

    ``stop_after_empty_polls`` is for tests that want a finite run.
    ``stop_event`` is the production graceful-shutdown channel — set it
    from a SIGTERM handler (see :func:`main`) to drain cleanly.
    """
    settings = get_settings()
    # Policy gate — refuse to boot if the resolved reasoning model is
    # OpenAI. Same check the API runs in its FastAPI lifespan; sharing
    # the helper keeps the two processes' policy in lock-step.
    assert_no_openai_default(settings)
    for weakened in weakened_safety_defaults(settings):
        logger.warning("safety default off: %s", weakened)
    # Error tracking — the worker ran BLIND until this: a crash or a
    # handler failure only ever reached Render logs, never an alert. The
    # SDK's logging integration turns every logger.error/.exception in the
    # loop guards into a Sentry event (WARNINGs become breadcrumbs), so
    # init alone gives coverage. No-op without a DSN, never breaks boot.
    configure_sentry(settings)
    # Langfuse tracing — the worker ran BLIND to Langfuse until this: the
    # metadata injector + run-tracer are installed in the API's FastAPI
    # lifespan (main.py), but the worker is a SEPARATE process, so its
    # planner + summarizer LLM calls produced no generation and no score.
    # Wire both here (same key gate; no-op when disabled) so the planner
    # prompt/model trace tags and the summarizer quality scores (Q72)
    # actually reach Langfuse from the worker path.
    configure_langfuse(settings)
    configure_tracer(settings)
    # PostHog — the worker's "webhook processed" / "webhook failed" captures
    # (ph_capture below) went nowhere until this: only the API's lifespan
    # (main.py) started the client, and this is a separate process, so every
    # capture here was a no-op whatever the env said. No-op without a key.
    configure_posthog(settings)
    # Same LiteLLM retry/fallback config the API uses (PR #71).
    configure_litellm_retries(settings)
    from helixis.llm.spend import configure_spend_tracking

    configure_spend_tracking()  # usage counts toward the 0050 ledger; never gates
    buildium = settings.buildium
    if stop_event is None:
        stop_event = asyncio.Event()
    await _init_pool_with_retry(
        settings.supabase.db_url,
        stop_event,
        max_size=settings.supabase.pool_max_worker,
    )
    # TODO(PR #101 follow-up): mirror the API's ``verify_schema`` gate
    # here. The worker doesn't serve user-facing API responses, so a
    # stale view shows up as a backfill bug we already have detection
    # for — keeping the worker fail-open for now.

    worker_id = _worker_id()
    empty_polls = 0

    # Email ingest (opt-in). Build one Composio client for the worker's
    # lifetime; the poll cycle reuses it across users. last_email_poll=0
    # forces a poll on the first iteration. Init failure degrades to
    # "ingest dark" (loudly logged) — email is an enhancement; it must
    # never keep the Buildium queues from being processed.
    composio = None
    # ``Any`` because the real callables are imported lazily below only when
    # their poll is enabled (keeps the Composio extra optional).
    run_email_poll_cycle: Any = None
    run_crm_lead_poll_cycle: Any = None
    last_email_poll = 0.0
    last_crm_poll = 0.0
    # CRM email-lead ingest rides the SAME Composio client as the Inbox poll;
    # it's on when the CRM master flag AND its sub-flag are set.
    crm_email_enabled = settings.crm.enabled and settings.crm.email_ingest_enabled
    if _email_ingest_enabled() or crm_email_enabled:
        try:
            from helixis.connectors.composio_client import ComposioClient

            composio = ComposioClient(settings)
            await composio.initialize()
            if _email_ingest_enabled():
                from helixis.webhooks.email_poll import run_email_poll_cycle

                logger.info(
                    "email ingest ENABLED — polling Gmail every %ds (composio available=%s)",
                    _EMAIL_POLL_INTERVAL_SECONDS,
                    composio.available,
                )
            if crm_email_enabled:
                from helixis.webhooks.crm_email_poll import run_crm_lead_poll_cycle

                logger.info(
                    "CRM email-lead ingest ENABLED — polling Gmail every %ds",
                    _CRM_POLL_INTERVAL_SECONDS,
                )
        except (Exception, SystemExit):
            composio = None
            run_email_poll_cycle = None
            run_crm_lead_poll_cycle = None
            logger.exception(
                "email ingest init failed — continuing WITHOUT email ingest "
                "(Buildium queues unaffected; restart the worker to retry)"
            )

    # Company-knowledge daily profile (ADR 0006), opt-in. last_knowledge_tick=0.0
    # forces a check on the first iteration; the generator's per-day guard makes
    # regeneration idempotent, so the first-tick check is cheap. Lazy-import so a
    # disabled feature costs nothing.
    run_company_knowledge_cycle: Any = None
    last_knowledge_tick = 0.0
    # 0.0 so the first cycle runs immediately after a deploy: a registration
    # queued while the worker was down should not wait a further tick.
    last_a2p_tick = 0.0
    last_seats_tick = 0.0
    if settings.company_knowledge.enabled:
        try:
            from helixis.company_knowledge import run_company_knowledge_cycle

            logger.info(
                "company knowledge ENABLED — refreshing profiles daily (checked every %ds)",
                _KNOWLEDGE_TICK_INTERVAL_SECONDS,
            )
        except (Exception, SystemExit):
            run_company_knowledge_cycle = None
            logger.exception(
                "company knowledge init failed — continuing WITHOUT it (queues unaffected)"
            )

    # Entity-mirror re-sync (cockpit freshness backstop), on by default.
    # The first check fires ~5 minutes after boot — soon enough that a healthy
    # restart re-syncs promptly after downtime, late enough that a crash-
    # looping worker (which dies in seconds) never reaches a Buildium crawl.
    # After that, hourly checks; the cycle's per-company guard makes the
    # actual backfill at-most-once-per-``mirror_sync_interval_hours``.
    run_mirror_sync_cycle: Any = None
    last_mirror_tick = time.monotonic() - _MIRROR_SYNC_TICK_INTERVAL_SECONDS + 300
    if buildium.mirror_sync_enabled:
        try:
            from helixis.webhooks.mirror_sync import run_mirror_sync_cycle

            logger.info(
                "mirror sync ENABLED — per-company backfill every %.0fh (checked every %ds)",
                buildium.mirror_sync_interval_hours,
                _MIRROR_SYNC_TICK_INTERVAL_SECONDS,
            )
        except (Exception, SystemExit):
            run_mirror_sync_cycle = None
            logger.exception("mirror sync init failed — continuing WITHOUT it (queues unaffected)")

    # Rentvine mirror refresh (ADR 0010) — the second PMS. Same lifecycle shape
    # as the Buildium sync above, and dark by default: the import only happens
    # when the deployment flag is on, so a Buildium-only install pays nothing.
    #
    # ⚠ The cycle checks the SAME flag again, which is not redundant — it is not
    # the only caller, and `sync_company` refuses underneath it as well. A
    # deployment flipping this on before Phase 8 is green would be mirroring
    # rows nobody has reconciled against a live account (docs/rentvine/
    # PROGRESS.md), which is a confident wrong number rather than a missing one.
    run_rentvine_sync_cycle: Any = None
    last_rentvine_tick = time.monotonic() - _RENTVINE_SYNC_TICK_INTERVAL_SECONDS + 300
    if settings.rentvine.enabled:
        try:
            from helixis.rentvine.sync_cycle import run_rentvine_sync_cycle

            logger.info(
                "rentvine sync ENABLED — per-company mirror every %.0fh (checked every %ds)",
                settings.rentvine.sync_interval_hours,
                _RENTVINE_SYNC_TICK_INTERVAL_SECONDS,
            )
        except (Exception, SystemExit):
            run_rentvine_sync_cycle = None
            logger.exception(
                "rentvine sync init failed — continuing WITHOUT it (queues unaffected)"
            )

    # The helixis.pms connectors' mirror refresh: the same lifecycle shape as
    # Rentvine's above, and dark by default. The cycle module imports no
    # connector; a connector's package loads only when its switch is on and a
    # company is due (`pms.sync_cycle.sync_one`), so a deployment with every
    # switch off never imports one.
    run_pms_sync_cycle: Any = None
    last_pms_tick = time.monotonic() - _PMS_SYNC_TICK_INTERVAL_SECONDS + 300
    try:
        from helixis.pms import sync_cycle as pms_sync_cycle

        switched_on = pms_sync_cycle.switched_on()
        if switched_on:
            run_pms_sync_cycle = pms_sync_cycle.run_pms_sync_cycle
            logger.info(
                "pms connector sync ENABLED for %s (checked every %ds)",
                ", ".join(switched_on),
                _PMS_SYNC_TICK_INTERVAL_SECONDS,
            )
    except (Exception, SystemExit):
        run_pms_sync_cycle = None
        logger.exception(
            "pms connector sync init failed — continuing WITHOUT it (queues unaffected)"
        )

    # Inbox reminders sweep — same lifecycle shape as mirror sync: first
    # check ~5 min after boot, hourly ticks, per-company interval guard
    # inside the cycle. Pure local SQL, so failure/cost posture is mild.
    run_reminder_sweep_cycle: Any = None
    last_reminder_tick = time.monotonic() - _REMINDER_SWEEP_TICK_INTERVAL_SECONDS + 300
    try:
        from helixis.reminders.sweep import run_reminder_sweep_cycle

        logger.info(
            "reminder sweep ENABLED (checked every %ds)", _REMINDER_SWEEP_TICK_INTERVAL_SECONDS
        )
    except (Exception, SystemExit):
        run_reminder_sweep_cycle = None
        logger.exception("reminder sweep init failed — continuing WITHOUT it")

    # Reconciliation sweep (roadmap §7 rank 4, Phase 1) — deterministic
    # hidden-risk rules over the mirror (rent under-charged, duplicate
    # vendor bills). Pure local SQL, idempotent upserts; same lifecycle
    # shape as the reminder sweep: first check ~5 min after boot.
    run_recon_cycle: Any = None
    last_recon_tick = time.monotonic() - _RECON_TICK_INTERVAL_SECONDS + 300
    try:
        from helixis.reconciliation import run_reconciliation_cycle as run_recon_cycle

        logger.info(
            "reconciliation sweep ENABLED (checked every %ds)", _RECON_TICK_INTERVAL_SECONDS
        )
    except (Exception, SystemExit):
        run_recon_cycle = None
        logger.exception("reconciliation sweep init failed — continuing WITHOUT it")

    # Phase-2 analyst — nightly LLM cross-examination, dark by default
    # (HELIXIS_RECON__ANALYST_ENABLED). First check ~10 min after boot so
    # a restart never immediately spends tokens.
    run_analyst: Any = None
    last_analyst_tick = time.monotonic() - _ANALYST_TICK_INTERVAL_SECONDS + 600
    if settings.recon.analyst_enabled:
        try:
            from helixis.reconciliation.analyst import run_analyst_cycle as run_analyst

            logger.info("recon analyst ENABLED — nightly per-company pass")
        except (Exception, SystemExit):
            run_analyst = None
            logger.exception("recon analyst init failed — continuing WITHOUT it")

    # Per-person standing agents (multiplayer phase 2), dark by default —
    # HELIXIS_STANDING_AGENTS_ENABLED, same worker-env gate as email ingest.
    # First check ~10 min after boot (same restraint as the recon analyst:
    # a restart never immediately spends tokens); each cycle claims due
    # rows BEFORE running so a crash skips a day rather than double-runs.
    run_standing_cycle_fn: Any = None
    last_standing_tick = time.monotonic() - _STANDING_TICK_INTERVAL_SECONDS + 600
    from helixis.webhooks.standing_agents import standing_agents_enabled

    if standing_agents_enabled():
        try:
            from helixis.webhooks.standing_agents import (
                run_standing_cycle as run_standing_cycle_fn,
            )

            logger.info(
                "standing agents ENABLED — due-task check every %ds",
                _STANDING_TICK_INTERVAL_SECONDS,
            )
        except (Exception, SystemExit):
            run_standing_cycle_fn = None
            logger.exception("standing agents init failed — continuing WITHOUT them")

    consecutive_errors = 0
    # 0.0 forces a janitor sweep on the first iteration, so rows left
    # stuck in ``running`` by a previous worker death are rescued as
    # soon as the replacement boots.
    last_janitor = 0.0

    async with BuildiumClient(buildium) as client, _SingleFlight("pms connector sync") as pms_sync:
        api = BuildiumApi(client)
        logger.info("Buildium worker %s started", worker_id)
        while True:
            if stop_event.is_set():
                logger.info("Buildium worker %s draining — stop signal received", worker_id)
                return

            # ── Loop guard ────────────────────────────────────────
            # The claim calls hit the DB raw; before this guard, one
            # transient Supabase/pooler hiccup at the wrong moment
            # propagated out of run_worker and killed the process
            # ("exited with status 1", Render alert 2026-06-30). The
            # per-row processors defend themselves — the loop must
            # defend the claims. asyncio.CancelledError is BaseException
            # (not Exception), so cancellation still propagates.
            try:
                pool = get_pool()

                # Stale-``running`` janitor — rescue rows orphaned by a
                # dead worker (see queue.reset_stale_running). Guarded
                # separately: a janitor failure must not delay claims.
                if time.monotonic() - last_janitor >= _JANITOR_INTERVAL_SECONDS:
                    last_janitor = time.monotonic()
                    try:
                        requeued, dead = await reset_stale_running(
                            pool,
                            stale_after_seconds=_STALE_RUNNING_SECONDS,
                            max_attempts=buildium.worker_max_attempts,
                        )
                        if requeued or dead:
                            logger.warning(
                                "janitor rescued stale running rows: requeued=%d dead=%d",
                                requeued,
                                dead,
                            )
                    except (Exception, SystemExit):
                        logger.exception("stale-running janitor failed — continuing")

                    # Idempotency reaper — a claim left ``in_progress`` by a
                    # crash in the write→record window blocks that exact action
                    # forever (guarded_action returns 409/"needs review" on every
                    # retry) unless something moves it past its 15-min lease to
                    # ``abandoned_unknown``. reap_stale() does that; before this it
                    # had NO caller (audit area 3), so orphaned claims never
                    # self-healed. Same interval + fail-safe guard as above.
                    try:
                        reaped = await PgIdempotencyStore(pool).reap_stale()
                        if reaped:
                            logger.warning(
                                "idempotency reaper: %d stale in_progress claim(s)"
                                " -> abandoned_unknown (needs review)",
                                reaped,
                            )
                    except (Exception, SystemExit):
                        logger.exception("idempotency reaper failed — continuing")

                    # Orchestrator-run reaper — a process death mid-run leaves
                    # telemetry rows stuck in ``running`` forever (complete_run
                    # never fires). Close them out as failed/AbandonedRun so
                    # /dev/runs stops showing phantom in-flight work. Same
                    # interval + fail-safe guard as the siblings above.
                    try:
                        stale_runs = await orchestrator_runs.reap_stale_runs(pool)
                        if stale_runs:
                            logger.warning(
                                "orchestrator-run reaper: %d stale running row(s)"
                                " -> failed/AbandonedRun",
                                stale_runs,
                            )
                    except (Exception, SystemExit):
                        logger.exception("orchestrator-run reaper failed — continuing")

                # Email poll runs on its own cadence, independent of the queue
                # batches, so it fires even when the Buildium queues are idle.
                if (
                    composio is not None
                    and run_email_poll_cycle is not None
                    and time.monotonic() - last_email_poll >= _EMAIL_POLL_INTERVAL_SECONDS
                ):
                    last_email_poll = time.monotonic()
                    try:
                        await run_email_poll_cycle(pool=pool, composio=composio, api=api)
                    except (Exception, SystemExit):
                        logger.exception("email poll cycle failed — continuing")

                # CRM email-lead poll — same cadence, its own timer, shares the
                # Composio client. Independent of the Inbox poll above.
                if (
                    composio is not None
                    and run_crm_lead_poll_cycle is not None
                    and time.monotonic() - last_crm_poll >= _CRM_POLL_INTERVAL_SECONDS
                ):
                    last_crm_poll = time.monotonic()
                    try:
                        await run_crm_lead_poll_cycle(pool=pool, composio=composio, api=api)
                    except (Exception, SystemExit):
                        logger.exception("crm lead poll cycle failed — continuing")

                # Entity-mirror re-sync — hourly check, per-company daily
                # backfill (cockpit freshness backstop). Independent of the
                # queues; a failure never delays claims.
                if (
                    run_mirror_sync_cycle is not None
                    and time.monotonic() - last_mirror_tick >= _MIRROR_SYNC_TICK_INTERVAL_SECONDS
                ):
                    last_mirror_tick = time.monotonic()
                    try:
                        await run_mirror_sync_cycle(pool)
                    except (Exception, SystemExit):
                        logger.exception("mirror sync cycle failed — continuing")

                # Rentvine mirror refresh — hourly check, per-company interval
                # guard inside the cycle. Independent of the queues; a failure
                # never delays claims. The cycle itself never raises, so this
                # guard is the belt for an import-time or settings failure.
                if (
                    run_rentvine_sync_cycle is not None
                    and time.monotonic() - last_rentvine_tick
                    >= _RENTVINE_SYNC_TICK_INTERVAL_SECONDS
                ):
                    last_rentvine_tick = time.monotonic()
                    try:
                        await run_rentvine_sync_cycle(pool)
                    except (Exception, SystemExit):
                        logger.exception("rentvine sync cycle failed — continuing")

                # The helixis.pms connectors' refresh — the same hourly check,
                # but OFF the claim loop: a pass reads for minutes under its
                # call budget, and awaited here it held every claim for that
                # long (``_SingleFlight``). A tick that finds the last cycle
                # still running starts nothing.
                if (
                    run_pms_sync_cycle is not None
                    and time.monotonic() - last_pms_tick >= _PMS_SYNC_TICK_INTERVAL_SECONDS
                ):
                    last_pms_tick = time.monotonic()
                    if not pms_sync.start(lambda: run_pms_sync_cycle(pool)):
                        logger.info(
                            "pms connector sync: the last cycle is still running — "
                            "this tick starts nothing"
                        )

                # Reconciliation sweep — hidden-risk rules over the mirror.
                if (
                    run_recon_cycle is not None
                    and time.monotonic() - last_recon_tick >= _RECON_TICK_INTERVAL_SECONDS
                ):
                    last_recon_tick = time.monotonic()
                    try:
                        await run_recon_cycle(pool)
                    except (Exception, SystemExit):
                        logger.exception("reconciliation cycle failed — continuing")

                # Standing agents — per-user scheduled prompts, flag-gated.
                # The cycle contains per-task failures itself; this guard
                # is the belt for anything escaping it.
                if (
                    run_standing_cycle_fn is not None
                    and time.monotonic() - last_standing_tick >= _STANDING_TICK_INTERVAL_SECONDS
                ):
                    last_standing_tick = time.monotonic()
                    try:
                        await run_standing_cycle_fn(pool, settings, composio_client=composio)
                    except (Exception, SystemExit):
                        logger.exception("standing agents cycle failed — continuing")

                # Recon analyst — nightly, flag-gated, token-budgeted.
                if (
                    run_analyst is not None
                    and time.monotonic() - last_analyst_tick >= _ANALYST_TICK_INTERVAL_SECONDS
                ):
                    last_analyst_tick = time.monotonic()
                    try:
                        await run_analyst(pool, settings)
                    except (Exception, SystemExit):
                        logger.exception("recon analyst cycle failed — continuing")

                # Inbox reminders — neglect-aware desk cards, local SQL only.
                if (
                    run_reminder_sweep_cycle is not None
                    and time.monotonic() - last_reminder_tick
                    >= _REMINDER_SWEEP_TICK_INTERVAL_SECONDS
                ):
                    last_reminder_tick = time.monotonic()
                    try:
                        # composio (None when email ingest is dark) enables the
                        # v1.5 Gmail-outbound suppression inside the sweep;
                        # settings enables the post-sweep manager SMS digest
                        # (dark unless twilio.enabled).
                        await run_reminder_sweep_cycle(pool, composio=composio, settings=settings)
                    except (Exception, SystemExit):
                        logger.exception("reminder sweep cycle failed — continuing")

                # Company-knowledge daily profile (ADR 0006) — its own hourly
                # check; the generator's per-day guard makes regeneration
                # at-most-once-per-day. Independent of the queues, fires even
                # when they're idle.
                if (
                    run_company_knowledge_cycle is not None
                    and time.monotonic() - last_knowledge_tick >= _KNOWLEDGE_TICK_INTERVAL_SECONDS
                ):
                    last_knowledge_tick = time.monotonic()
                    try:
                        await run_company_knowledge_cycle(pool=pool, settings=settings)
                    except (Exception, SystemExit):
                        logger.exception("company knowledge cycle failed — continuing")

                # A2P registration sweep — drives a queued registration through
                # the carrier chain, polls vetting, and buys the number once it
                # clears. Self-limiting: with no carrier credentials it returns
                # 0 without a query, and every row carries its own backoff.
                if time.monotonic() - last_a2p_tick >= _A2P_TICK_INTERVAL_SECONDS:
                    last_a2p_tick = time.monotonic()
                    try:
                        from helixis.comms.a2p_cycle import run_a2p_cycle
                        from helixis.comms.a2p_pipeline import A2PCredentials

                        await run_a2p_cycle(
                            pool,
                            A2PCredentials(
                                account_sid=settings.twilio.account_sid,
                                auth_token=settings.twilio.auth_token,
                                webhook_base_url=settings.twilio.webhook_base_url,
                                primary_profile_sid=settings.twilio.primary_profile_sid,
                            ),
                        )
                    except (Exception, SystemExit):
                        logger.exception("a2p registration cycle failed — continuing")
                # Seat reconciliation — repairs Stripe/membership drift left by
                # a sync that failed inline. Every seat sync is best-effort
                # AFTER its membership write (there is no transaction across
                # Postgres and Stripe), so without this pass a single Stripe
                # outage during an invite leaves a company mis-billed
                # indefinitely, in either direction, with nothing to notice.
                # Idempotent: a healthy fleet makes zero API calls.
                if time.monotonic() - last_seats_tick >= _SEATS_TICK_INTERVAL_SECONDS:
                    last_seats_tick = time.monotonic()
                    try:
                        from helixis.billing.seats_cycle import run_seat_reconcile_cycle

                        await run_seat_reconcile_cycle(pool, settings)
                    except (Exception, SystemExit):
                        logger.exception("seat reconcile cycle failed — continuing")
                    # Subscription reconciliation — the other half, and a
                    # DIFFERENT population on purpose. The seat sweep visits
                    # only companies that already have a subscription id, so a
                    # company whose `subscription.created` webhook was lost is
                    # excluded by the very rule that makes that sweep cheap
                    # (gotcha 62: a sweep whose population query excludes the
                    # companies that need it). This one reads Stripe for
                    # anyone we hold a CUSTOMER id for, and never writes to
                    # Stripe.
                    try:
                        from helixis.billing.subscription_reconcile import (
                            run_subscription_reconcile_cycle,
                        )

                        await run_subscription_reconcile_cycle(pool, settings)
                    except (Exception, SystemExit):
                        logger.exception("subscription reconcile cycle failed — continuing")
                batch = await claim_batch(
                    pool,
                    worker_id=worker_id,
                    batch_size=buildium.worker_batch_size,
                )

                # PR #89 — parallel planner claim. Runs alongside the legacy
                # event dispatch each tick so the planner queue drains at
                # the same cadence as the receiver enqueues. Empty-polls
                # accounting tracks ALL queues so the worker doesn't
                # exit early in tests when only one queue happens to be
                # idle on a given tick.
                planner_batch = await claim_planner_batch(
                    pool,
                    batch_size=buildium.worker_batch_size,
                    max_attempts=_PLANNER_MAX_ATTEMPTS,
                    cooldown_seconds=_PLANNER_RECLAIM_COOLDOWN_SECONDS,
                    # PR #107 — mirror the summarizer self-heal: also pick
                    # up rows whose action_surface.prompt_version is stale.
                    current_prompt_version=PLANNER_PROMPT_VERSION,
                )

                # PR #92 — same idea for the summarizer. Independent of
                # the planner queue; rows flow through both pipelines
                # concurrently. The empty-polls check below considers
                # all three queues.
                summary_batch = await claim_summary_batch(
                    pool,
                    batch_size=buildium.worker_batch_size,
                    max_attempts=_SUMMARY_MAX_ATTEMPTS,
                    cooldown_seconds=_SUMMARY_RECLAIM_COOLDOWN_SECONDS,
                    current_prompt_version=SUMMARY_PROMPT_VERSION,
                )
            except (Exception, SystemExit):
                consecutive_errors += 1
                delay = min(2**consecutive_errors, _LOOP_ERROR_BACKOFF_CAP_SECONDS)
                logger.exception(
                    "worker loop iteration failed (consecutive=%d) — backing off %ds",
                    consecutive_errors,
                    delay,
                )
                await _interruptible_sleep(delay, stop_event)
                continue

            consecutive_errors = 0

            if not batch and not planner_batch and not summary_batch:
                empty_polls += 1
                if stop_after_empty_polls is not None and empty_polls >= stop_after_empty_polls:
                    logger.info("Worker stopping after %d empty polls", empty_polls)
                    return
                await _interruptible_sleep(buildium.worker_poll_interval_seconds, stop_event)
                continue

            empty_polls = 0
            # Run the in-flight batches to completion even if SIGTERM
            # lands mid-flight: each row must reach a terminal status
            # so nothing is stuck in ``running`` / ``in_progress`` /
            # mid-summarize. The next loop-top check exits afterwards.
            #
            # The per-row processors catch their own errors; what can
            # still escape is a mark_* DB write failing INSIDE an
            # except handler (DB down mid-batch). Guarded so the
            # process survives — the janitor + reclaim cooldowns
            # rescue whichever rows were left non-terminal.
            try:
                await asyncio.gather(
                    *(_process_event(event, api, buildium.worker_max_attempts) for event in batch),
                    *(_process_planner_row(planner_row) for planner_row in planner_batch),
                    *(_process_summary_row(summary_row) for summary_row in summary_batch),
                    return_exceptions=False,
                )
            except (Exception, SystemExit):
                consecutive_errors += 1
                delay = min(2**consecutive_errors, _LOOP_ERROR_BACKOFF_CAP_SECONDS)
                logger.exception(
                    "batch processing escaped row guards (consecutive=%d) — backing off %ds",
                    consecutive_errors,
                    delay,
                )
                await _interruptible_sleep(delay, stop_event)


async def process_one(event_id: UUID) -> None:
    """Dispatch a specific event — useful for replay scripts."""
    settings = get_settings()
    await init_pool(settings.supabase.db_url)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, event_name, event_time, account_id, company_id, "
            "entity_ids, payload, attempts FROM buildium_events WHERE id = $1",
            event_id,
        )
    if row is None:
        raise ValueError(f"Event {event_id} not found")

    from helixis.webhooks.queue import _ensure_dict  # local import to reuse helper

    event = ClaimedEvent(
        id=row["id"],
        event_name=row["event_name"],
        event_time=row["event_time"],
        account_id=row["account_id"],
        company_id=row["company_id"] or "",
        entity_ids=_ensure_dict(row["entity_ids"]),
        payload=_ensure_dict(row["payload"]),
        attempts=row["attempts"],
    )

    async with BuildiumClient(settings.buildium, company_id=event.company_id or None) as client:
        api = BuildiumApi(client)
        await _process_event(event, api, settings.buildium.worker_max_attempts)


async def _run_with_signal_handlers() -> None:
    """Wire SIGTERM / SIGINT to a stop event before entering the loop.

    Render delivers SIGTERM on deploy / scaling events and waits up to
    30s before SIGKILL. We want every in-flight batch to complete and
    every claimed row to leave ``running`` before we exit.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal(signame: str) -> None:
        if not stop_event.is_set():
            logger.info("Received %s — beginning graceful shutdown", signame)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            # Windows event loop doesn't support add_signal_handler.
            # Production runs on Linux containers so this is a dev-only
            # concern; fall back to the default ctrl-C behaviour.
            signal.signal(sig, lambda *_: stop_event.set())

    try:
        await run_worker(stop_event=stop_event)
    finally:
        # Flush buffered PostHog + Sentry events and Langfuse observations inside the
        # SIGTERM grace window so a shutdown-adjacent error / score isn't
        # lost with the process. Both best-effort, no-op when disabled.
        shutdown_posthog()
        shutdown_sentry()
        shutdown_langfuse()
        tracing.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_with_signal_handlers())


if __name__ == "__main__":
    main()
