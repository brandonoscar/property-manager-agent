"""Payment promises (migration 0047) — one live promise per sender email.

Written by the email-ingest poll when a surfaced email carries a
deterministic payment promise; read by the ``rent_missing`` reminder
producer as a suppression signal. Later promises overwrite earlier ones
("actually, make it the 20th"), so the newest word always wins.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import asyncpg


@dataclass
class PromiseRow:
    email: str
    promised_date: dt.date
    updated_at: dt.datetime | None


async def list_promises(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    limit: int = 25,
) -> list[PromiseRow]:
    """Current live promises for the company, newest word first.

    Read side for the reasoning loop's known-state channel (W2 slice 3) — the
    table previously had writers only (email-ingest) plus the rent_missing
    producer's inline suppression query.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT email, promised_date, updated_at
              FROM helixis_payment_promises
             WHERE company_id = $1::uuid
             ORDER BY updated_at DESC NULLS LAST
             LIMIT $2
            """,
            company_id,
            limit,
        )
    return [
        PromiseRow(
            email=r["email"],
            promised_date=r["promised_date"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def record_promise(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    email: str,
    promised_date: dt.date,
    source_message_id: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_payment_promises
                (company_id, email, promised_date, source_message_id)
            VALUES ($1::uuid, lower($2), $3, $4)
            ON CONFLICT (company_id, email) DO UPDATE SET
                promised_date     = EXCLUDED.promised_date,
                source_message_id = EXCLUDED.source_message_id,
                updated_at        = now()
            """,
            company_id,
            email,
            promised_date,
            source_message_id,
        )


__all__ = ["PromiseRow", "list_promises", "record_promise"]
