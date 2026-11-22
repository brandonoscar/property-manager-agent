"""The prices the customer reads come from `plans.py`, on every branch.

Until 2026-09-02 `BillingPanel.tsx` carried its own table —

    starter: { price: '$199 / user / month', blurb: '400 questions per user…' }
    pro:     { price: '$500 / month',        blurb: '…1,200 questions a month.' }

— hand-copied from `billing/plans.py`, with nothing holding the two in
agreement. `plans.py`'s own docstring *invites* re-derivation before a price
change ("Re-derive these before changing a price"), so the drift was invited
on one side and invisible on the other. The failure is not cosmetic: the card
says $199, the button starts a Stripe Checkout at whatever the server now
charges, and the customer has a billing dispute rather than a display bug.

Gotchas 50/51/77/78 all end at the same move — put the field on the row and
the join disappears. The server sends FACTS (`price_usd`, `per_seat`,
`turns_included`) and the client formats the sentence, so there is no second
copy of a number left to drift.

⚠ Reads BOTH sides (gotcha 28): `PLANS` AND what the endpoint actually
returns. A test that only exercised `_plan_catalog()` would agree with itself
while `billing_status` forgot to call it — gotcha 62's rule, and the reason
both response branches are driven here rather than AST-scanned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.api.routes.billing import _plan_catalog, billing_status
from helixis.billing.gate import BillingState
from helixis.billing.plans import (
    ALL_FEATURES,
    FEATURE_CRM,
    PAID_PLANS,
    PLANS,
    cheapest_plan_with,
)

_COMPANY = "11111111-1111-1111-1111-111111111111"


def _user() -> MagicMock:
    user = MagicMock()
    user.company_ids = [_COMPANY]
    return user


def _state() -> BillingState:
    return BillingState(
        plan=PLANS["starter"],
        status="active",
        seats=2,
        turns_used=10,
        turns_allowed=800,
        trial_ends_at=None,
        period_end=datetime(2026, 10, 1, tzinfo=UTC),
        past_due=False,
    )


# ── The catalog is the plan table, not a second copy of it ────────────────


def test_the_catalog_carries_every_paid_plan_and_nothing_unbuyable() -> None:
    keys = [p.key for p in _plan_catalog()]
    assert keys == list(PAID_PLANS), (
        f"catalog is {keys}, PAID_PLANS is {list(PAID_PLANS)} — the panel renders "
        "one card per entry, so a missing plan is a plan nobody can buy and an "
        "extra one is a card whose Checkout call the server will refuse"
    )
    assert "trial" not in keys, (
        "trial is in the catalog — it cannot be bought, and rendering it as a "
        "choice offers a paying customer a downgrade into a 14-day clock"
    )


def test_every_number_the_customer_reads_is_the_plan_tables_own() -> None:
    """The whole point. A literal here would be the drift, relocated."""
    for out in _plan_catalog():
        plan = PLANS[out.key]
        assert (out.label, out.price_usd, out.per_seat, out.turns_included) == (
            plan.label,
            plan.price_usd,
            plan.per_seat,
            plan.turns_included,
        ), f"{out.key} disagrees with PLANS — the customer would be shown a price we do not charge"


def test_per_seat_is_carried_rather_than_inferred_from_the_key() -> None:
    """The seats input renders on `per_seat`, not on a hardcoded plan key.

    The panel keyed off the name until 2026-09-02, so a second per-seat plan
    would have shipped with no way to choose a seat count — and a per-seat
    plan bought at one seat undercharges silently.
    """
    by_key = {p.key: p for p in _plan_catalog()}
    # ⚠ The per-seat rung is `pro` since the 2026-09-04 ladder rename.
    assert by_key["pro"].per_seat is True
    assert by_key["starter"].per_seat is False
    assert by_key["scale"].per_seat is False


# ── …and the endpoint actually sends it, on BOTH branches ─────────────────


@pytest.mark.asyncio
async def test_status_sends_the_catalog_when_billing_is_readable() -> None:
    with (
        patch("helixis.api.routes.billing.load_state", AsyncMock(return_value=_state())),
        patch("helixis.api.routes.billing.check_turn_allowed", AsyncMock(return_value=None)),
    ):
        out = await billing_status(user=_user())
    assert [p.key for p in out.plans] == list(PAID_PLANS), (
        "the readable branch dropped the catalog — the panel renders no plan "
        "cards at all and an admin cannot subscribe"
    )


@pytest.mark.asyncio
async def test_status_sends_the_catalog_when_billing_is_unreadable() -> None:
    """The branch that matters more, and the one easiest to forget.

    `load_state` returning None is a database blip, and the panel still
    renders — so this is precisely when an admin is most likely to be looking
    at the page and wanting to pay.
    """
    with patch("helixis.api.routes.billing.load_state", AsyncMock(return_value=None)):
        out = await billing_status(user=_user())
    assert out.plan == "trial", "guard: this test must exercise the unreadable branch"
    assert [p.key for p in out.plans] == list(PAID_PLANS), (
        "the unreadable branch dropped the catalog"
    )


# ── the CURRENT plan's capabilities, on both branches ─────────────────


@pytest.mark.asyncio
async def test_status_reports_what_the_current_plan_unlocks() -> None:
    """⚠ A TOP-LEVEL field, not something to look up in `plans` below.

    The catalog holds only PURCHASABLE plans, so a company on the trial finds
    no row for itself and a client re-deriving its capabilities from the list
    would conclude it has none — and black out the Leasing section for every
    company still evaluating the product. Same rule as `block` (gotcha 73):
    the enforcement path and the UI read one field.
    """
    with (
        patch("helixis.api.routes.billing.load_state", AsyncMock(return_value=_state())),
        patch("helixis.api.routes.billing.check_turn_allowed", AsyncMock(return_value=None)),
    ):
        out = await billing_status(user=_user())
    assert out.plan == "starter", "guard: _state() is the Starter fixture"
    assert out.features == sorted(PLANS["starter"].features)
    assert FEATURE_CRM not in out.features, (
        "Starter is reporting Leasing as included while `crm.py` refuses its "
        "routes — the panel would offer a section the API declines"
    )


@pytest.mark.asyncio
async def test_an_unreadable_row_reports_every_capability() -> None:
    """Fail OPEN, matching `gate.py` rule 1 and `crm._primary_company_id`.

    A blip must not black out a section somebody is paying for, and the three
    layers have to agree on that — a UI that hides Leasing while the API
    serves it is its own kind of wrong.

    ⚠ ASSERTED AGAINST `ALL_FEATURES`, NOT AGAINST THE TRIAL. The route
    reports "trial" as the plan on this branch, and until 2026-09-04 the trial
    carried every capability — so an assertion phrased as "the trial's set"
    passed for a reason that had nothing to do with failing open. The day the
    trial dropped Leasing, that phrasing would have gone on passing while the
    branch quietly became fail-CLOSED. The bar here is the capability set, not
    whichever plan happens to be the fallback.
    """
    with patch("helixis.api.routes.billing.load_state", AsyncMock(return_value=None)):
        out = await billing_status(user=_user())
    assert out.plan == "trial", "guard: this test must exercise the unreadable branch"
    assert FEATURE_CRM in out.features
    assert out.features == sorted(ALL_FEATURES), (
        "an unreadable billing row must report EVERY capability. Reporting the "
        "fallback plan's set instead makes this branch fail closed the moment "
        "that plan stops carrying something."
    )
    assert not PLANS["trial"].includes(FEATURE_CRM), (
        "guard: this test is only meaningful while the trial does NOT carry "
        "Leasing — if it does again, the assertion above passes for the wrong "
        "reason and needs re-deriving, not deleting"
    )


def test_the_catalog_carries_each_plans_own_capabilities() -> None:
    served = {p.key: set(p.features) for p in _plan_catalog()}
    assert served == {k: set(PLANS[k].features) for k in PAID_PLANS}, (
        "the catalog's feature sets no longer match PLANS — the card and the "
        "refusal are now two sources of truth"
    )


def test_the_catalog_carries_the_copy_that_says_why() -> None:
    """Three prices with no reason to pick one is what the panel was before
    2026-09-04. The copy is server-owned for the same reason the price is."""
    for served, key in zip(_plan_catalog(), PAID_PLANS, strict=True):
        assert served.tagline == PLANS[key].tagline
        assert served.highlights == list(PLANS[key].highlights)


# ── The upgrade target: one answer, derived twice ─────────────────────────
#
# `LeasingComplianceGate.tsx` picks it with
#     billing?.plans.find((p) => p.features.includes('crm'))
# and its own comment states the inference out loud: "the FIRST plan in ladder
# order that carries the capability, SO IT IS THE CHEAPEST ONE THAT WORKS".
# That "so" holds only while the catalog's order ascends by price — and
# `PAID_PLANS`' docstring says that tuple is ALSO "the order the billing panel
# renders them in", which is a live reason to reorder it ("put the popular
# tier first") that has nothing to do with price. The comment asserts the
# conclusion and nothing checked the premise (gotcha 48; gotcha 122's shape,
# where an exemption rested on a premise nobody had measured).
#
# The server no longer depends on the order at all — `cheapest_plan_with`
# takes the minimum by price — so the remaining exposure is that the two sides
# would name DIFFERENT plans: the panel button says "See Scale" while the 402
# from `crm.py` says "Upgrade to Pro". Both tests below are kept because they
# fail on different edits (see each docstring).


def test_the_paid_ladder_ascends_by_price() -> None:
    """`PAID_PLANS` is ordered cheapest-first, which the client reads as a fact.

    Catches a reorder that is LATENT today — swapping two rungs where only one
    carries a feature leaves the agreement test below passing, because both
    sides still resolve to the same plan. It becomes a wrong answer the moment
    a capability is added to the cheaper rung, which is a change nobody would
    connect to a display-order edit made months earlier.
    """
    prices = [(k, PLANS[k].price_usd) for k in PAID_PLANS]
    assert prices == sorted(prices, key=lambda kv: kv[1]), (
        f"PAID_PLANS is {prices} — not cheapest-first. The billing panel renders "
        "in this order AND `LeasingComplianceGate` takes the first match as the "
        "cheapest one that works, so reordering for display quietly quotes a "
        "dearer plan than the customer needs. Reorder back, or change the client "
        "to compute the minimum itself and delete this test in the same commit."
    )


def test_the_client_and_the_server_name_the_same_upgrade_target() -> None:
    """First-match-over-the-catalog must equal `cheapest_plan_with`.

    The client cannot call a Python function, so this is the only place the
    two derivations are compared (gotcha 28). It fails on a reorder that
    actually moves a feature-carrying rung — where the panel and the API
    refusal would name different plans on the same screen.
    """
    assert ALL_FEATURES, (
        "canary: no enforced features, so the loop below asserts nothing — "
        "re-point this at whatever gates a plan now (gotchas 49/100/101)"
    )
    catalog = _plan_catalog()
    for feature in sorted(ALL_FEATURES):
        client_pick = next((p for p in catalog if feature in p.features), None)
        server_pick = cheapest_plan_with(feature)
        client_key = client_pick.key if client_pick else None
        server_key = server_pick.key if server_pick else None
        assert client_key == server_key, (
            f"for {feature!r} the panel would say {client_key!r} and the refusal "
            f"{server_key!r} — the customer reads two different answers to "
            "'what do I buy?' depending on which surface told them"
        )
