"""Rentvine bills: ``GET /accounting/payables/search`` -> ``helixis_bills``.

One payables row is one bill LINE (the bill record, with its ``transaction``,
``ledger`` and ``property`` beside it). ``mapper.group_payables`` folds lines
into bills, ``mapper.map_bill`` computes the total and the paid flag, and
``sync._sync_bills`` writes them and prunes what a COMPLETE read no longer
returns. All rows are synthetic (ids 990 000 xxx); the live row shape is NOT
VERIFIED (runbook "Bills").
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import mapper, sync
from helixis.settings import RentvineSettings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

CO = "6a456191-0000-4000-8000-000000000001"
PLUMBER, ELECTRIC = 990000501, 990000502


def _row(
    bill_id: int,
    txn_id: int,
    amount: Any,
    paid: Any,
    *,
    payee: int = PLUMBER,
    bill_voided: int = 0,
    line_voided: int = 0,
    ledger_type: int | None = 3,
    prop: int | None = 990000101,
    unit: int | None = None,
) -> dict[str, Any]:
    """A row AFTER the client unwrapped ``bill`` and attached the siblings."""
    return {
        "billID": bill_id,
        "parentBillID": None,
        "billDate": "2026-09-20",
        "dateDue": "2026-10-15",
        "reference": "INV-1",
        "isVoided": bill_voided,
        "description": "Kitchen sink repair",
        "payeeContactID": payee,
        "transaction": {
            "transactionID": txn_id,
            "billID": bill_id,
            "amount": amount,
            "amountPaid": paid,
            "isVoided": line_voided,
            "chargeAccountID": 990000851,
        },
        "ledger": {"ledgerID": 990000801, "ledgerTypeID": ledger_type},
        "property": None if prop is None else {"propertyID": prop},
        "unit": None if unit is None else {"unitID": unit},
    }


def _bills(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    records, _ = mapper.group_payables(rows)
    return {r["billID"]: mapper.map_bill(r) for r in records}


# ── the mapper ────────────────────────────────────────────────────────────────


def test_lines_fold_into_one_bill_with_the_summed_total():
    (bill,) = _bills([_row(1, 11, "300.00", "0.00"), _row(1, 12, "180.00", "0.00")]).values()
    assert bill["buildium_id"] == 1
    assert bill["total_amount"] == Decimal("480.00")
    assert bill["is_paid"] is False
    assert bill["vendor_id"] == PLUMBER
    assert bill["pms"] == "rentvine"
    assert [ln["transactionID"] for ln in bill["raw"]["lines"]] == [11, 12]


def test_a_fully_paid_bill_is_paid():
    (bill,) = _bills([_row(2, 21, "220.00", "220.00")]).values()
    assert bill["is_paid"] is True


def test_a_part_paid_bill_is_unpaid_at_its_full_total():
    """Same as the Buildium path: owed means the bill's total until it is paid."""
    (bill,) = _bills([_row(3, 31, "300.00", "300.00"), _row(3, 32, "200.00", "50.00")]).values()
    assert bill["is_paid"] is False
    assert bill["total_amount"] == Decimal("500.00")


def test_an_unreadable_amount_makes_the_total_unknown_not_smaller():
    (bill,) = _bills([_row(4, 41, "300.00", "0.00"), _row(4, 42, "n/a", "0.00")]).values()
    assert bill["total_amount"] is None
    assert bill["is_paid"] is None


def test_an_unreadable_paid_amount_makes_paid_unknown():
    (bill,) = _bills([_row(5, 51, "300.00", None)]).values()
    assert bill["total_amount"] == Decimal("300.00")
    assert bill["is_paid"] is None


def test_a_voided_bill_is_left_out_and_counted():
    records, voided = mapper.group_payables(
        [_row(6, 61, "999.00", "0.00", bill_voided=1), _row(7, 71, "10.00", "0.00")]
    )
    assert [r["billID"] for r in records] == [7]
    assert voided == 1


def test_a_voided_line_is_not_in_the_total():
    rows = [_row(8, 81, "100.00", "0.00"), _row(8, 82, "50.00", "0.00", line_voided=1)]
    assert _bills(rows)[8]["total_amount"] == Decimal("100.00")


def test_a_bill_whose_every_line_is_voided_is_left_out():
    records, voided = mapper.group_payables([_row(9, 91, "40.00", "0.00", line_voided=1)])
    assert records == [] and voided == 1


