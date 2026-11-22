"""Postgres-backed queue for Buildium webhook events.

Buildium requires a 2xx within 10 seconds, so the receiver writes to this
queue and returns. A separate worker process (see worker.py) claims rows
via ``SELECT ... FOR UPDATE SKIP LOCKED`` and dispatches to handlers.

This removes the need for Redis/BullMQ. The same ``buildium_events`` row
serves as both audit log and work item — status transitions from
``pending`` → ``running`` → ``done``/``failed``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

from helixis.webhooks.event_schemas import dedup_key
from helixis.webhooks.fair_queue import fair_claim_cte
from helixis.webhooks.inbox_filter import surfaces_in_inbox

logger = logging.getLogger(__name__)

#: Planner/summary status for an event the Inbox allowlist deliberately
#: excludes — a LeaseTransaction, an owner-ledger entry, anything
#: ``inbox_filter.INBOX_EVENT_PREFIXES`` does not name.
#:
#: ⚠ These rows were being planned and summarized anyway. The receiver gates
#: enrichment and routing on ``surfaces_in_inbox`` (router.py), but the row
#: landed at the column default ``'pending'`` and the claim queries carry no
#: allowlist — so the worker claimed every one. Measured live 2026-08-29:
#: **400 of 565 rows** are non-inbox families, and they account for **437 of
#: the 484** ``planner_status='failed'`` rows, every one reading "no enrichment
#: handler registered". Two costs, and the second is worse:
#:
#: 1. Background LLM spend on output no surface renders — including summaries
#:    that SUCCEEDED for owner-ledger bookkeeping nobody can read.
#: 2. The one status an operator would check is 90% noise. A genuinely broken
#:    planner row uses the same word as an event that was never eligible.
#:
#: Deliberately a distinct TERMINAL value rather than leaving them 'pending':
#: 'pending' means "waiting to be planned", and nothing will ever plan these,
#: so it is the same class of false statement one word further along. Neither
#: claim predicate matches it (planner claims pending/in_progress/timed_out,
#: summary claims pending/retrying), so the rows are inert by construction —
#: no claim-query change, and they drop out of both partial indexes.
#:
#: Invisible to the user by construction: the Inbox query filters on
#: ``inbox_like_patterns()`` (api/routes/buildium.py), so a row carrying this
#: status is one the frontend never fetches.
NOT_APPLICABLE = "not_applicable"


def _scrub_surface_dict(surface: Any) -> Any:
    """Identifier hygiene on a surface, imported LAZILY.

    Signature mirrors ``scrub_surface_dict``'s own ``Any -> Any`` — it works on
    the raw dict rather than the model, because the model is not the only
    shape that reaches it. Narrowing here would be a claim this wrapper cannot
    honour.

    ⚠ This import must not run at module scope. ``id_hygiene`` is a leaf
    utility, but reaching it executes ``agent/planner/__init__``, which pulls
    the whole planner → enrichment → detectors graph — and ``detectors/
    inbox_bridge`` imports ``enqueue`` back out of THIS module. At module
    scope that closes a cycle, and ``queue`` is the half still initializing:

        main → api.routes.admin → webhooks.handlers → webhooks.queue
             → agent.planner → agent.enrichment → detectors
             → detectors.inbox_bridge → webhooks.queue.enqueue  ✗

    which is a hard `ImportError` at boot. It took production down for three
    days (see the module docstring note in tests/unit/test_app_boots_cold.py)
    while every test passed, because a warmed module graph cannot reproduce
    it. Keep it lazy, or move ``scrub_surface_dict`` out of the planner
    package entirely — do not restore the module-level import.
    """
    from helixis.agent.planner.id_hygiene import scrub_surface_dict

    return scrub_surface_dict(surface)


@dataclass
class EnqueueResult:
    id: UUID
    duplicate: bool


@dataclass
class ClaimedEvent:
    id: UUID
    event_name: str
    event_time: datetime
    account_id: int
    entity_ids: dict[str, int]
    payload: dict[str, Any]
    attempts: int
    company_id: str = ""


async def enqueue(
    pool: asyncpg.Pool,
    *,
    event_name: str,
    event_time: datetime,
    account_id: int,
    entity_ids: dict[str, int],
    payload: dict[str, Any],
    company_id: str | None = None,
    claim_by: str | None = None,
) -> EnqueueResult:
    """Insert an event. Returns duplicate=True if the dedup key already exists.

    Duplicate inserts do not raise — Buildium legitimately redelivers.

    ``company_id`` is the Helixis company UUID — the router resolves it
    from the AccountId before calling. If omitted (e.g. legacy paths)
    it falls back to ``str(account_id)`` for backward compat.

    ``claim_by``: when set, a FRESH row is inserted already claimed
    (``status='running'``) under that name. The webhook receiver uses this
    for its synchronous inline dispatch — without it, the worker's poll
    claims the still-``pending`` row mid-flight and runs the same handler
    a second time in parallel (live-observed on applicant 5045,
    2026-07-05: mirror handlers fetch from Buildium, holding the window
    open for seconds, so the race hit nearly every time → duplicate
    activity lines + doubled Buildium GETs). The caller MUST then either
    ``mark_done`` or ``release_claim``; if it dies first,
    ``reset_stale_running`` requeues the row after the stale timeout.
    ``attempts`` stays 0 so the inline pass never burns the worker's
    retry budget. Duplicates are returned untouched — a redelivery never
    re-claims an existing row.
    """
    key = dedup_key(payload)
    resolved_company_id = company_id if company_id else str(account_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO buildium_events
              (dedup_key, event_name, event_time, account_id, company_id,
               entity_ids, payload, status, claimed_at, claimed_by,
               planner_status, summary_status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb,
                    CASE WHEN $8::text IS NULL THEN 'pending' ELSE 'running' END,
                    CASE WHEN $8::text IS NULL THEN NULL ELSE now() END,
                    $8,
                    CASE WHEN $9 THEN 'pending' ELSE 'not_applicable' END,
                    CASE WHEN $9 THEN 'pending' ELSE 'not_applicable' END)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
            """,
            key,
            event_name,
            event_time,
            account_id,
            resolved_company_id,
            json.dumps(entity_ids),
            json.dumps(payload),
            claim_by,
            # $9 — planner/summary eligibility, decided ONCE here rather than
            # in each of the five enqueue callers. Every non-Buildium producer
            # (email ingest, risk bridge, comms ingest, team tasks) enqueues an
            # inbox-eligible name, so they are unaffected.
            surfaces_in_inbox(event_name),
        )
        if row is not None:
            return EnqueueResult(id=row["id"], duplicate=False)

        # Duplicate — fetch the existing id so the caller still has a handle.
        existing = await conn.fetchrow(
            "SELECT id FROM buildium_events WHERE dedup_key = $1",
            key,
        )
        assert existing is not None, "row disappeared between insert and select"
        return EnqueueResult(id=existing["id"], duplicate=True)


