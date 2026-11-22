"""The billing canary — and why billing needed one more than anything else.

Every other paid dependency here fails in front of somebody: a dead Buildium
key 401s a page, a dead Tavily key empties a search. Billing fails in SILENCE.
A wrong ``stripe_webhook_secret`` makes `/billing/webhook` 403 every delivery —
so Stripe charges the customer, retries for days, eventually disables the
endpoint, and this product never learns anybody paid. The customer is billed
and locked out at `trial_expired` holding a receipt.

⚠ These tests drive the check against a FAKE Stripe. They pin what it does with
each answer; they cannot tell us the real API answers that way (gotcha 75 —
stripe.com is egress-blocked from the dev container, so every path in
`stripe_client.py` and here came from documentation).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from helixis.billing.plans import PLANS
from helixis.settings import Settings
from helixis.smoke import checks as _checks

PRICES = {"price_starter": "price_s", "price_pro": "price_p", "price_scale": "price_sc"}


def _settings(**over: Any) -> Settings:
    billing = {"stripe_secret_key": "sk_test", **PRICES, "portal_return_url": "https://app.x"}
    billing.update(over)
    return Settings(env="test", memory={"enabled": False}, billing=billing)


# The plan card's own numbers, so a "healthy" fixture is one where Stripe and
# `plans.py` AGREE. ⚠ The fixture used to answer `{"id": "p", "active": True}`
# and nothing else, which is not a healthy price — it is a price with no
# amount, no currency and no recurrence, i.e. exactly the shapes the check now
# catches. A fixture that omits the fields under test cannot fail the test
# (gotchas 67/72/73: the state has to say what it MEANS).
_PRICE_CENTS = {
    "price_s": round(PLANS["starter"].price_usd * 100),
    "price_p": round(PLANS["pro"].price_usd * 100),
    "price_sc": round(PLANS["scale"].price_usd * 100),
}


def _stripe(
    *,
    key_status: int = 200,
    price_status: int = 200,
    price_active: bool = True,
    hook_urls: list[str] | None = None,
    unit_amount: int | None = None,
    currency: str = "usd",
    recurring: bool = True,
    interval: str = "month",
):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/customers" in url:
            return httpx.Response(key_status, json={"data": []})
        if "/v1/prices/" in url:
            price_id = url.rsplit("/", 1)[-1]
            body = {
                "id": price_id,
                "active": price_active,
                "unit_amount": (
                    unit_amount if unit_amount is not None else _PRICE_CENTS.get(price_id)
                ),
                "currency": currency,
                "recurring": {"interval": interval} if recurring else None,
            }
            return httpx.Response(price_status, json=body)
        if "/v1/webhook_endpoints" in url:
            urls = ["https://app.x/api/v1/billing/webhook"] if hook_urls is None else hook_urls
            return httpx.Response(200, json={"data": [{"url": u} for u in urls]})
        return httpx.Response(404, json={})

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return patch("helixis.smoke.checks.httpx.AsyncClient", _Client)


@pytest.mark.asyncio
async def test_a_healthy_deployment_passes() -> None:
    with _stripe():
        result = await _checks.check_stripe(_settings())
    assert result.status == "pass", result.detail
    assert "webhook endpoint present" in result.detail


@pytest.mark.asyncio
async def test_no_key_skips_and_says_what_went_unchecked() -> None:
    """⚠ SKIP, not fail — gotcha 84. A canary pointed at something the
    deployment does not have is one people stop reading, and that is what
    buries the next real finding. But the detail must name the gap, or a skip
    reads as coverage (the LED-013 skip-floor rule)."""
    with _stripe():
        result = await _checks.check_stripe(_settings(stripe_secret_key=""))
    assert result.status == "skipped"
    assert "no price ids" in result.detail and "webhook endpoint" in result.detail


@pytest.mark.asyncio
async def test_a_rejected_key_fails() -> None:
    """A revoked key, or a test key on a live deployment. Every checkout 502s
    at the moment somebody tries to pay."""
    with _stripe(key_status=401):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "secret key" in result.detail


@pytest.mark.asyncio
async def test_a_price_that_does_not_resolve_fails() -> None:
    """A typo'd or deleted price 503s checkout for THAT PLAN ALONE — the ladder
    looks complete on screen and one rung silently does nothing."""
    with _stripe(price_status=404):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "checkout 503s" in result.detail


@pytest.mark.asyncio
async def test_an_archived_price_fails_even_though_it_resolves() -> None:
    """⚠ 200 is not enough. Archiving a price in the dashboard leaves it
    readable and unbuyable, so a status-only check would pass it. This is the
    only automated way that mistake is ever noticed."""
    with _stripe(price_active=False):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "archived" in result.detail


@pytest.mark.asyncio
async def test_a_plan_with_no_price_id_fails_rather_than_passing_quietly() -> None:
    """A plan on the ladder with no price id is not a partial config, it is a
    button the customer meets with "that plan is not available yet" AFTER
    choosing it."""
    with _stripe():
        result = await _checks.check_stripe(_settings(price_scale=""))
    assert result.status == "fail"
    assert "scale" in result.detail


@pytest.mark.asyncio
async def test_no_webhook_endpoint_fails() -> None:
    """⚠ THE SILENT ONE. Stripe charges the card and never calls us, so the row
    stays on `trial` and the customer is locked out on day 14 having paid."""
    with _stripe(hook_urls=["https://someone-elses-app.example/hook"]):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "webhook endpoint" in result.detail


@pytest.mark.asyncio
async def test_an_unreachable_stripe_fails_rather_than_raising() -> None:
    """Nothing in the smoke may raise — a check that explodes takes the whole
    run with it and every other dependency goes unreported."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(boom)
            super().__init__(*a, **kw)

    with patch("helixis.smoke.checks.httpx.AsyncClient", _Client):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "ConnectError" in result.detail


