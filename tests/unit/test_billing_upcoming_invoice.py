"""The next bill is Stripe's number, and "cannot say" is never "$0.00".

The billing panel stated `Next charge $597 on 04/10/2026` from list price x
seats. That arithmetic knows nothing about DISCOUNTS or proration, and it was
correct only while nobody could have a coupon — enabling promotion codes on
Checkout is exactly what made it able to overstate somebody's bill. A confident
wrong number about money is the one error that reaches an invoice.

⚠ Called DIRECTLY rather than through `TestClient`. `test_billing_change_plan`
boots the whole app, which is right for a route whose bug was reachability;
this one's whole surface is what it does with four shapes of Stripe response,
and the direct call is what makes each of them one line.

⚠ Its own endpoint, not a field on `/billing/status`. That endpoint is fetched
by `TrialBanner`, which renders on every page, so folding a Stripe round trip
into it would put Stripe's latency and Stripe's outages in front of every page
load — gotcha 37's lesson about a check placed on the wrong path.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import billing as billing_mod
from helixis.api.routes.billing import upcoming_invoice
from helixis.settings import Settings

COMPANY_ID = "22222222-2222-2222-2222-222222222222"
_SOURCE = pathlib.Path(billing_mod.__file__).read_text(encoding="utf-8")


class _User:
    user_id = "11111111-1111-1111-1111-111111111111"
    company_ids = [COMPANY_ID]


def _settings() -> Settings:
    return Settings(
        env="test",
        billing={"stripe_secret_key": "sk_test", "stripe_webhook_secret": "whsec_test"},
    )


def _pool(customer_id: str | None) -> tuple[MagicMock, list[tuple[str, tuple[Any, ...]]]]:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    pool = MagicMock()

    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        calls.append((sql, args))
        return {"stripe_customer_id": customer_id} if customer_id else None

    pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    return pool, calls


async def _ask(
    monkeypatch: pytest.MonkeyPatch,
    *,
    customer_id: str | None = "cus_1",
    invoice: dict[str, Any] | None,
    settings: Settings | None = None,
    card_expiry: tuple[int, int] | None = None,
    card_raises: bool = False,
) -> Any:
    """⚠ `card_expiry` defaults to None = NO CARD ON FILE, stated rather than
    left to a MagicMock.

    Before it existed, `client.get_default_card_expiry` auto-generated as a
    plain MagicMock, awaiting it raised TypeError, and the route's own
    never-fail catch swallowed that — so every test in this file silently
    exercised the failure path while reading as coverage. That is gotchas
    67/72/73's rule (a fixture has to say which state it MEANS) arriving as a
    mock that answers everything.
    """
    pool, _calls = _pool(customer_id)
    monkeypatch.setattr(billing_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(billing_mod, "_settings", lambda: settings or _settings())
    client = MagicMock()
    client.get_upcoming_invoice = AsyncMock(return_value=invoice)
    client.get_default_card_expiry = AsyncMock(
        side_effect=RuntimeError("stripe down") if card_raises else None,
        return_value=None if card_raises else card_expiry,
    )
    monkeypatch.setattr(billing_mod, "StripeClient", lambda _key: client)
    return await upcoming_invoice(user=_User())  # type: ignore[arg-type]


async def test_it_reports_what_stripe_says_after_discounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _ask(
        monkeypatch,
        invoice={
            "amount_due": 44850,
            "next_payment_attempt": 1791072000,
            "discounts": ["di_1"],
        },
    )
    # ⚠ Cents converted HERE. A client doing its own arithmetic on a currency
    # is how a bill ends up off by 100x on the one screen nobody forgives.
    assert out.total_usd == 448.50
    assert out.discounted is True
    assert out.at is not None and out.at.startswith("2026-")


async def test_a_zero_total_is_reported_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully-discounted period is a real answer and must not read as unknown
    — the client's fallback would then quote list price to somebody who owes
    nothing."""
    out = await _ask(monkeypatch, invoice={"amount_due": 0, "period_end": 1791072000})
    assert out.total_usd == 0.0


@pytest.mark.parametrize(
    "invoice",
    [
        None,  # Stripe unreachable, or a 404 — the ordinary no-subscription case
        {},  # answered, but with no total we can read
        {"amount_due": None, "total": None},
        {"amount_due": "lots"},  # a shape we do not understand
    ],
)
async def test_unknown_is_never_zero(
    monkeypatch: pytest.MonkeyPatch, invoice: dict[str, Any] | None
) -> None:
    """⚠ The rule this endpoint exists to keep. A customer with no
    subscription, a Stripe outage and an unparseable body all arrive the same
    way, and reporting any of them as "$0.00 due" is a false statement about
    money. `None` means the panel falls back to the estimate it already showed.
    """
    out = await _ask(monkeypatch, invoice=invoice)
    assert out.total_usd is None
    assert out.at is None
    assert out.discounted is False