async def claim_batch(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    batch_size: int = 10,
) -> list[ClaimedEvent]:
    """Atomically claim up to ``batch_size`` pending rows for processing.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers can share the
    queue without blocking each other.

    ⚠ Ordering is ROUND-ROBIN BY COMPANY, not global arrival order — see
    ``fair_queue``. A measured 185-event burst from one company used to take
    every slot in every batch until it drained.
    """
    query = (
        fair_claim_cte(
            table="buildium_events",
            where="status = 'pending' AND available_at <= now()",
            order_column="available_at",
        )
        + """
        UPDATE buildium_events b
        SET status      = 'running',
            claimed_at  = now(),
            claimed_by  = $2,
            attempts    = b.attempts + 1
        FROM claimed
        WHERE b.id = claimed.id
        RETURNING b.id, b.event_name, b.event_time, b.account_id,
                  b.company_id, b.entity_ids, b.payload, b.attempts
    """
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, batch_size, worker_id)

    return [
        ClaimedEvent(
            id=r["id"],
            event_name=r["event_name"],
            event_time=r["event_time"],
            account_id=r["account_id"],
            company_id=r["company_id"] or "",
            entity_ids=_ensure_dict(r["entity_ids"]),
            payload=_ensure_dict(r["payload"]),
            attempts=r["attempts"],
        )
        for r in rows
    ]


async def release_claim(pool: asyncpg.Pool, event_id: UUID) -> None:
    """Hand a receiver-claimed row back to the worker (→ ``pending``).

    Used when the receiver's inline dispatch fails after an
    ``enqueue(claim_by=...)`` insert. Guarded on ``status='running'`` so a
    row the receiver already marked done can never be demoted back into
    the queue.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE buildium_events
            SET status = 'pending',
                claimed_at = NULL,
                claimed_by = NULL
            WHERE id = $1 AND status = 'running'
            """,
            event_id,
        )


async def mark_done(pool: asyncpg.Pool, event_id: UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE buildium_events
            SET status = 'done',
                processed_at = now(),
                last_error = NULL
            WHERE id = $1
            """,
            event_id,
        )


async def store_enrichment(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    enrichment: dict[str, Any] | None,
    action_surface: dict[str, Any] | None,
) -> None:
    """Persist the planner output back onto the queue row.

    Defensive about migration drift: migration 0008 adds the two
    columns, but on environments where it hasn't been applied yet
    (production right now) the UPDATE would raise ``UndefinedColumn``.
    We catch and log so a missing migration never sinks the webhook
    receiver — the row stays without enrichment columns and the
    legacy dispatch path keeps working.
    """
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE buildium_events
                SET enrichment     = $2::jsonb,
                    action_surface = $3::jsonb
                WHERE id = $1
                """,
                event_id,
                json.dumps(enrichment) if enrichment is not None else None,
                json.dumps(_scrub_surface_dict(action_surface))
                if action_surface is not None
                else None,
            )
        except asyncpg.exceptions.UndefinedColumnError:
            logger.warning(
                "buildium_events.enrichment / action_surface column missing — "
                "apply supabase/migrations/0008_buildium_events_enrichment_columns.sql"
            )