def test_no_property_and_no_work_order_are_claimed():
    """ADR 0010 Decision (b): a line's ledger decides whether it belongs to a
    property, and the bill table has no such column; the payables bill
    carries no workOrderID. The ledger and property ride in raw instead."""
    (bill,) = _bills([_row(10, 101, "75.00", "0.00", ledger_type=2, prop=None)]).values()
    assert bill["work_order_id"] is None
    assert "property_id" not in bill
    line = bill["raw"]["lines"][0]
    assert line["ledgerTypeID"] == 2 and line["propertyID"] is None


# ── bill lines (ADR 0010 Decision (b)) ────────────────────────────────────────


def _lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records, _ = mapper.group_payables(rows)
    return [ln for r in records for ln in mapper.bill_lines(r)]


def test_a_property_or_unit_ledger_line_is_attributed_to_its_property():
    rows = [
        _row(20, 202, "80.00", "0.00", ledger_type=4, unit=990000301),
        _row(20, 201, "120.00", "0.00", ledger_type=3),
    ]
    first, second = _lines(rows)
    # Ordered by transaction id, so line_index is stable between runs.
    assert (first["line_index"], second["line_index"]) == (0, 1)
    assert first["amount"] == Decimal("120.00") and first["ledger_type"] == 3
    assert first["property_id"] == 990000101 and first["unit_id"] is None
    assert second["ledger_type"] == 4
    assert second["property_id"] == 990000101 and second["unit_id"] == 990000301
    assert first["gl_account_id"] == 990000851
    assert first["ledger_id"] == 990000801
    assert first["bill_id"] == 20


@pytest.mark.parametrize("ledger_type", [1, 2])
def test_a_manager_or_portfolio_ledger_line_is_attributed_to_no_property(ledger_type):
    """Even when Rentvine sends a property beside it: splitting portfolio
    money across properties would be an allocation basis nobody chose."""
    (line,) = _lines([_row(21, 211, "500.00", "0.00", ledger_type=ledger_type, unit=990000301)])
    assert line["property_id"] is None and line["unit_id"] is None
    assert line["ledger_type"] == ledger_type
    assert line["amount"] == Decimal("500.00")


@pytest.mark.parametrize("ledger_type", [None, 0, 5])
def test_an_unknown_ledger_type_attributes_nothing_and_stores_no_type(ledger_type):
    (line,) = _lines([_row(22, 221, "10.00", "0.00", ledger_type=ledger_type)])
    assert line["property_id"] is None and line["ledger_type"] is None


def test_a_voided_line_writes_no_line_row():
    rows = [_row(23, 231, "10.00", "0.00"), _row(23, 232, "99.00", "0.00", line_voided=1)]
    assert [ln["amount"] for ln in _lines(rows)] == [Decimal("10.00")]


def test_the_unit_sibling_does_not_leak_onto_the_bill():
    (record,), _ = mapper.group_payables([_row(24, 241, "10.00", "0.00", unit=990000301)])
    assert "unit" not in record


def test_money_is_a_decimal_and_dates_are_dates():
    from datetime import date

    (bill,) = _bills([_row(11, 111, "12.34", "0.00")]).values()
    assert isinstance(bill["total_amount"], Decimal)
    assert bill["bill_date"] == date(2026, 9, 20) and bill["due_date"] == date(2026, 10, 15)


# ── the client carries nullable siblings ──────────────────────────────────────


def _client(rows: list[dict[str, Any]], *, headers: dict[str, str] | None = None) -> Any:
    hdrs = {"pagination-total-pages": "1"} if headers is None else headers

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows, headers=hdrs)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
    return rvclient.RentvineClient(
        RentvineSettings(enabled=True, account_code="acme", api_key="k", api_secret="s"),
        client=http,
    )


def _wire(**over: Any) -> dict[str, Any]:
    row = {
        "bill": {"billID": 1, "payeeContactID": PLUMBER},
        "transaction": {"transactionID": 11, "amount": "1.00", "amountPaid": "0.00"},
        "ledger": {"ledgerID": 1, "ledgerTypeID": 1},
        "property": None,
    }
    row.update(over)
    return row


async def _read(c: Any) -> Any:
    async with c:
        return await c.iter_all(
            sync.PAYABLES_PATH, companion="transaction", nullable_companions=("ledger", "property")
        )


