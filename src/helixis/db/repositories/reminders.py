"""Inbox reminders (migration 0045) — storage + lifecycle.

The sweep hands each producer's CURRENT candidate set to
:func:`sync_producer`, which enforces the lifecycle in SQL:

* new candidate            → insert ``open``
* still-true candidate     → content/escalation refresh in place (ONE card
  per (producer, entity_key) — escalation never stacks cards)
* snoozed, not yet due     → stays snoozed (content refreshes silently)
* snoozed, past due        → reopens (the sweep only emits candidates whose
  condition still holds, so "recheck before return" is structural)
* dismissed                → stays hidden while the condition persists
* handled, inside cooldown → stays hidden
* handled, past cooldown   → reopens; see ``reminders/aging.py``
* candidate gone           → ``cleared`` (the desk drains itself) — from ANY
  status, so a dismissed instance that recurs later starts a fresh cycle

⚠ ``handled`` used to be permanent, while 0045's own header comment said it
was "suppressed for the producer's cooldown". No cooldown existed and
``handled_at`` was written by nothing that read it, so "I dealt with it on the
phone" silenced a card forever — including when the claim turned out to be
wrong. The cooldown described there now exists; the ladder that makes an
untouched card louder lives beside it. Both are in ``reminders/aging.py``, and
the SQL arms below are GENERATED from those constants rather than restated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from helixis.reminders.aging import (
    HANDLED_COOLDOWN_DAYS,
    age_escalation_sql,
    age_severity_sql,
)

VALID_USER_STATUSES = ("snoozed", "dismissed", "handled")


@dataclass
class ReminderCandidate:
    """What a producer emits for one entity whose condition holds NOW."""

    entity_key: str
    title: str
    severity: str = "normal"  # low | normal | high
    body: str | None = None
    link_path: str | None = None
    escalation: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReminderRow:
    id: str
    producer: str
    entity_key: str
    status: str
    severity: str
    title: str
    body: str | None
    link_path: str | None
    payload: dict[str, Any]
    snoozed_until: datetime | None
    escalation: int
    first_fired_at: datetime | None
    last_fired_at: datetime | None
    #: When the card was last marked handled. Written since 0045 and read by
    #: NOTHING until the cooldown existed — the column that would have
    #: implemented the guarantee 0045's comment already claimed.
    handled_at: datetime | None = None


def _row(r: asyncpg.Record) -> ReminderRow:
    payload = r["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return ReminderRow(
        id=str(r["id"]),
        producer=r["producer"],
        entity_key=r["entity_key"],
        status=r["status"],
        severity=r["severity"],
        title=r["title"],
        body=r["body"],
        link_path=r["link_path"],
        payload=payload or {},
        snoozed_until=r["snoozed_until"],
        escalation=r["escalation"],
        first_fired_at=r["first_fired_at"],
        last_fired_at=r["last_fired_at"],
        # ``SELECT *`` on both readers, but a test double may not carry it.
        handled_at=r["handled_at"] if "handled_at" in r else None,
    )


async def sync_producer(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    producer: str,
    candidates: list[ReminderCandidate],
) -> None:
    """Reconcile one producer's cards against its current candidate set."""
    async with pool.acquire() as conn:
        for c in candidates:
            await _upsert_one(conn, company_id=company_id, producer=producer, c=c)
        # Self-clear: anything this producer previously tracked whose
        # condition no longer holds. From ANY status — a dismissed
        # instance that ends is over; a future recurrence starts fresh.
        await conn.execute(
            """
            UPDATE helixis_reminders
               SET status = 'cleared', updated_at = now()
             WHERE company_id = $1::uuid AND producer = $2
               AND status != 'cleared'
               AND NOT (entity_key = ANY($3::text[]))
            """,
            company_id,
            producer,
            [c.entity_key for c in candidates],
        )


async def clear_unknown_producers(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    known: tuple[str, ...],
) -> int:
    """Clear cards whose producer is no longer registered. Returns the count.

    Retiring a producer must not strand its cards. ``sync_producer`` reconciles
    only within a producer that still runs, so an unregistered one's open cards
    would never be revisited — they'd sit in the Inbox permanently, with no
    code left that could ever clear them.
    """
    async with pool.acquire() as conn:
        cleared = await conn.fetch(
            """
            UPDATE helixis_reminders
               SET status = 'cleared', updated_at = now()
             WHERE company_id = $1::uuid
               AND status != 'cleared'
               AND NOT (producer = ANY($2::text[]))
         RETURNING id
            """,
            company_id,
            list(known),
        )
    return len(cleared)


async def upsert_card(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    producer: str,
    candidate: ReminderCandidate,
) -> None:
    """Event-driven single card (e.g. a webhook handler dropping a turnover
    card). Same lifecycle rules as the sweep upsert but NO clear pass —
    event producers aren't in the sweep's PRODUCERS registry, so their
    cards live until dismissed/handled (or a future self-clear producer)."""
    async with pool.acquire() as conn:
        await _upsert_one(conn, company_id=company_id, producer=producer, c=candidate)


