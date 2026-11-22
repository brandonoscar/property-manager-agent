"""Rentvine turnovers: an export lease gaining a move-out date opens the same
make-ready checklist and desk card Buildium's ``Lease.MoveOut.Created`` does.

The difference is the trigger. Buildium's is an event, delivered once per
filing; Rentvine's is a poll that sees the same move-out on every pass. So the
Rentvine path (``turnovers_repo.start_turnover(only_if_new=True)``) touches
nothing unless the turnover is new or its date moved: a turnover the manager
dismissed, or a card they handled, must not come back with the next sync.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.db.repositories import turnovers as turnovers_repo
from helixis.rentvine import mapper, sync

CO = "6a456191-0000-4000-8000-000000000001"


# ── the mapper ────────────────────────────────────────────────────────────────


def test_the_actual_move_out_wins_over_the_expected_one():
    row = {"moveOutDate": "2026-10-20", "expectedMoveOutDate": "2026-10-31"}
    assert mapper.lease_move_out(row) == date(2026, 10, 20)


def test_a_notice_alone_opens_on_the_expected_date():
    assert mapper.lease_move_out({"expectedMoveOutDate": "2026-10-31"}) == date(2026, 10, 31)


@pytest.mark.parametrize("row", [{}, {"moveOutDate": None, "expectedMoveOutDate": ""}])
def test_no_move_out_is_none(row):
    assert mapper.lease_move_out(row) is None


# ── the repository ────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, returning: Any = None, ctx: Any = None) -> None:
        self.execute = AsyncMock()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._returning = returning
        self._ctx = ctx

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if "INSERT INTO helixis_turnovers" in sql:
            return self._returning
        return self._ctx


def _pool(conn: _Conn) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _opened(returning: Any) -> tuple[bool, _Conn]:
    conn = _Conn(returning=returning)
    got = asyncio.run(
        turnovers_repo.open_turnover_if_new(
            _pool(conn),
            company_id=CO,
            lease_buildium_id=5,
            property_id=7,
            unit_id=9,
            move_out_date=date(2026, 10, 31),
            pms="rentvine",
        )
    )
    return got, conn


def test_the_polled_open_touches_a_row_only_when_its_date_moved():
    got, conn = _opened({"id": "x"})
    assert got is True
    ((sql, args),) = conn.calls
    # The WHERE on the conflict update is the whole rule: an unchanged date
    # updates nothing (and so reactivates nothing) and returns no row.
    assert "WHERE helixis_turnovers.move_out_date IS DISTINCT FROM EXCLUDED.move_out_date" in sql
    assert args == (CO, 5, 7, 9, date(2026, 10, 31), "rentvine")
    assert _opened(None)[0] is False


def test_the_webhook_open_still_writes_buildium_and_reactivates():
    conn = _Conn()
    asyncio.run(
        turnovers_repo.open_turnover(
            _pool(conn),
            company_id=CO,
            lease_buildium_id=5,
            property_id=7,
            unit_id=9,
            move_out_date=date(2026, 10, 31),
        )
    )
    sql, *args = conn.execute.await_args.args
    assert "IS DISTINCT FROM" not in sql
    assert "WHEN helixis_turnovers.status = 'dismissed'" in sql
    assert args[-1] == "buildium"


_CTX = {"property_id": 7, "unit_id": 9, "property_name": "Maple Court", "unit_number": "M-101"}


def _start(*, opened: bool, only_if_new: bool) -> tuple[bool, AsyncMock, AsyncMock, AsyncMock]:
    pool = _pool(_Conn(ctx=_CTX))
    with (
        patch.object(turnovers_repo, "open_turnover_if_new", AsyncMock(return_value=opened)) as new,
        patch.object(turnovers_repo, "open_turnover", AsyncMock()) as always,
        patch.object(turnovers_repo.reminders_repo, "upsert_card", AsyncMock()) as card,
    ):
        got = asyncio.run(
            turnovers_repo.start_turnover(
                pool,
                company_id=CO,
                lease_id=5,
                move_out_date=date(2026, 10, 31),
                pms="rentvine",
                only_if_new=only_if_new,
            )
        )
    return got, new, always, card


def test_an_unchanged_move_out_raises_no_card():
    """A card the manager handled would otherwise reopen every pass."""
    got, new, always, card = _start(opened=False, only_if_new=True)
    assert got is False
    new.assert_awaited_once()
    always.assert_not_awaited()
    card.assert_not_awaited()


def test_a_new_move_out_opens_the_row_and_the_card():
    got, new, _always, card = _start(opened=True, only_if_new=True)
    assert got is True
    assert new.await_args.kwargs["pms"] == "rentvine"
    assert new.await_args.kwargs["property_id"] == 7 and new.await_args.kwargs["unit_id"] == 9
    c = card.await_args.kwargs["candidate"]
    assert card.await_args.kwargs["producer"] == "turnover"
    assert c.entity_key == "lease:5"
    assert "Maple Court · Unit M-101" in c.title and "2026-10-31" in c.title
    assert c.link_path == "/properties/7"


def test_the_webhook_path_always_fires():
    got, new, always, card = _start(opened=False, only_if_new=False)
    assert got is True
    new.assert_not_awaited()
    always.assert_awaited_once()
    card.assert_awaited_once()


# ── the sync ──────────────────────────────────────────────────────────────────


def _leases() -> list[dict[str, Any]]:
    return [
        {"leaseID": 1, "moveOutDate": "2026-10-20"},  # upcoming
        {"leaseID": 2, "moveOutDate": "2026-09-05"},  # 27 days ago: inside the window
        {"leaseID": 3, "moveOutDate": "2024-05-01"},  # long re-let: outside it
        {"leaseID": 4},  # no move-out
        {"leaseID": 5, "expectedMoveOutDate": "2026-11-30"},  # notice given
    ]


def _run(start: AsyncMock) -> int:
    with (
        patch.object(sync, "company_timezone", AsyncMock(return_value=None)),
        patch.object(sync, "today_in", return_value=date(2026, 10, 2)),
        patch.object(turnovers_repo, "start_turnover", start),
    ):
        return asyncio.run(sync._start_turnovers(MagicMock(), CO, _leases()))


def test_only_recent_and_upcoming_move_outs_open_a_turnover():
    start = AsyncMock(return_value=True)
    assert _run(start) == 3
    opened = {c.kwargs["lease_id"]: c.kwargs for c in start.await_args_list}
    assert set(opened) == {1, 2, 5}
    assert all(k["only_if_new"] is True and k["pms"] == "rentvine" for k in opened.values())
    assert opened[5]["move_out_date"] == date(2026, 11, 30)


def test_the_count_is_turnovers_opened_not_leases_seen():
    start = AsyncMock(side_effect=[True, False, True])
    assert _run(start) == 2


def test_one_turnover_failing_does_not_stop_the_rest():
    start = AsyncMock(side_effect=[RuntimeError("db"), True, True])
    assert _run(start) == 2
    assert start.await_count == 3


def test_turnovers_are_fed_but_never_stamped_or_reconciled():
    from helixis.db.pms_coverage import FED_BY, RENTVINE

    assert "helixis_turnovers" in FED_BY[RENTVINE]
    assert "helixis_turnovers" in sync.DERIVED_TABLES
    assert "helixis_turnovers" not in sync.SYNC_STATE_STAGE


def test_the_webhook_open_binds_the_pms_it_is_given():
    """Every turnover row carries its PMS (migration 0101's discriminator)."""
    conn = _Conn()
    asyncio.run(
        turnovers_repo.open_turnover(
            _pool(conn),
            company_id=CO,
            lease_buildium_id=5,
            property_id=7,
            unit_id=9,
            move_out_date=date(2026, 10, 31),
            pms="rentvine",
        )
    )
    sql, *args = conn.execute.await_args.args
    assert "pms           = EXCLUDED.pms" in sql
    assert args[-1] == "rentvine"


def test_a_unit_named_after_its_property_is_said_once():
    """Measured 2026-10-02: "Move-out at 111 Kane · Unit 111 Kane"."""
    same = {"property_id": 7, "unit_id": 9, "property_name": "111 Kane", "unit_number": "111 kane "}
    pool = _pool(_Conn(ctx=same))
    with (
        patch.object(turnovers_repo, "open_turnover_if_new", AsyncMock(return_value=True)),
        patch.object(turnovers_repo.reminders_repo, "upsert_card", AsyncMock()) as card,
    ):
        asyncio.run(
            turnovers_repo.start_turnover(
                pool,
                company_id=CO,
                lease_id=2,
                move_out_date=date(2026, 10, 23),
                pms="rentvine",
                only_if_new=True,
            )
        )
    title = card.await_args.kwargs["candidate"].title
    assert title == "Move-out at 111 Kane on 2026-10-23 — start turnover"
