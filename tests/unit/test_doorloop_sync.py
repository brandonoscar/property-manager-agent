"""One company's DoorLoop pass, end to end.

``sync_company`` runs for real over a fake DoorLoop (an ``httpx.MockTransport``
paging a synthetic account the way the Pagination page documents) and a
recording pool that refuses what asyncpg refuses. What is asserted is what
would have been sent to DoorLoop and to Postgres.

The rules: a switched-off connector or a company without a usable key is
refused before any call; every request is a documented GET of a list at the
largest page; a list that fails is STALE with a sentence the customer can act
on, and the rest land; a partial read is written but neither stamped nor
pruned; the ledger is written only from four complete reads; a balance that
did not come through keeps the old snapshot; the call budget stops a pass and
stales what it did not reach; DoorLoop's edge 403 holds the account; a
disconnect during the reads writes nothing.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.doorloop import _paths, client, mapper, sync
from helixis.pms import http as pms_http
from helixis.pms import mirror
from helixis.pms.errors import Failure, PmsApiError, PmsUnavailableError
from helixis.pms.mirror import BALANCE_COLUMNS, KEYED_COLUMNS, LINK_TABLE, PER_PARENT, SyncResult
from tests.unit._doorloop_fake import (
    ACCT_BANK,
    ACCT_OFFICE,
    ACCT_RENT,
    API_KEY,
    API_PREFIX,
    APPLICANT_2,
    BILL_1,
    BILL_2,
    COMPANY,
    LEASE_1,
    LEASE_2,
    OWNER_1,
    OWNER_REQUEST_1,
    PROP_1,
    PROP_2,
    PROSPECT,
    TENANT_1,
    TENANT_2,
    TENANT_3,
    TENANT_REQUEST_1,
    TODAY,
    TODO_1,
    UNIT_1,
    UNIT_2,
    UNIT_3,
    VENDOR_1,
    WORK_ORDER_1,
    FakeDoorLoop,
    FakePool,
    account,
    creds_row,
    inserted_rows,
    oid,
    settings,
)

REPO = Path(__file__).resolve().parents[2]
RATE_PAGE = (
    REPO / "docs" / "doorloop" / "vendor" / "api.doorloop.com" / "pages" / "rate-limiting.md"
)

mid = mapper.mirror_id


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The shared client's backoff, so a retried failure costs no real time."""
    waits = AsyncMock()
    monkeypatch.setattr(pms_http, "_sleep", waits)
    return waits


@pytest.fixture
def switched_on(monkeypatch: pytest.MonkeyPatch, slept: AsyncMock) -> None:
    current = settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(sync, "today_in", lambda _zone: TODAY)


async def _run(
    server: FakeDoorLoop | None = None, pool: FakePool | None = None
) -> tuple[SyncResult, FakePool, FakeDoorLoop]:
    server = server or FakeDoorLoop()
    pool = pool or FakePool(creds=creds_row())
    result = await sync.sync_company(pool, COMPANY, http=server.http())
    return result, pool, server


def _by_id(pool: FakePool, table: str) -> dict[int, dict[str, Any]]:
    return {row["buildium_id"]: row for row in inserted_rows(pool, table)}


def _replaced_ledgers(pool: FakePool) -> set[int]:
    """The leases whose ledger this pass replaced (the per-lease DELETE)."""
    return {
        args[2]
        for sql, args in pool.deletes("helixis_lease_transactions")
        if sql.endswith("AND lease_id = $3")
    }


SLOW_DOWN = "DoorLoop asked Occupella to slow down. Sync again in a few minutes."


# ── refusals: nothing read, nothing sent ──────────────────────────────────


async def test_a_switched_off_connector_refuses_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(enabled=False)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    server, pool = FakeDoorLoop(), FakePool(creds=creds_row())
    with pytest.raises(sync.DoorLoopSyncRefused, match="switched off"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == [] and pool.log == []


async def test_a_company_without_a_key_is_refused_before_any_call(switched_on: None) -> None:
    server, pool = FakeDoorLoop(), FakePool(creds=None)
    with pytest.raises(sync.DoorLoopSyncRefused, match="no DoorLoop connection"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == []
    assert [s for s, _a in pool.log if not s.startswith("SELECT")] == [], "nothing was written"


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({**creds_row(), "encrypted_api_key": "not-a-token"}, id="undecryptable"),
        pytest.param(creds_row(api_key="dl-key\r\nX-Evil: 1"), id="header-injection"),
    ],
)
async def test_an_unusable_key_is_refused_before_any_call(
    switched_on: None, row: dict[str, Any]
) -> None:
    server, pool = FakeDoorLoop(), FakePool(creds=row)
    with pytest.raises(sync.DoorLoopSyncRefused, match="could not be decrypted"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert server.requests == []


# ── a clean pass ──────────────────────────────────────────────────────────


async def test_a_clean_pass_lands_every_table_and_stamps_every_stage(switched_on: None) -> None:
    result, pool, _server = await _run()
    assert result.pms == "doorloop" and result.company_id == COMPANY
    assert result.failed == {}
    assert set(result.rows_by_table) == set(sync.SYNC_STATE_STAGE) | sync.DERIVED_TABLES
    assert result.confirmed_complete == set(sync.SYNC_STATE_STAGE)
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values())
    sources = {args[3] for _s, args in pool.inserts("helixis_mirror_sync_state")}
    assert sources == {sync.SYNC_SOURCE}
    # Every row is DoorLoop's and this company's.
    for _sql, args in pool.inserts("helixis_properties"):
        assert args[2:4] == (COMPANY, "doorloop")
    # A complete read prunes what DoorLoop no longer has, by mirror id.
    ((_sql, args),) = pool.deletes("helixis_properties")
    assert args[:2] == (COMPANY, "doorloop")
    assert sorted(args[2]) == sorted([mid(PROP_1), mid(PROP_2)])


