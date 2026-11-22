"""A card in 3-D Secure is not a cancelled subscription.

Stripe's `incomplete` means the FIRST payment has not settled yet — the bank is
running a verification step, or the payment method is asynchronous. It lasts at
most 23 hours before becoming `incomplete_expired`.

`_SERVING_STATUSES` holds only `trialing` / `active` / `past_due`, so the gate
fell through to `subscription_canceled` and told the customer *"Your Occupella
subscription has ended. Start it again in Settings → Billing."* Both halves are
wrong: it never ended, and following that instruction starts a SECOND
subscription — which `create_checkout` now refuses with a 409, so the old
message sent a paying customer to an error message.

These stay REFUSING. The turn is still blocked, because the money has genuinely
not arrived; what changes is that the reason we give is true and the pointer
goes somewhere that can help.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from helixis.billing.gate import BillingState, check_turn_allowed
from helixis.billing.plans import PLANS


def _state(status: str) -> BillingState:
    return BillingState(
        plan=PLANS["pro"],
        status=status,
        seats=1,
        turns_used=0,
        turns_allowed=1000,
        trial_ends_at=datetime.now(UTC) + timedelta(days=7),
        period_end=datetime.now(UTC) + timedelta(days=29),
        past_due=False,
    )


async def _block(status: str):
    with patch("helixis.billing.gate.load_state", AsyncMock(return_value=_state(status))):
        return await check_turn_allowed("co_1")


@pytest.mark.asyncio
async def test_incomplete_is_never_described_as_ended() -> None:
    block = await _block("incomplete")
    assert block is not None, "an unpaid first invoice must still refuse the turn"
    assert block.reason == "payment_incomplete", (
        f"reported {block.reason!r} — 'subscription_canceled' tells somebody "
        "mid-bank-redirect to start another subscription, which 409s"
    )
    assert "ended" not in block.message.lower()
    assert "has ended" not in block.message.lower()


@pytest.mark.asyncio
async def test_incomplete_points_at_the_portal_not_the_plan_picker() -> None:
    """⚠ The pointer is load-bearing, not phrasing. The open invoice lives in
    the Stripe portal and can be retried there; the plan picker would create a
    second subscription, which is the defect this message exists to stop."""
    block = await _block("incomplete")
    assert block is not None
    assert "manage billing" in block.message.lower(), (
        "the message does not name the portal, so the only action it offers is "
        "the one that mints a duplicate subscription"
    )


@pytest.mark.asyncio
async def test_incomplete_expired_says_it_never_started() -> None:
    """The same first payment, definitively failed. "Ended" is still the wrong
    word — but here the action IS to begin again."""
    block = await _block("incomplete_expired")
    assert block is not None
    assert block.reason == "payment_incomplete"
    assert "never completed" in block.message.lower()
    assert "settings → billing" in block.message.lower()


@pytest.mark.asyncio
async def test_a_genuine_cancellation_still_reads_as_one() -> None:
    """⚠ The other direction. A rule that renamed every non-serving status
    would lose the message a real cancellation needs — gotcha 38's shape, where
    fixing one direction of a guard broke the other."""
    block = await _block("canceled")
    assert block is not None
    assert block.reason == "subscription_canceled"
    assert "has ended" in block.message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["trialing", "active", "past_due"])
async def test_the_serving_statuses_are_untouched(status: str) -> None:
    assert await _block(status) is None


@pytest.mark.asyncio
async def test_unpaid_points_at_the_portal_not_the_plan_picker() -> None:
    """`unpaid` is Stripe giving up on dunning, NOT a cancellation.

    The subscription still exists and carries an open invoice, so "start it
    again in Settings → Billing" sends the customer to mint a second one —
    which `create_checkout` 409s on. Same reasoning as the `incomplete` branch
    above and the same pointer: the portal is where the invoice can be paid.

    ⚠ This state was UNREACHABLE until migration 0097. The CHECK constraint
    could not store `unpaid`, so the row kept `past_due` — which IS in
    `_SERVING_STATUSES` — and the product was served free and indefinitely to a
    customer nobody was billing. The branch and the migration are two halves of
    one fix; neither works alone.
    """
    block = await _block("unpaid")
    assert block is not None, "an unpaid subscription must refuse the turn"
    assert block.reason == "subscription_unpaid", (
        f"reported {block.reason!r} — folding this into 'subscription_canceled' "
        "gives the customer an action that fails"
    )
    assert "manage billing" in block.message.lower()


@pytest.mark.asyncio
async def test_paused_still_refuses_with_the_generic_message() -> None:
    """The third status 0097 made storable, and deliberately WITHOUT its own
    branch: Stripe pauses collection through settings this deployment does not
    configure, so a bespoke sentence would be prose describing a state nobody
    can produce. What matters is that it refuses rather than falling into a
    serving branch."""
    block = await _block("paused")
    assert block is not None
    assert block.reason == "subscription_canceled"
