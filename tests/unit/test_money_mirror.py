"""Money mirror, slice 2 (migration 0042) — GL accounts + bills.

Pins: the GL chart upsert's hot columns, the bill upsert's date coercion
(Buildium ships bill dates as datetimes; the columns are DATE) and
WorkOrderId pull from the permissive dump, and the replace-on-upsert
bill-line semantics (delete-then-insert so an edited bill can't leave
stale lines behind).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.buildium.schemas import Bill, GLAccount
from helixis.db.repositories import entities


class _CapturingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))


def _pool(conn: _CapturingConn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_gl_account_upsert_extracts_hot_columns() -> None:
    gl = GLAccount.model_validate(
        {
            "Id": 9001,
            "AccountNumber": "6200",
            "Name": "Repairs & Maintenance",
            "Type": "Expense",
            "SubType": "CurrentLiability",
            "IsActive": True,
            "IsBankAccount": False,
            "ParentGLAccountId": 9000,
        }
    )
    conn = _CapturingConn()
    await entities.upsert_gl_account(_pool(conn), gl, account_id=1, company_id="co-1")

    sql, args = conn.calls[0]
    assert "INSERT INTO helixis_gl_accounts" in sql
    assert "6200" in args and "Repairs & Maintenance" in args
    assert "Expense" in args and 9000 in args


@pytest.mark.asyncio
async def test_bill_upsert_coerces_dates_and_replaces_lines() -> None:
    bill = Bill.model_validate(
        {
            "Id": 5100,
            "VendorId": 70239,
            "Date": "2026-07-01T00:00:00",
            "DueDate": "2026-07-31T00:00:00",
            "TotalAmount": 480.0,
            "IsPaid": False,
            "ReferenceNumber": "INV-88",
            "WorkOrderId": 3355,
            "Lines": [
                {
                    "Id": 1,
                    "PropertyId": 10282,
                    "UnitId": 55,
                    "GLAccountId": 9001,
                    "Amount": 300.0,
                    "Memo": "labor",
                },
                {
                    "Id": 2,
                    "PropertyId": 10282,
                    "GLAccountId": 9002,
                    "Amount": 180.0,
                    "Memo": "parts",
                },
            ],
        }
    )
    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), bill, account_id=1, company_id="co-1")

    bill_sql, bill_args = conn.calls[0]
    assert "INSERT INTO helixis_bills" in bill_sql
    # Buildium's datetime strings land as plain dates in the DATE columns.
    assert date(2026, 7, 1) in bill_args and date(2026, 7, 31) in bill_args
    assert 70239 in bill_args and 480.0 in bill_args
    # WorkOrderId comes off the permissive dump (no typed field on Bill).
    assert 3355 in bill_args

    # Replace-on-upsert: lines are wiped then re-inserted in index order.
    delete_sql, delete_args = conn.calls[1]
    assert "DELETE FROM helixis_bill_lines" in delete_sql
    assert delete_args == ("co-1", 5100)
    line_calls = conn.calls[2:]
    assert len(line_calls) == 2
    assert all("INSERT INTO helixis_bill_lines" in sql for sql, _ in line_calls)
    first, second = (args for _, args in line_calls)
    assert first[2] == 0 and 9001 in first and "labor" in first and 55 in first
    assert second[2] == 1 and 9002 in second and "parts" in second


#: Position of ``total_amount`` in the helixis_bills INSERT args. Pinned by
#: index because value-membership can't tell 0.0 from the ``is_paid`` False
#: that sits beside it.
_TOTAL_AMOUNT_ARG = 6
_IS_PAID_ARG = 7


@pytest.mark.asyncio
async def test_a_production_bill_is_mirrored_at_its_line_total() -> None:
    """Production bills carry NO top-level TotalAmount — 0 of 131 mirrored
    bills had one. Storing that None left every row NULL, and the mirror tool
    rendered the whole payables book as $0.00 (battery v2, Q2). The mirror
    must derive the total from the lines the way the live tool does."""
    bill = Bill.model_validate(
        {
            "Id": 5101,
            "VendorId": 70239,
            "Date": "2026-07-01T00:00:00",
            "IsPaid": False,
            "Lines": [
                {"Id": 1, "PropertyId": 10282, "GLAccountId": 9001, "Amount": 300.0},
                {"Id": 2, "PropertyId": 10282, "GLAccountId": 9002, "Amount": 180.0},
            ],
        }
    )
    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), bill, account_id=1, company_id="co-1")
    _sql, args = conn.calls[0]
    assert args[_TOTAL_AMOUNT_ARG] == 480.0


@pytest.mark.asyncio
async def test_paid_status_comes_from_paid_date_not_the_null_is_paid_flag() -> None:
    """``IsPaid`` is present and NULL on every production bill, so storing it
    verbatim left the mirror unable to tell paid from unpaid — and
    ``is_paid IS NOT TRUE`` then billed the customer for all 131."""
    paid = Bill.model_validate(
        {"Id": 5103, "VendorId": 1, "IsPaid": None, "PaidDate": "2026-02-13", "Lines": []}
    )
    unpaid = Bill.model_validate({"Id": 5104, "VendorId": 1, "IsPaid": None, "Lines": []})

    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), paid, account_id=1, company_id="co-1")
    assert conn.calls[0][1][_IS_PAID_ARG] is True

    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), unpaid, account_id=1, company_id="co-1")
    assert conn.calls[0][1][_IS_PAID_ARG] is False


@pytest.mark.asyncio
async def test_an_explicit_is_paid_flag_still_wins() -> None:
    """If Buildium ever does populate it, believe it."""
    bill = Bill.model_validate({"Id": 5105, "VendorId": 1, "IsPaid": True, "Lines": []})
    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), bill, account_id=1, company_id="co-1")
    assert conn.calls[0][1][_IS_PAID_ARG] is True


@pytest.mark.asyncio
async def test_a_bill_with_no_amount_anywhere_stays_null_not_zero() -> None:
    """NULL means "unknown". Writing 0.0 would launder a missing amount into
    a real-looking figure — which is the failure this whole fix is about."""
    bill = Bill.model_validate({"Id": 5102, "VendorId": 70239, "IsPaid": False, "Lines": []})
    conn = _CapturingConn()
    await entities.upsert_bill(_pool(conn), bill, account_id=1, company_id="co-1")
    _sql, args = conn.calls[0]
    assert args[_TOTAL_AMOUNT_ARG] is None


@pytest.mark.asyncio
async def test_gl_account_event_refreshes_whole_chart_and_prunes() -> None:
    """GLAccount.* events refetch the full chart (the envelope's id field
    is undocumented — no key guessing) and prune vanished rows."""
    from unittest.mock import AsyncMock, patch

    from helixis.webhooks.handlers import finance as finance_h

    @asynccontextmanager
    async def _yield_api(api):
        yield api

    gl_a, gl_b = MagicMock(id=9001), MagicMock(id=9002)
    api = MagicMock()
    api.general_ledger.list_accounts = AsyncMock(return_value=[gl_a, gl_b])

    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_gl_account", new=AsyncMock()) as upsert,
        patch.object(finance_h.entities_repo, "prune_gl_accounts", new=AsyncMock()) as prune,
    ):
        await finance_h.handle_gl_account_changed(
            "co-1", {"EventName": "GLAccount.Updated", "AccountId": 42}
        )
    assert upsert.await_count == 2
    prune.assert_awaited_once()
    assert prune.await_args.args[1] == [9001, 9002]


@pytest.mark.asyncio
async def test_gl_prune_deletes_rows_not_in_live_chart() -> None:
    conn = _CapturingConn()
    await entities.prune_gl_accounts(_pool(conn), [9001, 9002], company_id="co-1")
    sql, args = conn.calls[0]
    assert "DELETE FROM helixis_gl_accounts" in sql
    assert args == ("co-1", [9001, 9002])


@pytest.mark.asyncio
async def test_bill_delete_drops_lines_then_bill() -> None:
    conn = _CapturingConn()
    await entities.delete_bill(_pool(conn), 5100, company_id="co-1")

    assert "DELETE FROM helixis_bill_lines" in conn.calls[0][0]
    assert "DELETE FROM helixis_bills" in conn.calls[1][0]
    assert conn.calls[1][1] == (5100, "co-1")


@pytest.mark.asyncio
async def test_bank_account_upsert_strips_credentials_and_extracts_columns() -> None:
    """Account/routing numbers and payment config NEVER land at rest."""
    import json

    from helixis.buildium.schemas import BankAccount

    bank = BankAccount.model_validate(
        {
            "Id": 31,
            "Name": "Operating",
            "Description": "Main operating account",
            "BankAccountType": "Checking",
            "Country": "UnitedStates",
            "IsActive": True,
            "Balance": 15230.55,
            "GLAccount": {"Id": 9001, "Name": "Operating Cash"},
            "AccountNumber": "****1234",
            "AccountNumberUnmasked": "000111222333",
            "RoutingNumber": "021000021",
            "CheckPrintingInfo": {"SignatureHeading": "x"},
            "ElectronicPayments": {"IsVendorPaymentEnabled": True},
        }
    )
    conn = _CapturingConn()
    await entities.upsert_bank_account(_pool(conn), bank, account_id=1, company_id="co-1")

    sql, args = conn.calls[0]
    assert "INSERT INTO helixis_bank_accounts" in sql
    raw = json.loads(args[-1])
    for leaked in (
        "AccountNumber",
        "AccountNumberUnmasked",
        "RoutingNumber",
        "CheckPrintingInfo",
        "ElectronicPayments",
    ):
        assert leaked not in raw
    # Hot columns: balance, type, and the GL join key off the nested ref.
    assert 15230.55 in args and "Checking" in args and 9001 in args


@pytest.mark.asyncio
async def test_bank_account_prune_deletes_vanished_rows() -> None:
    conn = _CapturingConn()
    await entities.prune_bank_accounts(_pool(conn), [31, 32], company_id="co-1")
    sql, args = conn.calls[0]
    assert "DELETE FROM helixis_bank_accounts" in sql
    assert args == ("co-1", [31, 32])
