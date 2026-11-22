"""The customer paid, the webhook never arrived, and nothing would ever notice.

WHAT BREAKS
-----------
Every fact this product holds about a subscription arrives by webhook. If one
is lost — a deploy mid-delivery, a signature rejection, an endpoint 500, a
Stripe outage — the row keeps whatever it had and no code path ever asks
again. Two directions, both silent:

* **A lost ``subscription.created``.** ``POST /billing/checkout`` writes the
  ``stripe_customer_id`` synchronously, so the row exists and is correct about
  who they are; ``stripe_subscription_id``, ``plan`` and ``period_end`` all
  arrive later, by webhook. Lose it and a paying customer sits on the trial
  allowance and is refused with ``turns_exhausted`` while their card is
  charged every month.
* **A lost ``subscription.deleted``.** They cancelled; we keep serving the paid
  plan indefinitely, for free.

⚠ **``seats_cycle`` cannot cover this, and its own docstring is why.** It
sweeps "only companies with a subscription", deriving the population from the
thing it repairs — correct for seat quantities and exactly wrong here, because
a company missing its subscription id is excluded by that rule. That is
gotcha 62's finding restated: *a sweep whose population query silently
excludes the companies that need it.* So this is a different population, not a
line added to that one.

WHAT IT DOES
------------
Reads Stripe for companies we have a customer id for, and applies what comes
back through **the webhook's own handler**. It never writes to Stripe.

⚠ **``_apply_event`` is reused rather than reimplemented**, and that is the
load-bearing decision. Deciding what a subscription object means for our row —
which plan the price maps to, what a ``past_due`` status does to the dunning
clock, whether to learn the customer id, how prorations must not roll the
metering window — is about eighty lines of hard-won reasoning in
``api/routes/billing.py``, most of it annotated with the incident that put it
there. A second copy here would drift, and the direction it drifts in is
somebody's plan and somebody's allowance (gotcha 25's rule, pointed at money).

⚠ It is imported INSIDE the function. ``api/routes/billing.py`` imports this
package, so a module-level import is a cycle. The AST guard in
``test_subscription_reconcile.py`` pins that the call is to that exact
function, because a local import is easy to quietly replace with a local copy.

⚠ **Every uncertain answer changes NOTHING.** No Stripe key, a failed list
call, an empty result, an unparseable object: all of them leave the row as it
is. An empty answer is *no evidence*, not evidence of cancellation — reading
it the other way turns one Stripe outage into a fleet-wide downgrade, which is
strictly worse than the lost webhook this exists to repair (gotcha 55's
decline-to-assert, and the same asymmetry ``billing/gate.py`` rule 1 takes).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from helixis.billing.stripe_client import StripeClient
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: Companies to examine per sweep. A ceiling rather than a product limit: this
#: makes one Stripe call per company, and a fleet-wide sweep on a bad morning
#: should cost a bounded number of them. The ordering below makes the cap
#: fair rather than arbitrary.
MAX_COMPANIES_PER_SWEEP = 50

#: The population, and the reason it is not "everyone".
#:
#: ⚠ A company with NO customer id has never reached Stripe, so there is
#: nothing to ask about and a lookup could only cost a call. A company WITH one
#: is a company Stripe can answer for — including one that opened checkout and
#: abandoned it, which is why the Stripe read is the arbiter and not our guess
#: about what the missing subscription id means.
#:
#: ⚠ Ordered by the STALEST row first, so a fleet larger than the cap is swept
#: round-robin instead of the same fifty companies forever. NULLS FIRST puts a
#: row that has never seen a Stripe event — the exact lost-webhook case — at
#: the front.
_POPULATION_SQL = """
    select company_id::text as company_id,
           stripe_customer_id,
           stripe_subscription_id,
           status,
           plan
      from public.helixis_billing
     where coalesce(stripe_customer_id, '') <> ''
     order by last_stripe_event_at asc nulls first
     limit $1