async def store_routing_decision(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    decision: dict[str, Any],
    deterministic_surface: dict[str, Any] | None,
    enrichment: dict[str, Any] | None = None,
) -> None:
    """Persist the PR #80 routing decision + PR #82 deterministic
    surface + (PR #83) optional enrichment back onto the queue row.

    Always writes ``action_surface`` (the deterministic floor); the
    LLM enrichment path, when it runs, then layers the richer
    LLM-generated surface on top via :func:`coalesce_action_surface`.

    PR #83 — ``enrichment`` is now also optional here so the
    receiver can land the EnrichedEvent BEFORE the planner runs.
    That decouples the planner's silent failure (tracked separately
    as PR #84) from the enrichment data, which we still want on
    the row so the deterministic floor can render a rich summary.

    Defensive about migration drift the same way ``coalesce_action_surface``
    is — if any of the three columns is missing, log + no-op so the
    receiver still returns 200 within Buildium's 10s window.
    """
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE buildium_events
                SET routing_decision = $2::jsonb,
                    action_surface   = COALESCE(action_surface, $3::jsonb),
                    enrichment       = COALESCE($4::jsonb, enrichment)
                WHERE id = $1
                """,
                event_id,
                json.dumps(decision),
                json.dumps(_scrub_surface_dict(deterministic_surface))
                if deterministic_surface is not None
                else None,
                json.dumps(enrichment) if enrichment is not None else None,
            )
        except asyncpg.exceptions.UndefinedColumnError:
            logger.warning(
                "buildium_events.routing_decision / action_surface / enrichment column "
                "missing — apply migrations 0008 + 0016"
            )


async def coalesce_action_surface(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    llm_surface: dict[str, Any] | None,
    deterministic_surface: dict[str, Any] | None,
    enrichment: dict[str, Any] | None = None,
) -> None:
    """Pick the richer of two action surfaces and persist it (PR #82).

    The deterministic surface (from ``build_action_surface_from_routing``)
    is the floor; the LLM surface is the ceiling. We prefer the LLM
    surface IFF it looks valid (has ``summary``, ``actions``,
    ``freeform_placeholder``). Otherwise the deterministic surface
    stays in place — the row is never left without an
    ``action_surface`` once routing has produced one.

    ``enrichment`` is the LLM enrichment dict (also optional) — when
    present it lands in the ``enrichment`` column alongside the
    chosen surface, matching the legacy ``store_enrichment`` contract.
    """

    def _is_valid_surface(s: dict[str, Any] | None) -> bool:
        if not isinstance(s, dict):
            return False
        if "summary" not in s or "actions" not in s or "freeform_placeholder" not in s:
            return False
        if not isinstance(s["actions"], list):
            return False
        return True

    chosen = llm_surface if _is_valid_surface(llm_surface) else deterministic_surface
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE buildium_events
                SET enrichment     = COALESCE($2::jsonb, enrichment),
                    action_surface = $3::jsonb
                WHERE id = $1
                """,
                event_id,
                json.dumps(enrichment) if enrichment is not None else None,
                json.dumps(_scrub_surface_dict(chosen)) if chosen is not None else None,
            )
        except asyncpg.exceptions.UndefinedColumnError:
            logger.warning(
                "buildium_events.enrichment / action_surface column missing — "
                "apply supabase/migrations/0008_buildium_events_enrichment_columns.sql"
            )


