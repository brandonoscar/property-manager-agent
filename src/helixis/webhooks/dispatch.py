"""Dispatch a verified Buildium event envelope to the right handler.

Buildium publishes ~102 events across 34 categories. We handle the
ones below; the rest are KNOWN_IGNORED so the user can safely
subscribe to everything in Buildium's UI without us 5XXing.

  HANDLED:
    Rentals (Rental.*)              [3]
    Rental Units (RentalUnit.*)     [3]
    Leases (Lease.*)                [3]
    Lease Move Outs                 [1]
    Lease Tenants                   [3]
    Lease Transactions              [3]
    Work Orders                     [3]
    Tasks                           [3]
    Task History                    [3]
    Vendors                         [3]
    Vendor Transactions             [3]
    Bills                           [3]
    Bill Payments                   [3]
    Bank Account Transactions       [3]
    GL Accounts                     [3]
    Listings                        [3]
    Applicants                      [3]
    Applicant Applications          [2]
                                    ─────
                                    51 events handled

  IGNORED (200 silently):
    PhoneLogs, Rental Meter Readings, Application Transactions,
    Associations and all sub-entities (24 events),
    Budgets, BankAccounts (non-transaction),
    TaskCategories, VendorCategories, Installations
                                    ─────
                                    51 events ignored

  TOTAL: 102 events

Webhook payloads are deliberately minimal — they tell you *what
changed*, not *what the new state is*. Every handler does the same
basic dance:

    1. Pull entity_id from the envelope.
    2. Call the Buildium API to fetch the full entity.
    3. Update Helixis's local cache / send a notification / ping
       the agent / etc.

If an entity fetch 404s, the entity was deleted between when Buildium
sent the event and when we got around to handling it — log and drop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from helixis.webhooks.handlers.applicants import (
    handle_applicant_changed,
    handle_application_changed,
)
from helixis.webhooks.handlers.finance import (
    handle_bank_transaction,
    handle_bill_changed,
    handle_bill_payment,
    handle_gl_account_changed,
)
from helixis.webhooks.handlers.leases_v2 import (
    handle_lease_changed,
    handle_lease_deleted,
    handle_lease_moveout,
    handle_lease_tenant,
    handle_lease_transaction,
)
from helixis.webhooks.handlers.listings import handle_listing_changed
from helixis.webhooks.handlers.maintenance import (
    handle_task_changed,
    handle_task_history,
    handle_vendor_changed,
    handle_vendor_transaction,
    handle_workorder_changed,
    handle_workorder_deleted,
)
from helixis.webhooks.handlers.properties import (
    handle_rental_changed,
    handle_rental_unit_changed,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], Awaitable[None]]

DISPATCH_TABLE: dict[str, EventHandler] = {
    # Rental properties
    "Rental.Created": handle_rental_changed,
    "Rental.Updated": handle_rental_changed,
    "Rental.Deleted": handle_rental_changed,
    # Rental units
    "RentalUnit.Created": handle_rental_unit_changed,
    "RentalUnit.Updated": handle_rental_unit_changed,
    "RentalUnit.Deleted": handle_rental_unit_changed,
    # Leases
    "Lease.Created": handle_lease_changed,
    "Lease.Updated": handle_lease_changed,
    "Lease.Deleted": handle_lease_deleted,
    "Lease.MoveOut.Created": handle_lease_moveout,
    # Lease transactions
    "LeaseTransaction.Created": handle_lease_transaction,
    "LeaseTransaction.Updated": handle_lease_transaction,
    "LeaseTransaction.Deleted": handle_lease_transaction,
    # Lease tenants
    "LeaseTenant.Created": handle_lease_tenant,
    "LeaseTenant.Updated": handle_lease_tenant,
    "LeaseTenant.Deleted": handle_lease_tenant,
    # Work orders
    "WorkOrder.Created": handle_workorder_changed,
    "WorkOrder.Updated": handle_workorder_changed,
    "WorkOrder.Deleted": handle_workorder_deleted,
    # Tasks
    "Task.Created": handle_task_changed,
    "Task.Updated": handle_task_changed,
    "Task.Deleted": handle_task_changed,
    "Task.History.Created": handle_task_history,
    "Task.History.Updated": handle_task_history,
    "Task.History.Deleted": handle_task_history,
    # Vendors
    "Vendor.Created": handle_vendor_changed,
    "Vendor.Updated": handle_vendor_changed,
    "Vendor.Deleted": handle_vendor_changed,
    "Vendor.Transaction.Created": handle_vendor_transaction,
    "Vendor.Transaction.Updated": handle_vendor_transaction,
    "Vendor.Transaction.Deleted": handle_vendor_transaction,
    # Bills
    "Bill.Created": handle_bill_changed,
    "Bill.Updated": handle_bill_changed,
    "Bill.Deleted": handle_bill_changed,
    "Bill.Payment.Created": handle_bill_payment,
    "Bill.Payment.Updated": handle_bill_payment,
    "Bill.Payment.Deleted": handle_bill_payment,
    # GL accounts — chart-of-accounts mirror freshness (0042). The whole
    # chart refreshes per event (see handler); rare, cheap, exact.
    "GLAccount.Created": handle_gl_account_changed,
    "GLAccount.Updated": handle_gl_account_changed,
    "GLAccount.Deleted": handle_gl_account_changed,
    # Bank account transactions
    "BankAccount.Transaction.Created": handle_bank_transaction,
    "BankAccount.Transaction.Updated": handle_bank_transaction,
    "BankAccount.Transaction.Deleted": handle_bank_transaction,
    # Listings
    "Listing.Created": handle_listing_changed,
    "Listing.Updated": handle_listing_changed,
    "Listing.Deleted": handle_listing_changed,
    # Applicants & applications
    "Applicant.Created": handle_applicant_changed,
    "Applicant.Updated": handle_applicant_changed,
    "Applicant.Deleted": handle_applicant_changed,
    "ApplicantApplication.Created": handle_application_changed,
    "ApplicantApplication.Updated": handle_application_changed,
}


def _ops(*operations: str) -> tuple[str, ...]:
    return operations


_CRUD = _ops("Created", "Updated", "Deleted")


KNOWN_IGNORED: frozenset[str] = frozenset(
    [
        # Leasing-side bookkeeping we don't display. (Listing.* is handled —
        # see DISPATCH_TABLE.)
        *(f"PhoneLog.{op}" for op in _CRUD),
        *(f"Rental.MeterReading.{op}" for op in _CRUD),
        "Application.Transaction.Created",
        "Application.Transaction.Updated",
        "Application.Transaction.Deleted",
        # All Association.* — Brandon's demo is rentals-only.
        *(f"Association.{op}" for op in _CRUD),
        *(f"AssociationUnit.{op}" for op in _CRUD),
        *(f"AssociationOwner.{op}" for op in _CRUD),
        *(f"AssociationTenant.{op}" for op in _CRUD),
        *(f"AssociationBoardMember.{op}" for op in _CRUD),
        *(f"Association.MeterReading.{op}" for op in _CRUD),
        *(f"OwnershipAccount.{op}" for op in _CRUD),
        *(f"OwnershipAccountTransaction.{op}" for op in _CRUD),
        # Accounting setup events (read-mostly reference data). GLAccount.*
        # graduated to DISPATCH_TABLE with the 0042 chart mirror.
        *(f"Budget.{op}" for op in _CRUD),
        *(f"BankAccount.{op}" for op in _CRUD),  # the table itself, not transactions
        *(f"TaskCategory.{op}" for op in _CRUD),
        *(f"VendorCategory.{op}" for op in _CRUD),
        *(f"Installation.{op}" for op in _CRUD),
        # Email-ingest events have no legacy dispatch handler — they ride
        # the planner + summarizer queues only (email_ingest.py). Listed
        # here so the legacy claim loop acknowledges them quietly instead
        # of logging "Unknown event type … dropping" every poll.
        "Email.Received",
    ]
)


async def dispatch_event(company_id: str, event_name: str, envelope: dict) -> None:
    """Route a verified webhook envelope to its handler.

    ``company_id`` was already resolved by the router from ``AccountId``,
    so handlers don't need to repeat the lookup. Handlers receive the
    raw envelope dict — they should pull the entity id (LeaseId,
    WorkOrderId, etc.) themselves and call Buildium's API for the
    full entity.

    A handler that raises an exception is the caller's problem to
    log / persist; we don't catch here so the route can mark the
    queue row pending for retry.
    """
    handler = DISPATCH_TABLE.get(event_name)
    if handler is not None:
        await handler(company_id, envelope)
        await _touch_sync_state(company_id, event_name)
        return
    if event_name in KNOWN_IGNORED:
        logger.debug("Ignoring known event %s", event_name)
        return
    # New event type Buildium added since we last shipped — log it
    # for observability but acknowledge the webhook.
    logger.info("Unknown Buildium event type %s — dropping", event_name)


# Event family → the mirror table it refreshes (migration 0057 freshness).
# Keys are the event-name PREFIX before the operation suffix. Only families
# whose handler writes a mirror table are listed; anything else is skipped.
_EVENT_TABLE_MAP: dict[str, str] = {
    "Rental": "properties",
    "RentalUnit": "units",
    "Lease": "leases",
    "LeaseTransaction": "transactions",
    "LeaseTenant": "tenants",
    "WorkOrder": "work_orders",
    "Task": "tasks",
    "Vendor": "vendors",
    "Bill": "bills",
    "GLAccount": "gl_accounts",
    "BankAccount.Transaction": "bank_accounts",
    "Applicant": "applicants",
    "ApplicantApplication": "applicants",
}


def _table_for_event(event_name: str) -> str | None:
    # Longest-prefix match so "BankAccount.Transaction.Created" beats any
    # shorter family, and "Lease.MoveOut.Created" still maps to leases.
    best: str | None = None
    for prefix, table in _EVENT_TABLE_MAP.items():
        if event_name == prefix or event_name.startswith(prefix + "."):
            if best is None or len(prefix) > len(best):
                best = prefix
    return _EVENT_TABLE_MAP[best] if best else None


async def _touch_sync_state(company_id: str, event_name: str) -> None:
    """Best-effort freshness bookkeeping AFTER a successful handler run —
    a failure here must never mark the queue row for retry."""
    table = _table_for_event(event_name)
    if table is None:
        return
    try:
        from helixis.db.engine import get_pool
        from helixis.db.repositories.mirror_sync_state import record_sync

        await record_sync(get_pool(), company_id=company_id, table_name=table, source="webhook")
    except Exception:  # noqa: BLE001 — freshness is never load-bearing
        logger.debug("sync-state touch failed for %s (non-fatal)", event_name, exc_info=True)


__all__ = [
    "DISPATCH_TABLE",
    "EventHandler",
    "KNOWN_IGNORED",
    "dispatch_event",
]
