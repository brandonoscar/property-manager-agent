"""Unit tests for the cockpit mirror backfill (buildium/backfill.py).

The Buildium client + repo upserts are mocked — these pin the orchestration:
per-stage counts, stage isolation (one stage failing doesn't stop the rest),
the active-lease filter for transactions, and the lookback window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium import backfill


class _ClientCM:
    """Stand-in for ``BuildiumClient(...)`` used as an async context manager."""

    async def __aenter__(self) -> object:
        return MagicMock()

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _lease(lease_id: int, status: str = "Active") -> MagicMock:
    lease = MagicMock()
    lease.id = lease_id
    lease.status = status
    return lease


def _txn(txn_id: int, *, days_ago: int) -> MagicMock:
    txn = MagicMock()
    txn.id = txn_id
    txn.date = datetime.now(UTC) - timedelta(days=days_ago)
    return txn


def _api(
    *,
    properties: list | None = None,
    units: list | None = None,
    tenants: list | None = None,
    leases: list | None = None,
    balances: list | None = None,
    transactions: list | None = None,
    work_orders: list | None = None,
    applicants: list | None = None,
    vendors: list | None = None,
    owners: list | None = None,
    tasks: list | None = None,
    gl_accounts: list | None = None,
    bills: list | None = None,
    bank_accounts: list | None = None,
) -> MagicMock:
    api = MagicMock()
    api.rentals.list_properties = AsyncMock(return_value=properties or [])
    api.rentals.list_units = AsyncMock(return_value=units or [])
    api.rentals.list_tenants = AsyncMock(return_value=tenants or [])
    api.leases.list = AsyncMock(return_value=leases or [])
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances or [])
    api.leases.list_transactions = AsyncMock(return_value=transactions or [])
    api.work_orders.list = AsyncMock(return_value=work_orders or [])
    api.applicants.list = AsyncMock(return_value=applicants or [])
    api.vendors.list = AsyncMock(return_value=vendors or [])
    api.rentals.list_owners = AsyncMock(return_value=owners or [])
    api.tasks.list = AsyncMock(return_value=tasks or [])
    api.general_ledger.list_accounts = AsyncMock(return_value=gl_accounts or [])
    # Two calls per run (Unpaid, then Paid+frompaiddate — an unfiltered
    # GET /v1/bills 422s). The ``bills`` fixture plays the unpaid page.
    api.bills.list = AsyncMock(side_effect=[bills or [], []])
    api.bank_accounts.list = AsyncMock(return_value=bank_accounts or [])
    return api


def _patch_entities() -> dict[str, AsyncMock]:
    mocks = {
        "upsert_property": AsyncMock(),
        "upsert_unit": AsyncMock(),
        "upsert_tenant": AsyncMock(),
        "upsert_lease": AsyncMock(),
        "refresh_lease_tenant_links": AsyncMock(),
        "replace_lease_balances": AsyncMock(side_effect=lambda _p, b, **_k: len(b)),
        "upsert_lease_transaction": AsyncMock(),
        "upsert_work_order": AsyncMock(),
        "upsert_applicant": AsyncMock(),
        "upsert_vendor": AsyncMock(),
        "upsert_owner": AsyncMock(),
        "upsert_task": AsyncMock(),
        "upsert_gl_account": AsyncMock(),
        "upsert_bill": AsyncMock(),
        "upsert_bank_account": AsyncMock(),
        "prune_bank_accounts": AsyncMock(),
    }
    return mocks


async def _run(api: MagicMock, mocks: dict[str, AsyncMock]) -> backfill.BackfillResult:
    with (
        patch.object(backfill, "BuildiumClient", return_value=_ClientCM()),
        patch.object(backfill, "BuildiumApi", return_value=api),
        patch.object(backfill, "sync_lead_from_applicant", AsyncMock(return_value=None)),
        patch.multiple(backfill.entities, **mocks),
    ):
        return await backfill.backfill_company(
            MagicMock(), MagicMock(), company_id="co-1", account_id=7
        )


@pytest.mark.asyncio
async def test_counts_every_stage() -> None:
    api = _api(
        properties=[MagicMock(), MagicMock()],
        units=[MagicMock()],
        tenants=[MagicMock(), MagicMock(), MagicMock()],
        leases=[_lease(101)],
        balances=[MagicMock()],
        transactions=[_txn(1, days_ago=5)],
        work_orders=[MagicMock(), MagicMock()],
        applicants=[MagicMock()],
        vendors=[MagicMock(), MagicMock()],
        owners=[MagicMock()],
        tasks=[MagicMock(), MagicMock(), MagicMock()],
        gl_accounts=[MagicMock(), MagicMock(), MagicMock(), MagicMock()],
        bills=[MagicMock(), MagicMock()],
        bank_accounts=[MagicMock(id=31), MagicMock(id=32)],
    )
    result = await _run(api, _patch_entities())
    assert result.ok
    assert (result.properties, result.units, result.tenants) == (2, 1, 3)
    assert (result.leases, result.balances, result.transactions) == (1, 1, 1)
    assert result.work_orders == 2
    assert result.applicants == 1
    assert (result.vendors, result.owners, result.tasks) == (2, 1, 3)
    assert (result.gl_accounts, result.bills) == (4, 2)
    assert result.bank_accounts == 2
    # The two bill fetches carry the mandatory paid-status filters —
    # Paid needs BOTH date bounds (each 422s when missing, live-probed).
    first, second = api.bills.list.await_args_list
    assert first.kwargs == {"paidstatus": "Unpaid"}
    assert second.kwargs["paidstatus"] == "Paid"
    assert "frompaiddate" in second.kwargs
    assert "topaiddate" in second.kwargs


@pytest.mark.asyncio
async def test_paid_bills_failure_keeps_unpaid_bills() -> None:
    """The 2026-07-06 live FAIL: the paid-bills call 422'd and, because
    both fetches shared one try, the good unpaid fetch was discarded too.
    Now they're isolated — unpaid bills land even when the paid call dies."""
    api = _api()
    api.bills.list = AsyncMock(
        side_effect=[[MagicMock(), MagicMock()], RuntimeError("422 ToPaidDate")]
    )
    result = await _run(api, _patch_entities())
    assert result.bills == 2
    assert any(e.startswith("bills(paid)") for e in result.errors)
    assert not any(e.startswith("bills(unpaid)") for e in result.errors)