async def mark_failed_for_retry(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
    next_available_at: datetime,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE buildium_events
            SET status = 'pending',
                last_error = $2,
                available_at = $3,
                claimed_at = NULL,
                claimed_by = NULL
            WHERE id = $1
            """,
            event_id,
            error,
            next_available_at,
        )


async def mark_enrichment_failed(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
) -> None:
    """Persist a receiver-thread enrichment/planning failure on the row.

    The webhook receiver runs enrichment + planning synchronously
    (``router.py``). When that path raises, the row was never claimed
    by the worker — so we can't reuse ``mark_failed_for_retry`` (which
    assumes the worker has already incremented ``attempts`` and set a
    backoff-scheduled ``available_at``). This helper handles the
    receiver case:

    * ``last_error`` records the exception so a future operator can
      run ``select id, last_error from buildium_events
       where status='pending' and last_error is not null`` and see
      exactly what broke — closing the silent-failure gap audit
      finding #5 identified.
    * ``attempts`` increments by one to record that the receiver
      thread did make an attempt (and failed).
    * ``status`` stays ``'pending'`` so the worker can retry on its
      next pass.
    * ``available_at`` bumps forward by one minute so the worker
      doesn't immediately reclaim a row that just failed; matches the
      first slot of ``_RETRY_DELAYS`` in the worker.

    ⚠ **Guarded on ``status='running'`` and clears the claim stamp, to match
    ``release_claim``.** This is HARDENING, not a fix for anything observed —
    say which, because the difference decides how hard the next reader looks.

    What is true: the receiver inserts with ``claim_by="receiver"`` precisely
    so the worker cannot claim the row mid-flight (the applicant-5045
    double-processing race, live-observed 2026-07-05), and this helper flipped
    it back to ``'pending'`` in the MIDDLE of the receiver's own synchronous
    work — routing, the surface build and ``release_claim`` all come later.

    What is also true, and why this was never a live bug: ``claim_batch``
    requires ``available_at <= now()``, and the one-minute bump above puts the
    row out of reach for 60 s — far past the receiver's own 10 s Buildium ack
    window. The race was closed by the backoff rather than by intent. And
    ``last_error`` is non-null on **0 of 561 rows**, so this function has never
    run in production at all.

    What was genuinely wrong is consistency: both sibling mutators guard on the
    current status (``release_claim`` documents exactly why — "so a row the
    receiver already marked done can never be demoted back into the queue")
    and this one did not, while also half-releasing the claim — leaving
    ``status='pending'`` with ``claimed_by='receiver'``, a state no reader
    expects and the janitor's own comment calls anomalous. A latent trap for
    the first caller who uses this outside today's single narrow call site.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE buildium_events
            SET status       = 'pending',
                claimed_at   = NULL,
                claimed_by   = NULL,
                last_error   = $2,
                attempts     = attempts + 1,
                available_at = now() + interval '1 minute'
            WHERE id = $1 AND status = 'running'
            """,
            event_id,
            error,
        )


async def reset_stale_running(
    pool: asyncpg.Pool,
    *,
    stale_after_seconds: int,
    max_attempts: int,
) -> tuple[int, int]:
    """Janitor for orphaned legacy-queue rows (the gap worker.py's
    docstring promised to close "when we ship one" — this is it).

    ``claim_batch`` flips rows to ``status='running'``; if the worker is
    SIGKILL'd / OOM-killed / crashes mid-batch, those rows stay
    ``running`` forever and the event is silently lost — the planner and
    summary queues have cooldown-based reclaim, but the legacy queue had
    none. This resets any row stuck in ``running`` longer than
    ``stale_after_seconds``:

    * attempts still under ``max_attempts`` → back to ``pending`` so the
      next claim retries it (attempts was already incremented at claim
      time, so repeated worker deaths still march toward the cap);
    * attempts at/over the cap → ``failed``, same terminal state
      ``mark_dead`` uses, so a row that kills the worker every time it
      is claimed cannot crash-loop the queue forever.

    Returns ``(requeued_count, dead_count)`` for the caller's log line.
    ``claimed_at IS NULL`` with ``status='running'`` is an anomalous
    state — treated as stale so it can't wedge permanently.
    """
    async with pool.acquire() as conn:
        dead_rows = await conn.fetch(
            """
            UPDATE buildium_events
               SET status = 'failed',
                   processed_at = now(),
                   last_error = 'stale running row: worker died mid-processing; '
                                'attempts exhausted'
             WHERE status = 'running'
               AND (claimed_at IS NULL
                    OR claimed_at < now() - make_interval(secs => $1))
               AND attempts >= $2
            RETURNING id
            """,
            stale_after_seconds,
            max_attempts,
        )
        requeued_rows = await conn.fetch(
            """
            UPDATE buildium_events
               SET status = 'pending',
                   claimed_at = NULL,
                   claimed_by = NULL,
                   available_at = now(),
                   last_error = 'requeued stale running row: worker died mid-processing'
             WHERE status = 'running'
               AND (claimed_at IS NULL
                    OR claimed_at < now() - make_interval(secs => $1))
            RETURNING id
            """,
            stale_after_seconds,
        )
    return len(requeued_rows), len(dead_rows)


async def mark_dead(pool: asyncpg.Pool, event_id: UUID, *, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE buildium_events
            SET status = 'failed',
                processed_at = now(),
                last_error = $2
            WHERE id = $1
            """,
            event_id,
            error,
        )


def _ensure_dict(value: Any) -> dict[str, Any]:
    """asyncpg returns jsonb as str in some versions, dict in others."""
    if isinstance(value, str):
        # jsonb columns we read here are always objects; cast keeps the
        # declared return type honest (json.loads is typed -> Any).
        return cast(dict[str, Any], json.loads(value))
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    raise TypeError(f"Expected dict/str for jsonb, got {type(value).__name__}")


