"""Repository for ``company_knowledge`` — the standing company profile (ADR 0006).

One markdown doc per company, written by the daily background generator and read
both by the agent (injected as standing context) and the read-only Settings → AI
& Memory panel. This is deliberately NOT the Mem0 memory store — different shape
(one curated doc vs vector facts), different source, different cadence.

Reads are best-effort: if the table is missing (migration 0035 not yet applied —
gotcha #8) or the row doesn't exist, ``load`` returns ``None`` and callers
degrade gracefully rather than break the turn.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompanyKnowledge:
    """One company's standing profile.

    ``content_md`` is the auto-learned daily profile (written by the background
    job) and by uploaded documents / Google Drive sources — Helixis learns the
    company; nobody hand-types context into it (founder directive, #67).
    """

    company_id: str
    content_md: str
    generated_on: datetime.date | None
    updated_at: datetime.datetime | None


async def load(pool: asyncpg.Pool, *, company_id: str) -> CompanyKnowledge | None:
    """The company's profile row, or ``None`` if absent / the table is missing.

    Never raises — a missing table (unapplied migration) or a missing row both
    return ``None`` so the agent turn and the Settings read degrade gracefully.
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT company_id::text AS company_id,
                   content_md,
                   generated_on,
                   updated_at
            FROM public.company_knowledge
            WHERE company_id = $1::uuid
            """,
            company_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort read
        logger.warning("company_knowledge load failed (company=%s): %s", company_id, exc)
        return None
    if row is None:
        return None
    return CompanyKnowledge(
        company_id=row["company_id"],
        content_md=row["content_md"] or "",
        generated_on=row["generated_on"],
        updated_at=row["updated_at"],
    )


async def upsert(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    content_md: str,
    generated_on: datetime.date,
) -> None:
    """Insert or replace a company's profile. Called only by the daily job."""
    await pool.execute(
        """
        INSERT INTO public.company_knowledge (company_id, content_md, generated_on, updated_at)
        VALUES ($1::uuid, $2, $3, now())
        ON CONFLICT (company_id) DO UPDATE
            SET content_md   = EXCLUDED.content_md,
                generated_on = EXCLUDED.generated_on,
                updated_at   = now()
        """,
        company_id,
        content_md,
        generated_on,
    )


async def generated_on(pool: asyncpg.Pool, *, company_id: str) -> datetime.date | None:
    """The date the company's profile was last generated, or ``None``.

    Used by the daily job's idempotency guard (skip if it equals today). A
    missing table returns ``None`` (treat as never-generated).
    """
    try:
        row = await pool.fetchrow(
            "SELECT generated_on FROM public.company_knowledge WHERE company_id = $1::uuid",
            company_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("company_knowledge generated_on check failed: %s", exc)
        return None
    return row["generated_on"] if row else None


__all__ = ["CompanyKnowledge", "load", "upsert", "generated_on"]