async def test_every_request_is_a_documented_list_read_at_the_largest_page(
    switched_on: None,
) -> None:
    _result, _pool, server = await _run()
    assert server.paths() == [path for _name, path, _filters in sync.READS]
    allowed = client.operations()
    for request in server.requests:
        key = f"GET {request.url.path.removeprefix(API_PREFIX)}"
        assert request.method == "GET" and key in _paths.LISTS, key
        assert str(request.url).startswith(f"{_paths.BASE_URL}/")
        params = dict(request.url.params)
        assert set(params) <= allowed[key], (key, sorted(params))
        assert params["page_number"] == "1"
        assert params["page_size"] == str(_paths.MAX_PAGE_SIZE) == "1000"
        assert request.headers["authorization"] == f"bearer {API_KEY}"
        assert API_KEY not in str(request.url)
    # Re-pointed 2026-10-07 from filter_type=WORK_ORDER: one tasks read asks
    # for every type, and work orders and the other tasks each take theirs.
    (tasks,) = [r for r in server.requests if r.url.path.endswith("/tasks")]
    assert "filter_type" not in tasks.url.params


async def test_a_long_list_is_read_page_by_page_to_doorloops_total(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "PAGING", dataclasses.replace(client.PAGING, page_size=2))
    result, pool, server = await _run()
    assert result.failed == {}
    units = [r for r in server.requests if r.url.path.endswith("/units")]
    assert [r.url.params["page_number"] for r in units] == ["1", "2"]
    assert {r.url.params["page_size"] for r in units} == {"2"}
    assert set(_by_id(pool, "helixis_units")) == {mid(UNIT_1), mid(UNIT_2), mid(UNIT_3)}
    assert "units" in pool.stamped()


# ── what lands ────────────────────────────────────────────────────────────


async def test_a_unit_is_occupied_by_an_active_lease_and_vacant_otherwise(
    switched_on: None,
) -> None:
    _result, pool, _server = await _run()
    units = _by_id(pool, "helixis_units")
    assert units[mid(UNIT_1)]["is_occupied"] is True
    assert units[mid(UNIT_2)]["is_occupied"] is False
    assert units[mid(UNIT_3)]["is_occupied"] is False, "its only lease is inactive"


async def test_lease_status_reaches_the_mirror_in_the_readers_word(switched_on: None) -> None:
    """Every lease reader filters ``status = 'Active'``."""
    _result, pool, _server = await _run()
    leases = _by_id(pool, "helixis_leases")
    assert leases[mid(LEASE_1)]["status"] == "Active"
    assert leases[mid(LEASE_2)]["status"] == "INACTIVE"
    assert leases[mid(LEASE_1)]["unit_id"] == mid(UNIT_1)


async def test_a_prospect_never_lands_and_a_co_tenant_who_left_reads_moved_out(
    switched_on: None,
) -> None:
    _result, pool, _server = await _run()
    assert set(_by_id(pool, "helixis_tenants")) == {mid(TENANT_1), mid(TENANT_2), mid(TENANT_3)}
    links = {(args[1], args[2]): args[3] for _s, args in pool.inserts("helixis_lease_tenants")}
    assert links == {
        (mid(LEASE_1), mid(TENANT_1)): "Current",
        (mid(LEASE_1), mid(TENANT_2)): "MovedOut",
        (mid(LEASE_2), mid(TENANT_3)): "MovedOut",
    }


async def test_an_owner_owns_what_the_properties_read_names(switched_on: None) -> None:
    _result, pool, _server = await _run()
    (owner,) = inserted_rows(pool, "helixis_owners")
    assert owner["buildium_id"] == mid(OWNER_1)
    assert owner["property_ids"] == [mid(PROP_1)]


async def test_one_tasks_read_fills_work_orders_and_the_other_tasks(switched_on: None) -> None:
    """Re-pointed 2026-10-07 from "an ignored type filter costs pages, never
    rows": the tasks read now asks for every type, and each table keeps only
    its own (a work order never lands among the tasks, nor a to-do among the
    work orders)."""
    _result, pool, _server = await _run()
    (order,) = inserted_rows(pool, "helixis_work_orders")
    assert order["title"] == "Leaking faucet" and order["status"] == "InProgress"
    tasks = _by_id(pool, "helixis_tasks")
    assert mid(WORK_ORDER_1) not in tasks
    assert {row["task_type"] for row in tasks.values()} == {
        "Todo",
        "ResidentRequest",
        "RentalOwnerRequest",
    }


