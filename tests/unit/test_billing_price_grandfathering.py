"""A retired price id still has to resolve to the plan somebody is paying for.

WHY THIS EXISTS
---------------
Stripe cannot edit an existing price's amount, so raising a price means
creating a NEW one and repointing `HELIXIS_BILLING__PRICE_PRO`. Existing
subscriptions keep the OLD id forever — that IS the grandfathering, and it is
Stripe's behaviour rather than anything this repo chose.

`_plan_for_price` matched only the configured id. From the moment a price
changes, every grandfathered customer's subscription therefore resolves to no
plan at all:

* every `customer.subscription.*` event for them logs at ERROR, forever — a
  canary that cannot pass, which is how the next real finding gets scrolled
  past (gotcha 84);
* **`subscription_reconcile` cannot repair them.** Its whole job is restoring a
  company whose row lost its plan to a dropped webhook, and with the price
  unresolvable it has no plan to write — so a grandfathered customer stuck on
  the trial allowance stays there while their card is charged. That is the
  composition that makes this worth fixing rather than recording.

⚠ **What was ALREADY covered, checked before building anything.** The audit
note said "grandfathering on a price change is not encoded", which read as a
bigger gap than it is:

* `smoke/checks.py` already compares `plan.price_usd * 100` against Stripe's
  own `unit_amount` for every configured price, plus `active`, currency and
  recurring — so "the page says $199 and the card is charged $500" is caught.
* `BillingPanel.nextChargeLabel` already prefers Stripe's real figure over
  list price × seats, so a grandfathered customer is shown what they actually
  pay.
* `plans.py` states, in its opening docstring, that a plan NAME resolving to
  TODAY's allowance for everyone at once is DELIBERATE. Per-customer
  grandfathered allowances would contradict a stated design decision, so this
  file does not invent one (gotcha 88 — do not introduce a rule the codebase
  does not follow).

So the gap was the price→plan direction only, and naming the rest is what
stops the next reader rebuilding what exists.
"""

from __future__ import annotations

import logging

import pytest

from helixis.api.routes.billing import _plan_for_price
from helixis.billing.plans import PAID_PLANS
from helixis.settings import Settings, _parse_legacy_prices


def _settings(legacy: str = "") -> Settings:
    return Settings(
        env="test",
        billing={
            "stripe_secret_key": "sk_test",
            "stripe_webhook_secret": "whsec_test",
            "price_starter": "price_starter_now",
            "price_pro": "price_pro_now",
            "price_scale": "price_scale_now",
            "legacy_price_ids": legacy,
        },
    )


def test_the_current_price_still_resolves() -> None:
    """The direction that already worked, pinned so widening cannot break it."""
    s = _settings()
    assert _plan_for_price(s, "price_pro_now") == "pro"
    assert _plan_for_price(s, "price_scale_now") == "scale"


def test_a_grandfathered_price_resolves_to_its_plan() -> None:
    s = _settings("pro:price_pro_2025")
    assert _plan_for_price(s, "price_pro_2025") == "pro", (
        "a customer on last year's Pro price resolves to no plan, so every "
        "event for them errors and subscription_reconcile cannot restore their "
        "allowance"
    )


def test_a_plan_can_carry_several_retired_prices() -> None:
    """Prices rise more than once. A map that held one previous id would fail
    the customer who has been here longest — the one most worth keeping."""
    s = _settings("pro:price_pro_2024,pro:price_pro_2025,scale:price_scale_2025")
    assert _plan_for_price(s, "price_pro_2024") == "pro"
    assert _plan_for_price(s, "price_pro_2025") == "pro"
    assert _plan_for_price(s, "price_scale_2025") == "scale"


def test_an_unknown_price_still_resolves_to_nothing() -> None:
    """⚠ The other direction, and the one that matters most: widening the map
    must not make it answer for a price nobody configured. `_apply_event` does
    `plan = coalesce($3, plan)`, so a wrong answer here writes a plan the
    customer is not paying for."""
    assert _plan_for_price(_settings("pro:price_pro_2025"), "price_someone_elses") is None


