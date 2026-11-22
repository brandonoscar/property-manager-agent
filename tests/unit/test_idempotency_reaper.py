"""Unit tests for the real ``PgIdempotencyStore.reap_stale`` SQL method.

The DB-free ``FakeStore`` in test_action_idempotency.py exercises the guard
protocol; this pins the REAL asyncpg method's command-tag parsing (the count of
reaped rows), which is what the worker janitor now logs and acts on. A fake pool
records the SQL and returns a command tag, so no live Postgres is needed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from helixis.db.repositories.action_idempotency import PgIdempotencyStore


class _FakeConn:
    def __init__(self, tag: str) -> None:
        self._tag = tag
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        return self._tag


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn

        return _acq()


def _reap(tag: str) -> tuple[int, _FakeConn]:
    conn = _FakeConn(tag)
    reaped = asyncio.run(PgIdempotencyStore(_FakePool(conn)).reap_stale())  # type: ignore[arg-type]
    return reaped, conn


def test_reap_stale_returns_row_count_from_command_tag() -> None:
    # Two branches since the parked-approvals change: the same fake tag is
    # returned for each, so "UPDATE 3" twice sums to 6.
    reaped, conn = _reap("UPDATE 3")
    assert reaped == 6
    # Branch 1 targets in_progress rows past their lease and only moves them
    # to abandoned_unknown (the side effect MAY have fired).
    sql = conn.executed[0].lower()
    assert "abandoned_unknown" in sql
    assert "in_progress" in sql
    assert "lease_until <" in sql
    # Branch 2 targets awaiting_approval rows (parked at the confirm gate —
    # execute provably never started) and moves them to failed_retryable,
    # never abandoned.
    sql2 = conn.executed[1].lower()
    assert "awaiting_approval" in sql2
    assert "failed_retryable" in sql2
    assert "abandoned_unknown" not in sql2
    assert "lease_until <" in sql2


def test_reap_stale_zero_when_nothing_stale() -> None:
    reaped, _ = _reap("UPDATE 0")
    assert reaped == 0


def test_reap_stale_is_defensive_against_a_malformed_tag() -> None:
    # A non-numeric / unexpected command tag must not raise into the janitor —
    # it degrades to 0 (the janitor's own try/except is the second layer).
    assert _reap("UPDATE")[0] == 0
    assert _reap("")[0] == 0