# ── the ledger: four reads, all complete, replaced per lease ──────────────


async def test_the_ledger_lands_signed_and_replaces_every_leases_history(
    switched_on: None,
) -> None:
    rows = account()
    rows["/lease-reversed-payments"] = [
        {"id": oid(111), "lease": LEASE_1, "date": "2026-10-03", "amountReceived": 100}
    ]
    _result, pool, _server = await _run(FakeDoorLoop(rows))
    ledger = inserted_rows(pool, "helixis_lease_transactions")
    assert sorted((r["transaction_type"], r["total_amount"]) for r in ledger) == [
        ("Charge", Decimal("50")),
        ("Charge", Decimal("1200")),
        ("Credit", Decimal("-25")),
        ("Payment", Decimal("-1100")),
        ("Payment", Decimal("100")),
    ]
    assert {r["lease_id"] for r in ledger} == {mid(LEASE_1)}
    # The lease with no history is replaced too: it ends with none.
    assert _replaced_ledgers(pool) == {mid(LEASE_1), mid(LEASE_2)}
    assert "transactions" in pool.stamped()


@pytest.mark.parametrize(
    "path", ["/lease-charges", "/lease-credits", "/lease-payments", "/lease-reversed-payments"]
)
async def test_the_ledger_waits_for_all_four_of_its_reads(switched_on: None, path: str) -> None:
    """One lease's ledger with its payments missing would read "no payments"."""
    result, pool, _server = await _run(FakeDoorLoop(fail={path: 405}))
    assert result.failed["helixis_lease_transactions"] == sync.read_failure(
        PmsApiError(405, Failure.PERMISSION, False, "synthetic")
    )
    assert pool.inserts("helixis_lease_transactions") == []
    assert _replaced_ledgers(pool) == set()
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values()) - {"transactions"}


async def test_a_ledger_row_without_an_id_keeps_every_leases_history(switched_on: None) -> None:
    """The shared writer replaces each covered lease's ledger and skips a row
    with no id, so that lease would read "no charge". DL-11: the
    leaseTransaction schema lists no id."""
    rows = account()
    del rows["/lease-charges"][0]["id"]
    result, pool, _server = await _run(FakeDoorLoop(rows))
    reason = result.failed["helixis_lease_transactions"]
    assert reason == sync._LEDGER_NO_ID and sync.stale_reason(reason) == reason
    assert pool.inserts("helixis_lease_transactions") == []
    assert _replaced_ledgers(pool) == set()
    assert "transactions" not in pool.stamped()


async def test_a_ledger_read_that_stopped_short_writes_no_ledger(switched_on: None) -> None:
    result, pool, _server = await _run(FakeDoorLoop(totals={"/lease-payments": 2}))
    assert result.failed["helixis_lease_transactions"] == sync._LEDGER_PART_WAY
    assert pool.inserts("helixis_lease_transactions") == []
    assert _replaced_ledgers(pool) == set()
    assert "transactions" not in pool.stamped()


# ── rent balances ─────────────────────────────────────────────────────────


async def test_the_snapshot_holds_leases_that_owe_with_aging_that_adds_up(
    switched_on: None,
) -> None:
    _result, pool, _server = await _run()
    (row,) = inserted_rows(pool, "helixis_lease_balances")
    assert row["lease_id"] == mid(LEASE_1) and row["total_balance"] == Decimal("150")
    # 100 open on a charge 6 days old, 50 on one 127 days old.
    assert [row[name] for name, _ in mapper.BUCKETS] == [
        Decimal("100"),
        Decimal("0"),
        Decimal("0"),
        Decimal("50"),
    ]
    # The lease that owes nothing drops out of the snapshot.
    ((_sql, args),) = pool.deletes("helixis_lease_balances")
    assert args[2] == [mid(LEASE_1)]


async def test_a_lease_without_a_balance_keeps_the_old_snapshot(switched_on: None) -> None:
    rows = account()
    del rows["/leases"][1]["totalBalancedue"]
    result, pool, _server = await _run(FakeDoorLoop(rows))
    reason = result.failed["helixis_lease_balances"]
    assert reason.startswith("DoorLoop sent 1 of 2 leases without a balance")
    assert sync.stale_reason(reason) == reason
    assert pool.inserts("helixis_lease_balances") == []
    assert pool.deletes("helixis_lease_balances") == [], "one unknown balance reads as 'current'"
    assert "balances" not in pool.stamped()
    assert "leases" in pool.stamped(), "the leases themselves came through"


async def test_a_failed_charges_read_keeps_the_totals_and_leaves_aging_unknown(
    switched_on: None,
) -> None:
    result, pool, _server = await _run(FakeDoorLoop(fail={"/lease-charges": 405}))
    assert "helixis_lease_balances" not in result.failed
    (row,) = inserted_rows(pool, "helixis_lease_balances")
    assert row["total_balance"] == Decimal("150")
    assert all(row[name] is None for name, _ in mapper.BUCKETS)


