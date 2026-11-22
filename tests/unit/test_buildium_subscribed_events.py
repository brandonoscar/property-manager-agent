"""Guard: the documented subscribe list must match what we actually process.

Two silent failure modes this locks down:

* An event in ``SUBSCRIBED_EVENTS`` that the dispatch table doesn't handle (and
  is ``KNOWN_IGNORED``) — we register for a Buildium webhook only to drop it.
  This was the real ``Application.Transaction.*`` bug.
* An event the dispatch table DOES handle but that's missing from
  ``SUBSCRIBED_EVENTS`` — a working handler stays dark because the operator,
  following the docs, never subscribes to it (the old Bill / Vendor / Task /
  BankAccount-transaction gap).
"""

from __future__ import annotations

from helixis.webhooks.dispatch import DISPATCH_TABLE, KNOWN_IGNORED
from helixis.webhooks.event_schemas import SUBSCRIBED_EVENTS


def test_subscribed_events_match_dispatch_table() -> None:
    """Every subscribed event has a handler, and every handler is subscribed."""
    assert SUBSCRIBED_EVENTS == set(DISPATCH_TABLE), {
        "subscribed_but_no_handler": sorted(SUBSCRIBED_EVENTS - set(DISPATCH_TABLE)),
        "handled_but_not_subscribed": sorted(set(DISPATCH_TABLE) - SUBSCRIBED_EVENTS),
    }


def test_no_subscribed_event_is_known_ignored() -> None:
    """We never subscribe to an event we explicitly drop."""
    overlap = SUBSCRIBED_EVENTS & KNOWN_IGNORED
    assert not overlap, f"subscribed-but-ignored: {sorted(overlap)}"


def test_dispatch_table_and_known_ignored_are_disjoint() -> None:
    """A handled event must not also be in the ignore set (ambiguous routing)."""
    overlap = set(DISPATCH_TABLE) & KNOWN_IGNORED
    assert not overlap, f"both handled and ignored: {sorted(overlap)}"