#: The age ladder as Postgres, generated once from ``reminders/aging.py``.
#: Reads the EXISTING row (a bare table reference inside DO UPDATE gives the
#: pre-update values), which is what makes the ``cleared`` arm inside it
#: necessary — see that function's docstring.
_AGE_ESCALATION = age_escalation_sql(
    status_column="helixis_reminders.status",
    fired_column="helixis_reminders.first_fired_at",
)


async def _upsert_one(conn: Any, *, company_id: str, producer: str, c: ReminderCandidate) -> None:
    await conn.execute(
        f"""
                INSERT INTO helixis_reminders
                    (company_id, producer, entity_key, status, severity,
                     title, body, link_path, payload, escalation)
                VALUES ($1::uuid, $2, $3, 'open', $4, $5, $6, $7, $8::jsonb, $9)
                ON CONFLICT (company_id, producer, entity_key) DO UPDATE SET
                    severity      = {
            age_severity_sql(
                _AGE_ESCALATION,
                producer_severity="EXCLUDED.severity",
            )
        },
                    title         = EXCLUDED.title,
                    body          = EXCLUDED.body,
                    link_path     = EXCLUDED.link_path,
                    payload       = EXCLUDED.payload,
                    -- A card that self-cleared and came back is a NEW
                    -- instance: ``first_fired_at`` already resets below for
                    -- that reason, so the previous instance's urgency must
                    -- not ride along either. Otherwise the ratchet is
                    -- permanent across unrelated recurrences.
                    escalation    = CASE
                        WHEN helixis_reminders.status = 'cleared'
                            THEN EXCLUDED.escalation
                        ELSE GREATEST(helixis_reminders.escalation,
                                      EXCLUDED.escalation,
                                      {_AGE_ESCALATION})
                    END,
                    last_fired_at = now(),
                    updated_at    = now(),
                    status = CASE
                        WHEN helixis_reminders.status = 'dismissed'
                            THEN 'dismissed'
                        -- "I handled it outside Occupella" is a CLAIM. It holds
                        -- for the cooldown; past that, a condition that still
                        -- holds means the claim did not settle it, and the
                        -- card is the only thing that can say so.
                        -- ⚠ A NULL handled_at makes this comparison NULL, so
                        -- the row falls through and REOPENS. That direction is
                        -- deliberate: a mute we cannot date is one we cannot
                        -- prove is still owed, and reopening is recoverable in
                        -- one click while silence is not observable at all.
                        WHEN helixis_reminders.status = 'handled'
                             AND helixis_reminders.handled_at
                                 > now() - interval '{HANDLED_COOLDOWN_DAYS} days'
                            THEN 'handled'
                        WHEN helixis_reminders.status = 'snoozed'
                             AND helixis_reminders.snoozed_until > now()
                            THEN 'snoozed'
                        ELSE 'open'
                    END,
                    snoozed_until = CASE
                        WHEN helixis_reminders.status = 'snoozed'
                             AND helixis_reminders.snoozed_until > now()
                            THEN helixis_reminders.snoozed_until
                        ELSE NULL
                    END,
                    first_fired_at = CASE
                        WHEN helixis_reminders.status = 'cleared' THEN now()
                        ELSE helixis_reminders.first_fired_at
                    END
                """,
        company_id,
        producer,
        c.entity_key,
        c.severity,
        c.title,
        c.body,
        c.link_path,
        json.dumps(c.payload, default=str),
        c.escalation,
    )


async def list_active(pool: asyncpg.Pool, *, company_id: str) -> list[ReminderRow]:
    """Cards the desk should show: open, plus snoozes that have expired
    (the next sweep re-verifies their condition; showing them meanwhile
    beats a card silently sleeping past its snooze)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM helixis_reminders
             WHERE company_id = $1::uuid
               AND (status = 'open'
                    OR (status = 'snoozed' AND snoozed_until <= now()))
             ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                      last_fired_at DESC
            """,
            company_id,
        )
    return [_row(r) for r in rows]


async def set_status(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    reminder_id: str,
    status: str,
    snoozed_until: datetime | None = None,
) -> ReminderRow | None:
    """Apply a user action (snooze / dismiss / handled) to a visible card."""
    if status not in VALID_USER_STATUSES:
        raise ValueError(f"Unsupported reminder status: {status!r}")
    if status == "snoozed" and snoozed_until is None:
        raise ValueError("snoozed requires snoozed_until")
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE helixis_reminders
               SET status = $3,
                   snoozed_until = $4,
                   handled_at = CASE WHEN $3 = 'handled' THEN now() ELSE handled_at END,
                   updated_at = now()
             WHERE company_id = $1::uuid AND id = $2::uuid
               AND status IN ('open', 'snoozed')
            RETURNING *
            """,
            company_id,
            reminder_id,
            status,
            snoozed_until,
        )
    return _row(r) if r else None


__all__ = [
    "VALID_USER_STATUSES",
    "ReminderCandidate",
    "ReminderRow",
    "list_active",
    "set_status",
    "sync_producer",
    "upsert_card",
]
