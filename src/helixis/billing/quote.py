"""What adding people to the team does to the bill, stated before it happens.

⚠ **This exists because the product raised a bill nobody agreed to.** Pro is
priced per seat, so an admin inviting four colleagues moved a test account from
$199/month to $995/month — a five-fold increase, correctly computed, correctly
prorated by Stripe, and never once shown to the person who caused it. The
invite dialog asked for addresses and a role; the number appeared later on a
Billing page they had no reason to open (founder report, 2026-09-09).

⚠ **The estimate is computed HERE, not fetched from Stripe.** Stripe already
does the real arithmetic — every quantity write goes up with
``proration_behavior="create_prorations"`` — and it is the authority on what is
actually charged. What was missing is a number to show *before* the write, and
previewing one costs a live API call on a path that must not fail: an outage on
the preview would either block the invite or, worse, let it through with no
figure and no consent. A day-count proration against the period we already
store answers the same question offline, and the copy calls it an estimate.

⚠ **Three plan shapes, three honest answers, and only one of them is a
charge.** Inviting somebody changes the bill ONLY on a per-seat plan with a
live subscription, below the seat ceiling. On a flat plan (Starter, Scale) it
changes nothing and the quote must say so rather than showing $0.00, which
reads as a broken calculation. On a trial there is no subscription to prorate;
the seats are free and capped. A quote that invents a charge for those is worse
than no quote — it would talk somebody out of inviting a colleague they can
add for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from helixis.billing.plans import Plan
from helixis.billing.seats import billable_seats


@dataclass(frozen=True)
class SeatQuote:
    """What ``adding`` more people costs, before anyone is invited."""

    adding: int
    """How many people the caller asked about. Echoed so a client cannot
    render a quote against a different number than it requested."""

    seats_now: int
    seats_after: int

    monthly_now_usd: int
    monthly_after_usd: int

    billable: bool
    """True when this invite actually moves money.

    ⚠ False is the COMMON case and is not an error: flat plans, trials, and
    any invite that lands entirely above the plan's seat ceiling. The client
    branches on this rather than on ``monthly_after_usd > monthly_now_usd``,
    because those two are equal in every non-billable case and a client
    re-deriving it would have to know the plan rules — the exact duplication
    that lets two answers drift (gotcha 25).
    """

    prorated_now_usd: float | None
    """Rough charge for the rest of this period, or ``None`` when unanswerable.

    ⚠ ``None`` is NEVER rendered as zero. It means we could not read the
    period, and a missing number shown as $0.00 is a claim that the invite is
    free — the one direction that cannot be walked back once somebody has
    acted on it (gotcha 55).
    """

    days_left: int | None
    period_end: str | None

    reason: str
    """Why this quote is shaped the way it is, for the client's own copy.

    One of: ``per_seat`` · ``flat_plan`` · ``trial`` · ``at_seat_cap`` ·
    ``no_subscription``. A client showing prose keyed on this cannot invent a
    fourth explanation the server does not recognise.
    """


def _days_between(start: datetime, end: datetime) -> int:
    return max(0, (end - start).days)


def seat_change_quote(
    plan: Plan,
    *,
    member_count: int,
    adding: int,
    has_subscription: bool,
    period_end: datetime | None,
    now: datetime | None = None,
    period_start: datetime | None = None,
) -> SeatQuote:
    """Price ``adding`` more members on ``plan``, without touching Stripe.

    ⚠ Seats are computed through ``billable_seats`` on BOTH sides rather than
    by adding ``adding`` to a count. That helper owns the flat-plan rule and
    the per-plan ceiling, so a quote derived any other way would disagree with
    the quantity actually pushed to Stripe — a bill that does not match the
    screen that authorised it.
    """
    now = now or datetime.now(UTC)
    adding = max(0, adding)

    seats_now = billable_seats(plan, member_count)
    seats_after = billable_seats(plan, member_count + adding)

    monthly_now = plan.price_usd * seats_now if plan.per_seat else plan.price_usd
    monthly_after = plan.price_usd * seats_after if plan.per_seat else plan.price_usd

    if not plan.per_seat:
        reason = "flat_plan"
    elif plan.price_usd == 0:
        # The trial is per-seat AND free — the one plan where both are true.
        reason = "trial"
    elif seats_after == seats_now:
        reason = "at_seat_cap"
    elif not has_subscription:
        # Checkout reads live membership, so the FIRST bill already includes
        # them. Nothing is charged today and nothing is prorated.
        reason = "no_subscription"
    else:
        reason = "per_seat"

    billable = reason == "per_seat"

    prorated: float | None = None
    days_left: int | None = None
    if period_end is not None:
        days_left = _days_between(now, period_end)
        if billable:
            # A month, measured from the period we actually store rather than
            # assumed at 30 — a 28-day February billed as 30 under-charges by
            # 7% every time, silently and in the same direction.
            span = _days_between(period_start, period_end) if period_start else 30
            if span > 0:
                added = monthly_after - monthly_now
                prorated = round(added * (days_left / span), 2)

    return SeatQuote(
        adding=adding,
        seats_now=seats_now,
        seats_after=seats_after,
        monthly_now_usd=monthly_now,
        monthly_after_usd=monthly_after,
        billable=billable,
        prorated_now_usd=prorated,
        days_left=days_left,
        period_end=period_end.isoformat() if period_end else None,
        reason=reason,
    )