def utc_now() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────
# PR #89 — planner queue: separate claim loop for the LLM planner.
#
# The legacy worker (claim_batch + status='pending' on the existing
# ``status`` column) handles per-event dispatch and is unchanged. The
# planner runs on its own status column (``planner_status``) so its
# lifecycle is decoupled: the legacy worker can already have marked a
# row ``status='done'`` while the planner is still ``in_progress``.
#
# ``table_name`` is a forward-compat hook. Today only
# ``buildium_events`` exists; once LeadSimple / Gmail land, register
# the table name in ``_PLANNER_QUEUE_TABLES`` and the worker picks up
# pending rows from each. The set guards against SQL injection on the
# f-string interpolation below — the table name is NEVER taken from
# user input.
# ─────────────────────────────────────────────────────────────────────


_PLANNER_QUEUE_TABLES: frozenset[str] = frozenset({"buildium_events"})


def _assert_planner_table(table_name: str) -> None:
    """Belt-and-suspenders SQL-injection guard. The only callers pass
    literals from ``_PLANNER_QUEUE_TABLES`` (worker) or a dict lookup
    that maps user-controlled ``source`` strings to the same allow-list
    (replan endpoint), so this assert is a defensive check, not a
    user-facing validation."""
    if table_name not in _PLANNER_QUEUE_TABLES:
        raise ValueError(
            f"unsupported planner queue table: {table_name!r}; "
            f"expected one of {sorted(_PLANNER_QUEUE_TABLES)}"
        )


@dataclass
class PlannerClaimedRow:
    """A row claimed for planner processing. The worker reconstructs
    an ``EnrichedEvent`` from ``enrichment`` and calls ``planner.plan``
    against it."""

    id: UUID
    event_name: str
    company_id: str
    payload: dict[str, Any]
    enrichment: dict[str, Any] | None
    routing_decision: dict[str, Any] | None
    deterministic_surface: dict[str, Any] | None
    planner_attempts: int


