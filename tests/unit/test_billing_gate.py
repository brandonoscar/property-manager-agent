"""The billing gate's asymmetries, and the webhook signature.

This file exists because both directions of this gate are expensive and they
are expensive in *different* ways, so a one-sided test is worse than none:

* refusing wrongly locks a paying customer out of the product
* allowing wrongly gives it away

Every test below names which direction it holds.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.billing import stripe_signature
from helixis.billing.gate import BillingState, check_turn_allowed, load_state
from helixis.billing.plans import MEASURED_MEAN_TURN_USD, PLANS, plan_for


def _state(**over) -> BillingState:
    base = dict(
        plan=PLANS["starter"],
        status="active",
        seats=1,
        turns_used=0,
        turns_allowed=400,
        trial_ends_at=None,
        period_end=None,
        past_due=False,
    )
    base.update(over)
    return BillingState(**base)  # type: ignore[arg-type]


# ── The plan table is priced against a measurement ────────────────────────


def test_the_top_ceiling_sits_below_its_own_break_even():
    """⚠ THIS ASSERTION POINTED THE WRONG WAY ON 2026-09-02 AND SHIPPED.

    It read `turns_included > break_even` and called that the safe direction,
    reasoning that a lower cap would "cap a customer who is still profitable".
    That is backwards. A ceiling ABOVE break-even means a customer who uses
    everything they paid for costs more than they pay — at the 2,500 cap it
    first shipped with, $535.75 against $500. The plan was profitable only
    because most people would not finish their allowance, which is a
    utilisation BET, and this test asserted it as a guarantee.

    Below break-even, every Pro customer is profitable even at 100% use.
    """
    pro = PLANS["scale"]  # the top flat rung; was `pro` before the 2026-09-04 ladder rename
    break_even = pro.price_usd / MEASURED_MEAN_TURN_USD
    assert break_even == pytest.approx(2333, abs=50)
    assert pro.turns_included < break_even, (
        f"Pro's {pro.turns_included}-turn ceiling is ABOVE its "
        f"{break_even:.0f}-turn break-even — a customer who uses the whole "
        "plan loses money. Lower the cap, raise the price, or cut cost/turn."
    )


def test_pro_margin_at_full_use_clears_the_floor():
    """The cap has to leave a real margin, not merely avoid a loss.

    A cap one turn under break-even passes the test above and earns nothing.
    """
    pro = PLANS["scale"]  # the top flat rung; was `pro` before the 2026-09-04 ladder rename
    cogs = pro.turns_included * MEASURED_MEAN_TURN_USD
    margin = (pro.price_usd - cogs) / pro.price_usd
    assert margin >= 0.40, (
        f"Pro margin at full use is {margin:.0%} (COGS ${cogs:.2f} on "
        f"${pro.price_usd}). Below 40% the plan is not worth selling."
    )


def test_pro_beats_the_starter_seats_it_replaces():
    """⚠ The constraint that stops the cap being chosen on margin alone.

    A margin-optimal Pro cap lands near 700 turns — but two Starter seats
    already carry 800 for $398, so nobody would ever upgrade and the ladder
    is dead. Pro must give MORE allowance than the Starter spend it displaces.
    """
    pro, starter = PLANS["pro"], PLANS["starter"]
    two_seats = starter.turn_allowance(2)
    assert pro.turns_included > two_seats, (
        f"Pro gives {pro.turns_included} turns; two Starter seats already "
        f"give {two_seats} for ${starter.price_usd * 2}. Nobody upgrades."
    )
    # And at the seat count where the prices meet, Pro must be the better buy.
    seats_at_pro_price = pro.price_usd // starter.price_usd  # 2 at $199/$500
    assert pro.turns_included > starter.turn_allowance(int(seats_at_pro_price))


def test_the_middle_rung_is_per_seat_and_the_others_are_flat():
    # ⚠ Re-pointed 2026-09-04: the ladder became Starter $50 / Pro $199 /
    # Scale $500, so the PER-SEAT plan is `pro` now and `starter` is flat.
    # The property — exactly one rung multiplies by seats — did not move.
    assert PLANS["pro"].per_seat is True
    assert PLANS["pro"].turn_allowance(3) == 1200
    # Pro is per ACCOUNT: seats must not multiply it, or the client could ask
    # for 50 seats and be handed 125,000 turns.
    assert PLANS["scale"].per_seat is False
    assert PLANS["scale"].turn_allowance(50) == PLANS["scale"].turns_included
    assert PLANS["starter"].per_seat is False
    assert PLANS["starter"].turn_allowance(50) == PLANS["starter"].turns_included


def test_an_unknown_plan_falls_back_to_the_smallest_allowance():
    """⚠ Never to the largest. A row this build does not recognise was
    written by a newer deploy or is corrupt; handing it Pro's ceiling lets an
    unknown state buy the biggest thing we sell."""
    assert plan_for("enterprise_unlimited").key == "trial"
    assert plan_for("").turns_included == PLANS["trial"].turns_included


# ── Direction 1: never manufacture a lockout ──────────────────────────────


@pytest.mark.asyncio
async def test_an_unreadable_row_allows_the_turn():
    """A database blip must not lock out a paying customer."""
    with patch("helixis.billing.gate.db_engine.get_pool", side_effect=RuntimeError("down")):
        assert await load_state("c1") is None
        assert await check_turn_allowed("c1") is None


@pytest.mark.asyncio
async def test_a_missing_row_allows_the_turn():
    """A company with no billing row has no evidence against it — allow."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    with patch("helixis.billing.gate.db_engine.get_pool", return_value=pool):
        assert await check_turn_allowed("c1") is None


