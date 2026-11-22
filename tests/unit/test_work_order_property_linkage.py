"""A work order's property comes from its backing task, or from nowhere.

``GET /workorders`` sends no property of its own — not nested, not flat, null on
every production row — so ``helixis_work_orders.property_id`` was NULL on all 60
mirrored rows for the live account (and on 21 and 12 for two others) while
title, status and priority filled in fine. Every surface that groups maintenance
by building read the whole book as property-less.

The only portfolio reference the payload carries is ``WorkOrder.Task.Id``. These
tests pin the resolution that hangs off it, in both arrival orders, plus the
shared field-shape rule the three reading layers now agree on.

⚠ The pool is mocked, as everywhere else in the mirror tests, so what is under
test is the SQL and the parameter mapping — not the database's behaviour. The
live backfill was verified separately against helixis-test (93 of 93 rows
resolved), because a mock cannot tell you a join matches real data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.buildium.linkage import task_property_id, work_order_task_id
from helixis.db.repositories import entities


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool_and_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool, conn


# ── the shared rule ─────────────────────────────────────────────────────────


def test_task_property_comes_from_the_nested_object() -> None:
    """The shape production sends. Verified 2026-08-24: nested ``Property.Id``
    on 90 of 90 mirrored task rows, flat ``PropertyId`` on ZERO of them."""
    assert task_property_id({"Id": 39517, "Property": {"Id": 38038}}) == 38038


def test_task_property_falls_back_to_the_flat_key() -> None:
    """Our own write bodies use the flat key — ``endpoints/tasks.py`` converts
    nested → flat for the read-merge-write PUT — so it must still parse."""
    assert task_property_id({"Id": 1, "PropertyId": 31}) == 31


def test_nested_wins_when_both_are_present() -> None:
    assert task_property_id({"Property": {"Id": 38038}, "PropertyId": 31}) == 38038


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"Property": None},
        {"Property": {}},
        {"Property": {"Id": None}},
        {"PropertyId": None},
        {"PropertyId": "not-a-number"},
        None,
    ],
)
def test_unresolvable_property_is_none_not_a_guess(raw: object) -> None:
    """None means UNKNOWN. Callers rely on that to avoid overwriting a property
    they already know — the same discipline as a NULL bill amount never
    laundering into $0.00."""
    assert task_property_id(raw) is None


def test_work_order_task_id_reads_the_typed_model_and_the_raw_blob() -> None:
    """Both shapes occur: a parsed ``WorkOrder`` during ingest, a stored ``raw``
    dict when reasoning about mirrored rows."""
    typed = MagicMock()
    typed.task = MagicMock(id=39544)
    assert work_order_task_id(typed) == 39544

    plain = {"Id": 11369, "Task": {"Id": 39544}}
    assert work_order_task_id(plain) == 39544


def test_a_work_order_with_no_backing_task_resolves_to_none() -> None:
    no_task = MagicMock()
    no_task.task = None
    assert work_order_task_id(no_task) is None


# ── direction 1: the task is already mirrored ───────────────────────────────


def _work_order(*, task_id: int | None = 39544, property_id: int | None = None) -> MagicMock:
    wo = MagicMock()
    wo.id = 11369
    wo.property_id = property_id
    wo.unit_id = None
    wo.effective_unit_id = None
    wo.title = "Water stain spreading on bedroom ceiling"
    wo.status = "New"
    wo.priority = "Normal"
    wo.category_id = None
    wo.assigned_to_user_id = None
    wo.task = MagicMock(id=task_id) if task_id is not None else None
    wo.model_dump = MagicMock(return_value={"Id": 11369})
    return wo


@pytest.mark.asyncio
async def test_upsert_resolves_property_through_the_task_mirror() -> None:
    """The payload has no property, so the INSERT must go and find one.

    Asserting on the SQL rather than a returned value because that is where the
    resolution lives — doing it in Python would cost a second round trip per
    work order, and the backfill mirrors tasks first precisely so this join is
    already satisfied.
    """
    pool, conn = _pool_and_conn()

    await entities.upsert_work_order(pool, _work_order(), account_id=7, company_id="co-1")

    sql = conn.execute.await_args.args[0]
    assert "helixis_tasks" in sql, "the property is not resolved from anywhere"
    assert "COALESCE" in sql


@pytest.mark.asyncio
async def test_the_task_id_is_stored_as_a_column() -> None:
    """A real column, not a JSON path: both directions become indexed lookups
    and no query has to know the payload's shape."""
    pool, conn = _pool_and_conn()

    await entities.upsert_work_order(pool, _work_order(), account_id=7, company_id="co-1")

    args = conn.execute.await_args.args
    assert "task_id" in args[0]
    assert 39544 in args[1:]


