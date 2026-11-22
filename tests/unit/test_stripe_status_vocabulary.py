"""The status column can store every status Stripe can send.

WHAT THIS EXISTS TO CATCH, measured rather than imagined: 0087 wrote the CHECK
constraint with FIVE values and Stripe documents EIGHT. The three missing ones
were `incomplete_expired`, `unpaid` and `paused`, and each one made the
`customer.subscription.*` handler raise 23514 — which `stripe_webhook` swallows,
correctly, because raising makes Stripe retry for days and then disable the
endpoint.

So the cost was never the status column. `_apply_event` writes the subscription
id, the customer id, the plan, the seat count, the metering window and the
ordering stamp in ONE statement, and a violation on any column discards all of
them. The worst case was `unpaid`: the row kept `past_due`, `past_due` is in
`gate._SERVING_STATUSES`, and the product was served free and indefinitely to a
customer Stripe had stopped billing.

⚠ THE CORPUS, NOT THE FUNCTION (gotcha 45). `storable_status` was correct on
its own terms throughout — there was no `storable_status`, and a test feeding
it hand-written inputs would have passed the day it was written and said
nothing about a constraint in a SQL file. What has to be compared is the
migration the database actually runs against the set the code actually uses.

⚠ SCOPED TO THE CONSTRAINT STATEMENT, never the file (gotchas 40/59). 0097's
header names all three missing statuses in prose in order to explain them, so a
whole-file scan reads the explanation as the vocabulary and passes against a
constraint that lists none of them.
"""

from __future__ import annotations

import pathlib
import re

from helixis.billing.gate import _SERVING_STATUSES
from helixis.billing.stripe_status import STRIPE_SUBSCRIPTION_STATUSES, storable_status

_MIGRATIONS = pathlib.Path("supabase/migrations")


def _constraint_statuses() -> set[str]:
    """The values the LAST migration to define `helixis_billing_status_known`
    allows.

    ⚠ The whole corpus, latest wins — an applied migration cannot be edited, so
    a widening always arrives in a later file (the lesson gotcha 82's guard
    learned about views). Reading only 0087 would report the original five
    forever.
    """
    latest: set[str] | None = None
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        # Only the ADD CONSTRAINT statement, so the file's own prose about the
        # statuses it is fixing cannot be mistaken for the list.
        for stmt in re.findall(
            r"add\s+constraint\s+helixis_billing_status_known\s*(.*?);",
            sql,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            latest = set(re.findall(r"'([a-z_]+)'", stmt))
    assert latest, "no migration defines helixis_billing_status_known — has it been renamed?"
    return latest


def test_the_scan_finds_a_real_constraint() -> None:
    """A renamed constraint or a bad glob yields an empty set, and every
    comparison below then passes against nothing."""
    found = _constraint_statuses()
    assert len(found) >= 5, f"only parsed {found} out of the constraint statement"


def test_the_column_can_store_every_status_stripe_sends() -> None:
    missing = STRIPE_SUBSCRIPTION_STATUSES - _constraint_statuses()
    assert not missing, (
        f"Stripe can send {sorted(missing)} and helixis_billing_status_known rejects it. "
        "The write raises 23514, stripe_webhook swallows it, and the ENTIRE event — "
        "plan, seats, metering window, subscription id — is discarded with the status."
    )


def test_the_constraint_invents_no_status_the_code_does_not_know() -> None:
    """The other direction. A value only SQL knows about is a state the gate
    has no branch for, so it falls to the generic 'your subscription has ended'
    message for a customer who may be paying."""
    extra = _constraint_statuses() - STRIPE_SUBSCRIPTION_STATUSES
    assert not extra, (
        f"the constraint allows {sorted(extra)} and STRIPE_SUBSCRIPTION_STATUSES does not."
    )


def test_every_serving_status_is_one_the_column_can_hold() -> None:
    """`_SERVING_STATUSES` decides who gets the product. A value in there that
    the column cannot store is a serving state no row can ever reach."""
    assert _SERVING_STATUSES <= STRIPE_SUBSCRIPTION_STATUSES
    assert _SERVING_STATUSES <= _constraint_statuses()


def test_unpaid_and_paused_do_not_serve() -> None:
    """The half the migration alone cannot fix.

    Making `unpaid` storable is only safe because the gate refuses it. If it
    were ever added to the serving set, this migration would have widened the
    column specifically so a customer nobody is billing could be served —
    turning the fix into the bug it was written for.
    """
    assert "unpaid" not in _SERVING_STATUSES
    assert "paused" not in _SERVING_STATUSES
    assert "incomplete_expired" not in _SERVING_STATUSES


def test_an_unrecognised_status_is_not_written_rather_than_guessed() -> None:
    """`None` means "leave the column alone", and the caller COALESCEs it.

    Clamping to `active` serves the product free; clamping to `canceled` locks
    out somebody who is paying. Keeping the last known value loses one fact and
    lets the rest of the event land (gotchas 55/57 — unknown is not a value).
    """
    assert storable_status("this_is_not_a_stripe_status") is None
    assert storable_status(None) is None
    assert storable_status("") is None
    assert storable_status(123) is None


def test_a_missing_status_is_also_not_written() -> None:
    """The specific default that was there before: `str(obj.get("status") or
    "active")` turned a malformed or truncated event into a SERVING
    subscription — the one direction that hands out the product."""
    assert storable_status(None) != "active"


def test_a_recognised_status_is_written_verbatim() -> None:
    """The other direction, so a guard that returned None for everything —
    which would silently freeze every company's status — cannot pass."""
    for status in STRIPE_SUBSCRIPTION_STATUSES:
        assert storable_status(status) == status
    assert storable_status("  active  ") == "active"
