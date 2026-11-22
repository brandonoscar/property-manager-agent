"""Which tenant-to-lease links mean "lives here now".

``helixis_lease_tenants.status`` is the LINK's status, not the lease's. Buildium
writes ``Current``, ``Future`` and ``MovedOut`` for the people on a lease
(measured on helixis-test, 2026-10-05: 96 Current, 4 Future and 37 MovedOut
links across three companies, none without a status). Rentvine, Rent Manager
and Propertyware write ``Current`` for every link they keep (each mapper's
``LEASE_TENANT_STATUS``; ``tests/unit/test_tenant_links.py`` holds them to this
module).

⚠ A lease can stay Active after one of its tenants moves out: the co-tenants
remain, and the leaver's link reads ``MovedOut``. A reader that joins the links
with no status filter therefore names, or writes to, somebody who no longer
lives there: a renewal offer or a payment reminder addressed to them, an email
from them matched to the lease they left.

A reader that means "who lives here now" or "who we write to about this lease"
filters with :func:`current_link_sql`. A reader that means "this tenant's
leases" (their history) does not.

A link with no status counts as current. It keeps the lease's answer, the one
every reader gave before this filter existed; the other way, a sync that stopped
receiving the field would empty every tenant list and read every unit as vacant.
"""

from __future__ import annotations

#: The one link status that means the tenant lives under the lease now.
CURRENT_LINK_STATUS = "Current"
#: Buildium's other two link statuses (measured): moved out of a lease that may
#: continue for the co-tenants, and added to a lease but not moved in yet.
MOVED_OUT_LINK_STATUS = "MovedOut"
FUTURE_LINK_STATUS = "Future"


def current_link_sql(alias: str = "lt") -> str:
    """The SQL condition that keeps only current links (a link with no status
    included), on the link table aliased ``alias``. The status is this module's
    constant, never a caller's string; the alias must be a bare identifier."""
    if not alias.isidentifier():
        raise ValueError(f"not a table alias: {alias!r}")
    return f"COALESCE({alias}.status, '{CURRENT_LINK_STATUS}') = '{CURRENT_LINK_STATUS}'"


def is_current_link(status: str | None) -> bool:
    """:func:`current_link_sql` for a status already read into Python: the
    same exact comparison, so the two cannot disagree about a word."""
    return status is None or status == CURRENT_LINK_STATUS


__all__ = [
    "CURRENT_LINK_STATUS",
    "FUTURE_LINK_STATUS",
    "MOVED_OUT_LINK_STATUS",
    "current_link_sql",
    "is_current_link",
]