@pytest.mark.asyncio
async def test_an_unresolved_upsert_never_clobbers_a_known_property() -> None:
    """NULL means "not resolved yet", not "has no property".

    Without the COALESCE on the conflict branch, a work-order webhook arriving
    before its task would blank a property an earlier pass had resolved, and the
    column would flicker with delivery order.
    """
    pool, conn = _pool_and_conn()

    await entities.upsert_work_order(pool, _work_order(), account_id=7, company_id="co-1")

    sql = conn.execute.await_args.args[0]
    assert "property_id         = COALESCE(EXCLUDED.property_id," in sql, (
        "the conflict branch overwrites property_id; an unresolved upsert will "
        "erase a property that was already known"
    )
    assert "task_id             = COALESCE(EXCLUDED.task_id," in sql


# ── direction 2: the work order arrived first ───────────────────────────────


def _task(*, task_id: int = 39544, property_id: int | None = 37994) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = "Water stain spreading on bedroom ceiling"
    dumped: dict[str, object] = {"Id": task_id, "TaskType": "Todo", "TaskStatus": "New"}
    if property_id is not None:
        dumped["Property"] = {"Id": property_id}
    task.model_dump = MagicMock(return_value=dumped)
    return task


@pytest.mark.asyncio
async def test_upserting_a_task_repairs_work_orders_that_arrived_first() -> None:
    """Webhook order is not guaranteed — a WorkOrder.Created can beat its own
    Task.Created — so resolution has to work from this side too."""
    pool, conn = _pool_and_conn()

    await entities.upsert_task(pool, _task(), account_id=7, company_id="co-1")

    statements = [call.args[0] for call in conn.execute.await_args_list]
    repairs = [s for s in statements if "UPDATE helixis_work_orders" in s]
    assert len(repairs) == 1, "a task upsert does not propagate to its work orders"

    repair_args = next(
        c.args for c in conn.execute.await_args_list if "UPDATE helixis_work_orders" in c.args[0]
    )
    assert repair_args[1:] == ("co-1", 39544, 37994)
    assert "task_id" in repairs[0]
    assert "IS DISTINCT FROM" in repairs[0], (
        "the repair rewrites rows that already agree, so every task touch writes"
    )


@pytest.mark.asyncio
async def test_a_task_with_no_property_does_not_blank_its_work_orders() -> None:
    """Same rule as the upsert's COALESCE, on the other side of the link.

    A task whose property is unknown must leave the work order alone rather than
    propagate the unknown — otherwise the repair path becomes a way to lose data
    that the insert path was careful to protect.
    """
    pool, conn = _pool_and_conn()

    await entities.upsert_task(pool, _task(property_id=None), account_id=7, company_id="co-1")

    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert not [s for s in statements if "UPDATE helixis_work_orders" in s]


@pytest.mark.asyncio
async def test_the_task_mirror_reads_the_nested_property_too() -> None:
    """``upsert_task`` was already correct; this pins it against the shared rule
    so the two cannot drift apart the way the enricher did."""
    pool, conn = _pool_and_conn()

    await entities.upsert_task(pool, _task(), account_id=7, company_id="co-1")

    insert_args = next(
        c.args for c in conn.execute.await_args_list if "INSERT INTO helixis_tasks" in c.args[0]
    )
    assert 37994 in insert_args[1:]
