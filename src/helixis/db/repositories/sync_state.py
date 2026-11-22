"""Repository for ``buildium_sync_state`` — the onboarding backfill progress
(migration 0062). One row per company; the frontend's "still syncing…" banner
polls it and the orchestrator (``run_initial_sync``) writes it.

All functions are best-effort at the call site (sync progress is UX, never
load-bearing) but they do NOT swallow here — callers decide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

VALID_STATUSES = ("pending", "syncing", "done", "failed")

#: How long a row may sit in ``syncing`` before the banner stops believing it.
#:
#: ``run_initial_sync`` is fired and forgotten from ``POST /buildium/test``
#: (``asyncio.create_task``), so a process that dies mid-crawl — a deploy, an
#: OOM, a Render restart — never reaches ``mark_done`` or ``mark_failed`` and
#: the row stays ``syncing`` for the life of the account. Launch week is deploy
#: week, so this is likely rather than theoretical.
#:
#: 30 minutes is deliberately generous: a large account's first crawl is many
#: sequential paged reads, and calling a slow sync dead is worse than showing
#: the banner a while longer.
STALE_AFTER_SECONDS = 30 * 60


def is_stale(status: str, started_at: Any | None, *, now: Any | None = None) -> bool:
    """Has a ``syncing`` row stopped making progress?

    ⚠ This DERIVES staleness; it never writes. We cannot tell a dead crawl from
    a slow one, so asserting ``failed`` would be a claim we have not earned —
    gotcha 55's decline-to-assert. Deriving also self-heals: if the sync was
    merely slow and later finishes, ``mark_done`` lands and the row reads
    ``done`` with no reaper to undo.

    Only ``syncing`` can be stale. ``done`` and ``failed`` are terminal, and a
    row with no ``started_at`` gives nothing to measure against.
    """
    if status != "syncing" or started_at is None:
        return False
    reference = now if now is not None else datetime.now(UTC)
    started = started_at
    # A naive timestamp out of asyncpg is UTC; comparing it to an aware `now`
    # raises TypeError, which on a read path would 500 the banner rather than
    # degrade it.
    if getattr(started, "tzinfo", None) is None:
        started = started.replace(tzinfo=UTC)
    if getattr(reference, "tzinfo", None) is None:
        reference = reference.replace(tzinfo=UTC)
    # bool(...) is not decoration: `started_at` is typed Any (asyncpg hands
    # back an untyped row value), so the comparison result is Any too and
    # mypy rejects returning it from a `-> bool`. Making the narrowing
    # explicit is also what stops a truthy non-bool ever leaving here.
    return bool((reference - started).total_seconds() > STALE_AFTER_SECONDS)


@dataclass
class SyncState:
    company_id: str
    status: str
    started_at: Any | None
    finished_at: Any | None
    counts: dict[str, Any]
    last_error: str | None

    def to_public(self, *, now: Any | None = None) -> dict[str, Any]:
        """The shape GET /buildium/sync-status returns.

        ``stale`` is ADDITIVE — ``status`` keeps its stored value, so a client
        reading only ``status`` behaves exactly as before. The banner uses it
        to stop claiming "still syncing…" forever after a mid-crawl deploy.
        """
        return {
            "status": self.status,
            "counts": self.counts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_error": self.last_error,
            "stale": is_stale(self.status, self.started_at, now=now),
        }


def _row_to_state(row: Any) -> SyncState:
    counts = row["counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    return SyncState(
        company_id=str(row["company_id"]),
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        counts=counts or {},
        last_error=row["last_error"],
    )


async def get_sync_state(pool: asyncpg.Pool, company_id: str) -> SyncState | None:
    row = await pool.fetchrow(
        "SELECT company_id, status, started_at, finished_at, counts, last_error "
        "FROM buildium_sync_state WHERE company_id = $1::uuid",
        company_id,
    )
    return _row_to_state(row) if row else None


async def mark_syncing(pool: asyncpg.Pool, company_id: str) -> None:
    await pool.execute(
        """
        INSERT INTO buildium_sync_state
            (company_id, status, started_at, finished_at, counts, last_error, updated_at)
        VALUES ($1::uuid, 'syncing', now(), NULL, '{}'::jsonb, NULL, now())
        ON CONFLICT (company_id) DO UPDATE
          SET status = 'syncing', started_at = now(), finished_at = NULL,
              last_error = NULL, updated_at = now()
        """,
        company_id,
    )


async def mark_done(pool: asyncpg.Pool, company_id: str, counts: dict[str, Any]) -> None:
    await pool.execute(
        """
        INSERT INTO buildium_sync_state
            (company_id, status, finished_at, counts, last_error, updated_at)
        VALUES ($1::uuid, 'done', now(), $2::jsonb, NULL, now())
        ON CONFLICT (company_id) DO UPDATE
          SET status = 'done', finished_at = now(), counts = $2::jsonb,
              last_error = NULL, updated_at = now()
        """,
        company_id,
        json.dumps(counts or {}),
    )


async def mark_failed(
    pool: asyncpg.Pool, company_id: str, error: str, counts: dict[str, Any] | None = None
) -> None:
    await pool.execute(
        """
        INSERT INTO buildium_sync_state
            (company_id, status, finished_at, counts, last_error, updated_at)
        VALUES ($1::uuid, 'failed', now(), $2::jsonb, $3, now())
        ON CONFLICT (company_id) DO UPDATE
          SET status = 'failed', finished_at = now(), counts = $2::jsonb,
              last_error = $3, updated_at = now()
        """,
        company_id,
        json.dumps(counts or {}),
        (error or "")[:2000],
    )


async def clear_stranded(
    pool: asyncpg.Pool,
    company_id: str,
    counts: dict[str, Any],
    *,
    now: Any | None = None,
) -> bool:
    """A full pull finished elsewhere — retire a row that says it never did.

    The worker re-pulls every connected company's whole account daily
    (``webhooks/mirror_sync``), so a first crawl stranded by a deploy is
    repaired automatically within a day. Nothing recorded that, which left the
    banner telling a customer with a complete mirror that their import had
    stopped, indefinitely and with nothing they could do about it.

    ⚠ **IT ONLY RETIRES A ROW THAT IS ALREADY WRONG** — ``failed``, or a
    ``syncing`` row past :data:`STALE_AFTER_SECONDS`. Returns True when it
    wrote.

    Three states it deliberately leaves alone, and each would be a false claim:

    * ``done`` — already true, and rewriting it would replace the onboarding
      crawl's counts with a routine refresh's for no reason.
    * a ``syncing`` row that is NOT stale — a crawl is genuinely in flight and
      will mark itself; overwriting it makes the banner vanish mid-import and
      then reappear if that crawl fails.
    * no row at all — this company never started an import, so there is
      nothing stranded and inventing a ``done`` would claim an onboarding pull
      that never ran. ``unknown`` is the honest reading and the banner already
      says nothing for it.

    ⚠ The staleness rule is READ IN PYTHON, never re-expressed in the SQL. A
    second copy of "30 minutes" in a WHERE clause is a second answer to the
    same question one edit from disagreeing with :func:`is_stale`, which is
    the drift gotcha 28 exists for. The extra read costs one query on a path
    that runs once per company per day.
    """
    state = await get_sync_state(pool, company_id)
    if state is None:
        return False
    if state.status != "failed" and not is_stale(state.status, state.started_at, now=now):
        return False
    await mark_done(pool, company_id, counts)
    return True


__all__ = [
    "STALE_AFTER_SECONDS",
    "VALID_STATUSES",
    "SyncState",
    "clear_stranded",
    "get_sync_state",
    "is_stale",
    "mark_done",
    "mark_failed",
    "mark_syncing",
]
