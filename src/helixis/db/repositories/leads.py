"""CRM lead repository — thin SQL over ``public.crm_leads``.

Feeds the CRM pipeline board. Scaffolded behind ``HELIXIS_CRM__ENABLED``;
the ingest path that populates leads ships later. Company-scoped on every
query (``company_id`` is the tenancy boundary; RLS on the table is the
second line of defense for the raw-SQL caller — see 0028_crm_leads.sql).

Keep this free of business logic: it just translates a ``LeadRow`` to SQL
and back. The stage-move rules (reply-sent → contacted, calendar → …)
live in the pipeline layer, not here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# Mirrors the CHECK constraints in 0028_crm_leads.sql. Kept here so a bad
# value is caught before it hits the DB rather than as an opaque asyncpg
# error. The migration's CHECK is still the source of truth.
LEAD_STAGES: tuple[str, ...] = ("new", "contacted", "scheduled", "applied", "leased")
LEAD_KINDS: tuple[str, ...] = ("renter", "owner")
LEAD_SOURCES: tuple[str, ...] = (
    "zillow",
    "apartments",
    "referral",
    "web_form",
    "email",
    "sms",
    "manual",  # hand-entered by the PM (0031)
    "buildium",  # auto-created from a Buildium applicant webhook (0061)
)
# Pipeline membership (0031). 'lost' drops off the board but is kept for records.
LEAD_STATUSES: tuple[str, ...] = ("active", "lost")


@dataclass(frozen=True)
class MessageRow:
    """A single message in a lead's conversation (crm_lead_messages)."""

    company_id: str
    lead_id: str
    direction: str  # 'inbound' | 'outbound'
    channel: str  # 'sms' | 'email'
    body: str
    external_id: str | None = None
    raw: dict[str, Any] | None = None
    id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class LeadRow:
    """Shape of a single CRM lead row. Mirrors the frontend ``CrmLead``
    (minus the presentational fields the API derives)."""

    company_id: str
    """Helixis company UUID. Required — RLS / multi-tenancy live here."""

    name: str
    kind: str  # 'renter' | 'owner'
    stage: str  # one of LEAD_STAGES
    source: str  # one of LEAD_SOURCES
    status: str = "active"  # one of LEAD_STATUSES (0031); 'lost' = off the board
    email: str | None = None
    phone: str | None = None
    unit_label: str | None = None
    note: str | None = None
    raw: dict[str, Any] | None = None
    # Cached AI assist (migration 0030): "what Helixis noticed" signals +
    # a drafted first reply. None until generated.
    signals: list[str] | None = None
    draft_reply: str | None = None
    # Book-a-tour (migration 0036): the scheduled tour datetime (ISO string on
    # read) + the Google Calendar event id. None until a tour is booked.
    tour_at: str | None = None
    calendar_event_id: str | None = None
    # Soft Buildium applicant link (migration 0040). Set when an applicant
    # webhook/backfill matches this lead; makes later matches sticky.
    buildium_applicant_id: int | None = None
    id: str | None = None  # set on rows read back from the DB


async def insert_lead(pool: asyncpg.Pool, lead: LeadRow) -> str | None:
    """Insert a lead and return its new id, or ``None`` on failure.

    Best-effort: a failed insert logs and returns ``None`` rather than
    raising, so an ingest cycle processing a batch of leads isn't taken
    down by one malformed row.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.crm_leads
                    (company_id, kind, stage, source, name, email, phone,
                     unit_label, note, raw)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                RETURNING id::text AS id
                """,
                lead.company_id,
                lead.kind,
                lead.stage,
                lead.source,
                lead.name,
                lead.email,
                lead.phone,
                lead.unit_label,
                lead.note,
                json.dumps(lead.raw or {}),
            )
        return row["id"] if row else None
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to insert CRM lead (company=%s source=%s)",
            lead.company_id,
            lead.source,
        )
        return None


def _row_to_lead(r: asyncpg.Record) -> LeadRow:
    # Every SELECT that builds a LeadRow uses _LEAD_COLUMNS, so signals /
    # draft_reply are always present (possibly NULL).
    signals_raw: Any = r["signals"]
    if isinstance(signals_raw, str):
        signals_raw = json.loads(signals_raw)
    return LeadRow(
        id=r["id"],
        company_id=r["company_id"],
        kind=r["kind"],
        stage=r["stage"],
        source=r["source"],
        status=r["status"],
        name=r["name"],
        email=r["email"],
        phone=r["phone"],
        unit_label=r["unit_label"],
        note=r["note"],
        raw=json.loads(r["raw"]) if isinstance(r["raw"], str) else r["raw"],
        signals=list(signals_raw) if isinstance(signals_raw, list) else None,
        draft_reply=r["draft_reply"],
        tour_at=r["tour_at"].isoformat() if r["tour_at"] is not None else None,
        calendar_event_id=r["calendar_event_id"],
        buildium_applicant_id=r["buildium_applicant_id"],
    )