def test_a_null_sibling_is_kept_as_none():
    got = asyncio.run(_read(_client([_wire()])))
    assert got.rows[0]["property"] is None
    assert got.rows[0]["transaction"]["transactionID"] == 11


def test_a_missing_sibling_key_still_raises():
    row = _wire()
    del row["property"]
    with pytest.raises(rvclient.RentvineAPIError, match="property"):
        asyncio.run(_read(_client([row])))


def test_a_sibling_the_spec_does_not_declare_is_refused():
    async def go() -> Any:
        async with _client([_wire()]) as c:
            return await c.iter_all(sync.PAYABLES_PATH, nullable_companions=("tenant",))

    with pytest.raises(rvclient.RentvineAPIError, match="no 'tenant' companion"):
        asyncio.run(go())


def _lease_client(rows: list[dict[str, Any]]) -> Any:
    return _client(rows)


async def _read_leases(c: Any) -> Any:
    async with c:
        return await c.iter_all("/leases/export", optional_companions=("balances",))


def test_an_optional_sibling_that_is_missing_or_null_is_none_and_never_fails_the_read():
    """``balances`` rides beside the LIVE lease export: its absence must cost
    the balance (unknown), never the lease."""
    rows = [
        {"lease": {"leaseID": 1}, "balances": {"unpaidTotalAmount": 10}},
        {"lease": {"leaseID": 2}, "balances": None},
        {"lease": {"leaseID": 3}},
        {"lease": {"leaseID": 4}, "balances": "junk"},
    ]
    got = asyncio.run(_read_leases(_lease_client(rows)))
    assert [r["leaseID"] for r in got.rows] == [1, 2, 3, 4]
    # Carried as sent (the mapper judges "junk"); a missing key is None.
    assert [r["balances"] for r in got.rows] == [{"unpaidTotalAmount": 10}, None, None, "junk"]


def test_an_optional_sibling_the_spec_does_not_declare_is_refused():
    async def go() -> Any:
        async with _lease_client([{"lease": {"leaseID": 1}}]) as c:
            return await c.iter_all("/leases/export", optional_companions=("tenant",))

    with pytest.raises(rvclient.RentvineAPIError, match="no 'tenant' companion"):
        asyncio.run(go())


# ── the sync stage ────────────────────────────────────────────────────────────


def _pool() -> Any:
    conn = MagicMock()

    def _enforce(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_enforce)
    # ``_write_bill_lines`` replaces each bill's lines inside one transaction.
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


def _sql(pool: Any, prefix: str) -> list[Any]:
    return [c for c in pool._conn.execute.await_args_list if c.args[0].startswith(prefix)]


def _payables_wire() -> list[dict[str, Any]]:
    return [
        _wire(
            bill={"billID": 990000701, "payeeContactID": PLUMBER, "isVoided": 0},
            transaction={"transactionID": 1, "amount": "300.00", "amountPaid": "0.00"},
        ),
        _wire(
            bill={"billID": 990000702, "payeeContactID": ELECTRIC, "isVoided": 0},
            transaction={"transactionID": 2, "amount": "220.00", "amountPaid": "220.00"},
        ),
    ]


def _sync(client: Any) -> tuple[Any, sync.SyncResult]:
    pool = _pool()
    result = sync.SyncResult(company_id=CO)

    async def go() -> None:
        async with client:
            await sync._sync_bills(pool, client, CO, result)

    asyncio.run(go())
    return pool, result


def test_a_complete_read_writes_every_bill_and_prunes_the_rest():
    pool, result = _sync(_client(_payables_wire()))
    assert result.rows_by_table["helixis_bills"] == 2
    assert "helixis_bills" in result.confirmed_complete
    inserts = _sql(pool, "INSERT INTO helixis_bills")
    assert len(inserts) == 2
    assert all("ON CONFLICT (company_id, buildium_id)" in c.args[0] for c in inserts)
    (prune,) = _sql(pool, "DELETE FROM helixis_bills")
    assert "company_id = $1::uuid" in prune.args[0] and "pms = 'rentvine'" in prune.args[0]
    assert prune.args[1] == CO
    assert sorted(prune.args[2]) == [990000701, 990000702]
    # Recorded for sync_cycle's prune log line (every deletion is said).
    assert "helixis_bills" in result.pruned
    # The lines: each written bill's are replaced, and the lines of a bill the
    # complete read no longer returns are pruned with it.
    assert "helixis_bill_lines" in result.confirmed_complete
    assert result.rows_by_table["helixis_bill_lines"] == 2
    (line_prune,) = [
        c for c in _sql(pool, "DELETE FROM helixis_bill_lines") if "NOT (" in c.args[0]
    ]
    assert "company_id = $1::uuid" in line_prune.args[0]
    assert "pms = 'rentvine'" in line_prune.args[0]
    assert sorted(line_prune.args[2]) == [990000701, 990000702]


