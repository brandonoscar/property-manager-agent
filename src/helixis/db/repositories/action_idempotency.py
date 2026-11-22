"""Idempotency-claim store for irreversible agent actions.

Backs :mod:`helixis.agents.idempotency`. One row per write action keyed by
``(company_id, idem_key)``; the row is both the atomic dedup claim and the
replay record. See ``supabase/migrations/0021_action_idempotency.sql`` for the
status machine.

The SQL lives here; the decision logic lives in the primitive. The primitive
depends on the :class:`IdempotencyStore` Protocol (not this concrete class), so
it is unit-testable with an in-memory fake and runs in CI without Supabase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import asyncpg

logger = logging.getLogger(__name__)


# Status constants — single source of truth shared with the primitive.
STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED_RETRYABLE = "failed_retryable"
STATUS_ABANDONED_UNKNOWN = "abandoned_unknown"
STATUS_ABORTED = "aborted"
# Parked at the human confirm gate — execute() has PROVABLY not started, so a
# process death here is recoverable (→ failed_retryable), unlike in_progress
# where the side effect may have fired (→ abandoned_unknown). Before this
# status existed, a deploy landing mid-confirm turned an un-fired action into
# a scary needs-review row that blocked the user's retry.
STATUS_AWAITING_APPROVAL = "awaiting_approval"


@dataclass
class ClaimRow:
    """A snapshot of an idempotency row as the primitive needs to see it."""

    company_id: str
    idem_key: str
    action_type: str
    status: str
    result: Any | None
    error: str | None
    attempts: int
    expires_at: datetime | None


@dataclass
class ClaimResult:
    """Outcome of a ``claim`` call.

    ``won`` is True when THIS caller inserted the row (the single winner that
    must proceed to confirm+execute). When False, ``row`` carries the existing
    row the caller must branch on (replay / await / blocked / re-confirm).
    """

    won: bool
    row: ClaimRow


class IdempotencyStore(Protocol):
    """Interface the primitive depends on (concrete impl below; fake in tests)."""

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult: ...

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None: ...

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None: ...

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None: ...

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None: ...

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool: ...

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None: ...

    async def reap_stale(self) -> int: ...

    async def mark_awaiting_approval(self, *, company_id: str, idem_key: str) -> bool: ...

    async def resume_from_awaiting(self, *, company_id: str, idem_key: str) -> bool: ...

    async def release_awaiting(self) -> int: ...


def _row_to_claim(row: asyncpg.Record) -> ClaimRow:
    raw_result = row["result"]
    result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    return ClaimRow(
        company_id=row["company_id"],
        idem_key=row["idem_key"],
        action_type=row["action_type"],
        status=row["status"],
        result=result,
        error=row["error"],
        attempts=row["attempts"],
        expires_at=row["expires_at"],
    )


class PgIdempotencyStore:
    """asyncpg-backed :class:`IdempotencyStore`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        """Atomically claim the action. The INSERT wins exactly once per key
        thanks to ``ON CONFLICT DO NOTHING``; concurrent callers that lose the
        race read the existing row and branch on its status.
        """
        now = datetime.now(UTC)
        expires_at = now + window if window is not None else None
        lease_until = now + _LEASE
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchrow(
                """
                INSERT INTO helixis_action_idempotency
                    (company_id, idem_key, action_type, status, lease_until, expires_at)
                VALUES ($1, $2, $3, 'in_progress', $4, $5)
                ON CONFLICT (company_id, idem_key) DO NOTHING
                RETURNING *
                """,
                company_id,
                idem_key,
                action_type,
                lease_until,
                expires_at,
            )
            if inserted is not None:
                return ClaimResult(won=True, row=_row_to_claim(inserted))

            existing = await conn.fetchrow(
                "SELECT * FROM helixis_action_idempotency WHERE company_id = $1 AND idem_key = $2",
                company_id,
                idem_key,
            )
            assert existing is not None, "row vanished between insert and select"
            return ClaimResult(won=False, row=_row_to_claim(existing))

    async def _finalize(
        self, *, company_id: str, idem_key: str, status: str, result: Any, error: str | None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = $3, result = $4::jsonb, error = $5, updated_at = now()
                 WHERE company_id = $1 AND idem_key = $2
                """,
                company_id,
                idem_key,
                status,
                json.dumps(result) if result is not None else None,
                error,
            )

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        await self._finalize(
            company_id=company_id,
            idem_key=idem_key,
            status=STATUS_SUCCEEDED,
            result=result,
            error=None,
        )

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._finalize(
            company_id=company_id,
            idem_key=idem_key,
            status=STATUS_FAILED_RETRYABLE,
            result=None,
            error=error,
        )

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._finalize(
            company_id=company_id,
            idem_key=idem_key,
            status=STATUS_ABANDONED_UNKNOWN,
            result=None,
            error=error,
        )

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        """Record that the user declined at the gate — nothing fired. Distinct
        from ``failed_retryable``: a later attempt is treated as fresh."""
        await self._finalize(
            company_id=company_id,
            idem_key=idem_key,
            status=STATUS_ABORTED,
            result=None,
            error=None,
        )

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        """Flip a retryable row (``failed_retryable`` OR ``aborted`` — both mean
        the side effect did NOT fire) back to ``in_progress``. Single winner via
        the conditional ``WHERE status IN (...)``. Returns True iff this caller
        won. Never touches ``succeeded``/``in_progress``/``abandoned_unknown``."""
        now = datetime.now(UTC)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'in_progress',
                       attempts = attempts + 1,
                       lease_until = $3,
                       error = NULL,
                       updated_at = now()
                 WHERE company_id = $1 AND idem_key = $2
                   AND status IN ('failed_retryable', 'aborted')
                RETURNING id
                """,
                company_id,
                idem_key,
                now + _LEASE,
            )
            return row is not None

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM helixis_action_idempotency WHERE company_id = $1 AND idem_key = $2",
                company_id,
                idem_key,
            )
            return _row_to_claim(row) if row is not None else None

    async def reap_stale(self) -> int:
        """Reap claims whose lease expired, split by what we can PROVE.

        * ``in_progress`` — the side effect may have fired; we cannot know →
          ``abandoned_unknown`` (needs review), never back to retryable.
        * ``awaiting_approval`` — the claim was parked at the confirm gate,
          so ``execute()`` provably never started (the holder marks the claim
          back to ``in_progress`` BEFORE executing) → ``failed_retryable``:
          the user simply re-runs the action after the deploy/crash and gets
          a fresh confirm, instead of a blocked needs-review row.

        Returns the total number of rows reaped across both branches."""
        async with self._pool.acquire() as conn:
            ambiguous = await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'abandoned_unknown',
                       error = COALESCE(error, 'reaped: in_progress lease expired'),
                       updated_at = now()
                 WHERE status = 'in_progress' AND lease_until < now()
                """,
            )
            parked = await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'failed_retryable',
                       error = COALESCE(
                           error,
                           'reaped: awaiting_approval lease expired (nothing fired)'
                       ),
                       updated_at = now()
                 WHERE status = 'awaiting_approval' AND lease_until < now()
                """,
            )
            # asyncpg returns e.g. "UPDATE 3"
            total = 0
            for result in (ambiguous, parked):
                try:
                    total += int(result.split()[-1])
                except (ValueError, IndexError):
                    continue
            return total

    async def mark_awaiting_approval(self, *, company_id: str, idem_key: str) -> bool:
        """Park an ``in_progress`` claim at the confirm gate (lease refreshed).

        Called by the holder immediately BEFORE awaiting the human, so a death
        during the wait is provably pre-side-effect. Returns False if the row
        is no longer ``in_progress`` (defensive: the caller should then treat
        the park as failed and proceed unparked)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'awaiting_approval',
                       lease_until = now() + $3::interval,
                       updated_at = now()
                 WHERE company_id = $1 AND idem_key = $2 AND status = 'in_progress'
                """,
                company_id,
                idem_key,
                _LEASE,
            )
            return bool(result.endswith("1"))

    async def resume_from_awaiting(self, *, company_id: str, idem_key: str) -> bool:
        """Un-park after approval, back to ``in_progress`` (lease refreshed) —
        must be called BEFORE ``execute()`` so a death after this point is
        correctly ambiguous again. Returns False if the row was not parked
        (e.g. the shutdown release or reaper got there first — the caller must
        NOT execute in that case; the claim is no longer theirs)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'in_progress',
                       lease_until = now() + $3::interval,
                       updated_at = now()
                 WHERE company_id = $1 AND idem_key = $2 AND status = 'awaiting_approval'
                """,
                company_id,
                idem_key,
                _LEASE,
            )
            return bool(result.endswith("1"))

    async def release_awaiting(self) -> int:
        """Shutdown-time cleanup: every parked claim → ``failed_retryable``
        immediately (no lease wait). The process is about to die, so every
        pending confirm it owns is dead too; releasing now means the user's
        retry works the moment the new process is up, instead of after the
        15-minute reaper pass. Returns the number of rows released."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE helixis_action_idempotency
                   SET status = 'failed_retryable',
                       error = COALESCE(error, 'released: process shutdown during confirm wait'),
                       updated_at = now()
                 WHERE status = 'awaiting_approval'
                """,
            )
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0


# In-progress lease: how long a claim may stay ``in_progress`` before the
# reaper treats it as abandoned. Generous vs. the confirm timeout (300s in
# confirm_or_abort) so a legitimately-pending human approval is not reaped.
_LEASE = timedelta(minutes=15)


__all__ = [
    "STATUS_ABANDONED_UNKNOWN",
    "STATUS_ABORTED",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_FAILED_RETRYABLE",
    "STATUS_IN_PROGRESS",
    "STATUS_SUCCEEDED",
    "ClaimResult",
    "ClaimRow",
    "IdempotencyStore",
    "PgIdempotencyStore",
]
