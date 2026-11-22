"""Buildium webhook event schemas.

The payload is a flat JSON object with:
- EventName — dotted string, e.g. "Lease.Created"
- EventDateTime — ISO-8601 UTC
- AccountId — integer Buildium account
- entity-specific IDs (LeaseId, WorkOrderId, TenantId, UnitId, TransactionId, ...)

We validate the common envelope, keep the full payload as a dict for the
handler, and derive entity IDs generically so new events don't need code
changes to be accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Event names we explicitly subscribe to (see docs/buildium-webhooks.md).
#
# This MUST mirror ``dispatch.DISPATCH_TABLE`` exactly — every event we have a
# handler for, and nothing we don't. Subscribing to an event we KNOWN_IGNORE
# (the old ``Application.Transaction.*`` bug) just burns a webhook delivery;
# failing to subscribe to an event we DO handle (the old missing Bill / Vendor /
# Task / BankAccount-transaction rows) leaves a working handler dark because
# Buildium never sends it. ``tests/unit/test_buildium_subscribed_events.py``
# asserts ``SUBSCRIBED_EVENTS == set(DISPATCH_TABLE)`` so the two can't drift.
SUBSCRIBED_EVENTS: set[str] = {
    # Rental properties + units
    "Rental.Created",
    "Rental.Updated",
    "Rental.Deleted",
    "RentalUnit.Created",
    "RentalUnit.Updated",
    "RentalUnit.Deleted",
    # Leases, transactions, tenants, move-outs
    "Lease.Created",
    "Lease.Updated",
    "Lease.Deleted",
    "Lease.MoveOut.Created",
    "LeaseTransaction.Created",
    "LeaseTransaction.Updated",
    "LeaseTransaction.Deleted",
    "LeaseTenant.Created",
    "LeaseTenant.Updated",
    "LeaseTenant.Deleted",
    # Work orders
    "WorkOrder.Created",
    "WorkOrder.Updated",
    "WorkOrder.Deleted",
    # Tasks (+ history)
    "Task.Created",
    "Task.Updated",
    "Task.Deleted",
    "Task.History.Created",
    "Task.History.Updated",
    "Task.History.Deleted",
    # Vendors (+ transactions)
    "Vendor.Created",
    "Vendor.Updated",
    "Vendor.Deleted",
    "Vendor.Transaction.Created",
    "Vendor.Transaction.Updated",
    "Vendor.Transaction.Deleted",
    # Bills (+ payments)
    "Bill.Created",
    "Bill.Updated",
    "Bill.Deleted",
    "Bill.Payment.Created",
    "Bill.Payment.Updated",
    "Bill.Payment.Deleted",
    # GL accounts (chart-of-accounts mirror, 0042)
    "GLAccount.Created",
    "GLAccount.Updated",
    "GLAccount.Deleted",
    # Bank-account transactions
    "BankAccount.Transaction.Created",
    "BankAccount.Transaction.Updated",
    "BankAccount.Transaction.Deleted",
    # Listings
    "Listing.Created",
    "Listing.Updated",
    "Listing.Deleted",
    # Applicants + applications
    "Applicant.Created",
    "Applicant.Updated",
    "Applicant.Deleted",
    "ApplicantApplication.Created",
    "ApplicantApplication.Updated",
}

# Fields we promote to indexable columns on buildium_events for search/audit.
_ENTITY_ID_FIELDS = (
    "LeaseId",
    "WorkOrderId",
    "TaskId",
    "TenantId",
    "UnitId",
    "PropertyId",
    "TransactionId",
    "BillId",
    "PaymentId",
    "VendorId",
    "OwnerId",
    "BankAccountId",
    "GLAccountId",
    "ApplicantId",
    "ApplicationId",
    "RentalId",
)


class BuildiumEventEnvelope(BaseModel):
    """Common envelope validated on every inbound event."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event_name: str = Field(alias="EventName")
    event_date_time: datetime = Field(alias="EventDateTime")
    account_id: int = Field(alias="AccountId")

    def entity_ids(self, payload: dict[str, Any]) -> dict[str, int]:
        """Extract any known entity-id fields present in the raw payload."""
        return {
            key: int(payload[key])
            for key in _ENTITY_ID_FIELDS
            if key in payload and payload[key] is not None
        }


def dedup_key(payload: dict[str, Any]) -> str:
    """Natural dedup key — account_id + event_name + event_time + primary entity id.

    Buildium can redeliver; a unique index on this key prevents duplicate
    processing without requiring handler-side idempotency.

    The ``account=…`` prefix scopes the dedup namespace per Buildium
    tenant so two companies receiving structurally identical events in
    the same second cannot collide on the unique index. Existing rows
    inserted with the legacy keyspace (no prefix) keep their values
    immutably; new rows go into the new namespace, so the unique
    constraint holds across both.
    """
    parts = [
        f"account={payload.get('AccountId', '')}",
        str(payload.get("EventName", "")),
        str(payload.get("EventDateTime", "")),
    ]
    # Non-Buildium sources (email ingest) carry a string ``MessageId`` that
    # the integer ``_ENTITY_ID_FIELDS`` loop below can't use. When present
    # it IS the natural per-message identity, so fold it in — this makes
    # two distinct emails arriving in the same second dedup correctly
    # (account+event+second alone would collide). Buildium payloads have
    # no ``MessageId``, so this is inert for them.
    message_id = payload.get("MessageId")
    if message_id is not None:
        parts.append(f"MessageId={message_id}")
    # Include EVERY present entity id, not just the first. A single event
    # can carry several (a lease rent charge has BOTH LeaseId AND
    # TransactionId); keying on only the first (LeaseId) made two same-second
    # charges on ONE lease collide → the second silently dropped by the
    # ON CONFLICT DO NOTHING unique index (2026-07 Buildium release audit —
    # real data loss). Emitting all present ids in the fixed _ENTITY_ID_FIELDS
    # order gives multi-entity events distinct, deterministic keys while a
    # genuine Buildium redelivery (identical payload) still keys identically.
    for field in _ENTITY_ID_FIELDS:
        if field in payload and payload[field] is not None:
            parts.append(f"{field}={payload[field]}")
    return "|".join(parts)


__all__ = [
    "BuildiumEventEnvelope",
    "SUBSCRIBED_EVENTS",
    "dedup_key",
]
