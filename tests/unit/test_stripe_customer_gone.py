"""A stored Stripe customer id Stripe has never heard of must not brick billing.

WHAT HAPPENED (Sentry PYTHON-FASTAPI-3N, 2026-09-10 19:47 UTC, production)
-------------------------------------------------------------------------
A real browser opened the billing page. Stripe answered, verbatim::

    "No such customer: 'cus_VC5K…'; a similar object exists in test mode, but
     a live mode key was used to make this request."

A ``stripe_customer_id`` minted under a TEST key, read back under a LIVE one.
The id is an ordinary non-empty string, so every ``if not customer_id`` guard
in the product reads it as healthy — including checkout's own
``create_customer`` branch, the one piece of code that already knows how to
recover. It could never fire for the company that needed it.

What the customer got: a silently blank invoice figure, and 502s reading
*"Could not start checkout"* / *"Could not open the billing portal"* — which
say **Stripe is down, try later** about a state that never clears. Retrying
fails identically forever, on the one page where somebody is trying to pay us.

THE PROPERTY THESE TESTS HOLD, AND ITS ASYMMETRY
-----------------------------------------------
Recovery fires ONLY on positive evidence that the id names nothing, because
the two mistakes cost opposite things:

  * a MISSED classification leaves exactly today's 502 — bad, no worse;
  * a FALSE one mints a SECOND Stripe customer for a company that already has
    a working one, which is gotcha 81's duplicate-customer harm.

So the negative tests here are the load-bearing ones. In particular
``get_upcoming_invoice``'s own docstring records that Stripe answers **404**
for *"the ordinary case of a customer with no live subscription"* — keying on
the status code would have classified every trial customer as dead and handed
each a duplicate customer the moment they clicked Subscribe. That is the
regression this file exists to make unshippable.

⚠ stripe.com is EGRESS-BLOCKED here (gotcha 75). The recognised shape is the
VERBATIM body of the production event; the shapes it must decline are
reasoned from Stripe's documented error codes, not probed. Both directions of
a wrong guess fail safe (no recovery), never loud.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.billing.stripe_client import StripeClient
from helixis.billing.stripe_errors import StripeFailure, customer_is_gone
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

# The production event, byte for byte. Anything this file claims about the
# recognised shape is claimed about THIS.
PRODUCTION_BODY = json.dumps(
    {
        "error": {
            "code": "resource_missing",
            "doc_url": "https://stripe.com/docs/error-codes/resource-missing",
            "message": (
                "No such customer: 'cus_VC5KxIZFEO5u8b'; a similar object exists in "
                "test mode, but a live mode key was used to make this request."
            ),
            "param": "customer",
            "type": "invalid_request_error",
        }
    }
)

# Stripe's answer for a healthy customer who simply has nothing upcoming. Also
# a 4xx, also about a customer — and it must NOT trigger recovery.
NO_UPCOMING_BODY = json.dumps(
    {
        "error": {
            "code": "invoice_upcoming_none",
            "message": "No upcoming invoices for customer: cus_healthy",
            "type": "invalid_request_error",
        }
    }
)


# ── 1. the classifier ─────────────────────────────────────────────────────


def test_it_recognises_the_production_body() -> None:
    assert customer_is_gone(400, PRODUCTION_BODY) is True


def test_a_customer_with_nothing_upcoming_is_not_gone() -> None:
    """THE REGRESSION THIS FILE EXISTS FOR.

    A 404 here is ordinary — the client's own docstring says so. Classifying
    it as a dead id would mint a duplicate Stripe customer for every trialling
    company at their first Subscribe click.
    """
    assert customer_is_gone(404, NO_UPCOMING_BODY) is False


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # Same code, different object. Re-minting the customer would not fix a
        # missing price and would destroy a working linkage on the way.
        (
            "a missing price",
            json.dumps({"error": {"code": "resource_missing", "param": "price"}}),
        ),
        # Same param, different failure. Plenty of customer-parameter errors
        # are about something other than the object's existence.
        (
            "a customer parameter that is invalid rather than absent",
            json.dumps({"error": {"code": "parameter_invalid_string", "param": "customer"}}),
        ),
        ("an error that is not an object", json.dumps({"error": "boom"})),
        ("no error key at all", json.dumps({"ok": False})),
        ("a proxy error page", "<html>502 Bad Gateway</html>"),
        ("an empty body", ""),
    ],
)
def test_it_declines_on_everything_it_does_not_recognise(label: str, body: str) -> None:
    assert customer_is_gone(400, body) is False, (
        f"{label} was classified as a dead customer id — that mints a duplicate "
        "Stripe customer for a company that already has a working one"
    )


def test_a_success_is_never_a_missing_customer() -> None:
    """Guards the ordering: the status check must come first.

    A 200 body can legitimately contain the word ``customer``; only a failure
    can carry Stripe's error object.
    """
    assert customer_is_gone(200, PRODUCTION_BODY) is False


# ── 2. the client threads it, from the FULL body ──────────────────────────


def _answer(status: int, body: str):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode())

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return patch("helixis.billing.stripe_client.httpx.AsyncClient", _Client)


@pytest.mark.asyncio
async def test_the_post_path_reports_a_dead_customer() -> None:
    failure = StripeFailure()
    with _answer(400, PRODUCTION_BODY):
        result = await StripeClient("sk_live").create_portal_session(
            customer_id="cus_dead", return_url="https://x", failure=failure
        )
    # Still None — the module's "nothing raises into a handler" rule is intact.
    assert result is None
    assert failure.customer_gone is True


@pytest.mark.asyncio
async def test_a_caller_that_passes_no_sink_is_unaffected() -> None:
    """The sink is opt-in; every existing call site must behave as before."""
    with _answer(400, PRODUCTION_BODY):
        assert (
            await StripeClient("sk_live").create_portal_session(
                customer_id="cus_dead", return_url="https://x"
            )
            is None
        )


@pytest.mark.asyncio
async def test_it_classifies_from_the_full_body_not_the_truncated_log_text() -> None:
    """⚠ The log line carries ``resp.text[:500]``; the classifier must not.

    Stripe pads errors with ``doc_url``, ``request_log_url`` and a long
    message, so the ``param`` that decides this can sit past the cut. Reusing
    the truncated string would make the classifier decline on the exact shape
    it was written for — silently, and only for the longer real bodies.
    """
    padded = json.dumps(
        {
            "error": {
                "message": "No such customer: 'cus_x'. " + ("pad " * 200),
                "code": "resource_missing",
                "param": "customer",
            }
        }
    )
    assert len(padded) > 500, "the fixture no longer straddles the truncation point"
    failure = StripeFailure()
    with _answer(400, padded):
        await StripeClient("sk_live").create_portal_session(
            customer_id="cus_dead", return_url="https://x", failure=failure
        )
    assert failure.customer_gone is True, (
        "classified from the truncated log text — a real Stripe body is longer "
        "than 500 chars and the deciding field sits past the cut"
    )


# ── 3. the routes ─────────────────────────────────────────────────────────

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


AUTH = {"Authorization": f"Bearer {_token()}"}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        billing={
            "stripe_secret_key": "sk_live_x",
            "stripe_webhook_secret": "whsec_x",
            "price_starter": "price_fake_starter",
            "price_pro": "price_fake_pro",
            "price_scale": "price_fake_scale",
            "portal_return_url": "https://app.example.com",
        },
    )


def _pool(*, subscription: str | None = None, customer: str | None = "cus_dead"):
    written: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(sql: str, *_args: Any):
        low = sql.lower()
        if "from company_users" in low:
            return {"role": "admin"}
        if "count(*)" in low:
            return {"n": 4}
        if "stripe_subscription_id" in low or "stripe_customer_id" in low:
            return {
                "stripe_customer_id": customer,
                "stripe_subscription_id": subscription,
                "name": "Acme Property Co",
            }
        return None

    async def execute(sql: str, *args: Any) -> str:
        written.append((sql, args))
        return "INSERT 1"

    pool = MagicMock()
    pool.fetchrow = fetchrow
    pool.execute = execute

    @asynccontextmanager
    async def _acquire():
        yield pool

    pool.acquire = _acquire
    return pool, written


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


def _stripe_that_reports_a_dead_customer(*, heals: bool = True) -> Any:
    """A client behaving the way the real one does against a stale id.

    The first call sets the sink and returns None — exactly what the live
    ``_post`` does on the production body. If ``heals``, a call carrying no
    sink (the retry, after re-minting) succeeds.
    """
    s = MagicMock()
    s.create_customer = AsyncMock(return_value={"id": "cus_fresh"})
    calls: list[str] = []

    async def checkout(*, customer_id: str, failure: StripeFailure | None = None, **_kw: Any):
        calls.append(customer_id)
        if failure is not None:
            failure.customer_gone = True
            return None
        return {"url": "https://checkout.example/x"} if heals else None

    async def portal(*, customer_id: str, failure: StripeFailure | None = None, **_kw: Any):
        calls.append(customer_id)
        if failure is not None:
            failure.customer_gone = True
        return None

    s.create_checkout_session = checkout
    s.create_portal_session = portal
    s.calls = calls
    return s


def _stripe_that_just_fails() -> Any:
    """Stripe unreachable: returns None and sets NOTHING on the sink."""
    s = MagicMock()
    s.create_customer = AsyncMock(return_value={"id": "cus_fresh"})
    s.create_checkout_session = AsyncMock(return_value=None)
    s.create_portal_session = AsyncMock(return_value=None)
    return s


def test_checkout_re_mints_when_the_stored_customer_is_gone(settings: Settings) -> None:
    """The customer trying to PAY gets through instead of a doomed 502."""
    pool, written = _pool(subscription=None)
    stripe = _stripe_that_reports_a_dead_customer()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 200, (
        f"got {resp.status_code} — a company whose stored Stripe id is stale still "
        "cannot pay us, which is the whole defect"
    )
    assert stripe.calls == ["cus_dead", "cus_fresh"], (
        f"calls were {stripe.calls} — the retry must use the NEWLY minted id"
    )
    assert any("stripe_customer_id" in sql for sql, _ in written), (
        "the fresh customer id was never written back, so the next request "
        "repeats the whole dance and Stripe accumulates a customer per click"
    )


def test_checkout_does_not_re_mint_when_stripe_merely_failed(settings: Settings) -> None:
    """THE DANGEROUS DIRECTION.

    A timeout or a 500 leaves ``customer_gone`` false. Minting there would give
    a company with a perfectly good Stripe customer a second one, during an
    outage, at the worst possible moment.
    """
    pool, _ = _pool(subscription=None)
    stripe = _stripe_that_just_fails()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 502, f"got {resp.status_code}"
    assert stripe.create_customer.await_count == 0, (
        "a second Stripe customer was minted on an ordinary failure — the "
        "duplicate-customer harm gotcha 81 records"
    )


def test_checkout_retries_once_and_never_loops(settings: Settings) -> None:
    """A second dead-customer verdict is not a stale cache; it is unknown.

    Looping would mint a fresh Stripe customer per attempt.
    """
    pool, _ = _pool(subscription=None)
    stripe = _stripe_that_reports_a_dead_customer(heals=False)
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 502
    assert stripe.create_customer.await_count == 1, (
        f"minted {stripe.create_customer.await_count} customers — the retry is looping"
    )


def test_the_portal_says_the_true_thing_and_never_re_mints(settings: Settings) -> None:
    """A fresh customer here would show an empty billing page to a payer.

    So the portal explains instead — and says retrying will not help, because
    the 502 it replaces reads as a transient outage and sends an admin back to
    click forever.
    """
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe_that_reports_a_dead_customer()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/portal", headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 409, f"got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"].lower()
    assert "retrying will not" in detail, (
        f"the message does not stop the doomed retry loop: {detail!r}"
    )
    assert stripe.create_customer.await_count == 0, (
        "the portal minted a customer — that opens a billing page showing no "
        "subscription, no card and no invoices to somebody who is paying us"
    )


def test_the_portal_still_answers_502_when_stripe_merely_failed(settings: Settings) -> None:
    """The honest message must not swallow an ordinary outage.

    A real Stripe blip is transient and 502 is the right answer; saying "this
    needs to be reset on our side" there would be its own false claim.
    """
    pool, _ = _pool(subscription="sub_existing")
    stripe = _stripe_that_just_fails()
    client = _client(settings, pool, stripe)
    try:
        resp = client.post("/api/v1/billing/portal", headers=AUTH)
    finally:
        client.close()

    assert resp.status_code == 502, f"got {resp.status_code}: {resp.text}"