@pytest.mark.asyncio
async def test_the_check_writes_nothing_to_stripe() -> None:
    """⚠ READ-ONLY, and this runs daily against the LIVE account. A POST here
    would create a real billable object every morning."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if "/v1/webhook_endpoints" in str(request.url):
            return httpx.Response(200, json={"data": [{"url": "https://x/billing/webhook"}]})
        return httpx.Response(200, json={"data": [], "active": True})

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    with patch("helixis.smoke.checks.httpx.AsyncClient", _Client):
        await _checks.check_stripe(_settings())

    assert methods, "the check made no calls at all — it is measuring nothing"
    assert set(methods) == {"GET"}, f"the billing canary wrote to Stripe: {set(methods)}"


@pytest.mark.asyncio
async def test_a_price_that_charges_a_different_amount_than_the_card_shows_fails() -> None:
    """⚠ THE NUMBER ON THE PAGE vs THE NUMBER ON THE CARD. `plans.py` is what
    the customer READS and Stripe is what CHARGES them, and nothing compared
    the two — so a price id pasted into the wrong plan's slot, or an amount
    edited in the dashboard, showed $199 while $500 left their account. No
    exception, no log line, and a chargeback as the first signal."""
    with _stripe(unit_amount=50_000):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "500.00" in result.detail and "plan card says" in result.detail


@pytest.mark.asyncio
async def test_a_price_in_another_currency_fails() -> None:
    """`plans.py` calls the field `price_usd` and the UI writes a `$`. A price
    created in another currency renders as dollars and bills as something
    else."""
    with _stripe(currency="eur"):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "eur" in result.detail


@pytest.mark.asyncio
async def test_a_one_time_price_fails() -> None:
    """Stripe rejects a non-recurring price in a `mode=subscription` Checkout
    Session — at the moment somebody tries to pay, not at boot."""
    with _stripe(recurring=False):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "recurring" in result.detail


@pytest.mark.asyncio
async def test_a_price_stripe_does_not_price_is_not_reported_as_a_mismatch() -> None:
    """⚠ The other direction. `unit_amount` is null on a tiered or
    usage-metered price, and reading absent as zero would report every one of
    those as charging $0.00 against the card — a confident false alarm on a
    correctly-configured deployment, which is how a canary gets switched off
    (gotcha 52). Unknown declines to assert (gotcha 55)."""
    # ⚠ `_stripe(unit_amount=None)` cannot express this: None is that
    # parameter's "use the plan's own price" default, so the absent case needs
    # its own client rather than a value the fixture reads as unset.
    with patch("helixis.smoke.checks.httpx.AsyncClient", _absent_amount_client()):
        result = await _checks.check_stripe(_settings())
    assert result.status == "pass", result.detail


def _absent_amount_client():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/customers" in url:
            return httpx.Response(200, json={"data": []})
        if "/v1/prices/" in url:
            return httpx.Response(
                200,
                json={
                    "id": "p",
                    "active": True,
                    "currency": "usd",
                    "recurring": {"interval": "month"},
                },
            )
        if "/v1/webhook_endpoints" in url:
            return httpx.Response(
                200, json={"data": [{"url": "https://app.x/api/v1/billing/webhook"}]}
            )
        return httpx.Response(404, json={})

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return _Client


@pytest.mark.asyncio
async def test_a_yearly_price_fails_because_nothing_announces_a_yearly_charge() -> None:
    """⚠ Not a dashboard typo — a missing FEATURE, and the check says so.

    This product has no annual plan and no advance renewal notice.
    `notices.py` carries `trial_ending` and `payment_failed` and nothing else,
    deliberately: a monthly renewal announced in advance every single month is
    a banner nobody reads. A yearly price makes that reasoning FALSE — a large
    charge lands unannounced twelve months after anybody last thought about
    it — and every allowance in `plans.py` is stated per month while the card
    prints "/ month".

    The interval is a fact the app never learns; nothing in the codebase reads
    it. The smoke already has the price object in hand, which is why the one
    place it can be checked is here.
    """
    with _stripe(interval="year"):
        result = await _checks.check_stripe(_settings())
    assert result.status == "fail"
    assert "year" in result.detail
    assert "renewal notice" in result.detail, (
        "the detail must say what has to be BUILT, not just that the interval "
        "differs — otherwise the fix reads as 'change it back in the dashboard'"
    )


@pytest.mark.asyncio
async def test_a_monthly_price_is_still_healthy() -> None:
    """The other direction, so a check that failed every interval — including
    the only one this product sells — cannot pass the test above."""
    with _stripe(interval="month"):
        assert (await _checks.check_stripe(_settings())).status == "pass"