async def test_a_company_with_no_stripe_customer_is_not_asked_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No customer id, no call — there is nothing for Stripe to project, and
    asking would spend a round trip to be told so."""
    pool, _ = _pool(None)
    monkeypatch.setattr(billing_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(billing_mod, "_settings", _settings)
    client = MagicMock()
    client.get_upcoming_invoice = AsyncMock(return_value={"amount_due": 999})
    monkeypatch.setattr(billing_mod, "StripeClient", lambda _key: client)

    out = await upcoming_invoice(user=_User())  # type: ignore[arg-type]
    assert out.total_usd is None
    client.get_upcoming_invoice.assert_not_awaited()


async def test_an_unconfigured_deployment_says_nothing_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with no Stripe keys is a normal state, not a fault. The
    panel shows its estimate; nothing 500s."""
    out = await _ask(
        monkeypatch,
        invoice={"amount_due": 999},
        settings=Settings(env="test"),
    )
    assert out.total_usd is None


async def test_the_customer_lookup_is_company_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ gotcha 47. An unscoped read here hands one customer another company's
    invoice — on the endpoint whose whole output is an amount of money."""
    pool, calls = _pool("cus_1")
    monkeypatch.setattr(billing_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(billing_mod, "_settings", _settings)
    client = MagicMock()
    client.get_upcoming_invoice = AsyncMock(return_value=None)
    monkeypatch.setattr(billing_mod, "StripeClient", lambda _key: client)

    await upcoming_invoice(user=_User())  # type: ignore[arg-type]
    sql, args = calls[0]
    assert "company_id = $1" in sql
    assert COMPANY_ID in args


def test_the_invoice_is_not_fetched_from_the_status_route() -> None:
    """⚠ A STATIC guard, because the cost is invisible to a behavioural one.

    `/billing/status` is fetched by `TrialBanner` on every page. Folding the
    Stripe call into it would still pass every test in this file and every test
    of the panel, while putting Stripe's latency — and Stripe's outages — in
    front of every page load. Nothing that runs can see that; only the call
    site can.
    """
    tree = ast.parse(_SOURCE)
    status_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "billing_status"
    )
    called = {
        node.func.attr
        for node in ast.walk(status_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "get_upcoming_invoice" not in called, (
        "the status route now calls Stripe — every page load pays for it"
    )


# ---------------------------------------------------------------------------
# The card that will not survive the next charge
# ---------------------------------------------------------------------------
#
# `test_card_expiry.py` covers the decision thoroughly in isolation. Nothing
# there can see whether the ROUTE asks the question — gotcha 62's rule, and the
# same shape as gotchas 66/69: a well-tested helper whose one production call
# site quietly ignores it.
#
# ⚠ It rides on THIS endpoint and not on `/billing/status`, for the reason the
# module docstring already gives: status is fetched by `TrialBanner` on every
# page, so a second Stripe round trip there puts Stripe's latency in front of
# the whole app (gotcha 37).

# 2026-10-01, comfortably inside the 30-day warning window from a card that
# dies at the end of September.
_OCT_1 = 1791158400


async def test_a_card_that_dies_before_the_charge_is_warned_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _ask(
        monkeypatch,
        invoice={"amount_due": 19900, "next_payment_attempt": _OCT_1},
        card_expiry=(9, 2026),
    )
    assert out.card_expires_first is True
    assert out.card_expiry == "09/2026", (
        "the label must be the form printed on the card, so the reader can "
        "compare it without translating"
    )


async def test_a_card_good_past_the_charge_warns_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The other direction, and the one a one-sided test would miss (gotcha
    38). A version that warned every customer would pass the test above."""
    out = await _ask(
        monkeypatch,
        invoice={"amount_due": 19900, "next_payment_attempt": _OCT_1},
        card_expiry=(12, 2030),
    )
    assert out.card_expires_first is False
    assert out.card_expiry is None, (
        "an expiry rendered when there is nothing to act on reads as a defect"
    )


async def test_no_card_on_file_warns_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _ask(
        monkeypatch,
        invoice={"amount_due": 19900, "next_payment_attempt": _OCT_1},
        card_expiry=None,
    )
    assert out.card_expires_first is False


async def test_an_unreadable_card_never_costs_the_invoice_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing one. A warning is an extra; the total is what the
    customer opened the page for, and a Stripe hiccup reading the payment
    method must not withhold it."""
    out = await _ask(
        monkeypatch,
        invoice={"amount_due": 19900, "next_payment_attempt": _OCT_1},
        card_raises=True,
    )
    assert out.total_usd == 199.0
    assert out.card_expires_first is False


async def test_the_card_is_not_asked_about_when_stripe_gave_no_charge_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No date, no comparison — and specifically no fabricated one. `at` is
    absent whenever Stripe's invoice carries neither a next attempt nor a
    period end."""
    out = await _ask(monkeypatch, invoice={"amount_due": 19900}, card_expiry=(9, 2026))
    assert out.at is None
    assert out.card_expires_first is False
