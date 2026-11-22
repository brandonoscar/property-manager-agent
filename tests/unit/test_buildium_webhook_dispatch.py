"""Routing tests for the new dispatch table.

We don't exercise the per-category handlers here — those have their
own tests. We just confirm that the right handler fires for a given
``EventName``, that KNOWN_IGNORED events silently 200, and that
unknown event types are logged but do NOT raise.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from helixis.webhooks import dispatch as dispatch_module
from helixis.webhooks.dispatch import KNOWN_IGNORED, dispatch_event

COMPANY_ID = "company-A"


def _envelope(event_name: str, **extra: object) -> dict[str, object]:
    return {
        "EventName": event_name,
        "EventDateTime": "2026-04-29T12:00:00.0000000Z",
        "AccountId": 42,
        **extra,
    }


@pytest.mark.parametrize(
    ("event_name", "handler_attr", "extra"),
    [
        ("Rental.Created", "handle_rental_changed", {"PropertyId": 1}),
        ("Rental.Updated", "handle_rental_changed", {"PropertyId": 1}),
        ("Rental.Deleted", "handle_rental_changed", {"PropertyId": 1}),
        ("RentalUnit.Created", "handle_rental_unit_changed", {"UnitId": 2}),
        ("RentalUnit.Updated", "handle_rental_unit_changed", {"UnitId": 2}),
        ("RentalUnit.Deleted", "handle_rental_unit_changed", {"UnitId": 2}),
        ("Lease.Created", "handle_lease_changed", {"LeaseId": 3}),
        ("Lease.Updated", "handle_lease_changed", {"LeaseId": 3}),
        ("Lease.Deleted", "handle_lease_deleted", {"LeaseId": 3}),
        ("Lease.MoveOut.Created", "handle_lease_moveout", {"LeaseId": 3}),
        (
            "LeaseTransaction.Created",
            "handle_lease_transaction",
            {"LeaseId": 3, "TransactionId": 9},
        ),
        (
            "LeaseTenant.Created",
            "handle_lease_tenant",
            {"LeaseId": 3, "TenantId": 8},
        ),
        ("WorkOrder.Created", "handle_workorder_changed", {"WorkOrderId": 4}),
        ("WorkOrder.Updated", "handle_workorder_changed", {"WorkOrderId": 4}),
        ("WorkOrder.Deleted", "handle_workorder_deleted", {"WorkOrderId": 4}),
        ("Task.Created", "handle_task_changed", {"TaskId": 5}),
        (
            "Task.History.Created",
            "handle_task_history",
            {"TaskId": 5, "TaskHistoryId": 6},
        ),
        ("Vendor.Created", "handle_vendor_changed", {"VendorId": 7}),
        (
            "Vendor.Transaction.Created",
            "handle_vendor_transaction",
            {"VendorId": 7, "TransactionId": 10},
        ),
        ("Bill.Created", "handle_bill_changed", {"BillId": 11}),
        (
            "Bill.Payment.Created",
            "handle_bill_payment",
            {"PaymentId": 12, "BillIds": [11]},
        ),
        (
            "BankAccount.Transaction.Created",
            "handle_bank_transaction",
            {"BankAccountId": 13, "TransactionId": 14},
        ),
        ("Applicant.Created", "handle_applicant_changed", {"ApplicantId": 15}),
        (
            "ApplicantApplication.Created",
            "handle_application_changed",
            {"ApplicantId": 15, "ApplicationId": 16},
        ),
    ],
)
async def test_dispatch_routes_to_correct_handler(
    event_name: str,
    handler_attr: str,
    extra: dict[str, object],
) -> None:
    envelope = _envelope(event_name, **extra)
    target = AsyncMock(return_value=None)
    # Swap the entry on the dispatch table for the duration of the test.
    original = dispatch_module.DISPATCH_TABLE[event_name]
    dispatch_module.DISPATCH_TABLE[event_name] = target
    try:
        await dispatch_event(COMPANY_ID, event_name, envelope)
    finally:
        dispatch_module.DISPATCH_TABLE[event_name] = original
    target.assert_awaited_once_with(COMPANY_ID, envelope)


async def test_dispatch_silently_ignores_known_unhandled_events() -> None:
    """PhoneLog.* / Association.* etc. should not raise so the user can
    subscribe to everything in Buildium without us 5XXing on the events we
    don't care about."""
    envelope = _envelope("PhoneLog.Created", PhoneLogId=99)
    # No raise = pass.
    await dispatch_event(COMPANY_ID, "PhoneLog.Created", envelope)


async def test_dispatch_logs_unknown_events(caplog) -> None:
    """An entirely new event name (e.g. one Buildium adds after we
    ship) should log at INFO so we notice in observability — and 200
    silently rather than 5XXing."""
    envelope = _envelope("TotallyMadeUp.Event", FakeId=1)
    with caplog.at_level(logging.INFO, logger="helixis.webhooks.dispatch"):
        await dispatch_event(COMPANY_ID, "TotallyMadeUp.Event", envelope)
    assert any("Unknown Buildium event type" in rec.message for rec in caplog.records), [
        rec.message for rec in caplog.records
    ]


def test_known_ignored_covers_associations() -> None:
    """Spot-check the KNOWN_IGNORED set — these are the events most
    likely to flow on a freshly subscribed account."""
    assert "Association.Created" in KNOWN_IGNORED
    assert "OwnershipAccountTransaction.Created" in KNOWN_IGNORED
    assert "PhoneLog.Updated" in KNOWN_IGNORED
    # Real handler-backed events must NOT be in the ignore set — Listing.*
    # is now handled (see DISPATCH_TABLE), so it must not be ignored.
    assert "Listing.Created" not in KNOWN_IGNORED
    assert "Listing.Deleted" not in KNOWN_IGNORED
    assert "Lease.Created" not in KNOWN_IGNORED
    assert "WorkOrder.Created" not in KNOWN_IGNORED


def test_dispatch_table_event_count_is_exactly_51() -> None:
    """Lock the supported surface so accidental drops are caught."""
    assert len(dispatch_module.DISPATCH_TABLE) == 51