async def claim_planner_batch(
    pool: asyncpg.Pool,
    *,
    batch_size: int = 5,
    max_attempts: int = 5,
    cooldown_seconds: int = 30,
    current_prompt_version: str | None = None,
    table_name: str = "buildium_events",
) -> list[PlannerClaimedRow]:
    """Atomically claim up to ``batch_size`` rows with
    ``planner_status='pending'`` for planner processing.

    Uses ``FOR UPDATE SKIP LOCKED`` so multiple worker replicas don't
    fight. Atomically flips ``planner_status`` to ``'in_progress'``,
    bumps ``planner_attempts``, and stamps ``planner_last_attempted_at``.

    The cooldown reclaim window protects against stuck ``'in_progress'``
    rows: if the worker SIGKILLs mid-plan, the row's
    ``planner_last_attempted_at`` was set, and the next worker waits
    ``cooldown_seconds`` before reclaiming. ``max_attempts`` caps
    retries so a deterministically-failing row eventually stops
    consuming LLM budget; the worker flips such a row to
    ``planner_status='failed'`` once it observes the cap.

    ⚠ **That paragraph was false until 2026-08-29 and this predicate is what
    made it true.** The claim selected ``planner_status = 'pending'`` and
    nothing else, so a row flipped to ``'in_progress'`` and then abandoned —
    SIGKILL, crash, deploy mid-batch — matched no branch and was never
    re-selected. The cooldown only ever gated WHEN a ``pending`` row could be
    re-claimed; it brought nothing back. ``'timed_out'`` was equally
    unreachable, which made ``mark_planner_timed_out`` a fourth terminal
    state rather than a retry. Live-verified on helixis-test: **7 rows stuck
    ``in_progress``, the oldest for 52 days.** A comment cannot fail
    (gotcha 48), so it went on describing a reclaim that did not exist.

    ``'failed'`` stays terminal on purpose — that is the cap doing its job,
    and ``reset_planner_for_replan`` is the deliberate way back. Only the two
    states that mean "we stopped mid-flight" are reclaimable.

    ⚠ The cooldown is now load-bearing in a way it was not before. A row
    being planned RIGHT NOW is also ``'in_progress'`` with a recent
    ``planner_last_attempted_at``, so elapsed time is the ONLY thing
    separating it from an abandoned one — see
    ``worker._PLANNER_RECLAIM_COOLDOWN_SECONDS``, which is derived from the
    planner's own worst-case duration rather than chosen.

    The caller is responsible for setting the final status
    (``'succeeded'`` / ``'timed_out'`` / ``'failed'``) via the
    ``mark_planner_*`` helpers after running the planner.

    PR #107 — ``current_prompt_version``: when set, also claim rows
    whose ``action_surface->>'prompt_version'`` is DISTINCT FROM the
    current version AND whose ``planner_status='succeeded'``. This
    makes bumping ``action_planner.PLANNER_PROMPT_VERSION`` self-
    healing: the worker automatically re-plans stale rows without a
    migration or one-shot script. Stale-version rows respect the
    same ``max_attempts`` and cooldown gates so a bad prompt version
    can't burn infinite LLM budget. Mirrors the PR #99 pattern that
    ``claim_summary_batch`` uses for ``summary_prompt_version``.
    """
    _assert_planner_table(table_name)

    if current_prompt_version is not None:
        query = (
            fair_claim_cte(
                table=table_name,
                where="""(
                     planner_status IN ('pending', 'in_progress', 'timed_out')
                     OR (
                         planner_status = 'succeeded'
                         AND (action_surface->>'prompt_version') IS DISTINCT FROM $4
                     )
                 )
                   AND planner_attempts < $2
                   AND (
                       planner_last_attempted_at IS NULL
                       OR planner_last_attempted_at < now() - make_interval(secs => $3)
                   )
                   -- PR #128 — wait for enrichment to land (15s grace).
                   -- Same race as the summarizer: receiver inserts the
                   -- row BEFORE running Buildium API calls, so the
                   -- worker could claim it for planning while enrichment
                   -- was still NULL → generic "Add note + Create task"
                   -- actions instead of context-aware ones.
                   AND (
                       enrichment IS NOT NULL
                       OR received_at < now() - interval '15 seconds'
                   )""",
                order_column="received_at",
            )
            + f"""
            UPDATE {table_name} t
               SET planner_status            = 'in_progress',
                   planner_attempts          = planner_attempts + 1,
                   planner_last_attempted_at = now()
              FROM claimed
             WHERE t.id = claimed.id
            RETURNING t.id, t.event_name, t.company_id, t.payload, t.enrichment,
                      t.routing_decision, t.action_surface, t.planner_attempts;
        """
        )  # noqa: S608 — table_name is allow-listed above; user input never reaches this string
        rows = await pool.fetch(
            query, batch_size, max_attempts, cooldown_seconds, current_prompt_version
        )
    else:
        query = (
            fair_claim_cte(
                table=table_name,
                where="""planner_status IN ('pending', 'in_progress', 'timed_out')
                   AND planner_attempts < $2
                   AND (
                       planner_last_attempted_at IS NULL
                       OR planner_last_attempted_at < now() - make_interval(secs => $3)
                   )
                   -- PR #128 — see comment above. Same 15s grace.
                   AND (
                       enrichment IS NOT NULL
                       OR received_at < now() - interval '15 seconds'
                   )""",
                order_column="received_at",
            )
            + f"""
            UPDATE {table_name} t
               SET planner_status            = 'in_progress',
                   planner_attempts          = planner_attempts + 1,
                   planner_last_attempted_at = now()
              FROM claimed
             WHERE t.id = claimed.id
            RETURNING t.id, t.event_name, t.company_id, t.payload, t.enrichment,
                      t.routing_decision, t.action_surface, t.planner_attempts;
        """
        )  # noqa: S608 — table_name is allow-listed above; user input never reaches this string
        rows = await pool.fetch(query, batch_size, max_attempts, cooldown_seconds)
    return [
        PlannerClaimedRow(
            id=r["id"],
            event_name=r["event_name"],
            company_id=str(r["company_id"]),
            payload=_ensure_dict(r["payload"]),
            enrichment=_ensure_dict(r["enrichment"]) if r["enrichment"] is not None else None,
            routing_decision=_ensure_dict(r["routing_decision"])
            if r["routing_decision"] is not None
            else None,
            deterministic_surface=_ensure_dict(r["action_surface"])
            if r["action_surface"] is not None
            else None,
            planner_attempts=r["planner_attempts"],
        )
        for r in rows
    ]


async def mark_planner_succeeded(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``planner_status='succeeded'`` and clear any
    prior error. Called by the worker after ``coalesce_action_surface``
    has persisted the LLM-generated surface."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET planner_status = 'succeeded',
               planner_error  = NULL
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id)


async def mark_planner_timed_out(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``planner_status='timed_out'`` with the error
    text. The deterministic floor (already on ``action_surface``)
    stays — the inbox renders the floor + a Replan affordance."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET planner_status = 'timed_out',
               planner_error  = $2
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id, error[:2000])


async def mark_planner_failed(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``planner_status='failed'`` with the error
    text. Used for ``PlannerError`` (LLM returned invalid output) and
    for max-attempts exhaustion. As with timeouts, the deterministic
    floor stays so the inbox always has something to show."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET planner_status = 'failed',
               planner_error  = $2
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id, error[:2000])