def test_a_retired_price_is_never_sellable() -> None:
    """⚠ Two directions, two maps, deliberately. `price_ids` is what
    `/billing/checkout` sends Stripe; `price_ids_by_plan` is only for reading an
    EXISTING subscription back to its plan. Collapsing them would let a
    customer buy a price we withdrew."""
    s = _settings("pro:price_pro_2025")
    # ⚠ Assert the WHOLE map, not "the retired id is absent". A mutation that
    # leaked the legacy SETTING into a slot — `self.legacy_price_ids or
    # self.price_starter` — survived a not-in check, because the raw setting
    # reads `pro:price_pro_2025` and the needle was the bare id. Anything other
    # than exactly the three configured prices is a price we could sell and did
    # not mean to.
    assert s.billing.price_ids == {
        "starter": "price_starter_now",
        "pro": "price_pro_now",
        "scale": "price_scale_now",
    }


def test_the_current_price_leads_its_plans_tuple() -> None:
    """Order is not cosmetic: it is what makes the CURRENT price the one a
    reader sees first in a log or a debugger, and it pins that the widening is
    additive rather than a replacement."""
    by_plan = _settings("pro:price_pro_2025").billing.price_ids_by_plan
    assert by_plan["pro"][0] == "price_pro_now"
    assert "price_pro_2025" in by_plan["pro"]


def test_every_purchasable_plan_appears_in_the_widened_map() -> None:
    """Both sides (gotcha 28). A plan that fell out of `price_ids_by_plan`
    would resolve to nothing for every customer on it, current price included."""
    by_plan = _settings().billing.price_ids_by_plan
    assert set(by_plan) >= set(PAID_PLANS)


def test_a_malformed_legacy_entry_is_dropped_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo in an env var must not stop the app booting. It degrades to
    exactly the behaviour that exists today — the retired id resolves to
    nothing — which is a known handled state rather than an outage.

    ⚠ THE ASSERTION THAT MATTERS IS THE LOG, and a mutation is what showed it.
    Deleting the guard admits an empty price into the tuple, and that is
    HARMLESS: `_plan_for_price` returns early on a falsy price id, so nothing
    can match it. What is lost is the WARNING — and a typo'd env var dropped in
    silence is how a grandfathered customer stays broken with nobody looking
    (gotcha 58: check what the mutation actually changes before writing the
    test to catch it).
    """
    # ⚠ Clear the parser's cache first. It is keyed on the raw string so the
    # warning fires ONCE per configuration, which means a previous test in this
    # file would otherwise have consumed the only emission and this assertion
    # would pass or fail depending on execution order.
    _parse_legacy_prices.cache_clear()
    raw = "this-has-no-colon, :missing_plan, pro: , pro:price_pro_2025"
    with caplog.at_level(logging.WARNING, logger="helixis.settings"):
        s = _settings(raw)
        _ = s.billing.price_ids_by_plan
    assert _plan_for_price(s, "price_pro_2025") == "pro"
    assert _plan_for_price(s, "this-has-no-colon") is None
    reported = [
        r.getMessage() for r in caplog.records if "malformed legacy price" in r.getMessage()
    ]
    for bad in ("this-has-no-colon", ":missing_plan", "pro:"):
        assert any(repr(bad) in m for m in reported), (
            f"{bad!r} was dropped without naming itself — the operator then knows "
            f"only that grandfathering is not working, not which line to fix. Got {reported}"
        )
    # ⚠ Exactly three, not "at least three". The property re-evaluates on every
    # access and this test makes several; a version that logged per access would
    # repeat the same line forever on a misconfigured deployment, which is the
    # noise that buries the next real finding (gotcha 84).
    assert len(reported) == 3, f"the warning repeated on re-access: {reported}"


def test_an_empty_setting_changes_nothing() -> None:
    """The default state of every deployment today, so the widening is inert
    until somebody actually retires a price."""
    by_plan = _settings().billing.price_ids_by_plan
    assert by_plan == {k: (v,) for k, v in _settings().billing.price_ids.items()}