def test_line_rows_carry_their_ledger_and_property():
    wire = [
        _wire(
            bill={"billID": 990000703, "payeeContactID": PLUMBER, "isVoided": 0},
            transaction={
                "transactionID": 3,
                "amount": "75.00",
                "amountPaid": "0.00",
                "chargeAccountID": 990000851,
            },
            ledger={"ledgerID": 990000802, "ledgerTypeID": 4},
            property={"propertyID": 990000101},
            unit={"unitID": 990000301},
        )
    ]
    pool, result = _sync(_client(wire))
    calls = pool._conn.execute.await_args_list
    (insert,) = [c for c in calls if "INSERT INTO helixis_bill_lines" in c.args[0]]
    args = insert.args[1:]
    assert args[0] == CO and args[1] == 990000703 and args[2] == 0 and args[3] == "rentvine"
    assert args[4:7] == (990000101, 990000301, 990000851)
    assert args[7] == Decimal("75.00")
    assert args[9:] == (990000802, 4)


def test_a_read_whose_end_was_guessed_deletes_nothing():
    pool, result = _sync(_client(_payables_wire(), headers={}))
    assert result.rows_by_table["helixis_bills"] == 2
    assert "helixis_bills" not in result.confirmed_complete
    assert "helixis_bill_lines" not in result.confirmed_complete
    # Nothing is PRUNED: no bill row, and no line of a bill this read did not
    # return. The only deletes allowed are each WRITTEN bill replacing its own
    # lines (scoped to that one bill_id), which a partial read is entitled to.
    assert _sql(pool, "DELETE FROM helixis_bills") == []
    line_deletes = _sql(pool, "DELETE FROM helixis_bill_lines")
    assert line_deletes, "each written bill replaces its lines"
    assert all("NOT (" not in c.args[0] for c in line_deletes)
    assert all("bill_id = $2" in c.args[0] for c in line_deletes)
    assert sorted(c.args[2] for c in line_deletes) == [990000701, 990000702]
    assert "helixis_bills" not in result.pruned


def test_a_failed_read_is_stale_and_deletes_nothing():
    class _Down:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def iter_all(self, *a: Any, **k: Any) -> Any:
            raise rvclient.RentvineAPIError(
                403, rvclient.transport.Failure.PERMISSION, False, "forbidden"
            )

    pool, result = _sync(_Down())
    assert "helixis_bills" in result.failed
    assert "helixis_bill_lines" in result.failed
    assert "helixis_bills" not in result.rows_by_table
    assert pool._conn.execute.await_args_list == []


def test_an_account_with_no_bills_prunes_to_empty():
    """A complete, empty read is a fact: every Rentvine bill on file is gone."""
    pool, result = _sync(_client([]))
    assert result.rows_by_table["helixis_bills"] == 0
    (prune,) = _sql(pool, "DELETE FROM helixis_bills")
    assert prune.args[2] == []


def test_bills_are_stamped_fresh_under_the_buildium_stage_name():
    assert sync.SYNC_STATE_STAGE["helixis_bills"] == "bills"
    assert "helixis_bills" in sync.PAGED_TABLES


def test_bill_lines_are_fed_and_share_the_bills_stage():
    """Re-pointed 2026-10-02: migration 0104 added the ledger columns ADR 0010
    Decision (b) asked for, so the lines sync (property-attributed only for
    property/unit ledgers) and spend by category or property answers."""
    from helixis.db.pms_coverage import FED_BY, RENTVINE

    assert {"helixis_bills", "helixis_bill_lines", "helixis_gl_accounts"} <= FED_BY[RENTVINE]
    assert sync.SYNC_STATE_STAGE["helixis_bill_lines"] == "bills"
