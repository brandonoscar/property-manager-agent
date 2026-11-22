"""Static mapping from Buildium event names to routing categories (PR #80).

Routing is deterministic: every supported Buildium webhook event maps
to exactly one of the five property × roles categories Brandon
designed for the Properties grid. The agent does NOT classify at
LLM-time — categories are an audit surface that the property manager
configures and that the webhook router applies the same way every
time.

Unknown events default to ``other`` rather than failing. A future PR
can fold per-event LLM classification in for ambiguous cases (the
brief flags tenant-portal requests with no clear category as a
candidate), but v1 keeps everything in this table so the dispatch
log is human-readable.
"""

from __future__ import annotations

# Brandon's five categories from PR #80 brief. Order here is the
# column order on the Properties grid; ``other`` is the catch-all
# for events that don't fit into a workflow lane.
CATEGORIES: tuple[str, ...] = (
    "maintenance",
    "leasing",
    "accounting",
    "tenant_relations",
    "other",
)


EVENT_CATEGORY: dict[str, str] = {
    # ── Maintenance — work orders + tenant maintenance requests ──
    "WorkOrder.Created": "maintenance",
    "WorkOrder.Updated": "maintenance",
    "WorkOrder.Deleted": "maintenance",
    "TenantRequest.Created": "maintenance",
    "TenantRequest.Updated": "maintenance",
    # ── Leasing — leases, renewals, applicants, listings ──
    "Lease.Created": "leasing",
    "Lease.Updated": "leasing",
    "Lease.Deleted": "leasing",
    "LeaseRenewal.Created": "leasing",
    "Applicant.Created": "leasing",
    "Applicant.Updated": "leasing",
    "Applicant.Deleted": "leasing",
    "ApplicantApplication.Created": "leasing",
    "ApplicantApplication.Updated": "leasing",
    "Listing.Created": "leasing",
    "Listing.Updated": "leasing",
    "Listing.Deleted": "leasing",
    # ── Accounting — ledger, bills, owner contributions, vendor payments ──
    "LeaseTransaction.Created": "accounting",
    "LeaseTransaction.Updated": "accounting",
    "LeaseTransaction.Deleted": "accounting",
    "Bill.Created": "accounting",
    "Bill.Paid": "accounting",
    "OwnerContribution.Created": "accounting",
    "OwnerDraw.Created": "accounting",
    "VendorPayment.Created": "accounting",
    # ── Tenant relations — tenant identity changes ──
    "LeaseTenant.Created": "tenant_relations",
    "LeaseTenant.Updated": "tenant_relations",
    "LeaseTenant.Deleted": "tenant_relations",
    "Tenant.Created": "tenant_relations",
    "Tenant.Updated": "tenant_relations",
    # ── Other — explicit catch-alls so the router doesn't fall through
    # to the default branch on common events. (Rentals/units/owners
    # don't have a natural "workflow" lane — they're metadata changes.)
    "Rental.Created": "other",
    "Rental.Updated": "other",
    "Rental.Deleted": "other",
    "RentalUnit.Created": "other",
    "RentalUnit.Updated": "other",
    "RentalUnit.Deleted": "other",
    "RentalOwner.Created": "other",
    "RentalOwner.Updated": "other",
    "RentalOwner.Deleted": "other",
    "Vendor.Created": "other",
    "Vendor.Updated": "other",
    # Generic tasks land in ``other`` for v1 — Buildium's Task.Category
    # field would let us subdivide in a future PR, but the source of
    # truth on category is the grid the property manager configures.
    "Task.Created": "other",
    "Task.Updated": "other",
}


def categorize(event_name: str) -> str:
    """Return the routing category for a Buildium event name.

    Unknown events default to ``other`` so the router can always make
    a decision; the property × roles grid's "other" cell catches them.
    Adding LLM classification here is explicitly out of scope for v1
    — the routing log must stay auditable.
    """
    return EVENT_CATEGORY.get(event_name, "other")


__all__ = ["CATEGORIES", "EVENT_CATEGORY", "categorize"]