_LEAD_COLUMNS = (
    "id::text AS id, company_id::text AS company_id, kind, stage, "
    "source, status, name, email, phone, unit_label, note, raw, signals, draft_reply, "
    "tour_at, calendar_event_id, buildium_applicant_id"
)


async def list_leads(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    stage: str | None = None,
    query: str | None = None,
    status: str | None = "active",
    limit: int = 200,
    offset: int = 0,
) -> list[LeadRow]:
    """Return the company's leads, newest first. Company-scoped, with an
    optional ``stage`` filter, a case-insensitive ``query`` over
    name/phone/unit/note, and pagination — the stage-detail list's backing
    query. ``stage`` is validated against LEAD_STAGES (an unknown stage
    yields no rows rather than an error).

    ``status`` defaults to ``"active"`` so the board hides won't-pursue
    (``lost``) leads; pass ``status=None`` to include every status (the
    records view) or ``status="lost"`` to list only dropped leads. An
    unknown status yields no rows."""
    if stage is not None and stage not in LEAD_STAGES:
        return []
    if status is not None and status not in LEAD_STATUSES:
        return []
    conds = ["company_id = $1::uuid"]
    params: list[object] = [company_id]
    if status is not None:
        params.append(status)
        conds.append(f"status = ${len(params)}")
    if stage is not None:
        params.append(stage)
        conds.append(f"stage = ${len(params)}")
    if query:
        params.append(f"%{query.strip()}%")
        i = len(params)
        conds.append(
            f"(name ILIKE ${i} OR phone ILIKE ${i} OR unit_label ILIKE ${i} OR note ILIKE ${i})"
        )
    params.append(limit)
    limit_i = len(params)
    params.append(offset)
    offset_i = len(params)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE {" AND ".join(conds)}
             ORDER BY created_at DESC
             LIMIT ${limit_i} OFFSET ${offset_i}
            """,
            *params,
        )
    return [_row_to_lead(r) for r in rows]


async def find_lead_by_email(pool: asyncpg.Pool, *, company_id: str, email: str) -> LeadRow | None:
    """Find an existing lead by email within the company — the email dedup
    key so a repeat inquiry from the same address updates its lead instead of
    spawning a new one. Returns the most-recent match or ``None``."""
    if not email:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE company_id = $1::uuid AND lower(email) = lower($2)
             ORDER BY created_at DESC
             LIMIT 1
            """,
            company_id,
            email,
        )
    return _row_to_lead(row) if row else None


async def count_leads_by_stage(pool: asyncpg.Pool, *, company_id: str) -> dict[str, int]:
    """Count leads per stage for the company. Every stage in LEAD_STAGES is
    present in the result (0 when empty), so the overview never has holes."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT stage, count(*)::int AS n
              FROM public.crm_leads
             WHERE company_id = $1::uuid AND status = 'active'
             GROUP BY stage
            """,
            company_id,
        )
    counts = {stage: 0 for stage in LEAD_STAGES}
    for r in rows:
        if r["stage"] in counts:
            counts[r["stage"]] = r["n"]
    return counts


async def recent_leads_by_stage(
    pool: asyncpg.Pool, *, company_id: str, per_stage: int = 3
) -> list[LeadRow]:
    """The ``per_stage`` newest leads in EACH stage (one query, window
    function). Returned flat, newest-first within each stage; the caller
    groups by ``.stage``."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_LEAD_COLUMNS} FROM (
                SELECT {_LEAD_COLUMNS}, created_at,
                       row_number() OVER (PARTITION BY stage ORDER BY created_at DESC) AS rn
                  FROM public.crm_leads
                 WHERE company_id = $1::uuid AND status = 'active'
            ) ranked
             WHERE rn <= $2
             ORDER BY stage, created_at DESC
            """,
            company_id,
            per_stage,
        )
    return [_row_to_lead(r) for r in rows]


# A lead is "going cold" when it's still in play (active, not leased) but has
# had no activity — no inbound/outbound message, and it wasn't just created —
# for this many days. Surfaced so a PM can follow up before it slips.
COLD_LEAD_DAYS = 3