async def reset_planner_for_replan(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    company_id: str,
    table_name: str = "buildium_events",
) -> bool:
    """User-triggered: reset ``planner_status`` to ``'pending'`` so
    the worker re-runs the planner. Clears the error and attempts so
    a previously-exhausted row gets a fresh budget.

    Returns True if a row was updated, False if no row matched
    (wrong id or wrong tenant — both surface as 404 at the API
    boundary; we don't disambiguate to avoid leaking event-existence).
    Scoped to the caller's company via the WHERE clause."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET planner_status            = 'pending',
               planner_error             = NULL,
               planner_attempts          = 0,
               planner_last_attempted_at = NULL
         WHERE id = $1
           AND company_id = $2::uuid
        RETURNING id
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, event_id, company_id)
    return row is not None


# ─────────────────────────────────────────────────────────────────────
# PR #92 — summary queue: sibling claim loop for the agent-written
# narrative the Inbox detail pane renders above the field grid.
#
# Independent of the planner queue in every dimension: a row can have
# ``planner_status='succeeded'`` and ``summary_status='pending'`` at
# the same time, and that's the expected state for the first ~30s
# after a webhook lands (planner is usually faster than the summarizer
# because the summarizer prompt is bigger).
#
# Same SQL-injection allow-list pattern as planner.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SummaryClaimedRow:
    """A row claimed for summary processing. The worker hands
    ``payload`` + ``enrichment`` to ``Summarizer.summarize`` and
    persists the resulting text via ``mark_summary_succeeded``."""

    id: UUID
    event_name: str
    company_id: str
    event_time: datetime
    payload: dict[str, Any]
    enrichment: dict[str, Any] | None
    summary_attempts: int


async def claim_summary_batch(
    pool: asyncpg.Pool,
    *,
    batch_size: int = 5,
    max_attempts: int = 5,
    cooldown_seconds: int = 30,
    current_prompt_version: str | None = None,
    table_name: str = "buildium_events",
) -> list[SummaryClaimedRow]:
    """Atomically claim up to ``batch_size`` rows with
    ``summary_status IN ('pending', 'retrying')`` for summarizer
    processing.

    Same FOR UPDATE SKIP LOCKED + cooldown + max_attempts shape as
    ``claim_planner_batch``. ``'retrying'`` is included alongside
    ``'pending'`` because a transient LLM error flips the row to
    ``'retrying'`` (not ``'failed'``) until attempts exhaust — see
    ``mark_summary_retrying``.

    The claim itself does NOT flip the status (unlike planner's
    ``in_progress`` interim state) — we leave ``summary_status`` as
    it was and stamp ``summary_last_attempted_at`` so the cooldown
    window does the equivalent work. This keeps the row's external
    status legible to the inbox: a polling client sees ``'pending'``
    or ``'retrying'`` for the full duration of the LLM call rather
    than briefly seeing a third interim state.

    PR #99 — ``current_prompt_version``: when set, also claim rows
    whose ``summary_prompt_version IS DISTINCT FROM`` the current
    version AND whose ``summary_status = 'succeeded'``. This makes
    bumping ``SUMMARY_PROMPT_VERSION`` self-healing: the worker
    automatically re-summarizes stale rows without a migration or
    one-shot script. The stale-version rows still respect the
    ``max_attempts`` and cooldown gates so a bad prompt version
    can't burn infinite LLM budget.
    """
    _assert_planner_table(table_name)

    if current_prompt_version is not None:
        query = (
            fair_claim_cte(
                table=table_name,
                where="""(
                     summary_status IN ('pending', 'retrying')
                     OR (
                         summary_status = 'succeeded'
                         AND summary_prompt_version IS DISTINCT FROM $4
                     )
                 )
                   AND summary_attempts < $2
                   AND (
                       summary_last_attempted_at IS NULL
                       OR summary_last_attempted_at < now() - make_interval(secs => $3)
                   )
                   -- Wait for enrichment to land. The receiver inserts the
                   -- row with enrichment=NULL, then runs Buildium API calls
                   -- (~1-3s), then UPDATES the row with enrichment. Claiming
                   -- between the INSERT and the UPDATE makes the summarizer
                   -- run against NULL enrichment and write "A new task was
                   -- created..." (PR #128).
                   --
                   -- ⚠ PR #128's 15s wall-clock grace does NOT hold for a row
                   -- the receiver never enriched. There, enrichment is written
                   -- by the PLANNER (worker.py's re-enrich branch) — and the
                   -- planner and the summarizer are claimed in the SAME poll
                   -- cycle, ~200ms apart, while the planner's Buildium calls
                   -- take seconds. Measured 2026-08-25: planner claimed
                   -- 02:13:09.495, summary claimed 02:13:09.685, row 39s old.
                   -- The grace had long expired, so the summarizer won the
                   -- race every time and wrote "the enrichment didn't return a
                   -- property, unit, reporter, or description" onto cards
                   -- whose enrichment landed seconds later and had all four.
                   --
                   -- A wall clock cannot express "has the producer had its
                   -- turn yet", so ask that directly: hold while the planner
                   -- is still pending or running, because that is exactly when
                   -- enrichment is in flight. Once the planner has RESOLVED,
                   -- a still-null enrichment means none is coming (no handler
                   -- for this event_name) and payload-only is the honest
                   -- summary. The wall-clock arm survives only as an
                   -- anti-stuck valve, widened so it cannot pre-empt a planner
                   -- that is merely slow.
                   AND (
                       enrichment IS NOT NULL
                       OR planner_status NOT IN ('pending', 'in_progress')
                       OR received_at < now() - interval '5 minutes'
                   )""",
                order_column="received_at",
            )
            + f"""
            UPDATE {table_name} t
               SET summary_attempts          = summary_attempts + 1,
                   summary_last_attempted_at = now()
              FROM claimed
             WHERE t.id = claimed.id
            RETURNING t.id, t.event_name, t.company_id, t.event_time,
                      t.payload, t.enrichment, t.summary_attempts;
        """
        )  # noqa: S608 — table_name is allow-listed above; user input never reaches this string
        rows = await pool.fetch(
            query, batch_size, max_attempts, cooldown_seconds, current_prompt_version
        )
    else:
        query = (
            fair_claim_cte(
                table=table_name,
                where="""summary_status IN ('pending', 'retrying')
                   AND summary_attempts < $2
                   AND (
                       summary_last_attempted_at IS NULL
                       OR summary_last_attempted_at < now() - make_interval(secs => $3)
                   )
                   -- See the comment on the versioned query above: hold
                   -- while the planner is still producing the enrichment.
                   AND (
                       enrichment IS NOT NULL
                       OR planner_status NOT IN ('pending', 'in_progress')
                       OR received_at < now() - interval '5 minutes'
                   )""",
                order_column="received_at",
            )
            + f"""
            UPDATE {table_name} t
               SET summary_attempts          = summary_attempts + 1,
                   summary_last_attempted_at = now()
              FROM claimed
             WHERE t.id = claimed.id
            RETURNING t.id, t.event_name, t.company_id, t.event_time,
                      t.payload, t.enrichment, t.summary_attempts;
        """
        )  # noqa: S608 — table_name is allow-listed above; user input never reaches this string
        rows = await pool.fetch(query, batch_size, max_attempts, cooldown_seconds)

    return [
        SummaryClaimedRow(
            id=r["id"],
            event_name=r["event_name"],
            company_id=str(r["company_id"]),
            event_time=r["event_time"],
            payload=_ensure_dict(r["payload"]),
            enrichment=_ensure_dict(r["enrichment"]) if r["enrichment"] is not None else None,
            summary_attempts=r["summary_attempts"],
        )
        for r in rows
    ]


async def mark_summary_succeeded(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    text: str,
    prompt_version: str,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``summary_status='succeeded'`` with the text
    + prompt version stamped on the row. Clears any prior error."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET summary_status         = 'succeeded',
               summary_text           = $2,
               summary_prompt_version = $3,
               summary_error          = NULL
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id, text, prompt_version)


async def mark_summary_retrying(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``summary_status='retrying'`` with the error
    text. Use this for transient errors (litellm.Timeout / 5xx /
    RateLimit) when attempts < max — the claim loop will pick it
    back up after the cooldown window."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET summary_status = 'retrying',
               summary_error  = $2
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id, error[:2000])


async def mark_summary_failed(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    error: str,
    table_name: str = "buildium_events",
) -> None:
    """Flip the row to ``summary_status='failed'`` with the error
    text. Used for sanitizer rejections (non-retryable) and for
    max-attempts exhaustion."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET summary_status = 'failed',
               summary_error  = $2
         WHERE id = $1
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        await conn.execute(query, event_id, error[:2000])


async def reset_summary_for_resummarize(
    pool: asyncpg.Pool,
    event_id: UUID,
    *,
    company_id: str,
    table_name: str = "buildium_events",
) -> bool:
    """User-triggered: reset ``summary_status`` to ``'pending'`` so
    the worker re-runs the summarizer. Mirrors
    ``reset_planner_for_replan`` exactly so the API endpoint shape
    is symmetric.

    Returns True if a row was updated, False if no row matched
    (wrong id or wrong tenant)."""
    _assert_planner_table(table_name)
    query = f"""
        UPDATE {table_name}
           SET summary_status            = 'pending',
               summary_error             = NULL,
               summary_attempts          = 0,
               summary_last_attempted_at = NULL
         WHERE id = $1
           AND company_id = $2::uuid
        RETURNING id
    """  # noqa: S608 — table_name allow-listed
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, event_id, company_id)
    return row is not None


__all__ = [
    "ClaimedEvent",
    "EnqueueResult",
    "PlannerClaimedRow",
    "SummaryClaimedRow",
    "claim_batch",
    "claim_planner_batch",
    "claim_summary_batch",
    "enqueue",
    "mark_dead",
    "mark_done",
    "mark_enrichment_failed",
    "mark_failed_for_retry",
    "mark_planner_failed",
    "mark_planner_succeeded",
    "mark_planner_timed_out",
    "mark_summary_failed",
    "mark_summary_retrying",
    "mark_summary_succeeded",
    "reset_planner_for_replan",
    "reset_stale_running",
    "reset_summary_for_resummarize",
    "store_enrichment",
    "utc_now",
]
