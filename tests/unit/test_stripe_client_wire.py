"""What `StripeClient` actually PUTS ON THE WIRE, not what it intends to.

⚠ THIS FILE EXISTS BECAUSE A MUTATION SURVIVED. `test_billing_change_plan`
mocks the whole client, so it proves the ROUTE asks for the right price and is
structurally blind to what the client then sends. Replacing

    "items[0][id]": item_id, "items[0][price]": price_id

with `"items[1][price]": price_id` — appending a second subscription item
instead of re-pricing the existing one — passed all nine of those tests. On the
wire that is the double-charge this whole change exists to remove, wearing a
different costume: the customer keeps paying for their old plan AND starts
paying for the new one, on one subscription, with the app showing a single
tidy plan.

That is gotcha 28's both-sides rule with a TRANSPORT as the second side —
exactly the shape CLAUDE.md records for A2P's `MessageSamples`, where producer
and consumer were each correct and the encoding between them was wrong.

⚠ stripe.com is egress-blocked from the dev container (gotcha 75), so every
path and parameter in `stripe_client.py` came from documentation rather than a
live call. These tests pin the bytes we BELIEVE are right; they cannot tell us
Stripe agrees. Confirm on the first real run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

from helixis.billing.stripe_client import StripeClient

SUBSCRIPTION = {
    "id": "sub_1",
    "items": {"data": [{"id": "si_1", "quantity": 3, "price": {"id": "price_old"}}]},
}


def _intercept(responses: dict[str, Any], sent: list[httpx.Request]):
    """Patch the client's httpx so every call is answered from `responses`.

    Keyed on the path so a GET of the subscription and the POST that follows
    can return different bodies — the read-then-write shape both mutating
    methods use.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        for fragment, body in responses.items():
            if fragment in str(request.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unmapped"})

    class _Client(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    return patch("helixis.billing.stripe_client.httpx.AsyncClient", _Client)


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


@pytest.mark.asyncio
async def test_a_plan_switch_replaces_the_item_price_and_never_adds_a_second() -> None:
    """⚠ THE MUTATION THAT SURVIVED. `items[0][id]` is what makes this an edit.

    Without it Stripe treats the price as a NEW item and the subscription
    carries both — the customer pays for the plan they left and the plan they
    chose, every month, and nothing in the app shows two.
    """
    sent: list[httpx.Request] = []
    with _intercept({"/subscriptions/sub_1": SUBSCRIPTION}, sent):
        await StripeClient("sk_test").switch_subscription_price(
            "sub_1", price_id="price_new", quantity=1
        )

    posts = [r for r in sent if r.method == "POST"]
    assert len(posts) == 1, f"expected one write, got {len(posts)}"
    form = _form(posts[0])

    assert form.get("items[0][id]") == ["si_1"], (
        "the write does not name the EXISTING subscription item, so Stripe adds "
        "a second one — the customer pays for both plans at once."
    )
    assert form.get("items[0][price]") == ["price_new"]
    assert form.get("items[0][quantity]") == ["1"]
    # Nothing may address a second item. A `items[1][...]` key is the bug.
    assert not [k for k in form if k.startswith("items[1]")], (
        f"the write addresses a second subscription item: {sorted(form)}"
    )


@pytest.mark.asyncio
async def test_a_plan_switch_states_its_proration_rather_than_inheriting_it() -> None:
    """Left unset, this follows an account-level dashboard setting — so what a
    customer is charged mid-cycle would depend on a toggle nobody in this repo
    can see. The seat path states it for the same reason."""
    sent: list[httpx.Request] = []
    with _intercept({"/subscriptions/sub_1": SUBSCRIPTION}, sent):
        await StripeClient("sk_test").switch_subscription_price(
            "sub_1", price_id="price_new", quantity=1
        )
    form = _form([r for r in sent if r.method == "POST"][0])
    assert form.get("proration_behavior") == ["create_prorations"]


@pytest.mark.asyncio
async def test_switching_to_the_price_and_quantity_already_set_writes_nothing() -> None:
    """⚠ Not merely an optimisation. Every write creates a proration line, so
    re-confirming the plan somebody is already on puts a $0 entry on their next
    invoice and reads as a change they did not make."""
    sent: list[httpx.Request] = []
    with _intercept({"/subscriptions/sub_1": SUBSCRIPTION}, sent):
        result = await StripeClient("sk_test").switch_subscription_price(
            "sub_1", price_id="price_old", quantity=3
        )
    assert result is not None
    assert not [r for r in sent if r.method == "POST"], "a no-op switch still wrote to Stripe"


@pytest.mark.asyncio
async def test_a_subscription_that_cannot_be_read_is_not_guessed_at() -> None:
    """No item id means no safe write. Posting without one is what appends a
    second item, so the honest answer is None and the caller 502s."""
    sent: list[httpx.Request] = []
    with _intercept({"/subscriptions/sub_1": {"id": "sub_1", "items": {"data": []}}}, sent):
        result = await StripeClient("sk_test").switch_subscription_price(
            "sub_1", price_id="price_new", quantity=1
        )
    assert result is None
    assert not [r for r in sent if r.method == "POST"]


@pytest.mark.asyncio
async def test_a_seat_change_also_edits_the_existing_item() -> None:
    """The same wire property for `update_subscription_quantity`, which had no
    test of its bytes either — and it runs on EVERY invite and removal."""
    sent: list[httpx.Request] = []
    with _intercept({"/subscriptions/sub_1": SUBSCRIPTION}, sent):
        await StripeClient("sk_test").update_subscription_quantity("sub_1", quantity=5)

    form = _form([r for r in sent if r.method == "POST"][0])
    assert form.get("items[0][id]") == ["si_1"]
    assert form.get("items[0][quantity]") == ["5"]
    # ⚠ A seat change must NOT carry a price. Sending one would let a seat sync
    # silently move somebody's plan.
    assert "items[0][price]" not in form, (
        "the seat sync sends a price — an invite could change the customer's plan"
    )


@pytest.mark.asyncio
async def test_checkout_is_form_encoded_and_carries_the_company(  # noqa: D103
) -> None:
    sent: list[httpx.Request] = []
    with _intercept({"/checkout/sessions": {"id": "cs_1", "url": "https://x"}}, sent):
        await StripeClient("sk_test").create_checkout_session(
            customer_id="cus_1",
            price_id="price_pro",
            quantity=2,
            success_url="https://a",
            cancel_url="https://b",
            company_id="co_1",
        )
    request = sent[0]
    # ⚠ FORM, not JSON. Stripe answers a JSON body with a 400 that reads like a
    # parameter error rather than an encoding one (module docstring).
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = _form(request)
    assert form["mode"] == ["subscription"]
    assert form["line_items[0][price]"] == ["price_pro"]
    assert form["line_items[0][quantity]"] == ["2"]
    # Without this the webhook cannot resolve the event to a tenant except via
    # the customer id, and it arrives before that row is written.
    assert form["subscription_data[metadata][company_id]"] == ["co_1"]
    # Our trial is ours; Stripe's would hand a second free fortnight to someone
    # who just chose to pay.
    assert "subscription_data[trial_period_days]" not in form
    # A discount has to be ENTERABLE or it does not exist: Checkout hides the
    # promotion-code field unless this is set, so running a launch offer used
    # to mean a code change and a deploy.
    assert form["allow_promotion_codes"] == ["true"]
    # ⚠ Stripe REFUSES a session carrying both, and the refusal lands on a
    # customer in the act of paying. A `discounts[…]` param appearing here is
    # therefore not a nice addition — it breaks checkout outright.
    assert not any(k.startswith("discounts") for k in form), (
        "discounts[…] cannot be sent alongside allow_promotion_codes — Stripe 400s the session"
    )


@pytest.mark.asyncio
async def test_a_creating_call_carries_an_idempotency_key_and_a_declarative_one_does_not() -> None:
    """⚠ The asymmetry is deliberate and both halves matter.

    A retried CREATE without a key makes a second subscription. A declarative
    write WITH one replays a stale response — a company going 3 → 4 → 3 → 4
    would have the first "4" replayed and Stripe would never see the change.
    """
    sent: list[httpx.Request] = []
    with _intercept({"/checkout/sessions": {"url": "https://x"}}, sent):
        await StripeClient("sk_test").create_checkout_session(
            customer_id="cus_1",
            price_id="price_pro",
            quantity=1,
            success_url="https://a",
            cancel_url="https://b",
            company_id="co_1",
        )
    assert "Idempotency-Key" in sent[0].headers

    sent.clear()
    with _intercept({"/subscriptions/sub_1": SUBSCRIPTION}, sent):
        await StripeClient("sk_test").switch_subscription_price(
            "sub_1", price_id="price_new", quantity=9
        )
    post = [r for r in sent if r.method == "POST"][0]
    assert "Idempotency-Key" not in post.headers, (
        "a plan switch carries an idempotency key — a Pro → Scale → Pro sequence "
        "would replay the first response and strand the customer on Scale."
    )


# ---------------------------------------------------------------------------
# The card that will be charged next
# ---------------------------------------------------------------------------
#
# ⚠ THESE EXIST BECAUSE TWO MUTATIONS SURVIVED. Every route-level test fakes
# `get_default_card_expiry` at the client boundary, so the method's own two
# branches — an unexpanded id, and a payment method that is not a card — had no
# coverage at all. Both survivors were a false ABSENCE (gotchas 26/55): the
# customer has a card, we report none, and the warning that exists to save them
# a declined renewal never fires.


@pytest.mark.asyncio
async def test_the_expanded_card_is_read_in_one_call() -> None:
    """The happy path, and it pins the `expand` that makes it one round trip."""
    sent: list[httpx.Request] = []
    with _intercept(
        {
            "/customers/cus_1": {
                "invoice_settings": {
                    "default_payment_method": {"card": {"exp_month": 9, "exp_year": 2026}}
                }
            }
        },
        sent,
    ):
        got = await StripeClient("sk_test").get_default_card_expiry("cus_1")
    assert got == (9, 2026)
    assert "expand" in str(sent[0].url), (
        "without the expand Stripe returns a bare id and this becomes two calls "
        "on every billing-panel load"
    )
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_unexpanded_id_is_fetched_rather_than_read_as_no_card() -> None:
    """⚠ The survivor. stripe.com is egress-blocked here (gotcha 75), so
    `expand` is documented rather than probed — and an expand that silently
    does nothing returns the payment method as a bare id STRING. Treating that
    as "no card on file" is a false absence about somebody's billing, and it
    fails in exactly the deployment where the fallback was most needed."""
    sent: list[httpx.Request] = []
    with _intercept(
        {
            "/customers/cus_1": {"invoice_settings": {"default_payment_method": "pm_9"}},
            "/payment_methods/pm_9": {"card": {"exp_month": 3, "exp_year": 2027}},
        },
        sent,
    ):
        got = await StripeClient("sk_test").get_default_card_expiry("cus_1")
    assert got == (3, 2027)
    assert any("/payment_methods/pm_9" in str(r.url) for r in sent)


@pytest.mark.asyncio
async def test_a_payment_method_with_no_card_invents_no_expiry() -> None:
    """⚠ The other survivor. A bank debit or a wallet has no expiry to warn
    about — reading `card` as an empty dict and carrying on would hand the
    caller `None`s that a laxer version could turn into a date."""
    sent: list[httpx.Request] = []
    with _intercept(
        {
            "/customers/cus_1": {
                "invoice_settings": {
                    "default_payment_method": {"type": "us_bank_account", "us_bank_account": {}}
                }
            }
        },
        sent,
    ):
        assert await StripeClient("sk_test").get_default_card_expiry("cus_1") is None


@pytest.mark.asyncio
async def test_a_customer_with_no_default_payment_method_is_not_guessed_at() -> None:
    sent: list[httpx.Request] = []
    with _intercept({"/customers/cus_1": {"invoice_settings": {}}}, sent):
        assert await StripeClient("sk_test").get_default_card_expiry("cus_1") is None


@pytest.mark.asyncio
async def test_an_implausible_expiry_is_refused_rather_than_warned_on() -> None:
    """Month 13 or year 12 is a shape nobody should render. `card_valid_through`
    would raise on the first and produce a date in antiquity for the second —
    a warning on every customer who has one."""
    sent: list[httpx.Request] = []
    with _intercept(
        {
            "/customers/cus_1": {
                "invoice_settings": {
                    "default_payment_method": {"card": {"exp_month": 13, "exp_year": 26}}
                }
            }
        },
        sent,
    ):
        assert await StripeClient("sk_test").get_default_card_expiry("cus_1") is None


@pytest.mark.asyncio
async def test_reading_the_card_writes_nothing() -> None:
    """A billing read must never be a billing write — the same property
    `test_the_check_writes_nothing_to_stripe` pins for the smoke."""
    sent: list[httpx.Request] = []
    with _intercept(
        {
            "/customers/cus_1": {
                "invoice_settings": {
                    "default_payment_method": {"card": {"exp_month": 9, "exp_year": 2026}}
                }
            }
        },
        sent,
    ):
        await StripeClient("sk_test").get_default_card_expiry("cus_1")
    assert [r.method for r in sent] == ["GET"]


@pytest.mark.asyncio
async def test_a_malformed_card_field_returns_none_rather_than_raising() -> None:
    """⚠ THE NARROW HALF OF THE isinstance GUARD, and a mutation is what pinned
    down what it is actually for.

    Replacing `if not isinstance(card, dict)` with `card = method.get("card")
    or {}` survived every other test here, because the `isinstance(month, int)`
    check below already rejects the ABSENT case — the two are the same answer
    for every shape Stripe documents (gotcha 58: check whether the mutant
    changes behaviour before writing a test to catch it).

    Where they differ is a `card` that is present and not an object. Then
    `.get` raises `AttributeError`, which leaves this method — and a billing
    client whose contract is "never raises into a request handler" (its own
    module docstring) would be broken by a body it merely did not expect. So
    the branch stays and this is the input that distinguishes it.
    """
    sent: list[httpx.Request] = []
    with _intercept(
        {"/customers/cus_1": {"invoice_settings": {"default_payment_method": {"card": "pm_x"}}}},
        sent,
    ):
        assert await StripeClient("sk_test").get_default_card_expiry("cus_1") is None
