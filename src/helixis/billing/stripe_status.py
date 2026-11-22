"""Which subscription statuses Stripe can actually send us.

WHAT BROKE
----------
``helixis_billing.status`` carries a CHECK constraint listing five values
(0087): ``trialing``, ``active``, ``past_due``, ``canceled``, ``incomplete``.
Stripe's subscription object has **eight**. The three it cannot store are the
three that matter most:

* ``incomplete_expired`` — the first payment definitively failed. ``gate.py``
  has written the correct message for this since it was added, and the row
  could never hold the status that reaches it, so a customer whose card failed
  at checkout was told *"your bank is still confirming the first payment, this
  usually clears within a few minutes"* — forever.
* ``unpaid`` — Stripe exhausted dunning and gave up. The row keeps ``past_due``,
  which IS in ``gate._SERVING_STATUSES``, so **the product is served free,
  indefinitely, to a customer Stripe has stopped billing.**
* ``paused`` — collection paused; the row keeps whatever it had.

⚠ **The whole update is lost, not just the status.** ``_apply_event`` writes
the subscription id, the customer id, the plan, the seat count, the metering
window and the ordering stamp in ONE statement. A CHECK violation on one column
raises, `stripe_webhook` swallows it (correctly — a raise would make Stripe
retry for days and then disable the endpoint), and every other field in that
event is discarded too. So a status Stripe invents next year does not merely
fail to store: it silently stops the metering window from rolling.

That is why an unrecognised status is written as NULL and COALESCEd rather than
clamped to a guess. Keeping the last known value loses one fact; guessing
``active`` serves the product free and guessing ``canceled`` locks out somebody
who is paying, and the rest of the event still lands either way.

⚠ **Documented, not probed.** stripe.com is egress-blocked from the dev
container (gotcha 75), so this set comes from Stripe's subscription-status
documentation. That is exactly why the unknown case has to be safe: the list
being incomplete is the expected failure, not a surprising one.
"""

from __future__ import annotations

from typing import Any, Final

#: Every status Stripe's subscription object documents.
#:
#: ⚠ ONE definition. The migration's CHECK constraint is pinned against this
#: set by ``tests/unit/test_stripe_status_vocabulary.py``, because a list typed
#: out in two places is the drift that produced the defect above — the SQL was
#: written once and the code grew three branches past it (gotcha 28: a string
#: crossing a producer/consumer boundary needs a test that reads BOTH sides).
STRIPE_SUBSCRIPTION_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
    }
)


def storable_status(raw: Any) -> str | None:
    """The status to write, or ``None`` for "leave the column alone".

    ``None`` on an ABSENT status too, not just an unrecognised one. The caller
    used to default a missing status to ``"active"``, which turns a malformed
    or truncated event into a serving subscription — the one direction that
    hands out the product. Unknown is not a value (gotchas 55/57).
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if value in STRIPE_SUBSCRIPTION_STATUSES else None


#: Statuses a subscription never comes back from. It exists as a Stripe object
#: and as an id on our row, and it will never charge anybody again.
#:
#: ⚠ `canceled` is here as DEFENCE IN DEPTH, not as the common path.
#: `customer.subscription.deleted` sets the status AND nulls
#: `stripe_subscription_id` in one statement, so a cancelled company normally
#: reaches the callers below with no id at all. This covers the dropped or
#: out-of-order webhook that leaves `canceled` sitting next to a live-looking
#: id — the state migration 0093's ordering guard exists because events do not
#: arrive in the order they were sent.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"incomplete_expired", "canceled"})


def subscription_can_bill(status: Any) -> bool:
    """Could the subscription on this row still charge the customer?

    Three callers ask this and every one of them was asking
    ``bool(stripe_subscription_id)`` instead — which is a different question,
    and answers it wrongly for exactly the customer who is trying to pay us.

    * ``create_checkout`` refuses a second subscription. Its own comment gives
      the reason as *"the ORIGINAL is invisible to this product and bills
      forever"* — a property of a subscription that CAN bill.
    * ``change_plan`` re-prices one. Stripe cannot re-price a dead
      subscription, and that endpoint's comment already says a *"lapsed
      company"* belongs in checkout.
    * ``seat_change_quote`` decides whether a new teammate is prorated onto an
      existing subscription or simply included in the first bill of the next
      one. For a dead subscription it is the second, always.

    So a company whose first payment expired (``incomplete_expired`` — a real,
    persisting status since migration 0097, with the id still on the row) was
    told *"You already have a subscription"* by checkout and handed a 502 by
    change-plan. Both exits shut, on the one screen where somebody is trying to
    start paying.

    ⚠ **DO NOT IMPLEMENT THIS AS ``status not in gate._SERVING_STATUSES``.**
    That is the obvious shortcut and it re-opens the double-subscription bug
    this repo already shipped once: ``incomplete`` is not *serving* — the gate
    correctly pauses the product mid-3-D-Secure — but it is very much still
    *billing*, and it becomes ``active`` the moment the bank confirms. Letting
    that customer open a second checkout is precisely what ``gate.py``'s
    ``incomplete`` branch refuses to suggest. Serving and billing are two
    questions about one row and they disagree on two of the eight statuses.

    ⚠ **Unknown means "yes it can bill", which is today's behaviour exactly.**
    ``storable_status`` writes NULL for a status Stripe invents after this file
    and the column is COALESCEd, so a stale or absent value is expected rather
    than surprising. Both error directions here are real — refusing wrongly
    blocks a customer from paying, allowing wrongly charges them twice for a
    subscription the app can no longer see — so the default is the one where
    being wrong costs a support email instead of their money, and where a new
    Stripe status changes nothing until somebody decides it should.
    """
    if not isinstance(status, str):
        return True
    return status.strip() not in TERMINAL_STATUSES


__all__ = [
    "STRIPE_SUBSCRIPTION_STATUSES",
    "TERMINAL_STATUSES",
    "storable_status",
    "subscription_can_bill",
]