@pytest.mark.asyncio
async def test_a_trial_with_no_end_date_has_not_expired():
    """⚠ A null timestamp is a provisioning gap, not an expiry. Reading it as
    expired turns one missing field into a total outage."""
    state = _state(
        plan=PLANS["trial"], status="trialing", trial_ends_at=None, turns_allowed=150, turns_used=0
    )
    with patch("helixis.billing.gate.load_state", AsyncMock(return_value=state)):
        assert await check_turn_allowed("c1") is None


@pytest.mark.asyncio
async def test_past_due_keeps_serving():
    """A declined card is the customer's bank, not their intent, and Stripe
    retries for days. `canceled` is what stops service."""
    state = _state(status="past_due", past_due=True)
    with patch("helixis.billing.gate.load_state", AsyncMock(return_value=state)):
        assert await check_turn_allowed("c1") is None


# ── Direction 2: the refusals that must actually fire ─────────────────────


@pytest.mark.asyncio
async def test_an_expired_trial_is_refused():
    state = _state(
        plan=PLANS["trial"],
        status="trialing",
        turns_allowed=150,
        trial_ends_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    with patch("helixis.billing.gate.load_state", AsyncMock(return_value=state)):
        block = await check_turn_allowed("c1")
    assert block is not None and block.reason == "trial_expired"


@pytest.mark.asyncio
async def test_a_canceled_subscription_is_refused():
    with patch(
        "helixis.billing.gate.load_state", AsyncMock(return_value=_state(status="canceled"))
    ):
        block = await check_turn_allowed("c1")
    assert block is not None and block.reason == "subscription_canceled"


@pytest.mark.asyncio
async def test_running_out_of_turns_is_refused():
    with patch(
        "helixis.billing.gate.load_state",
        AsyncMock(return_value=_state(turns_used=400, turns_allowed=400)),
    ):
        block = await check_turn_allowed("c1")
    assert block is not None and block.reason == "turns_exhausted"


@pytest.mark.asyncio
async def test_cancellation_outranks_exhaustion():
    """⚠ Both conditions true at once. They need OPPOSITE actions from the
    user — restart a subscription vs upgrade a plan — so a cancelled customer
    told they are 'out of questions' is sent to do the wrong thing."""
    state = _state(status="canceled", turns_used=999, turns_allowed=400)
    with patch("helixis.billing.gate.load_state", AsyncMock(return_value=state)):
        block = await check_turn_allowed("c1")
    assert block is not None and block.reason == "subscription_canceled"


def test_every_refusal_names_a_way_out():
    """A block that says no without saying how is a support ticket."""
    for state in (
        _state(status="canceled"),
        _state(
            plan=PLANS["trial"],
            status="trialing",
            turns_allowed=150,
            trial_ends_at=datetime.now(UTC) - timedelta(days=1),
        ),
        _state(turns_used=400, turns_allowed=400),
    ):
        import asyncio

        with patch("helixis.billing.gate.load_state", AsyncMock(return_value=state)):
            block = asyncio.run(check_turn_allowed("c1"))
        assert block is not None
        assert "Billing" in block.message, block.message


# ── Metering ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_blank_turn_is_not_billable():
    """⚠ Gotcha 37 measured 22% of runs returning nothing. Charging for our
    own timeout is the fastest way to make the meter feel dishonest — so the
    counting query must exclude a run with no text AND no card tool."""
    pool = MagicMock()
    captured: dict[str, str] = {}

    async def _fetchrow(sql, *args):
        if "helixis_billing" in sql:
            return {
                "plan": "starter",
                "status": "active",
                "seats": 1,
                "trial_ends_at": None,
                "period_start": datetime.now(UTC),
                "period_end": None,
                "turn_limit_override": None,
            }
        captured["sql"] = sql
        return {"n": 7}

    pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    with patch("helixis.billing.gate.db_engine.get_pool", return_value=pool):
        state = await load_state("c1")

    assert state is not None and state.turns_used == 7
    sql = captured["sql"]
    # ⚠ THIS ASSERTION USED TO READ `"final_text" in sql and "tool_calls" in
    # sql`, WHICH THE DEFECT SATISFIED. Both strings were in the broken query —
    # it counted `jsonb_array_length(tool_calls) > 0`, i.e. any tool at all, so
    # every deadline death that made a specialist consult billed as a real
    # turn. Gotcha 40's substring trap, in the guard written for this property.
    # Re-pointed, never deleted (gotchas 89/93/95); the named-set form and the
    # corpus rule live in `test_billable_turn.py`.
    assert "final_text" in sql, "prose is one of the two ways a turn answers"
    assert "jsonb_array_length" not in sql, (
        "counting ANY tool bills the user for our own timeout — the second arm "
        "must name the tools whose output actually reaches them (gotcha 56)."
    )
    # And it must be company-scoped (gotcha 47).
    assert "company_id = $1" in sql


@pytest.mark.asyncio
async def test_unreadable_usage_counts_as_zero():
    """Same asymmetry: a failed count must not become a lockout."""
    pool = MagicMock()

    async def _fetchrow(sql, *args):
        if "helixis_billing" in sql:
            return {
                # The PER-SEAT rung, so the allowance below exercises the
                # multiply. That was `starter` until the 2026-09-04 ladder
                # rename made it the flat $50 tier — using it here now would
                # assert 150 and quietly stop testing seat multiplication.
                "plan": "pro",
                "status": "active",
                "seats": 2,
                "trial_ends_at": None,
                "period_start": datetime.now(UTC),
                "period_end": None,
                "turn_limit_override": None,
            }
        raise RuntimeError("count failed")

    pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    with patch("helixis.billing.gate.db_engine.get_pool", return_value=pool):
        state = await load_state("c1")
    assert state is not None
    assert state.turns_used == 0
    assert state.turns_allowed == 800  # 400 x 2 seats


@pytest.mark.asyncio
async def test_an_override_beats_the_plan_allowance():
    pool = MagicMock()

    async def _fetchrow(sql, *args):
        if "helixis_billing" in sql:
            return {
                "plan": "starter",
                "status": "active",
                "seats": 1,
                "trial_ends_at": None,
                "period_start": datetime.now(UTC),
                "period_end": None,
                "turn_limit_override": 9000,
            }
        return {"n": 0}

    pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    with patch("helixis.billing.gate.db_engine.get_pool", return_value=pool):
        state = await load_state("c1")
    assert state is not None and state.turns_allowed == 9000


# ── Webhook signature ─────────────────────────────────────────────────────
#
# ⚠ These prove the algorithm is SELF-CONSISTENT, not that it matches Stripe:
# stripe.com is egress-blocked from this container (gotcha 75), so the recipe
# came from documentation. A wrong recipe rejects every real webhook — loud
# and safe rather than quiet — and the first live delivery is what settles it.


def _sig(secret: str, payload: bytes, ts: int | None = None) -> tuple[str, bytes]:
    ts = ts or int(time.time())
    return f"t={ts},v1={stripe_signature.compute_signature(secret, str(ts), payload)}", payload


def test_a_genuine_signature_verifies():
    header, payload = _sig("whsec_x", b'{"id":"evt_1"}')
    assert stripe_signature.verify("whsec_x", header, payload)


def test_a_tampered_body_is_rejected():
    header, _ = _sig("whsec_x", b'{"amount":100}')
    assert not stripe_signature.verify("whsec_x", header, b'{"amount":999999}')


def test_the_wrong_secret_is_rejected():
    header, payload = _sig("whsec_x", b"{}")
    assert not stripe_signature.verify("whsec_other", header, payload)


def test_a_replayed_event_is_rejected():
    """⚠ The whole reason the timestamp is signed. Without the window a
    captured `invoice.paid` renews an account forever."""
    old = int(time.time()) - 10_000
    header, payload = _sig("whsec_x", b"{}", ts=old)
    assert not stripe_signature.verify("whsec_x", header, payload)


def test_a_future_timestamp_is_rejected():
    """Guarded in BOTH directions — a forward-dated signature would otherwise
    stay valid indefinitely."""
    header, payload = _sig("whsec_x", b"{}", ts=int(time.time()) + 10_000)
    assert not stripe_signature.verify("whsec_x", header, payload)


def test_both_signatures_are_tried_during_a_secret_rotation():
    """⚠ Stripe sends two v1 values while a signing secret is rotating.
    Reading only the first makes a routine rotation look like an attack and
    silently drops half the events."""
    ts = str(int(time.time()))
    good = stripe_signature.compute_signature("whsec_new", ts, b"{}")
    header = f"t={ts},v1=deadbeef,v1={good}"
    assert stripe_signature.verify("whsec_new", header, b"{}")


def test_missing_or_malformed_headers_are_rejected():
    payload = b"{}"
    for header in ("", "garbage", "t=123", "v1=abc", "t=notanumber,v1=abc"):
        assert not stripe_signature.verify("whsec_x", header, payload), header
    # And an unconfigured secret can never verify anything.
    real, _ = _sig("whsec_x", payload)
    assert not stripe_signature.verify("", real, payload)
