"""Unit tests for ``work_order_history.record_status_transition_if_changed``.

Pins the transition semantics the maintenance handler depends on: first
sighting inserts (previous NULL), no-change is silent, a real transition
inserts with the previous status, and any DB error degrades to False (the
handler must never fail over telemetry).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from helixis.db.repositories.work_order_history import record_status_transition_if_changed

COMPANY = "11111111-1111-1111-1111-111111111111"


class _FakeConn:
    def __init__(self, current_status: str | None | Exception) -> None:
        # None = no mirror row yet; a string = that row's status.
        self._current = current_status
        self.inserted: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if isinstance(self._current, Exception):
            raise self._current
        if self._current is None:
            return None
        return {"status": self._current}

    async def execute(self, sql: str, *args: Any) -> str:
        self.inserted.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acq():
            yield self.conn

        return _acq()


def _record(current: str | None | Exception, new_status: str | None) -> tuple[bool, _FakeConn]:
    conn = _FakeConn(current)
    wrote = asyncio.run(
        record_status_transition_if_changed(
            _FakePool(conn),  # type: ignore[arg-type]
            company_id=COMPANY,
            work_order_buildium_id=4821,
            new_status=new_status,
        )
    )
    return wrote, conn


def test_first_sighting_inserts_with_null_previous() -> None:
    wrote, conn = _record(None, "New")
    assert wrote is True
    sql, args = conn.inserted[0]
    assert "helixis_work_order_status_history" in sql
    # (company_id, wo_id, previous, status, source)
    assert args == (COMPANY, 4821, None, "New", "webhook")


def test_real_transition_inserts_with_previous_status() -> None:
    wrote, conn = _record("New", "Completed")
    assert wrote is True
    assert conn.inserted[0][1] == (COMPANY, 4821, "New", "Completed", "webhook")


def test_unchanged_status_is_silent() -> None:
    wrote, conn = _record("New", "New")
    assert wrote is False
    assert conn.inserted == []


def test_whitespace_and_empty_status_are_skipped() -> None:
    assert _record("New", "  ")[0] is False
    assert _record("New", None)[0] is False
    # Mirror status differing only by padding is not a transition.
    assert _record("New ", "New")[0] is False


def test_db_error_degrades_to_false() -> None:
    wrote, conn = _record(RuntimeError("pool exploded"), "New")
    assert wrote is False
    assert conn.inserted == []
