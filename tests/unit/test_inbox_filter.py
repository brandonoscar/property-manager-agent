"""Unit tests for the Inbox event allowlist (`webhooks/inbox_filter.py`).

The Inbox is an ALLOWLIST of actionable Buildium event families. The
load-bearing subtlety is prefix-boundary correctness: ``Lease.`` must surface
new leases / move-outs but must NOT leak ``LeaseTransaction.*`` (payments) or
``LeaseTenant.*`` (tenant-identity edits) — a denylist gap there once fanned 20
owner-charge cards into the General lane (see module docstring).
"""

from __future__ import annotations

import pytest

from helixis.webhooks.inbox_filter import (
    INBOX_EVENT_PREFIXES,
    inbox_like_patterns,
    surfaces_in_inbox,
)


@pytest.mark.parametrize(
    "event_name",
    [
        "Task.Created",
        "Task.Updated",
        "Task.History.Created",  # sub-event covered by prefix, no code change
        "WorkOrder.Created",
        "TenantRequest.Created",
        "ResidentRequest.Updated",
        "Lease.Created",
        "Lease.MoveOut.Created",  # nested lease milestone
        "LeaseRenewal.Created",
        "Applicant.Created",
        "ApplicantApplication.Submitted",
    ],
)
def test_actionable_events_surface(event_name: str) -> None:
    assert surfaces_in_inbox(event_name) is True


@pytest.mark.parametrize(
    "event_name",
    [
        # The critical near-miss exclusions — same "Lease" root, different family.
        "LeaseTransaction.Created",  # a rent payment, not a lease milestone
        "LeaseTenant.Created",  # tenant-identity edit
        # Bookkeeping / metadata families the product keeps out of the Inbox.
        "RentPayment.Created",
        "Bill.Created",
        "BankAccountTransaction.Created",
        "Rental.Updated",
        "GeneralLedger.Created",
        "Ownership.Updated",
        "",  # empty event name must never surface
    ],
)
def test_non_actionable_events_excluded(event_name: str) -> None:
    assert surfaces_in_inbox(event_name) is False


def test_prefix_boundary_is_not_substring_match() -> None:
    # Matching is startswith, not "in": a family that merely *contains* an
    # allowlisted token mid-string must not surface.
    assert surfaces_in_inbox("MyTask.Created") is False
    assert surfaces_in_inbox("SubLease.Created") is False


def test_every_prefix_ends_with_dot() -> None:
    # The trailing dot is what makes ``Lease.`` exclude ``LeaseTransaction.``;
    # a missing dot would silently widen the allowlist.
    for prefix in INBOX_EVENT_PREFIXES:
        assert prefix.endswith("."), f"prefix {prefix!r} missing trailing dot"


def test_like_patterns_mirror_the_prefixes() -> None:
    patterns = inbox_like_patterns()
    assert patterns == [f"{p}%" for p in INBOX_EVENT_PREFIXES]
    # Every LIKE pattern's literal stem is exactly an allowlist prefix, so the
    # SQL query and the Python predicate can never drift apart.
    for pattern, prefix in zip(patterns, INBOX_EVENT_PREFIXES, strict=True):
        assert pattern[:-1] == prefix
        assert pattern.endswith("%")