# Shared SQL for a lead's last activity: the most recent of its creation and
# its latest message. A never-messaged lead falls back to created_at, so a
# brand-new lead nobody has replied to also ages into "cold".
_LAST_ACTIVITY_SQL = (
    "greatest(created_at, coalesce("
    "(SELECT max(m.created_at) FROM public.crm_lead_messages m "
    "WHERE m.lead_id = crm_leads.id), created_at))"
)


async def count_cold_leads(
    pool: asyncpg.Pool, *, company_id: str, threshold_days: int = COLD_LEAD_DAYS
) -> int:
    """Count the company's going-cold leads (active, not leased, quiet for
    ``threshold_days``). The overview badges this so the PM sees at a glance
    how many need a nudge."""
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            f"""
            SELECT count(*)::int
              FROM public.crm_leads
             WHERE company_id = $1::uuid
               AND status = 'active'
               AND stage <> 'leased'
               AND {_LAST_ACTIVITY_SQL} < now() - make_interval(days => $2)
            """,
            company_id,
            threshold_days,
        )
    return int(n or 0)


async def cold_leads(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    threshold_days: int = COLD_LEAD_DAYS,
    limit: int = 100,
) -> list[LeadRow]:
    """The going-cold leads themselves, coldest (longest-quiet) first — the
    backing query for the "needs a nudge" list. Active + not leased; a lost or
    leased lead is out of play and never nags."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE company_id = $1::uuid
               AND status = 'active'
               AND stage <> 'leased'
               AND {_LAST_ACTIVITY_SQL} < now() - make_interval(days => $2)
             ORDER BY {_LAST_ACTIVITY_SQL} ASC
             LIMIT $3
            """,
            company_id,
            threshold_days,
            limit,
        )
    return [_row_to_lead(r) for r in rows]


@dataclass(frozen=True)
class LeadMetrics:
    """This-month + lifetime pipeline metrics for the overview analytics."""

    new_this_month: int
    new_prev_month: int
    replies_this_month: int
    active: int  # in pipeline (not leased)
    leased: int
    total: int


async def lead_metrics(pool: asyncpg.Pool, *, company_id: str) -> LeadMetrics:
    """Compute the overview analytics from our own tables (no Buildium).
    Month boundaries use the DB clock (``date_trunc('month', now())``)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              count(*) FILTER (WHERE created_at >= date_trunc('month', now()))::int
                AS new_this_month,
              count(*) FILTER (
                WHERE created_at >= date_trunc('month', now()) - interval '1 month'
                  AND created_at <  date_trunc('month', now())
              )::int AS new_prev_month,
              count(*) FILTER (WHERE stage <> 'leased' AND status = 'active')::int AS active,
              count(*) FILTER (WHERE stage = 'leased')::int AS leased,
              count(*)::int AS total
              FROM public.crm_leads
             WHERE company_id = $1::uuid
            """,
            company_id,
        )
        replies = await conn.fetchval(
            """
            SELECT count(*)::int
              FROM public.crm_lead_messages
             WHERE company_id = $1::uuid
               AND direction = 'outbound'
               AND created_at >= date_trunc('month', now())
            """,
            company_id,
        )
    return LeadMetrics(
        new_this_month=row["new_this_month"] if row else 0,
        new_prev_month=row["new_prev_month"] if row else 0,
        replies_this_month=int(replies or 0),
        active=row["active"] if row else 0,
        leased=row["leased"] if row else 0,
        total=row["total"] if row else 0,
    )


async def get_lead(pool: asyncpg.Pool, *, company_id: str, lead_id: str) -> LeadRow | None:
    """Fetch one lead by id, scoped to the company. ``None`` if missing or
    cross-tenant (the WHERE filters both)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE company_id = $1::uuid AND id = $2::uuid
            """,
            company_id,
            lead_id,
        )
    return _row_to_lead(row) if row else None