async def test_a_partial_charges_read_keeps_the_totals_and_leaves_aging_unknown(
    switched_on: None,
) -> None:
    """A charge not read could be the open one: the aging would put its amount
    in no bucket, and an unknown bucket must never read as $0 (rule 14)."""
    result, pool, _server = await _run(FakeDoorLoop(totals={"/lease-charges": 5}))
    assert "helixis_lease_balances" not in result.failed
    (row,) = inserted_rows(pool, "helixis_lease_balances")
    assert row["total_balance"] == Decimal("150")
    assert all(row[name] is None for name, _ in mapper.BUCKETS)


async def test_a_partial_properties_read_leaves_owners_properties_as_they_were(
    switched_on: None,
) -> None:
    """A partial list would name an owner's properties as if it were all."""
    _result, pool, _server = await _run(FakeDoorLoop(totals={"/properties": 5}))
    (owner,) = inserted_rows(pool, "helixis_owners")
    assert "property_ids" not in owner


async def test_a_partial_leases_read_keeps_the_balances_and_leaves_occupancy_unknown(
    switched_on: None,
) -> None:
    result, pool, _server = await _run(FakeDoorLoop(totals={"/leases": 3}))
    assert result.failed["helixis_lease_balances"] == sync._LEASES_PART_WAY
    assert pool.inserts("helixis_lease_balances") == []
    assert pool.deletes("helixis_lease_balances") == []
    assert "helixis_leases" in result.failed and "leases" not in pool.stamped()
    units = _by_id(pool, "helixis_units")
    assert units[mid(UNIT_1)]["is_occupied"] is True, "an active lease that came through"
    assert "is_occupied" not in units[mid(UNIT_2)], "the lease not read may list it"


# ── tasks, applicants, the chart of accounts, bills, turnovers ───────────


async def test_the_six_land_in_the_readers_words(switched_on: None) -> None:
    result, pool, _server = await _run()
    for table in ("helixis_tasks", "helixis_applicants", "helixis_gl_accounts", "helixis_bills"):
        assert result.rows_by_table[table] > 0, table
        assert table in result.confirmed_complete, table
    assert {"tasks", "applicants", "gl_accounts", "bills"} <= pool.stamped()
    tasks = _by_id(pool, "helixis_tasks")
    assert tasks[mid(TODO_1)]["due_date"] == date(2026, 10, 20)
    assert tasks[mid(TENANT_REQUEST_1)]["status"] == "New", "RECEIVED: taken in, not started"
    assert tasks[mid(OWNER_REQUEST_1)]["status"] == "Completed"
    applicants = _by_id(pool, "helixis_applicants")
    assert set(applicants) == {mid(PROSPECT), mid(APPLICANT_2)}, "the lead is not an applicant"
    assert applicants[mid(PROSPECT)]["status"] == "New"
    assert applicants[mid(PROSPECT)]["unit_id"] == mid(UNIT_2)
    assert applicants[mid(APPLICANT_2)]["property_id"] is None, "interested in two"
    accounts = _by_id(pool, "helixis_gl_accounts")
    assert accounts[mid(ACCT_BANK)]["is_bank_account"] is True
    assert accounts[mid(ACCT_RENT)]["type"] == "Income"
    assert accounts[mid(ACCT_OFFICE)]["is_active"] is False


async def test_a_bill_names_its_work_order_and_its_lines_are_replaced_per_bill(
    switched_on: None,
) -> None:
    _result, pool, _server = await _run()
    bills = _by_id(pool, "helixis_bills")
    assert bills[mid(BILL_1)]["work_order_id"] == mid(WORK_ORDER_1)
    assert bills[mid(BILL_1)]["is_paid"] is False and bills[mid(BILL_2)]["is_paid"] is True
    assert bills[mid(BILL_2)]["work_order_id"] is None
    replaced = {
        args[2] for sql, args in pool.deletes("helixis_bill_lines") if "bill_id = $3" in sql
    }
    assert replaced == {mid(BILL_1), mid(BILL_2)}
    lines = inserted_rows(pool, "helixis_bill_lines")
    company = [line for line in lines if line["property_id"] is None]
    assert len(lines) == 3 and len(company) == 1
    assert company[0]["ledger_type"] == mapper.MANAGER_LEDGER
    assert company[0]["gl_account_id"] == mid(ACCT_OFFICE)


async def test_a_partial_tasks_read_leaves_a_bills_work_order_as_it_was(switched_on: None) -> None:
    _result, pool, _server = await _run(FakeDoorLoop(totals={"/tasks": 9}))
    assert all("work_order_id" not in row for row in inserted_rows(pool, "helixis_bills"))


@pytest.mark.parametrize(
    ("path", "tables"),
    [
        ("/tasks", {"helixis_work_orders", "helixis_tasks"}),
        ("/accounts", {"helixis_gl_accounts"}),
        ("/vendor-bills", {"helixis_bills", "helixis_bill_lines"}),
    ],
)
async def test_a_failed_list_stales_every_table_it_feeds_and_only_those(
    switched_on: None, path: str, tables: set[str]
) -> None:
    result, pool, _server = await _run(FakeDoorLoop(fail={path: 405}))
    assert set(result.failed) == tables
    for table in tables:
        assert pool.inserts(table) == [], table
    assert "helixis_applicants" not in result.failed


