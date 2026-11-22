"""Unit tests for ``orchestrator_runs.reap_stale_runs`` (loose-ends batch).

A process death mid-run leaves telemetry rows stuck in ``running`` forever —
this pins the reaper's SQL contract (terminal vocabulary, stale predicate,
NULL latency) and its command-tag parsing, worker-janitor safe (returns 0 on
any error). Same fake-pool pattern as test_idempotency_reaper.py.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from helixis.db.repositories.orchestrator_runs import reap_stale_runs


class _FakeConn:
    def __init__(self, tag: str | Exception) -> None:
        self._tag = tag
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if isinstance(self._tag, Exception):
            raise self._tag
        return self._tag


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn

        return _acq()


def _reap(tag: str | Exception, **kwargs: Any) -> tuple[int, _FakeConn]:
    conn = _FakeConn(tag)
    reaped = asyncio.run(reap_stale_runs(_FakePool(conn), **kwargs))  # type: ignore[arg-type]
    return reaped, conn


def test_reaps_to_the_documented_terminal_vocabulary() -> None:
    reaped, conn = _reap("UPDATE 2")
    assert reaped == 2
    sql, args = conn.executed[0]
    lowered = sql.lower()
    # Only stuck-running rows, moved to the documented 'failed' terminal state
    # with the sentinel error_type so readers can tell reaped from real fails.
    assert "status = 'running'" in lowered
    assert "'failed'" in lowered
    assert "abandonedrun" in lowered
    assert "started_at < now()" in lowered
    # latency_ms is deliberately untouched — the true duration is unknowable.
    assert "latency_ms" not in lowered
    # Default threshold is 1h, passed as the single parameter.
    assert args == ("3600",)


def test_threshold_is_configurable() -> None:
    _, conn = _reap("UPDATE 0", stale_after_seconds=7200)
    assert conn.executed[0][1] == ("7200",)


def test_returns_zero_when_nothing_stale() -> None:
    assert _reap("UPDATE 0")[0] == 0


def test_is_defensive_against_malformed_tags_and_db_errors() -> None:
    # Janitor path: neither a weird command tag nor a DB error may propagate.
    assert _reap("UPDATE")[0] == 0
    assert _reap("")[0] == 0
    assert _reap(RuntimeError("pool exploded"))[0] == 0
