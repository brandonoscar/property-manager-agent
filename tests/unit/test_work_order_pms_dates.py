"""A Rentvine work order carries the PMS's own last-change time, and "stalled"
reads it.

The stall counts (first scan, morning brief) compared the mirror's
``updated_at``, which every Rentvine pass rewrites, so no Rentvine work order
could ever read as stalled. Measured 2026-10-03: all 8 sandbox work orders open,
``dateTimeModified`` as old as 2026-09-24, mirror ``updated_at`` today on all 8
(gotcha 190). Migration 0105 adds ``pms_updated_at``; the readers' fallback is
pinned in ``test_rentvine_sync.test_a_rentvine_only_column_is_read_with_a_fallback``.

The sync test drives the real ``sync.sync_company`` over an HTTP mock, with a
pool that refuses what asyncpg refuses. All rows are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from helixis.rentvine import mapper, sync

from .test_rentvine_source_prune import CO, _app, _complete, _pool
from .test_rentvine_sync import PROPERTY, WORK_ORDER, _client, export_leases, lease_route, wire


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The measured shape: no zone, read as UTC.
        ("2026-09-24 14:03:11", datetime(2026, 9, 24, 14, 3, 11, tzinfo=UTC)),
        ("2026-09-24T14:03:11Z", datetime(2026, 9, 24, 14, 3, 11, tzinfo=UTC)),
        (
            "2026-09-24T14:03:11-05:00",
            datetime(2026, 9, 24, 14, 3, 11, tzinfo=timezone(timedelta(hours=-5))),
        ),
        ("2026-09-24", datetime(2026, 9, 24, tzinfo=UTC)),
        (datetime(2026, 9, 24, 14, 3), datetime(2026, 9, 24, 14, 3, tzinfo=UTC)),
    ],
)
def test_a_timestamp_becomes_an_aware_instant(raw: Any, expected: datetime) -> None:
    got = mapper._instant(raw)
    assert got == expected and got is not None and got.tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "yesterday", "2026-13-45 99:00:00", True])
def test_an_unreadable_timestamp_is_none_never_a_raise(raw: Any) -> None:
    """Gotcha 148: one bad field must not cost the row, or the tables after it."""
    assert mapper._instant(raw) is None


def test_the_work_order_carries_its_last_change() -> None:
    row = mapper.map_work_order({**WORK_ORDER, "dateTimeModified": "2026-09-24 14:03:11"})
    assert row["pms_updated_at"] == datetime(2026, 9, 24, 14, 3, 11, tzinfo=UTC)
    assert mapper.map_work_order(WORK_ORDER)["pms_updated_at"] is None


def _handler(work_order: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/properties"):
            return httpx.Response(
                200, json=wire("GET /properties", PROPERTY), headers=_complete([PROPERTY])
            )
        if path.endswith("/maintenance/work-orders"):
            body = wire("GET /maintenance/work-orders", work_order)
            return httpx.Response(200, json=body, headers=_complete([work_order]))
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler


@pytest.mark.asyncio
async def test_the_sync_writes_it_as_a_timestamp_not_the_specs_string() -> None:
    """The pool refuses a ``str`` for a ``timestamptz`` the way asyncpg does."""
    pool, _log = _pool()
    work_order = {**WORK_ORDER, "dateTimeModified": "2026-09-24 14:03:11"}
    result = await sync.sync_company(
        pool, CO, settings=_app(), client=_client(_handler(work_order))
    )
    assert result.rows_by_table["helixis_work_orders"] == 1, result.failed
    (insert,) = [
        c.args
        for c in pool._conn.execute.await_args_list
        if c.args[0].startswith("INSERT INTO helixis_work_orders")
    ]
    at = 5 + sync.TABLE_COLUMNS["helixis_work_orders"].index("pms_updated_at")
    assert insert[at] == datetime(2026, 9, 24, 14, 3, 11, tzinfo=UTC)