async def test_a_partial_bills_read_replaces_only_the_bills_it_read(switched_on: None) -> None:
    result, pool, _server = await _run(FakeDoorLoop(totals={"/vendor-bills": 5}))
    assert {"helixis_bills", "helixis_bill_lines"} <= set(result.failed)
    assert pool.deletes("helixis_bills") == [], "no prune from a partial read"
    replaced = {
        args[2] for sql, args in pool.deletes("helixis_bill_lines") if "bill_id = $3" in sql
    }
    assert replaced == {mid(BILL_1), mid(BILL_2)}, "each bill read carries all its lines"
    assert "bills" not in pool.stamped()


async def test_a_lease_whose_tenants_all_leave_opens_a_turnover_once(switched_on: None) -> None:
    result, pool, _server = await _run()
    # LEASE_1's tenants leave by 2026-10-31; LEASE_2's left in 2024.
    assert pool.turnovers == {mid(LEASE_1): date(2026, 10, 31)}
    assert result.rows_by_table["helixis_turnovers"] == 1
    assert len(pool.inserts("helixis_reminders")) == 1, "one desk card"
    again, pool, _server = await _run(pool=pool)
    assert again.rows_by_table["helixis_turnovers"] == 0, "an unchanged move-out is not reopened"


async def test_a_recent_move_out_opens_one_and_a_staying_tenant_prevents_one(
    switched_on: None,
) -> None:
    rows = account()
    rows["/leases/tenants"][2]["moveOutAt"] = "2026-09-20"  # LEASE_2, 16 days ago
    del rows["/leases/tenants"][0]["moveOutAt"]  # LEASE_1's tenant is staying after all
    _result, pool, _server = await _run(FakeDoorLoop(rows))
    assert pool.turnovers == {mid(LEASE_2): date(2026, 9, 20)}


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(lambda: FakeDoorLoop(fail={"/leases": 405}), id="leases failed"),
        pytest.param(lambda: FakeDoorLoop(totals={"/leases/tenants": 9}), id="tenants partial"),
        pytest.param(lambda: FakeDoorLoop(fail={"/leases/tenants": 405}), id="tenants failed"),
    ],
)
async def test_no_turnover_opens_from_leases_or_tenants_that_did_not_land(
    switched_on: None, server: Any
) -> None:
    """A lease whose staying tenant was not read would look vacated."""
    result, pool, _server = await _run(server())
    assert pool.turnovers == {}
    assert "helixis_turnovers" not in result.rows_by_table