"""


async def run_subscription_reconcile_cycle(pool: asyncpg.Pool, settings: Settings) -> int:
    """Re-read Stripe for companies we may be out of date about. Returns the
    number of companies whose row was repaired.

    Never raises — the worker's other cycles must not die because a billing
    repair did.
    """
    key = settings.billing.stripe_secret_key
    if not key:
        # No keys, nothing to reconcile against. Debug, not warning: this is
        # the normal state of a deployment that has not switched billing on.
        logger.debug("subscription reconcile skipped — no Stripe key")
        return 0

    try:
        rows = await pool.fetch(_POPULATION_SQL, MAX_COMPANIES_PER_SWEEP)
    except Exception:
        logger.exception("subscription reconcile: could not read the population")
        return 0

    client = StripeClient(key)
    repaired = 0
    for row in rows:
        try:
            if await _reconcile_one(
                settings,
                client,
                company_id=row["company_id"],
                customer_id=row["stripe_customer_id"],
                known=(row["stripe_subscription_id"], row["status"], row["plan"]),
            ):
                repaired += 1
        except Exception:
            logger.exception(
                "subscription reconcile failed for company=%s — continuing",
                row["company_id"],
            )
    if repaired:
        logger.warning(
            "subscription reconcile repaired %d of %d companies — a webhook was "
            "lost for each of them",
            repaired,
            len(rows),
        )
    return repaired


async def _reconcile_one(
    settings: Settings,
    client: StripeClient,
    *,
    company_id: str,
    customer_id: str,
    known: tuple[str | None, str | None, str | None],
) -> bool:
    """Repair one company from Stripe. True when the row was actually written.

    ⚠ Returns False for "already correct" AND for "could not tell", and the
    caller only counts and logs, so conflating them costs nothing here. What
    would cost something is acting on "could not tell" — and no branch does.
    """
    subs = await client.list_subscriptions(customer_id)
    sub = _current_subscription(subs)
    if sub is None or not sub.get("id"):
        # NO EVIDENCE — the one branch this module's safety rests on. An empty
        # list means "Stripe has none" AND "the call failed", indistinguishable
        # by design, so it can never be a reason to change a row. It is also
        # the abandoned-checkout shape: a customer id with no subscription is a
        # normal, permanent state rather than a defect.
        #
        # ⚠ There was an `if not subs: return False` above this, and it could
        # not change any output — `_current_subscription([])` already returns
        # None, so both paths ended here for every input. Deleted per gotcha
        # 58: a branch that cannot alter behaviour is dead code wearing a
        # safety check's costume, and pinning it teaches the next reader that
        # a distinction exists where none does.
        return False

    from helixis.api.routes.billing import _apply_event, _plan_for_price

    if not _drifted(sub, known, settings=settings, plan_for_price=_plan_for_price):
        return False

    # ⚠ The company id must be OURS, not the object's. Stripe's `metadata` is
    # written at checkout and a subscription created any other way (the Stripe
    # dashboard, an import, a support action) carries none — and `_apply_event`
    # matches on `company_id OR stripe_customer_id`, so passing None would
    # narrow the repair to exactly the rows least likely to need it.
    obj = {**sub, "metadata": {**(sub.get("metadata") or {}), "company_id": company_id}}

    # ⚠ Stamped with the moment we READ it, not with the subscription's own
    # `created` — that is when the customer first subscribed, which for a
    # year-old account is far older than every event already applied, and the
    # ordering guard would correctly refuse it. A live read is by construction
    # the newest view anyone has, so "now" is the true statement about it, and
    # a webhook arriving afterwards still carries a later stamp and wins.
    await _apply_event(settings, "customer.subscription.updated", obj, created=time.time())
    logger.warning(
        "subscription reconcile: repaired company=%s from Stripe (subscription=%s "
        "status=%s) — the webhook for this never arrived",
        company_id,
        sub.get("id"),
        sub.get("status"),
    )
    return True


def _current_subscription(subs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The subscription that decides what this company is entitled to.

    ⚠ A customer can hold several — a cancelled one plus its replacement is
    the ordinary shape after a re-subscribe. Taking ``subs[0]`` would let list
    ordering decide somebody's plan, so a SERVING status wins outright and
    only the leftovers fall back to Stripe's order.
    """
    serving = [s for s in subs if str(s.get("status") or "") in _SERVING]
    if serving:
        return serving[0]
    return subs[0] if subs else None


#: Stripe statuses that mean the customer is entitled to their plan right now.
#: ⚠ ``past_due`` is HERE deliberately: it is the dunning window, during which
#: `gate.py` keeps serving on purpose. Treating it as not-serving would let a
#: reconcile pick a stale cancelled subscription over the live delinquent one
#: and cut off a customer whose card is merely late.
_SERVING = frozenset({"active", "trialing", "past_due"})


def _drifted(
    sub: dict[str, Any],
    known: tuple[str | None, str | None, str | None],
    *,
    settings: Settings,
    plan_for_price: Any,
) -> bool:
    """Does Stripe disagree with the row about anything that changes service?

    Three fields, and each one is a lost webhook with its own consequence:

    * **subscription id** — the lost ``created``. A paying customer on the
      trial allowance.
    * **status** — the lost ``deleted`` or the lost ``past_due``. Serving a
      plan nobody is paying for, or a dunning clock that never starts. ⚠ The
      id alone cannot see this: a cancellation keeps the SAME id, so an
      id-only check would leave exactly half this module's reason to exist
      structurally unreachable.
    * **plan** — the lost upgrade or downgrade. Same id, same ``active``
      status, a different price, and the customer is charged for Scale on
      Starter's allowance (or the reverse).

    ⚠ **The plan is derived by the WEBHOOK'S OWN mapper**, passed in rather
    than re-implemented. A second price→plan table here would drift from the
    one that runs on every real event, and the direction it drifts in is what
    somebody is charged (gotcha 25). Everything deeper than these three —
    seats, the metering window, the dunning timestamp — is deliberately NOT
    compared: ``_apply_event`` owns those, and re-deriving them to decide
    whether to call it is the duplicate-rule problem in miniature. Missing one
    costs a sweep's delay; disagreeing about one costs a wrong row.
    """
    known_id, known_status, known_plan = known
    if str(sub.get("id") or "") != (known_id or ""):
        return True
    if str(sub.get("status") or "") != (known_status or ""):
        return True

    items = (sub.get("items") or {}).get("data") or []
    price_id = ((items[0].get("price") or {}).get("id")) if items else None
    # ⚠ A price we do not recognise yields None, and `_apply_event` COALESCEs
    # that into "leave the plan alone" — so reporting drift on it would mean
    # re-applying forever on every sweep for a subscription nothing can
    # change. No price, no plan opinion, no drift.
    plan = plan_for_price(settings, price_id) if price_id else None
    return bool(plan) and plan != known_plan


__all__ = [
    "MAX_COMPANIES_PER_SWEEP",
    "run_subscription_reconcile_cycle",
]