@pytest.mark.asyncio
async def test_stage_failure_is_isolated() -> None:
    api = _api(properties=[MagicMock()], leases=[_lease(101)])
    api.rentals.list_units = AsyncMock(side_effect=RuntimeError("units 500"))
    result = await _run(api, _patch_entities())
    assert not result.ok
    assert any("units" in e for e in result.errors)
    # Later stages still ran.
    assert result.properties == 1
    assert result.leases == 1


@pytest.mark.asyncio
async def test_transactions_only_for_active_leases() -> None:
    api = _api(leases=[_lease(101, "Active"), _lease(102, "Past")])
    api.leases.list_transactions = AsyncMock(return_value=[_txn(1, days_ago=3)])
    result = await _run(api, _patch_entities())
    # One transactions fetch — for the active lease only, with the
    # server-side date filter (verified param: transactiondatefrom).
    api.leases.list_transactions.assert_awaited_once()
    args, kwargs = api.leases.list_transactions.await_args
    assert args == (101,)
    assert "transactiondatefrom" in kwargs
    assert result.transactions == 1


@pytest.mark.asyncio
async def test_transactions_outside_lookback_are_skipped() -> None:
    api = _api(
        leases=[_lease(101)],
        transactions=[
            _txn(1, days_ago=5),
            _txn(2, days_ago=backfill.TXN_LOOKBACK_DAYS + 30),
        ],
    )
    mocks = _patch_entities()
    result = await _run(api, mocks)
    assert result.transactions == 1
    mocks["upsert_lease_transaction"].assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_stage_refreshes_tenant_links() -> None:
    api = _api(leases=[_lease(101), _lease(102, "Past")])
    mocks = _patch_entities()
    result = await _run(api, mocks)
    assert result.leases == 2
    assert mocks["refresh_lease_tenant_links"].await_count == 2