async def test_a_turnover_that_fails_never_fails_the_pass(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helixis.db.repositories import turnovers as turnovers_repo

    async def broken(_pool: Any, **_kw: Any) -> bool:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(turnovers_repo, "start_turnover", broken)
    result, _pool, _server = await _run()
    assert result.failed == {} and result.rows_by_table["helixis_turnovers"] == 0


# ── stale, never empty ────────────────────────────────────────────────────


async def test_a_failed_list_is_stale_with_a_sentence_and_the_rest_land(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result, pool, _server = await _run(FakeDoorLoop(fail={"/vendors": 405}))
    assert set(result.failed) == {"helixis_vendors"}
    reason = result.failed["helixis_vendors"]
    assert isinstance(reason, sync.CustomerReason) and "access role" in reason
    assert sync.stale_reason(reason) == reason
    assert "/vendors failed" in caplog.text and "405" in caplog.text
    assert pool.inserts("helixis_vendors") == [] and pool.deletes("helixis_vendors") == []
    assert pool.stamped() == set(sync.SYNC_STATE_STAGE.values()) - {"vendors"}


async def test_a_404_on_a_list_is_stale_never_no_rows(switched_on: None) -> None:
    result, pool, _server = await _run(FakeDoorLoop(fail={"/properties": 404}))
    assert "did not expect" in result.failed["helixis_properties"]
    assert pool.deletes("helixis_properties") == [], "a 404 must never empty the table"
    assert "properties" not in pool.stamped()
    (owner,) = inserted_rows(pool, "helixis_owners")
    assert "property_ids" not in owner, "an owner's properties are left as they were"


async def test_a_partial_read_is_written_but_never_stamped_or_pruned(switched_on: None) -> None:
    result, pool, _server = await _run(FakeDoorLoop(totals={"/vendors": 9}))
    assert set(_by_id(pool, "helixis_vendors")) == {mid(VENDOR_1)}, "real rows land"
    assert pool.deletes("helixis_vendors") == []
    assert "vendors" not in pool.stamped()
    assert "helixis_vendors" not in result.confirmed_complete
    assert sync.stale_reason(result.failed["helixis_vendors"]) == sync._STOPPED_PART_WAY


async def test_the_key_never_reaches_a_log_or_the_result(
    switched_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result, _pool, _server = await _run(FakeDoorLoop(fail={"/vendors": 401}))
    assert "Zapier & API Keys" in result.failed["helixis_vendors"]
    assert API_KEY not in caplog.text
    assert all(API_KEY not in reason for reason in result.failed.values())


# ── DoorLoop's limits ─────────────────────────────────────────────────────


async def test_doorloops_edge_403_holds_the_account_for_the_rest_of_the_pass(
    switched_on: None,
) -> None:
    """The Rate Limiting page: a 403 from the edge means this IP is over its
    limit, and the backoff starts at 30 seconds or more. One request meets it;
    nothing is sent after it, and every list not read says so."""
    result, pool, server = await _run(FakeDoorLoop(fail={"/leases": 403}))
    assert server.paths() == ["/properties", "/units", "/leases"]
    unread = set(sync.SYNC_STATE_STAGE) - {"helixis_properties", "helixis_units"}
    assert set(result.failed) == unread
    assert {result.failed[table] for table in unread} == {SLOW_DOWN}
    assert {"properties", "units"} <= pool.stamped()


async def test_the_call_budget_stops_the_pass_and_stales_what_it_did_not_reach(
    switched_on: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sync, "CALL_BUDGET", 5)
    with caplog.at_level(logging.WARNING):
        result, pool, server = await _run()
    assert server.paths() == [path for _name, path, _filters in sync.READS[:5]]
    # Re-pointed 2026-10-07: the lists added after the tenants (tasks, the
    # chart of accounts, vendor bills) are unread too. The applicants come from
    # the tenants read, inside the budget, so they land.
    unread = {
        "helixis_owners",
        "helixis_vendors",
        "helixis_work_orders",
        "helixis_tasks",
        "helixis_gl_accounts",
        "helixis_bills",
        "helixis_bill_lines",
        "helixis_lease_transactions",
    }
    assert set(result.failed) == unread
    assert {result.failed[table] for table in unread} == {sync.CALL_BUDGET_REACHED}
    assert "call budget reached (5 pages)" in caplog.text
    assert pool.inserts("helixis_owners") == [] and "owners" not in pool.stamped()
    # The balances stand on the leases; without the charges, their aging is unknown.
    (row,) = inserted_rows(pool, "helixis_lease_balances")
    assert all(row[name] is None for name, _ in mapper.BUCKETS)


async def test_a_list_the_budget_cuts_short_is_stale_never_half_written(
    switched_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "CALL_BUDGET", 2)
    monkeypatch.setattr(client, "PAGING", dataclasses.replace(client.PAGING, page_size=2))
    result, pool, server = await _run()
    # The properties fit one page; the units' second page is never asked for.
    assert server.paths() == ["/properties", "/units"]
    assert result.failed["helixis_units"] == sync.CALL_BUDGET_REACHED
    assert pool.inserts("helixis_units") == []


def _documented_limits() -> tuple[int, int]:
    """Per key, a minute and an hour, from DoorLoop's own Rate Limiting page."""
    text = RATE_PAGE.read_text(encoding="utf-8")
    minute = re.search(r"\*\*(\d+) requests per minute\*\*", text)
    hour = re.search(r"\*\*(\d+) requests per hour\*\*", text)
    assert minute and hour, "the Rate Limiting page no longer states the limits"
    return int(minute.group(1)), int(hour.group(1))


def test_the_pace_keeps_a_rolling_minute_above_the_quota_hold() -> None:
    """Both sides (rule 15): the pace against the documented minute limit and
    the client's hold. At 45 a minute the minute would sit on the hold."""
    per_minute, _per_hour = _documented_limits()
    sent = client.REQUESTS_PER_SEC * 60
    assert per_minute - sent > per_minute * client.RATE_HEADERS.low_fraction


def test_one_pass_leaves_the_hour_above_the_quota_hold() -> None:
    _per_minute, per_hour = _documented_limits()
    assert per_hour - sync.CALL_BUDGET > per_hour * client.RATE_HEADERS.low_fraction


# ── a disconnect while the reads run ──────────────────────────────────────


@pytest.mark.parametrize(
    "then",
    [
        pytest.param(None, id="disconnected"),
        pytest.param(creds_row(api_key="dl-synthetic-another-key-0008"), id="another-key"),
        pytest.param({**creds_row(), "encrypted_api_key": "not-a-token"}, id="unusable"),
    ],
)
async def test_a_pass_whose_key_changed_while_it_read_writes_nothing(
    switched_on: None, then: dict[str, Any] | None
) -> None:
    """The disconnect deletes the row and purges the mirror. A pass already
    reading must not write afterwards: that would put the purged rows back,
    or another account's rows in place of the new connection's."""
    server = FakeDoorLoop()
    pool = FakePool(creds=[creds_row(), then])
    with pytest.raises(sync.DoorLoopSyncRefused, match="nothing was written"):
        await sync.sync_company(pool, COMPANY, http=server.http())
    assert len(server.requests) == len(sync.READS), "the check comes after every read"
    assert [s for s, _a in pool.log if s.startswith(("INSERT", "DELETE", "UPDATE"))] == []


async def test_the_key_is_read_again_before_the_first_write(switched_on: None) -> None:
    _result, pool, _server = await _run()
    reads = [i for i, (s, _a) in enumerate(pool.log) if "company_doorloop_credentials" in s]
    writes = [i for i, (s, _a) in enumerate(pool.log) if s.startswith(("INSERT", "DELETE"))]
    assert len(reads) == 2 and writes
    assert reads[1] < min(writes)


# ── mapping, pure ─────────────────────────────────────────────────────────


def _reads(rows: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, sync.ListRead]:
    source = rows if rows is not None else account()
    return {
        name: sync.ListRead(rows=copy.deepcopy(source[path]), complete=True, started=0.0)
        for name, path, _filters in sync.READS
    }


def _link_keys() -> set[str]:
    """The keys the shared link writer reads off a row, from its own source."""
    tree = ast.parse(inspect.getsource(mirror._write_links))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _allowed(table: str) -> set[str]:
    if table in KEYED_COLUMNS:
        columns = set(KEYED_COLUMNS[table])
    elif table == "helixis_lease_balances":
        columns = set(BALANCE_COLUMNS)
    elif table in PER_PARENT:
        columns = set(PER_PARENT[table][1])
    elif table == LINK_TABLE:
        columns = _link_keys()
    else:
        raise AssertionError(f"{table} is not a table the shared writer knows")
    return columns | {"buildium_id", "raw"}


def test_the_link_writer_reads_the_keys_this_test_holds_links_to() -> None:
    assert _link_keys() == {"lease_buildium_id", "tenant_buildium_id", "status", "move_in_date"}


def test_every_mapped_row_carries_only_columns_the_writer_writes() -> None:
    """The writer drops any other key without a sound, so a misspelt column
    would lose its data silently."""
    rows = sync.mirror_rows(_reads(), today=TODAY, now=1.0)
    assert set(rows) == set(sync.SYNC_STATE_STAGE)
    checked = 0
    for table, read in rows.items():
        assert read.error is None, table
        allowed = _allowed(table)
        for row in read.rows:
            assert set(row) <= allowed, (table, sorted(set(row) - allowed))
            checked += 1
    assert checked > 15, "the guard checked almost nothing"


def test_the_prune_window_runs_from_each_tables_own_read() -> None:
    reads = _reads()
    reads["properties"] = dataclasses.replace(reads["properties"], started=10.0)
    for name, started in (("charges", 80.0), ("credits", 85.0), ("payments", 90.0)):
        reads[name] = dataclasses.replace(reads[name], started=started)
    reads["reversed_payments"] = dataclasses.replace(reads["reversed_payments"], started=95.0)
    reads["leases"] = dataclasses.replace(reads["leases"], started=40.0)
    rows = sync.mirror_rows(reads, today=TODAY, now=100.0)
    assert rows["helixis_properties"].read_seconds == 90.0
    assert rows["helixis_lease_transactions"].read_seconds == 20.0, "from the earliest of four"
    assert rows["helixis_lease_balances"].read_seconds == 60.0


def test_a_mapping_bug_stales_its_table_and_spares_the_rest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any) -> Any:
        raise KeyError("synthetic bug")

    monkeypatch.setattr(mapper, "map_vendor", broken)
    monkeypatch.setattr(mapper, "ledger_row", broken)
    monkeypatch.setattr(mapper, "lease_balance", broken)
    with caplog.at_level(logging.ERROR):
        rows = sync.mirror_rows(_reads(), today=TODAY, now=1.0)
    for table in ("helixis_vendors", "helixis_lease_transactions", "helixis_lease_balances"):
        assert rows[table].error == sync._MAPPING_FAILED, table
        assert f"mapping {table} failed (KeyError)" in caplog.text
    assert rows["helixis_properties"].error is None


def test_a_lease_whose_units_are_not_a_list_never_breaks_the_pass() -> None:
    rows = account()
    rows["/leases"][0]["units"] = 7
    tables = sync.mirror_rows(_reads(rows), today=TODAY, now=1.0)
    assert all(read.error is None for read in tables.values())
    leases = {row["buildium_id"]: row for row in tables["helixis_leases"].rows}
    assert leases[mid(LEASE_1)]["unit_id"] is None
    units = {row["buildium_id"]: row for row in tables["helixis_units"].rows}
    assert units[mid(UNIT_1)]["is_occupied"] is False


def test_two_records_on_one_mirror_id_stale_their_table_and_write_neither(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = account()
    rows["/vendors"].append({"id": oid(52), "name": "Second Co", "active": True})
    real = mapper.mirror_id
    monkeypatch.setattr(
        mapper, "mirror_id", lambda value: real(VENDOR_1) if value == oid(52) else real(value)
    )
    with caplog.at_level(logging.ERROR):
        tables = sync.mirror_rows(_reads(rows), today=TODAY, now=1.0)
    assert tables["helixis_vendors"].error == sync._ID_COLLISION
    assert tables["helixis_vendors"].rows == []
    assert "ids collide in helixis_vendors" in caplog.text
    assert tables["helixis_properties"].error is None


def test_a_charge_and_a_payment_on_one_mirror_id_stale_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = account()
    payment = rows["/lease-payments"][0]["id"]
    charge = rows["/lease-charges"][0]["id"]
    real = mapper.mirror_id
    monkeypatch.setattr(
        mapper, "mirror_id", lambda value: real(charge) if value == payment else real(value)
    )
    tables = sync.mirror_rows(_reads(rows), today=TODAY, now=1.0)
    assert tables["helixis_lease_transactions"].error == sync._ID_COLLISION


# ── what the sync status says (stale reasons) ─────────────────────────────


def _error(status: int, failure: Failure, *, sent: bool = True) -> PmsApiError:
    return PmsApiError(status, failure, False, "synthetic vendor text", sent=sent)


#: Every kind of read failure, and what each reason must ask the person to do.
_ACTIONS = [
    (_error(401, Failure.CREDENTIALS), "Zapier & API Keys"),
    (_error(405, Failure.PERMISSION), "give the key a role"),
    (_error(404, Failure.NOT_FOUND), "team@occupella.com"),
    (_error(400, Failure.BAD_REQUEST), "team@occupella.com"),
    (_error(403, Failure.UNDOCUMENTED), "Sync again"),
    (_error(429, Failure.UNDOCUMENTED), "Sync again"),
    (_error(429, Failure.UNDOCUMENTED, sent=False), "Sync again"),
    (_error(502, Failure.UNDOCUMENTED), "Sync again"),
    (_error(0, Failure.UNDOCUMENTED), "Sync again"),
    (PmsUnavailableError("DoorLoop", 120.0), "Sync again"),
]


@pytest.mark.parametrize(("exc", "action"), _ACTIONS)
def test_every_read_failure_names_an_action_and_no_vendor_text(
    exc: PmsApiError, action: str
) -> None:
    """Rule 16. The reason is shown to the customer, so DoorLoop's own text and
    the status code stay in the log."""
    reason = sync.read_failure(exc)
    assert action in reason
    assert "synthetic vendor text" not in reason and str(exc.status) not in reason


def test_a_refused_role_never_says_reconnect() -> None:
    """Gotcha 102: a 405 means DoorLoop accepted the key and its role cannot
    read this list, so the fix is the role, never a reconnect."""
    reason = sync.read_failure(_error(405, Failure.PERMISSION))
    assert "access role" in reason and "reconnect" not in reason.lower()


#: Every reason the pass sets itself, which must reach the status unchanged.
_OWN_REASONS = [
    *(sync.read_failure(exc) for exc, _action in _ACTIONS),
    sync._READ_CRASHED,
    sync._MAPPING_FAILED,
    sync._ID_COLLISION,
    sync._LEDGER_PART_WAY,
    sync._LEDGER_NO_ID,
    sync._LEASES_PART_WAY,
    sync.CALL_BUDGET_REACHED,
]


@pytest.mark.parametrize("reason", _OWN_REASONS)
def test_the_passs_own_reasons_reach_the_status_unchanged(reason: str) -> None:
    assert sync.stale_reason(reason) == reason
    actions = ("Sync again", "team@occupella.com", "save it here")
    assert any(action in reason for action in actions), "rule 16: an action"


def _writer_reasons() -> list[str]:
    """Every stale reason ``pms.mirror`` can set, as it would read, from the
    source: each ``result.failed[...] = <text>``, both arms of a conditional,
    and an f-string with a number in each placeholder. A reason that is a
    variable (the connector's own, passed through) is not the writer's."""

    def texts(node: ast.expr) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return ["".join(v.value if isinstance(v, ast.Constant) else "3" for v in node.values)]
        if isinstance(node, ast.IfExp):
            return texts(node.body) + texts(node.orelse)
        return []

    tree = ast.parse(inspect.getsource(mirror))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "failed"
                ):
                    found += texts(node.value)
    return found


def test_every_reason_the_shared_writer_sets_is_worded_for_the_customer() -> None:
    """Both sides of the seam (rule 15): the writer's reasons are written for
    an operator, and the sync status shows reasons to the customer."""
    reasons = _writer_reasons()
    # A scan that finds nothing proves nothing (rule 10): these are known.
    assert "incomplete read: written, not stamped, not pruned" in reasons
    assert any(r.startswith("write failed") for r in reasons)
    assert len(reasons) >= 6
    for reason in reasons:
        worded = sync.stale_reason(reason)
        assert worded != reason, f"the writer's reason {reason!r} is not worded"
        assert "Sync again" in worded or "team@occupella.com" in worded


def test_a_reason_is_ours_by_its_type_never_by_its_words() -> None:
    ours = sync.read_failure(_error(405, Failure.PERMISSION))
    assert isinstance(ours, sync.CustomerReason)
    assert sync.stale_reason(str(ours)) != str(ours)


async def test_a_failed_lists_reason_survives_the_writer_as_ours(switched_on: None) -> None:
    """Both sides of the seam (rule 15): through the real pass and the shared
    writer, a failed list's reason reaches the result as this pass's own."""
    result, _pool, _server = await _run(FakeDoorLoop(fail={"/vendors": 405}))
    reason = result.failed["helixis_vendors"]
    assert isinstance(reason, sync.CustomerReason)
    assert sync.stale_reason(reason) == reason


def test_every_table_the_pass_fills_has_words_for_the_status() -> None:
    assert set(sync.TABLE_WORDS) == set(sync.SYNC_STATE_STAGE)
