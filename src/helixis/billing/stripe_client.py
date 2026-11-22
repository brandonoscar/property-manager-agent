"""Thin Stripe REST client — customers, Checkout, and the billing portal.

Direct httpx rather than the ``stripe`` SDK, matching
``comms/twilio_signature.py`` and ``memory/voyage_embedder.py``: four calls do
not justify a dependency, and gotcha 79 is this repo's record of an SDK's
Python identifiers being mistaken for wire paths.

⚠ stripe.com is EGRESS-BLOCKED from the dev container (gotcha 75), so every
path and parameter here comes from Stripe's documentation, NOT from a live
call. Confirm on the first real run. The endpoints used:

    POST /v1/customers
    POST /v1/checkout/sessions
    POST /v1/billing_portal/sessions
    GET  /v1/subscriptions/{id}

⚠ Stripe's API is FORM-ENCODED, not JSON, and nested values use bracket keys
(``line_items[0][price]``). Posting JSON returns a 400 that reads like a
parameter error rather than an encoding one.

Nothing here raises into a request handler: every call returns ``None`` on
failure and logs. A billing lookup that explodes must not take down the page
that shows it.

⚠ THAT RULE COLLAPSES TWO FACTS INTO ONE VALUE, and the ``failure`` sink is
how a caller gets them back. A timeout and *"No such customer: cus_…"* both
return ``None``, so a stored id that will never work again is indistinguishable
from a blip — and the product answered a 502 that reads as *try later* to
somebody in the act of paying us (Sentry PYTHON-FASTAPI-3N). Pass a
``StripeFailure`` to any call whose caller must tell those apart and read
``customer_gone`` after; ``billing/stripe_errors.py`` holds the classifier and
the reasoning for why it recognises exactly one shape.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from helixis.billing.stripe_errors import StripeFailure, customer_is_gone

logger = logging.getLogger(__name__)

_API = "https://api.stripe.com/v1"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class StripeClient:
    def __init__(self, secret_key: str) -> None:
        self._key = secret_key

    async def _post(
        self,
        path: str,
        form: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        failure: StripeFailure | None = None,
    ) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key:
            # ⚠ Not optional on anything that creates a billable object. A
            # retried checkout without this makes a SECOND subscription, and
            # the customer is charged twice for one click.
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(f"{_API}{path}", data=form, headers=headers)
            if resp.status_code >= 400:
                # Stripe's own words, which name the wrong field precisely —
                # far more useful in a log than "stripe call failed".
                logger.error("stripe POST %s -> %s: %s", path, resp.status_code, resp.text[:500])
                # ⚠ Classified from the FULL body, never the truncated text
                # the log line carries. `[:500]` is a display budget; cutting
                # the JSON mid-object makes it unparseable and the classifier
                # would decline on a shape it should recognise.
                if failure is not None and customer_is_gone(resp.status_code, resp.text):
                    failure.customer_gone = True
                return None
            return dict(resp.json())
        except Exception:
            logger.exception("stripe POST %s failed", path)
            return None

    async def _get(
        self, path: str, *, failure: StripeFailure | None = None
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{_API}{path}", headers={"Authorization": f"Bearer {self._key}"}
                )
            if resp.status_code >= 400:
                logger.error("stripe GET %s -> %s: %s", path, resp.status_code, resp.text[:500])
                if failure is not None and customer_is_gone(resp.status_code, resp.text):
                    failure.customer_gone = True
                return None
            return dict(resp.json())
        except Exception:
            logger.exception("stripe GET %s failed", path)
            return None

    async def create_customer(
        self, *, company_id: str, name: str, email: str
    ) -> dict[str, Any] | None:
        return await self._post(
            "/customers",
            {
                "name": name,
                "email": email,
                # The company id rides on the customer so a webhook carrying
                # only Stripe ids can still be resolved back to a tenant. The
                # DB index is the primary route; this is the fallback for an
                # event that arrives before the row is written.
                "metadata[company_id]": company_id,
            },
            idempotency_key=f"customer:{company_id}",
        )

    async def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        quantity: int,
        success_url: str,
        cancel_url: str,
        company_id: str,
        failure: StripeFailure | None = None,
    ) -> dict[str, Any] | None:
        return await self._post(
            "/checkout/sessions",
            {
                "mode": "subscription",
                "customer": customer_id,
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": str(quantity),
                "success_url": success_url,
                "cancel_url": cancel_url,
                "client_reference_id": company_id,
                "subscription_data[metadata][company_id]": company_id,
                # A discount has to be enterable or it does not exist. Stripe
                # supports promotion codes and Checkout hides the field unless
                # this is set, so running a launch offer was a code change and
                # a deploy — which is why there had never been one.
                #
                # ⚠ It only accepts codes that already exist in the Stripe
                # dashboard, so this grants nothing by itself; it moves
                # "can we discount someone" from an engineering task to a
                # dashboard one. And it is mutually exclusive with sending
                # `discounts[…]` on the same session — do not add both, or
                # Stripe 400s the checkout a customer is standing in front of.
                "allow_promotion_codes": "true",
                # ⚠ Stripe's own trial is NOT used. Ours starts at signup with
                # no card and no Stripe object; a customer reaching Checkout
                # has already had it. Setting trial_period_days here would
                # hand a second free fortnight to somebody who just chose to
                # pay.
            },
            # Deliberately NOT keyed on company alone: a customer who cancels
            # and later resubscribes must be able to check out again. The
            # price and seat count make each distinct intent its own key.
            idempotency_key=f"checkout:{company_id}:{price_id}:{quantity}",
            failure=failure,
        )

    async def create_portal_session(
        self, *, customer_id: str, return_url: str, failure: StripeFailure | None = None
    ) -> dict[str, Any] | None:
        # No idempotency key: a portal session is a short-lived link with no
        # billable effect, and reusing one across clicks would hand back an
        # expired URL.
        return await self._post(
            "/billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
            failure=failure,
        )

    async def get_upcoming_invoice(self, customer_id: str) -> dict[str, Any] | None:
        """What this customer will actually be billed next, from Stripe.

        ⚠ The reason this exists rather than multiplying price by seats: that
        arithmetic knows nothing about DISCOUNTS or PRORATION. The panel has
        been stating "Next charge $597 on 04/10/2026" from list price × seats,
        which was true only while nobody had a coupon — and enabling promotion
        codes on Checkout is what made it possible to be wrong. A confident
        wrong number about somebody's money is the failure this closes.

        ⚠ Read-only and it creates nothing, despite the path. Stripe's
        "upcoming" invoice is a projection it computes on request; there is no
        object to be idempotent about.

        Returns ``None`` like every other call here — including the ordinary
        case of a customer with no live subscription, where Stripe answers 404.
        A caller must treat that as "cannot say" and never as "nothing is
        owed".
        """
        return await self._get(f"/invoices/upcoming?customer={customer_id}")

    async def get_default_card_expiry(self, customer_id: str) -> tuple[int, int] | None:
        """``(exp_month, exp_year)`` of the card that will be charged next.

        A card expiring mid-cycle is the failure nobody is warned about. The
        renewal declines, `invoice.payment_failed` lands, the row goes
        `past_due`, and `notices.py` finally says something — AFTER the money
        did not move and with the grace clock already running. Stripe knows the
        expiry the whole time; nothing here had ever read it.

        ⚠ **Read on the UPCOMING-INVOICE endpoint, never on `/billing/status`.**
        That one is fetched by `TrialBanner` on every page, so a Stripe round
        trip there puts Stripe's latency and Stripe's outages in front of every
        page load — `get_upcoming_invoice`'s own docstring, and gotcha 37's
        rule about a check placed on the wrong path.

        ⚠ **Two shapes, and the fallback is not defensive clutter.** `expand`
        asks Stripe to inline the payment method, and an unexpanded response
        returns the same field as a bare id STRING. stripe.com is
        egress-blocked from this container (gotcha 75), so this is documented
        rather than probed — and an `expand` that silently does nothing would
        otherwise read as "this customer has no card", which is a false
        absence about somebody's billing (gotchas 26/55). So a string is
        FETCHED rather than assumed empty.

        Returns ``None`` for every uncertainty: no customer, no default method,
        a method that is not a card, an unreadable expiry. The caller must
        treat that as "cannot say" and warn nobody.
        """
        body = await self._get(
            f"/customers/{customer_id}?expand[]=invoice_settings.default_payment_method"
        )
        method = ((body or {}).get("invoice_settings") or {}).get("default_payment_method")
        if isinstance(method, str) and method:
            method = await self._get(f"/payment_methods/{method}")
        if not isinstance(method, dict):
            return None
        card = method.get("card")
        if not isinstance(card, dict):
            # A bank debit or a wallet has no expiry to warn about. Not an
            # error, and not something to guess a date for.
            return None
        month, year = card.get("exp_month"), card.get("exp_year")
        if not isinstance(month, int) or not isinstance(year, int):
            return None
        if not (1 <= month <= 12) or year < 2000:
            logger.warning("stripe: implausible card expiry %s/%s — ignoring", month, year)
            return None
        return month, year

    async def get_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        return await self._get(f"/subscriptions/{subscription_id}")

    async def list_subscriptions(self, customer_id: str) -> list[dict[str, Any]]:
        """Every subscription Stripe holds for this customer, newest first.

        Read-only, and the reconciler's whole basis: when a webhook is lost,
        Stripe is the only party that still knows what the customer bought.

        ⚠ ``status=all`` on purpose. A cancelled subscription is exactly as
        load-bearing as an active one — a lost ``subscription.deleted`` leaves
        somebody on a paid plan they stopped paying for, and asking only for
        active ones would make that half of the repair structurally
        impossible. The caller decides what a status means; this asks Stripe
        what exists.

        ⚠ Returns ``[]`` for "none" AND for "the call failed", which is safe
        only because the caller treats an empty answer as *no evidence* and
        changes nothing. A reconciler that read empty as "cancel them" would
        turn one Stripe outage into a fleet-wide downgrade.
        """
        body = await self._get(f"/subscriptions?customer={customer_id}&status=all&limit=10")
        data = (body or {}).get("data")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    async def update_subscription_quantity(
        self, subscription_id: str, *, quantity: int
    ) -> dict[str, Any] | None:
        """Set the subscription's seat count, prorated by Stripe.

        Reads the subscription first because the write is keyed on the
        SUBSCRIPTION ITEM id (``si_...``), which only the subscription carries;
        there is no "set quantity by subscription id" call.

        ⚠ NO IDEMPOTENCY KEY, deliberately — and this is the opposite of the
        rule on ``create_checkout_session``. A key makes a CREATE safe to
        retry; here the operation is declarative (setting quantity to N twice
        leaves N), and a key would actively break it: a company going
        3 → 4 → 3 → 4 would replay the first "4" response on the second, and
        Stripe would never receive the change. A stale key silently reverting a
        seat change is worse than a duplicate no-op write.

        ⚠ Returns ``None`` on failure like everything else here, so a caller
        must decide what an unsynced seat count means. It must never take down
        the membership change that triggered it — see ``billing/seats.py``.
        """
        sub = await self.get_subscription(subscription_id)
        if sub is None:
            return None
        items = (sub.get("items") or {}).get("data") or []
        if not items:
            logger.error("stripe: subscription %s has no items to re-quantity", subscription_id)
            return None
        item_id = items[0].get("id")
        current = int(items[0].get("quantity") or 1)
        if current == quantity:
            # Already right. Skipping is not just an optimisation: every
            # quantity write creates a proration line, so a redundant one
            # leaves a $0 entry on the customer's next invoice for no reason.
            return sub
        return await self._post(
            f"/subscriptions/{subscription_id}",
            {
                "items[0][id]": str(item_id),
                "items[0][quantity]": str(quantity),
                # Stripe's default, stated rather than assumed: adding someone
                # mid-cycle charges the remainder of the period, removing
                # someone credits it. That is the behaviour the seat model
                # promises, so it should not depend on an account-level setting
                # that could be changed in the dashboard.
                "proration_behavior": "create_prorations",
            },
        )

    async def switch_subscription_price(
        self, subscription_id: str, *, price_id: str, quantity: int
    ) -> dict[str, Any] | None:
        """Move an EXISTING subscription onto a different plan's price.

        ⚠ THIS IS WHAT "CHANGE PLAN" HAS TO DO, and its absence was a
        double-charge. The billing panel labels itself "Change plan" for anyone
        already paying and offers a button on every other plan — and every one
        of those buttons opened a fresh Checkout session. Stripe would happily
        create a SECOND subscription: the customer pays for both, and the
        `customer.subscription.created` webhook then overwrites our
        `stripe_subscription_id` with the new one, so the ORIGINAL becomes
        invisible to this product and bills forever with nothing in the app
        that can see it, let alone cancel it.

        Same read-then-write shape as `update_subscription_quantity`: the write
        is keyed on the subscription ITEM id, which only the subscription
        carries. Sending `items[0][price]` REPLACES the price on that item —
        appending a second item instead would leave the customer paying for
        both plans at once, which is the bug in a different costume.

        ⚠ `quantity` is passed too, and must be. Plans differ in whether they
        are per-seat: moving Pro (per-seat, quantity = headcount) to Scale
        (flat, quantity 1) without resetting it bills five Scales. The caller
        computes it with `billable_seats` for the TARGET plan.

        ⚠ NO IDEMPOTENCY KEY, for `update_subscription_quantity`'s reason: this
        is declarative, and a key would replay a stale response on a
        Pro → Scale → Pro sequence and silently strand the customer on Scale.
        """
        sub = await self.get_subscription(subscription_id)
        if sub is None:
            return None
        items = (sub.get("items") or {}).get("data") or []
        if not items:
            logger.error("stripe: subscription %s has no items to re-price", subscription_id)
            return None
        item = items[0]
        item_id = item.get("id")
        current_price = (item.get("price") or {}).get("id")
        current_quantity = int(item.get("quantity") or 1)
        if current_price == price_id and current_quantity == quantity:
            # Already there. Skipping matters for the same reason as above: a
            # redundant write still creates a proration line, so re-confirming
            # a plan somebody is already on would put a $0 entry on their next
            # invoice and read as a change they did not make.
            return sub
        return await self._post(
            f"/subscriptions/{subscription_id}",
            {
                "items[0][id]": str(item_id),
                "items[0][price]": price_id,
                "items[0][quantity]": str(quantity),
                "proration_behavior": "create_prorations",
            },
        )
