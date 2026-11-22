"""Poll cursor / dedup ledger for the Gmail email-ingest worker.

Backs ``public.helixis_gmail_seen`` (migration 0024). The worker uses
these to (a) enumerate which (user, company) pairs to poll, (b) skip
messages it already examined, and (c) record what it examined this cycle.

Every function is best-effort about the table being absent (migration not
yet applied) — it logs and returns a safe default rather than crashing
the worker, matching the queue's ``UndefinedTableError`` discipline.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)


async def list_poll_targets(pool: asyncpg.Pool) -> list[tuple[str, str]]:
    """Return ``(user_id, company_id)`` pairs to poll — every membership
    in ``company_users``. The worker checks each for an active Gmail
    connection before fetching, so listing all members is cheap and
    correct (a member with no Gmail connection is skipped).
    """
    try:
        rows = await pool.fetch(
            """
            SELECT user_id::text AS user_id, company_id::text AS company_id
              FROM public.company_users
             WHERE user_id IS NOT NULL
            """
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("company_users table missing — no email poll targets")
        return []
    except Exception:  # noqa: BLE001
        logger.exception("failed to list email poll targets")
        return []
    return [(r["user_id"], r["company_id"]) for r in rows]


async def get_seen_message_ids(pool: asyncpg.Pool, user_id: str) -> frozenset[str]:
    """Message ids already examined for ``user_id`` (so we don't
    re-enqueue). Returns an empty set on any error — the worst case is
    re-examining a message, which the dedup_key unique index then drops
    at enqueue, so a transient read failure can't double-surface."""
    try:
        rows = await pool.fetch(
            "SELECT message_id FROM public.helixis_gmail_seen WHERE user_id = $1::uuid",
            user_id,
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("helixis_gmail_seen missing — apply migration 0024")
        return frozenset()
    except Exception:  # noqa: BLE001
        logger.exception("failed to read seen gmail ids for user %s", user_id)
        return frozenset()
    return frozenset(r["message_id"] for r in rows)


async def mark_messages_seen(
    pool: asyncpg.Pool,
    user_id: str,
    company_id: str,
    *,
    surfaced_ids: set[str],
    examined_ids: set[str],
) -> None:
    """Record every message examined this cycle, flagging the ones that
    surfaced (passed the gate + were enqueued). Idempotent —
    ``ON CONFLICT DO UPDATE`` keeps the row and bumps ``surfaced`` to
    True if it later qualifies (it won't normally, but the upsert is
    safe either way)."""
    if not examined_ids:
        return
    records = [(user_id, mid, company_id, mid in surfaced_ids) for mid in examined_ids]
    try:
        await pool.executemany(
            """
            INSERT INTO public.helixis_gmail_seen
                (user_id, message_id, company_id, surfaced)
            VALUES ($1::uuid, $2, $3::uuid, $4)
            ON CONFLICT (user_id, message_id) DO UPDATE
                SET surfaced = public.helixis_gmail_seen.surfaced OR EXCLUDED.surfaced
            """,
            records,
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("helixis_gmail_seen missing — apply migration 0024")
    except Exception:  # noqa: BLE001
        logger.exception("failed to mark gmail messages seen for user %s", user_id)


__all__ = [
    "get_seen_message_ids",
    "list_poll_targets",
    "mark_messages_seen",
]
