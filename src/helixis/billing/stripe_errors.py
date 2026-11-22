"""Telling "Stripe is unreachable" apart from "that object does not exist".

WHY THIS MODULE EXISTS
----------------------
``stripe_client``'s own docstring states the rule the whole file is built on:

    Nothing here raises into a request handler: every call returns ``None`` on
    failure and logs. A billing lookup that explodes must not take down the
    page that shows it.

That is right, and it collapses two facts into one value. A network timeout
and *"No such customer: cus_…"* both arrive as ``None``, so the caller cannot
tell a blip from a stored id that will never work again — and the decision
that needs them apart lives one layer up. Gotcha 46 is this repo's record of
exactly that: the runner knew the spend cap was terminal and the layer below
had already thrown the string away.

WHAT IT COST (measured, Sentry PYTHON-FASTAPI-3N, 2026-09-10 19:47 UTC)
----------------------------------------------------------------------
A real browser opened the billing page and Stripe answered::

    "No such customer: 'cus_VC5K…'; a similar object exists in test mode,
     but a live mode key was used to make this request."

A ``stripe_customer_id`` minted under a TEST key, read back under a LIVE one.
The id is a perfectly ordinary non-empty string, so every ``if not
customer_id`` guard in the product reads it as fine, and the one branch that
could recover — checkout's ``create_customer`` for a company that has none —
never fires. What the customer got instead:

    /upcoming-invoice   panel silently shows no figure
    /checkout           502 "Could not start checkout"
    /portal             502 "Could not open the billing portal"

Both 502s read as *Stripe is down, try later*. They never clear, and retrying
fails identically forever — the founder's "looks like there was no fallback"
shape (gotchas 102/106: an instruction that cannot work is worse than none),
landing on somebody in the act of paying us.

THE ASYMMETRY, WHICH IS THE WHOLE DESIGN
----------------------------------------
``customer_is_gone`` answers only the narrow question *did Stripe positively
tell us the customer id we sent names nothing*. It recognises one shape and
declines on everything else, because the two errors cost opposite things:

  * A MISSED classification keeps today's behaviour exactly — a 502 nobody
    can act on. Bad, and no worse than yesterday.
  * A FALSE classification mints a SECOND Stripe customer for a company that
    already has a working one, which is the duplicate-customer harm gotcha 81
    records: the webhook's customer lookup then resolves nothing.

So an unrecognised error, a timeout, a 500 and a connection reset all answer
``False`` and change nothing. Only a recognised one can move the product.

⚠ THE OBVIOUS PREDICATE IS WRONG AND THE CLIENT'S OWN DOCSTRING SAYS SO.
``get_upcoming_invoice`` records that Stripe answers **404** for *"the
ordinary case of a customer with no live subscription"*. Keying on the status
code would therefore classify every trial customer who has not subscribed as
having a dead customer id — and hand each of them a duplicate Stripe customer
the moment they clicked Subscribe. The pair below is what separates them:
that ordinary case carries a different ``code`` and does not name ``customer``
as the offending parameter.

⚠ stripe.com is EGRESS-BLOCKED from this container (gotcha 75), so this is
classified from the VERBATIM body of a real production event rather than from
a live probe. The pair is structured and documented; the message text is not
matched, because wording is the least stable part of any provider's error and
a wording change must cost recovery, never safety.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Stripe's code for "the id you sent names no such object". Documented at
#: https://stripe.com/docs/error-codes/resource-missing and carried verbatim
#: by the production event this module was written from.
_MISSING = "resource_missing"

#: The request parameter Stripe names when the CUSTOMER is the thing missing.
#: Required alongside the code: the same code appears for a missing price, a
#: missing subscription and a missing payment method, and none of those mean
#: the stored customer id is stale.
_CUSTOMER_PARAM = "customer"


@dataclass
class StripeFailure:
    """A per-call sink the caller creates and reads after the call.

    ⚠ Deliberately NOT state on ``StripeClient``. The routes happen to build a
    fresh client per request today, so instance state would be safe *by
    accident* — and "safe because every caller currently constructs it
    per-request" is the kind of implicit coupling that breaks silently the
    first time somebody shares one. A sink passed in and read back cannot be
    wrong under any construction or concurrency.

    ⚠ It also keeps every public method's return type at ``dict | None``, so
    no existing call site or mock changes. A signature churn across three
    routes and four test files, to carry one boolean, would have been its own
    risk.
    """

    customer_gone: bool = False


def customer_is_gone(status_code: int, body: str) -> bool:
    """Did Stripe positively say the customer id we sent names nothing?

    ``True`` ONLY for the recognised shape. Everything else — a timeout, a
    500, an unparseable body, a different ``code``, a different ``param`` —
    answers ``False`` and leaves the caller doing exactly what it does today.
    """
    if status_code < 400:
        return False
    try:
        error = (json.loads(body) or {}).get("error")
    except (ValueError, TypeError):
        # Not JSON at all — a proxy error page, an empty body, a truncated
        # read. We know nothing, so we claim nothing.
        return False
    if not isinstance(error, dict):
        return False
    return _is_missing_customer(error)


def _is_missing_customer(error: dict[str, Any]) -> bool:
    """Both signals, never either alone.

    ``code`` alone is wrong — a missing PRICE is also ``resource_missing``,
    and re-minting the customer would not fix it while destroying a working
    linkage. ``param`` alone is wrong too: plenty of customer-parameter errors
    are about something other than the object's existence.
    """
    return error.get("code") == _MISSING and error.get("param") == _CUSTOMER_PARAM