async def find_lead_by_phone(pool: asyncpg.Pool, *, company_id: str, phone: str) -> LeadRow | None:
    """Find an existing lead by phone within the company — the SMS dedup
    key so a returning texter updates their lead instead of spawning a new
    one. Returns the most-recent match or ``None``."""
    if not phone:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE company_id = $1::uuid AND phone = $2
             ORDER BY created_at DESC
             LIMIT 1
            """,
            company_id,
            phone,
        )
    return _row_to_lead(row) if row else None


async def find_lead_by_applicant(
    pool: asyncpg.Pool, *, company_id: str, buildium_applicant_id: int
) -> LeadRow | None:
    """Find the lead already linked to a Buildium applicant (0040). The
    sticky match: once linked, applicant webhooks resolve the lead by id even
    if the email/phone on either side is later edited."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_LEAD_COLUMNS}
              FROM public.crm_leads
             WHERE company_id = $1::uuid AND buildium_applicant_id = $2
             ORDER BY created_at DESC
             LIMIT 1
            """,
            company_id,
            buildium_applicant_id,
        )
    return _row_to_lead(row) if row else None


async def link_lead_applicant(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, buildium_applicant_id: int
) -> None:
    """Attach a Buildium applicant id to a lead (0040). Best-effort — the
    link is an enrichment; a failure must not fail the webhook that found
    the match."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.crm_leads
                   SET buildium_applicant_id = $3, updated_at = now()
                 WHERE company_id = $1::uuid AND id = $2::uuid
                """,
                company_id,
                lead_id,
                buildium_applicant_id,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to link lead to Buildium applicant (lead=%s applicant=%s)",
            lead_id,
            buildium_applicant_id,
        )


async def update_stage(pool: asyncpg.Pool, *, company_id: str, lead_id: str, stage: str) -> None:
    """Move a lead to ``stage`` (company-scoped). Best-effort — a failed
    stage bump must not fail the action that triggered it (e.g. a reply
    that already sent)."""
    if stage not in LEAD_STAGES:
        logger.warning("Refusing to set unknown lead stage: %s", stage)
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.crm_leads
                   SET stage = $3, updated_at = now()
                 WHERE company_id = $1::uuid AND id = $2::uuid
                """,
                company_id,
                lead_id,
                stage,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to update lead stage (lead=%s stage=%s)", lead_id, stage)


async def set_lead_status(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, status: str
) -> bool:
    """Set a lead's ``status`` — 'lost' drops it off the board (kept for
    records), 'active' restores it. Company-scoped. Returns ``True`` when a
    row was updated, ``False`` on an unknown status or no matching lead (so
    the caller can 404 a bad id). Unlike ``update_stage`` this raises on a DB
    error — it's a user-initiated action whose failure the caller surfaces,
    not a best-effort side effect."""
    if status not in LEAD_STATUSES:
        logger.warning("Refusing to set unknown lead status: %s", status)
        return False
    async with pool.acquire() as conn:
        result: str = await conn.execute(
            """
            UPDATE public.crm_leads
               SET status = $3, updated_at = now()
             WHERE company_id = $1::uuid AND id = $2::uuid
            """,
            company_id,
            lead_id,
            status,
        )
    # asyncpg returns e.g. "UPDATE 1" — the trailing count is rows affected.
    return result.rsplit(" ", 1)[-1] != "0"


async def set_lead_draft(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lead_id: str,
    signals: list[str],
    draft_reply: str,
) -> None:
    """Persist the generated AI assist (signals + drafted reply) on a lead,
    stamping ``draft_generated_at``. Company-scoped. Best-effort — a failed
    cache write must not fail the read that triggered generation (the draft
    is still returned to the caller in-memory)."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.crm_leads
                   SET signals = $3::jsonb,
                       draft_reply = $4,
                       draft_generated_at = now(),
                       updated_at = now()
                 WHERE company_id = $1::uuid AND id = $2::uuid
                """,
                company_id,
                lead_id,
                json.dumps(signals),
                draft_reply,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to cache lead draft (lead=%s)", lead_id)


async def set_lead_signals(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, signals: list[str]
) -> None:
    """Cache the "what Helixis noticed" signals on a lead WITHOUT touching
    ``draft_reply``. Used on first contact, when the drafted reply comes from
    the company's default template (not the LLM) — so leaving ``draft_reply``
    NULL lets the conversational draft still generate once a reply goes out.
    Best-effort: a failed cache write must not fail the drawer that triggered
    generation (the signals are still returned in-memory)."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.crm_leads
                   SET signals = $3::jsonb,
                       draft_generated_at = now(),
                       updated_at = now()
                 WHERE company_id = $1::uuid AND id = $2::uuid
                """,
                company_id,
                lead_id,
                json.dumps(signals),
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to cache lead signals (lead=%s)", lead_id)


