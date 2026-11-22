"""Allowlist for which Buildium events surface in the Inbox.

The Inbox is for genuinely actionable, important items — tasks, work
orders, resident requests (and their comments), and leasing-pipeline
milestones (new leases, renewals, move-outs, applicants/applications).

Everything else Buildium sends — rent payments and charges, lease
transactions, tenant-identity edits, bills, bank/vendor transactions,
rental metadata, owner-ledger bookkeeping, GL/association setup — is
accepted (200'd) and persisted for audit, but never shown in the
Inbox. We use an ALLOWLIST rather than a denylist on purpose: Buildium
keeps adding event types, and a denylist always lags one billing-run
behind (a Buildium owner-charge run once fanned 20 bare cards into the
General lane because the routing pipeline didn't know to ignore them).

Matching is prefix-based on the dotted event name so future sub-events
(e.g. a new ``Task.History.*`` variant) are covered without a code
change. The trailing dot matters: ``"Lease."`` matches ``Lease.Created``
and ``Lease.MoveOut.Created`` but NOT ``LeaseTransaction.Created`` (a
payment) or ``LeaseTenant.Created`` (tenant identity) — both of which
the product deliberately keeps out of the Inbox.
"""

from __future__ import annotations

# Each entry is the dotted prefix (including the trailing dot) of an
# event family the Inbox surfaces. Keep this list and the product
# intent in §1 of the runbook in sync.
INBOX_EVENT_PREFIXES: tuple[str, ...] = (
    "Task.",  # tasks + resident requests (Buildium models requests as Tasks) + Task.History
    "WorkOrder.",  # maintenance work orders
    "TenantRequest.",  # resident/tenant maintenance requests
    "ResidentRequest.",  # Buildium's alternate naming for the same
    "Lease.",  # new leases, updates, move-outs (NOT LeaseTransaction / LeaseTenant)
    "LeaseRenewal.",  # renewals
    "Applicant.",  # applicant applied / updated
    "ApplicantApplication.",  # application submitted / updated
    "Email.",  # important+long inbound emails from known contacts (email_triage)
    "Helixis.",  # internal events — teammate task requests (team_tasks)
    "Risk.",  # detector findings bridged into the pipeline (detectors/inbox_bridge)
    "Comms.",  # inbound resident texts / calls (webhooks/comms_ingest)
)


def surfaces_in_inbox(event_name: str) -> bool:
    """True if ``event_name`` is an actionable event the Inbox shows."""
    return any(event_name.startswith(prefix) for prefix in INBOX_EVENT_PREFIXES)


def inbox_like_patterns() -> list[str]:
    """SQL ``LIKE`` patterns for the allowlist (e.g. ``Task.%``).

    Used by the ``recent-events`` Inbox query as
    ``event_name LIKE ANY($n::text[])``. ``.`` is a literal in ``LIKE``
    (only ``%`` and ``_`` are wildcards), so ``'Lease.%'`` will not
    leak ``LeaseTransaction.*``.
    """
    return [f"{prefix}%" for prefix in INBOX_EVENT_PREFIXES]


__all__ = ["INBOX_EVENT_PREFIXES", "inbox_like_patterns", "surfaces_in_inbox"]
