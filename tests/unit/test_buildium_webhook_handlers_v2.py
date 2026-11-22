"""Unit tests for the new per-category webhook handlers.

We stub ``open_api_for_company`` to yield a mock BuildiumApi and
patch the database helpers so handlers run without Postgres. Each
test exercises the happy path — fetch the entity, write activity,
upsert the mirror — plus a few sad paths (missing IDs, 404 races).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.buildium.errors import BuildiumApiError
from helixis.webhooks.handlers import (
    applicants as applicants_h,
)
from helixis.webhooks.handlers import (
    finance as finance_h,
)
from helixis.webhooks.handlers import (
    leases_v2 as leases_h,
)
from helixis.webhooks.handlers import (
    maintenance as maintenance_h,
)
from helixis.webhooks.handlers import (
    properties as properties_h,
)

COMPANY_ID = "company-A"
ACCOUNT_ID = 42


def _envelope(event_name: str, **extra: object) -> dict[str, object]:
    return {
        "EventName": event_name,
        "EventDateTime": "2026-04-29T12:00:00Z",
        "AccountId": ACCOUNT_ID,
        **extra,
    }


def _mock_api() -> MagicMock:
    api = MagicMock()
    api.leases = MagicMock()
    api.work_orders = MagicMock()
    api.rentals = MagicMock()
    api.vendors = MagicMock()
    api.applicants = MagicMock()
    api.bills = MagicMock()
    return api


@asynccontextmanager
async def _yield_api(api):
    yield api


# ─────────────────────────────────────────────────────────────────────
# Lease handlers
# ─────────────────────────────────────────────────────────────────────


async def test_lease_changed_fetches_and_upserts() -> None:
    api = _mock_api()
    fake_lease = MagicMock()
    fake_lease.id = 5678
    fake_lease.unit_id = 99
    fake_lease.rent = 1850.0
    fake_lease.current_tenants = []
    api.leases.get = AsyncMock(return_value=fake_lease)

    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease", new=AsyncMock()) as upsert,
        patch.object(leases_h, "refresh_lease_tenant_links", new=AsyncMock()) as links,
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_changed(COMPANY_ID, _envelope("Lease.Created", LeaseId=5678))

    api.leases.get.assert_awaited_once_with(5678)
    upsert.assert_awaited_once()
    # Cockpit mirror (0038): the lease↔tenant join refreshes from the payload.
    links.assert_awaited_once()
    activity.assert_awaited_once()


async def test_lease_changed_skips_when_lease_id_missing() -> None:
    api = _mock_api()
    api.leases.get = AsyncMock()
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease", new=AsyncMock()) as upsert,
        patch.object(leases_h, "refresh_lease_tenant_links", new=AsyncMock()),
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_changed(COMPANY_ID, _envelope("Lease.Created"))
    api.leases.get.assert_not_awaited()
    upsert.assert_not_awaited()
    activity.assert_not_awaited()


async def test_lease_changed_handles_404_gracefully() -> None:
    api = _mock_api()
    api.leases.get = AsyncMock(side_effect=BuildiumApiError(status=404, message="gone"))
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease", new=AsyncMock()) as upsert,
        patch.object(leases_h, "refresh_lease_tenant_links", new=AsyncMock()),
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_changed(COMPANY_ID, _envelope("Lease.Updated", LeaseId=5678))
    upsert.assert_not_awaited()
    activity.assert_not_awaited()


async def test_lease_deleted_evicts_the_mirror_row_and_writes_activity() -> None:
    """⚠ Was ``test_lease_deleted_writes_activity_only``, and the name was the
    defect written down as the contract. Writing an activity line saying a
    lease was deleted while leaving it in ``helixis_leases`` means every
    rollup keeps reporting it — a confident false PRESENCE, which is worse
    than the false absence gotcha 26 warns about: an absence sends the manager
    to go and look, a presence sends them to act on a record that is gone."""
    with (
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(leases_h, "delete_lease", new=AsyncMock()) as deleted,
    ):
        await leases_h.handle_lease_deleted(COMPANY_ID, _envelope("Lease.Deleted", LeaseId=5678))
    deleted.assert_awaited_once()
    assert deleted.await_args.args[1] == 5678
    assert deleted.await_args.kwargs["company_id"] == COMPANY_ID
    activity.assert_awaited_once()


async def test_lease_moveout_fetches_move_out_list_and_logs() -> None:
    api = _mock_api()
    move_out = MagicMock()
    move_out.move_out_date = None
    api.leases.list_move_outs = AsyncMock(return_value=[move_out])
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_moveout(
            COMPANY_ID, _envelope("Lease.MoveOut.Created", LeaseId=5678)
        )
    api.leases.list_move_outs.assert_awaited_once_with(5678)
    activity.assert_awaited_once()


async def test_lease_moveout_picks_earliest_date_across_tenants() -> None:
    """When multiple tenants have move-out rows on a single lease, the
    activity entry should call out the earliest pending move-out."""
    from datetime import datetime as _dt

    api = _mock_api()
    early = MagicMock()
    early.move_out_date = _dt(2026, 5, 1)
    late = MagicMock()
    late.move_out_date = _dt(2026, 6, 15)
    api.leases.list_move_outs = AsyncMock(return_value=[late, early])
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_moveout(
            COMPANY_ID, _envelope("Lease.MoveOut.Created", LeaseId=5678)
        )
    activity.assert_awaited_once()
    description = activity.await_args.args[1].description
    assert "2026-05-01" in description
    assert "2 tenants" in description


async def test_lease_transaction_mirrors_and_writes_activity() -> None:
    api = _mock_api()
    txn = MagicMock()
    txn.id = 9
    api.leases.get_transaction = AsyncMock(return_value=txn)
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease_transaction", new=AsyncMock()) as mirror,
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_transaction(
            COMPANY_ID,
            _envelope(
                "LeaseTransaction.Created",
                LeaseId=5678,
                TransactionId=9,
            ),
        )
    api.leases.get_transaction.assert_awaited_once_with(5678, 9)
    # Cockpit mirror (0038): the payment lands in helixis_lease_transactions
    # so rent-roll "collected" moves immediately.
    mirror.assert_awaited_once()
    assert mirror.await_args.args[1] is txn
    activity.assert_awaited_once()


async def test_lease_transaction_deleted_evicts_the_ledger_row() -> None:
    """⚠ Was ``..._skips_fetch_but_logs``. Not re-fetching a deleted record is
    right; leaving it in ``helixis_lease_transactions`` is not. These rows are
    MONEY — they feed the balance arithmetic behind ``delinquency_report``,
    which drives late-notice recommendations, so a deleted charge surviving
    meant telling a manager a resident owed something they did not.

    The handler's comment previously said deletes were "left to the re-sync
    (rare, and the mirror is advisory)". Overridden deliberately: "advisory"
    is true of the mirror in general and not of a number someone acts on, and
    the daily re-sync is up to 24 hours of a wrong balance."""
    api = _mock_api()
    api.leases.get_transaction = AsyncMock()
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease_transaction", new=AsyncMock()) as mirror,
        patch.object(leases_h, "delete_lease_transaction", new=AsyncMock()) as deleted,
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_transaction(
            COMPANY_ID,
            _envelope(
                "LeaseTransaction.Deleted",
                LeaseId=5678,
                TransactionId=9,
            ),
        )
    api.leases.get_transaction.assert_not_awaited()
    mirror.assert_not_awaited()
    deleted.assert_awaited_once()
    assert deleted.await_args.args[1] == 9
    activity.assert_awaited_once()


async def test_lease_tenant_fetches_lease_and_upserts() -> None:
    api = _mock_api()
    lease = MagicMock()
    lease.id = 5678
    lease.unit_id = 99
    lease.rent = 1850.0
    lease.current_tenants = []
    api.leases.get = AsyncMock(return_value=lease)
    tenant = MagicMock()
    tenant.id = 8
    api.rentals.get_tenant = AsyncMock(return_value=tenant)
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),
        patch.object(leases_h, "upsert_lease", new=AsyncMock()) as upsert,
        patch.object(leases_h, "refresh_lease_tenant_links", new=AsyncMock()) as links,
        patch.object(leases_h, "upsert_tenant", new=AsyncMock()) as tenant_upsert,
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_tenant(
            COMPANY_ID,
            _envelope("LeaseTenant.Created", LeaseId=5678, TenantId=8),
        )
    upsert.assert_awaited_once()
    # Cockpit mirror (0038): join rows refresh + the tenant roster row too.
    links.assert_awaited_once()
    tenant_upsert.assert_awaited_once()
    assert tenant_upsert.await_args.args[1] is tenant
    activity.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────
# Maintenance handlers
# ─────────────────────────────────────────────────────────────────────


async def test_workorder_changed_upserts_mirror_and_logs() -> None:
    api = _mock_api()
    wo = MagicMock()
    wo.id = 123
    wo.priority = "High"
    wo.status = "New"
    wo.property_id = 1
    api.work_orders.get = AsyncMock(return_value=wo)
    with (
        patch.object(maintenance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "upsert_work_order", new=AsyncMock()) as upsert,
        patch.object(maintenance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await maintenance_h.handle_workorder_changed(
            COMPANY_ID, _envelope("WorkOrder.Created", WorkOrderId=123)
        )
    upsert.assert_awaited_once()
    activity.assert_awaited_once()


async def test_workorder_deleted_evicts_the_mirror_row() -> None:
    """⚠ Was ``test_workorder_deleted_logs_only``. The handler is NAMED for
    deletion and only logged — so a deleted work order kept being counted as
    outstanding maintenance by every rollup that reads
    ``helixis_work_orders``."""
    with (
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(maintenance_h.entities_repo, "delete_work_order", new=AsyncMock()) as deleted,
    ):
        await maintenance_h.handle_workorder_deleted(
            COMPANY_ID, _envelope("WorkOrder.Deleted", WorkOrderId=123)
        )
    deleted.assert_awaited_once()
    assert deleted.await_args.args[1] == 123
    activity.assert_awaited_once()


async def test_vendor_changed_includes_vendor_label_when_fetchable() -> None:
    api = _mock_api()
    vendor = MagicMock()
    vendor.id = 7
    vendor.company_name = "Acme Plumbing"
    vendor.name = None
    api.vendors.get = AsyncMock(return_value=vendor)
    with (
        patch.object(maintenance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(maintenance_h.entities_repo, "upsert_vendor", new=AsyncMock()) as upsert,
    ):
        await maintenance_h.handle_vendor_changed(
            COMPANY_ID, _envelope("Vendor.Updated", VendorId=7)
        )
    activity.assert_awaited_once()
    description = activity.await_args.args[1].description
    assert "Acme Plumbing" in description
    # Entity-graph mirror (0041): the refetched vendor is mirrored.
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] is vendor


async def test_vendor_deleted_skips_fetch() -> None:
    api = _mock_api()
    api.vendors.get = AsyncMock()
    with (
        patch.object(maintenance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(maintenance_h, "get_pool", return_value=object()),
        patch.object(maintenance_h, "write_activity", new=AsyncMock()),
        patch.object(maintenance_h.entities_repo, "delete_vendor", new=AsyncMock()) as delete,
    ):
        await maintenance_h.handle_vendor_changed(
            COMPANY_ID, _envelope("Vendor.Deleted", VendorId=7)
        )
    api.vendors.get.assert_not_awaited()
    # Entity-graph mirror (0041): the deleted vendor's row is dropped.
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == 7


# ─────────────────────────────────────────────────────────────────────
# Property handlers
# ─────────────────────────────────────────────────────────────────────


async def test_rental_changed_fetches_and_logs_with_name() -> None:
    api = _mock_api()
    prop = MagicMock()
    prop.name = "123 Main"
    api.rentals.get_property = AsyncMock(return_value=prop)
    with (
        patch.object(properties_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(properties_h, "get_pool", return_value=object()),
        patch.object(properties_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(properties_h, "upsert_property", new=AsyncMock()) as mirror,
    ):
        await properties_h.handle_rental_changed(
            COMPANY_ID, _envelope("Rental.Updated", PropertyId=10)
        )
    activity.assert_awaited_once()
    assert "123 Main" in activity.await_args.args[1].description
    # Cockpit mirror (0038): the re-fetched entity is upserted too.
    mirror.assert_awaited_once()
    assert mirror.await_args.args[1] is prop


async def test_rental_unit_changed_fetches_unit_number() -> None:
    api = _mock_api()
    unit = MagicMock()
    unit.unit_number = "4B"
    api.rentals.get_unit = AsyncMock(return_value=unit)
    with (
        patch.object(properties_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(properties_h, "get_pool", return_value=object()),
        patch.object(properties_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(properties_h, "upsert_unit", new=AsyncMock()) as mirror,
    ):
        await properties_h.handle_rental_unit_changed(
            COMPANY_ID, _envelope("RentalUnit.Created", UnitId=99)
        )
    activity.assert_awaited_once()
    assert "#4B" in activity.await_args.args[1].description
    mirror.assert_awaited_once()
    assert mirror.await_args.args[1] is unit


# ─────────────────────────────────────────────────────────────────────
# Finance handlers
# ─────────────────────────────────────────────────────────────────────


async def test_bill_changed_includes_total_amount() -> None:
    api = _mock_api()
    bill = MagicMock()
    bill.total_amount = 250.0
    api.bills.get = AsyncMock(return_value=bill)
    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_bill", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bill_changed(COMPANY_ID, _envelope("Bill.Created", BillId=11))
    activity.assert_awaited_once()
    assert "$250.00" in activity.await_args.args[1].description
    # Money mirror (0042): the refetched bill lands in helixis_bills.
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] is bill


async def test_bill_deleted_removes_mirror_row() -> None:
    with (
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "delete_bill", new=AsyncMock()) as delete,
        patch.object(finance_h.entities_repo, "upsert_bill", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bill_changed(COMPANY_ID, _envelope("Bill.Deleted", BillId=11))
    delete.assert_awaited_once()
    upsert.assert_not_awaited()
    activity.assert_awaited_once()


async def test_bill_payment_lists_affected_bill_ids() -> None:
    api = _mock_api()
    covered = MagicMock()
    api.bills.get = AsyncMock(return_value=covered)
    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_bill", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bill_payment(
            COMPANY_ID,
            _envelope(
                "Bill.Payment.Created",
                PaymentId=12,
                BillIds=[11, 13],
            ),
        )
    activity.assert_awaited_once()
    description = activity.await_args.args[1].description
    assert "#11" in description and "#13" in description
    # Money mirror (0042): a payment refreshes every covered bill's paid flag.
    assert api.bills.get.await_count == 2
    assert upsert.await_count == 2


async def test_bill_payment_refresh_skips_deleted_bills() -> None:
    api = _mock_api()
    api.bills.get = AsyncMock(side_effect=BuildiumApiError(status=404, message="gone"))
    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_bill", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bill_payment(
            COMPANY_ID,
            _envelope("Bill.Payment.Created", PaymentId=12, BillIds=[11]),
        )
    # A since-deleted covered bill is skipped, not fatal — activity still lands.
    upsert.assert_not_awaited()
    activity.assert_awaited_once()


async def test_bank_transaction_logs_and_refreshes_balance() -> None:
    api = _mock_api()
    bank = MagicMock()
    api.bank_accounts.get = AsyncMock(return_value=bank)
    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_bank_account", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bank_transaction(
            COMPANY_ID,
            _envelope(
                "BankAccount.Transaction.Created",
                BankAccountId=13,
                TransactionId=14,
            ),
        )
    activity.assert_awaited_once()
    # Money mirror (0043): every transaction refreshes the account balance.
    api.bank_accounts.get.assert_awaited_once_with(13)
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] is bank


async def test_bank_transaction_survives_deleted_account() -> None:
    api = _mock_api()
    api.bank_accounts.get = AsyncMock(side_effect=BuildiumApiError(status=404, message="gone"))
    with (
        patch.object(finance_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(finance_h, "get_pool", return_value=object()),
        patch.object(finance_h.entities_repo, "upsert_bank_account", new=AsyncMock()) as upsert,
        patch.object(finance_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await finance_h.handle_bank_transaction(
            COMPANY_ID,
            _envelope(
                "BankAccount.Transaction.Deleted",
                BankAccountId=13,
                TransactionId=14,
            ),
        )
    upsert.assert_not_awaited()
    activity.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────
# Applicant handlers
# ─────────────────────────────────────────────────────────────────────


async def test_applicant_changed_fetches_full_name_and_mirrors() -> None:
    api = _mock_api()
    applicant = MagicMock()
    applicant.first_name = "Sarah"
    applicant.last_name = "Chen"
    api.applicants.get = AsyncMock(return_value=applicant)
    with (
        patch.object(applicants_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(applicants_h, "get_pool", return_value=object()),
        patch.object(applicants_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(applicants_h.entities_repo, "upsert_applicant", new=AsyncMock()) as upsert,
        patch.object(applicants_h, "sync_lead_from_applicant", new=AsyncMock()) as crm_sync,
    ):
        await applicants_h.handle_applicant_changed(
            COMPANY_ID, _envelope("Applicant.Created", ApplicantId=15)
        )
    activity.assert_awaited_once()
    assert "Sarah Chen" in activity.await_args.args[1].description
    # 0040: the applicant is mirrored and the CRM auto-move runs on the
    # SAME fetched payload — one Buildium GET per webhook.
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] is applicant
    crm_sync.assert_awaited_once()
    assert crm_sync.await_args.args[1] is applicant


async def test_applicant_deleted_drops_mirror_row_without_fetch() -> None:
    with (
        patch.object(applicants_h, "get_pool", return_value=object()),
        patch.object(applicants_h, "write_activity", new=AsyncMock()) as activity,
        patch.object(applicants_h.entities_repo, "delete_applicant", new=AsyncMock()) as delete,
        patch.object(applicants_h, "sync_lead_from_applicant", new=AsyncMock()) as crm_sync,
    ):
        await applicants_h.handle_applicant_changed(
            COMPANY_ID, _envelope("Applicant.Deleted", ApplicantId=15)
        )
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == 15
    crm_sync.assert_not_awaited()
    activity.assert_awaited_once()


async def test_application_changed_logs_with_both_ids() -> None:
    with (
        patch.object(applicants_h, "get_pool", return_value=object()),
        patch.object(applicants_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await applicants_h.handle_application_changed(
            COMPANY_ID,
            _envelope(
                "ApplicantApplication.Created",
                ApplicantId=15,
                ApplicationId=16,
            ),
        )
    activity.assert_awaited_once()
    desc = activity.await_args.args[1].description
    assert "#16" in desc and "#15" in desc
