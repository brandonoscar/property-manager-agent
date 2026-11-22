"""Changing plan re-prices the subscription you have; it never buys a second.

⚠ THE BUG THIS FILE EXISTS TO CLOSE, and it was one click from a customer.

`BillingPanel` reads "Change plan" for anybody already paying and renders a
button on every plan that is not their current one. Every one of those buttons
called `POST /billing/checkout`, which CREATES — it has no notion of an existing
subscription and Stripe is perfectly happy to give one customer two. So a
Starter customer choosing Pro would have been charged $50 AND $199, and the
resulting `customer.subscription.created` webhook overwrites
`helixis_billing.stripe_subscription_id` with the new subscription — after which
the ORIGINAL is invisible to this product, bills forever, and nothing in the app
can see it, let alone cancel it. The customer's only route to it is the Stripe
portal, which they would have to know to look in.

⚠ EVERY CHECKOUT TEST THAT EXISTED WAS A SOURCE SCAN. `test_billing_plans` and
`test_billing_trial_provisioning` read the route's TEXT — that the price comes
from a lookup, that the upsert is an upsert. All correct, all structurally
unable to see that the route was reachable in a state it must refuse. These
tests call it.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
USER_ID = "00000000-0000-0000-0000-000000000001"
COMPANY_ID = USER_ID


def _token() -> str:
    return jwt.encode(
        {
            "sub": USER_ID,
            "email": "admin@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def settings() -> Settings:
    """Billing CONFIGURED, with distinct fake price ids per plan.

    ⚠ Distinct on purpose. Reusing one id across two plans would let a
    price→plan map that returns the first match pass while mis-attributing the
    second — the shape of the Scale bug this same change fixed.
    """
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        billing={
            "stripe_secret_key": "sk_test_x",
            "stripe_webhook_secret": "whsec_x",
            "price_starter": "price_fake_starter",
            "price_pro": "price_fake_pro",
            "price_scale": "price_fake_scale",
            "portal_return_url": "https://app.example.com",
        },
    )


def _pool(
    *,
    subscription: str | None,
    members: int = 4,
    role: str = "admin",
    status: str = "active",
):
    """Fake asyncpg pool answering the three reads these routes make.

    ⚠ `status` DEFAULTS TO A LIVE SUBSCRIPTION because that is what every test
    below already meant — they exercise the "one already exists" path. Stating
    it is not decoration: both routes now read the column, and a stored id is
    not the same fact as a subscription that can still bill. A fixture that
    left it unsaid would answer the new question by accident (gotchas 72/73).
    """
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(sql: str, *args: Any):
        low = sql.lower()
        if "from company_users" in low:
            return {"role": role}
        if "count(*)" in low:
            return {"n": members}
        if "stripe_subscription_id" in low or "stripe_customer_id" in low:
            return {
                "stripe_customer_id": "cus_abc",
                "stripe_subscription_id": subscription,
                "status": status,
                "name": "Acme Property Co",
            }
        return None

    async def execute(sql: str, *args: Any) -> str:
        captured.append((sql, args))
        return "UPDATE 1"

    pool = MagicMock()
    pool.fetchrow = fetchrow
    pool.execute = execute

    @asynccontextmanager
    async def _acquire():
        yield pool

    pool.acquire = _acquire
    return pool, captured


def _client(settings: Settings, pool: Any, stripe: Any) -> TestClient:
    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    patches = [
        patch("helixis.api.routes.billing.get_pool", return_value=pool),
        patch("helixis.api.routes.billing._settings", return_value=settings),
        patch("helixis.api.routes.billing.StripeClient", return_value=stripe),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ]
    for p in patches:
        p.start()
    client = TestClient(create_app(settings))
    original = client.close

    def close() -> None:
        for p in reversed(patches):
            p.stop()
        original()

    client.close = close  # type: ignore[method-assign]
    return client


def _stripe() -> Any:
    s = MagicMock()
    s.switch_subscription_price = AsyncMock(return_value={"id": "sub_1"})
    s.create_checkout_session = AsyncMock(return_value={"url": "https://checkout.example/x"})
    s.create_customer = AsyncMock(return_value={"id": "cus_abc"})
    s.create_portal_session = AsyncMock(return_value={"url": "https://portal.example/x"})
    return s


AUTH = {"Authorization": f"Bearer {_token()}"}


# ── checkout must refuse a second subscription ───────────────────────────


def test_checkout_refuses_when_a_subscription_already_exists(settings: Settings) -> None:
    """⚠ THE DOUBLE-CHARGE. Without this the customer pays for both plans and
    the first subscription becomes unreachable from inside the product."""
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 409, (
        f"got {resp.status_code} — checkout started a SECOND subscription for a "
        "customer who already has one."
    )
    assert stripe.create_checkout_session.await_count == 0, (
        "a Checkout session was created anyway — the refusal has to happen "
        "BEFORE Stripe is called, or the customer is already on the hook."
    )


def test_checkout_still_works_for_a_first_purchase(settings: Settings) -> None:
    """The guard must not close the door it exists to keep open. A trialling
    company has no subscription and must reach Checkout."""
    pool, _ = _pool(subscription=None)
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 200, resp.text
    assert stripe.create_checkout_session.await_count == 1


# ── change-plan re-prices the subscription that exists ───────────────────


def test_change_plan_switches_the_price_on_the_existing_subscription(
    settings: Settings,
) -> None:
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"] == "pro"
    stripe.switch_subscription_price.assert_awaited_once()
    kwargs = stripe.switch_subscription_price.await_args.kwargs
    assert stripe.switch_subscription_price.await_args.args[0] == "sub_existing"
    assert kwargs["price_id"] == "price_fake_pro"
    # ⚠ NO new Checkout session. That is the whole distinction between this
    # endpoint and the other one.
    assert stripe.create_checkout_session.await_count == 0


def test_the_quantity_is_computed_for_the_target_plan_not_carried_over(settings: Settings) -> None:
    """⚠ Not carried over from the current subscription.

    Pro is per-seat, so its quantity is the headcount (4 here). Scale is
    per-ACCOUNT and must be quantity 1 — Scale's own pricing highlight promises
    "the allowance is pooled, not charged per person". Reusing the Pro
    subscription's quantity across the switch would bill four Scales, which is
    $2,000/month for a $500 plan.
    """
    pool, _ = _pool(subscription="sub_existing", members=4)
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        pro = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
        scale = client.post("/api/v1/billing/change-plan", json={"plan": "scale"}, headers=AUTH)
    finally:
        client.close()

    assert pro.json()["seats"] == 4, "a per-seat plan must be quantity = headcount"
    assert scale.json()["seats"] == 1, (
        "a flat plan was billed per person — Scale at quantity 4 is $2,000/month for a $500 plan."
    )
    quantities = [c.kwargs["quantity"] for c in stripe.switch_subscription_price.await_args_list]
    assert quantities == [4, 1]


def test_change_plan_refuses_when_there_is_nothing_to_change(settings: Settings) -> None:
    """A trialling company has no subscription to re-price. Say so rather than
    500ing on a null — the panel sends these callers to checkout instead."""
    pool, _ = _pool(subscription=None)
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 409
    assert stripe.switch_subscription_price.await_count == 0


def test_change_plan_does_not_write_our_own_billing_row(settings: Settings) -> None:
    """⚠ ONE WRITER. `customer.subscription.updated` owns plan/status/seats.

    A second writer here would show the customer a plan they are not on the
    moment a Stripe call half-succeeds — and unlike the webhook, this path has
    no way to learn it was wrong. The response reports what we ASKED for; the
    panel re-reads /billing/status for what actually happened.
    """
    pool, captured = _pool(subscription="sub_existing")
    client = _client(settings, pool, _stripe())
    try:
        client.post("/api/v1/billing/change-plan", json={"plan": "scale"}, headers=AUTH)
    finally:
        client.close()

    writes = [sql for sql, _ in captured if "helixis_billing" in sql.lower()]
    assert not writes, f"change-plan wrote to helixis_billing: {writes}"


def test_change_plan_refuses_a_plan_nobody_sells(settings: Settings) -> None:
    """`plan_for` falls back to TRIAL on junk, and the trial is not purchasable
    — so a bogus key must not silently re-price somebody onto nothing."""
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        junk = client.post("/api/v1/billing/change-plan", json={"plan": "enterprise"}, headers=AUTH)
        trial = client.post("/api/v1/billing/change-plan", json={"plan": "trial"}, headers=AUTH)
    finally:
        client.close()

    assert junk.status_code == 422
    assert trial.status_code == 422
    assert stripe.switch_subscription_price.await_count == 0


def test_change_plan_is_admin_only(settings: Settings) -> None:
    """Same floor as checkout: this changes what the company is charged."""
    pool, _ = _pool(subscription="sub_existing", role="manager")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 403
    assert stripe.switch_subscription_price.await_count == 0


def test_a_failed_stripe_call_is_a_502_and_not_a_silent_success(settings: Settings) -> None:
    """⚠ `StripeClient` returns None on failure rather than raising (its module
    docstring: a billing lookup that explodes must not take down the page). So
    the caller MUST check — an unchecked None here would return 200 and tell the
    customer their plan changed when Stripe never heard about it."""
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe()
    stripe.switch_subscription_price = AsyncMock(return_value=None)
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 502


# ── a DEAD subscription is neither a subscription nor a Stripe outage ─────
#
# ⚠ The customer whose FIRST PAYMENT EXPIRED had both exits shut, and the two
# refusals contradicted each other. Checkout said "you already have a
# subscription"; the chat gate said "that subscription was never completed —
# pick a plan again in Settings → Billing", which is the door checkout had just
# closed. Their third route, the panel's "Change plan" button, pushed a
# re-price at a subscription that never charged and came back as "Could not
# change the plan with Stripe" — blaming a vendor that is working fine.
#
# `incomplete_expired` is a real persisting status here, not a hypothetical:
# migration 0097 exists because the CHECK constraint could not store it, and
# `customer.subscription.deleted` — the only handler that nulls the stored id —
# is not what fires for it.


def test_checkout_proceeds_when_the_stored_subscription_is_dead(settings: Settings) -> None:
    """The headline. A stored id is not a subscription that can still bill."""
    pool, _ = _pool(subscription="sub_expired", status="incomplete_expired")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        r = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://checkout.example/x"
    stripe.create_checkout_session.assert_awaited_once()


def test_checkout_still_refuses_a_subscription_stripe_has_not_finished(
    settings: Settings,
) -> None:
    """⚠ THE OTHER DIRECTION, and it is the double-charge.

    `incomplete` is NOT serving — the gate pauses the product mid-3-D-Secure —
    and it is very much still billing: the bank confirms and it becomes
    `active`. A fix written as "not in _SERVING_STATUSES" passes the test above
    and lets this customer buy a second subscription, which is the exact bug
    this file exists to close.
    """
    pool, _ = _pool(subscription="sub_pending", status="incomplete")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        r = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert r.status_code == 409
    stripe.create_checkout_session.assert_not_awaited()


def test_change_plan_refuses_a_dead_subscription_without_blaming_stripe(
    settings: Settings,
) -> None:
    """A 409 naming the real state, not a 502 naming an innocent vendor.

    Stripe is never called: re-pricing a subscription that never charged is a
    request we already know the answer to, and its failure would arrive as
    "Could not change the plan with Stripe".
    """
    pool, _ = _pool(subscription="sub_expired", status="incomplete_expired")
    stripe = _stripe()
    client = _client(settings, pool, stripe)
    try:
        r = client.post("/api/v1/billing/change-plan", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert r.status_code == 409
    stripe.switch_subscription_price.assert_not_awaited()
    # It has to point at the door that IS open — the gate's own message for
    # this customer says "pick a plan again", so this one must not contradict it.
    assert "pick a plan" in r.json()["detail"].lower()