async def clear_lead_assist(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, reason: str
) -> None:
    """Drop the cached signals + drafted reply so the next read recomputes.

    The assist is a WORKING picture of one lead, not a durable fact, and it has
    exactly two moments where it stops being true:

    * **The lead said something new.** ``get_lead_draft`` returns the cached
      draft whenever it is non-null, and nothing invalidated it — so a lead who
      texted three more times (a new budget, a dog, a changed date) still got
      the signals and the reply worked out from their first sentence. Frozen,
      not current.
    * **The lead is done.** A drafted reply on a leased or lost lead is clutter
      pointing at a conversation nobody will continue, and it is the personal
      data of that conversation kept past its purpose.

    Best-effort, like its sibling setters: a failed clear must never fail the
    inbound webhook or the stage change that triggered it. Worst case the stale
    assist survives one more read, which is where it was already.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.crm_leads
                   SET signals = NULL,
                       draft_reply = NULL,
                       draft_generated_at = NULL,
                       updated_at = now()
                 WHERE company_id = $1::uuid AND id = $2::uuid
                """,
                company_id,
                lead_id,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to clear lead assist (lead=%s reason=%s)", lead_id, reason)


async def insert_message(pool: asyncpg.Pool, message: MessageRow) -> str | None:
    """Append a message to a lead's conversation. Idempotent on
    ``(company_id, external_id)`` — a redelivered Twilio webhook with the
    same MessageSid returns the existing id instead of duplicating."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.crm_lead_messages
                    (company_id, lead_id, direction, channel, body, external_id, raw)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb)
                ON CONFLICT (company_id, external_id) WHERE external_id IS NOT NULL
                DO UPDATE SET body = public.crm_lead_messages.body
                RETURNING id::text AS id
                """,
                message.company_id,
                message.lead_id,
                message.direction,
                message.channel,
                message.body,
                message.external_id,
                json.dumps(message.raw or {}),
            )
        # ⚠ HERE, not at the call sites. The assist is a working picture of a
        # conversation, so every message that lands makes it a little less
        # true — and there are four places messages arrive (inbound SMS,
        # inbound call, the outbound tail, email). Clearing at each is the
        # fan-out that a fifth one silently reopens, which is how the assist
        # came to be frozen at first contact in the first place.
        #
        # Both directions clear, for different reasons. An INBOUND message is
        # new information the signals have not seen. An OUTBOUND one means the
        # drafted reply has been SENT — leaving it cached re-offers the message
        # you just sent as the thing to send next.
        #
        # The cost is bounded by drawer opens, not by messages: nothing
        # regenerates until somebody looks.
        if row:
            await clear_lead_assist(
                pool,
                company_id=message.company_id,
                lead_id=message.lead_id,
                reason=f"{message.direction} message",
            )
        return row["id"] if row else None
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to insert lead message (lead=%s direction=%s)",
            message.lead_id,
            message.direction,
        )
        return None


async def set_message_transcription(
    pool: asyncpg.Pool, *, company_id: str, external_id: str, transcription: str
) -> bool:
    """Attach a transcription to an existing voicemail message, matched by
    ``(company_id, external_id)`` (the RecordingSid). Merges into ``raw`` so
    the recording_url/duration already stored survive. Best-effort — returns
    whether a matching row was updated (the transcribe callback can arrive
    before or after the recording callback; a miss is retried by neither, so
    we simply drop it rather than fail)."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE public.crm_lead_messages
                   SET raw = COALESCE(raw, '{}'::jsonb)
                             || jsonb_build_object('transcription', $3::text)
                 WHERE company_id = $1::uuid AND external_id = $2
                RETURNING id::text AS id
                """,
                company_id,
                external_id,
                transcription,
            )
        return row is not None
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to attach transcription (company=%s external_id=%s)",
            company_id,
            external_id,
        )
        return False


async def list_messages(
    pool: asyncpg.Pool, *, company_id: str, lead_id: str, limit: int = 200
) -> list[MessageRow]:
    """Return a lead's conversation, oldest first (thread order)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, company_id::text AS company_id,
                   lead_id::text AS lead_id, direction, channel, body,
                   external_id, raw, created_at::text AS created_at
              FROM public.crm_lead_messages
             WHERE company_id = $1::uuid AND lead_id = $2::uuid
             ORDER BY created_at ASC
             LIMIT $3
            """,
            company_id,
            lead_id,
            limit,
        )
    return [
        MessageRow(
            id=r["id"],
            company_id=r["company_id"],
            lead_id=r["lead_id"],
            direction=r["direction"],
            channel=r["channel"],
            body=r["body"],
            external_id=r["external_id"],
            raw=json.loads(r["raw"]) if isinstance(r["raw"], str) else r["raw"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
