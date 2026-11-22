"""Durable store for confirm-gate decisions.

Backs the durability layer of :func:`helixis.agents.tools.confirm_or_abort`.
One row per surfaced confirmation; see
``supabase/migrations/0022_pending_confirmations.sql`` for the status machine.

The live in-process ``asyncio.Future`` remains the authoritative fast path for
a running agent. This store is additive: it records every decision (audit) and
lets ``/agent/confirm`` distinguish a live resolve from one orphaned by a
process restart, so an orphaned confirm fails honestly instead of silently.

The SQL lives here; the policy lives in the primitive. The primitive depends on
the :class:`ConfirmationStore` Protocol (not this concrete class), so it is
unit-testable with an in-memory fake and runs in CI without Supabase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import asyncpg

logger = logging.getLogger(__name__)


# Status constants — single source of truth shared with the primitive.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_ORPHANED = "orphaned"


@dataclass
class ConfirmationRow:
    """A snapshot of a confirmation row as the primitive needs to see it."""

    confirm_id: str
    company_id: str
    session_id: str | None
    user_id: str | None
    action: str
    status: str
    created_at: datetime | None
    resolved_at: datetime | None


class ConfirmationStore(Protocol):
    """Interface the primitive depends on (concrete impl below; fake in tests)."""

    async def create(
        self,
        *,
        confirm_id: str,
        company_id: str,
        session_id: str | None,
        user_id: str | None,
        action: str,
        title: str,
        details: str,
        items: list[str],
        timeout_seconds: float,
    ) -> None: ...

    async def get(self, *, confirm_id: str) -> ConfirmationRow | None: ...

    async def resolve(
        self,
        *,
        confirm_id: str,
        approved: bool,
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> ConfirmationRow | None: ...

    async def mark_expired(self, *, confirm_id: str) -> ConfirmationRow | None: ...

    async def mark_orphaned(self, *, confirm_id: str) -> ConfirmationRow | None: ...

    async def orphan_all_pending(self) -> int: ...

    async def expire_stale(self) -> int: ...


def _row(row: asyncpg.Record) -> ConfirmationRow:
    return ConfirmationRow(
        confirm_id=str(row["confirm_id"]),
        company_id=row["company_id"],
        session_id=row["session_id"],
        user_id=row["user_id"],
        action=row["action"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


class PgConfirmationStore:
    """asyncpg-backed :class:`ConfirmationStore`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        confirm_id: str,
        company_id: str,
        session_id: str | None,
        user_id: str | None,
        action: str,
        title: str,
        details: str,
        items: list[str],
        timeout_seconds: float,
    ) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO helixis_pending_confirmations
                    (confirm_id, company_id, session_id, user_id, action,
                     title, details, items, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, 'pending', $9)
                ON CONFLICT (confirm_id) DO NOTHING
                """,
                confirm_id,
                company_id,
                session_id,
                user_id,
                action,
                title,
                details,
                json.dumps(items or []),
                expires_at,
            )

    async def get(self, *, confirm_id: str) -> ConfirmationRow | None:
        """Read a confirmation row by id (any status), or ``None`` if absent.

        Used by ``/agent/confirm`` to verify the caller's company owns the
        gated run before resolving it — a cross-tenant ownership check on the
        write-consent gate.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM helixis_pending_confirmations WHERE confirm_id = $1",
                confirm_id,
            )
        return _row(row) if row is not None else None

    async def _terminate(self, *, confirm_id: str, status: str) -> ConfirmationRow | None:
        """Move a row out of ``pending`` into a terminal ``status``.

        Conditional on ``status = 'pending'`` so it is idempotent and races
        (double POST, timeout-vs-answer) resolve to a single winner. Returns
        the updated row iff this call performed the transition, else ``None``.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE helixis_pending_confirmations
                   SET status = $2, resolved_at = now()
                 WHERE confirm_id = $1 AND status = 'pending'
                RETURNING *
                """,
                confirm_id,
                status,
            )
            return _row(row) if row is not None else None

    async def resolve(
        self,
        *,
        confirm_id: str,
        approved: bool,
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> ConfirmationRow | None:
        status = STATUS_APPROVED if approved else STATUS_DENIED
        if reason is None and resolved_by is None:
            return await self._terminate(confirm_id=confirm_id, status=status)
        # Extended resolve (0056): also record WHO decided and (on decline)
        # WHY. Falls back to the legacy UPDATE if the columns aren't applied
        # yet — migrations are manual (gotcha 8), so the deploy→migrate window
        # must not break every resolution.
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE helixis_pending_confirmations
                       SET status = $2, resolved_at = now(),
                           resolution_reason = $3, resolved_by = $4
                     WHERE confirm_id = $1 AND status = 'pending'
                    RETURNING *
                    """,
                    confirm_id,
                    status,
                    reason,
                    resolved_by,
                )
                return _row(row) if row is not None else None
        except asyncpg.UndefinedColumnError:
            logger.warning(
                "helixis_pending_confirmations missing 0056 columns — "
                "resolving without reason/resolver (apply the migration)"
            )
            return await self._terminate(confirm_id=confirm_id, status=status)

    async def mark_expired(self, *, confirm_id: str) -> ConfirmationRow | None:
        return await self._terminate(confirm_id=confirm_id, status=STATUS_EXPIRED)

    async def mark_orphaned(self, *, confirm_id: str) -> ConfirmationRow | None:
        return await self._terminate(confirm_id=confirm_id, status=STATUS_ORPHANED)

    async def _bulk_terminate(self, *, status: str, only_stale: bool) -> int:
        clause = "AND expires_at < now()" if only_stale else ""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE helixis_pending_confirmations
                   SET status = $1, resolved_at = now()
                 WHERE status = 'pending' {clause}
                """,
                status,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def orphan_all_pending(self) -> int:
        """Boot reconcile: every ``pending`` row is from a process that died
        (this one just started, so it holds no live waiters yet). Mark them
        ``orphaned`` so a stale Approve resolves against a truthful record."""
        return await self._bulk_terminate(status=STATUS_ORPHANED, only_stale=False)

    async def expire_stale(self) -> int:
        """Mark ``pending`` rows past their expiry as ``expired``."""
        return await self._bulk_terminate(status=STATUS_EXPIRED, only_stale=True)


def store_from_pool() -> ConfirmationStore | None:
    """Build a Pg-backed store from the process pool, or ``None`` if the pool
    isn't initialized (CI, tests, degraded boot). Callers degrade to the
    in-memory-only confirm path when this is ``None``."""
    from helixis.db import engine as db_engine

    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return None
    return PgConfirmationStore(pool)


__all__ = [
    "STATUS_APPROVED",
    "STATUS_DENIED",
    "STATUS_EXPIRED",
    "STATUS_ORPHANED",
    "STATUS_PENDING",
    "ConfirmationRow",
    "ConfirmationStore",
    "PgConfirmationStore",
    "store_from_pool",
]
