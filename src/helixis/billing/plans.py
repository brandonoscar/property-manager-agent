"""What each plan costs, what it includes, and what a billable turn IS.

The limits live here rather than in the database on purpose: a cap stored per
row drifts per customer with no audit trail, while a plan NAME stored per row
resolves to today's limit for everyone at once.

⚠ THE NUMBERS BELOW ARE PRICED AGAINST A MEASUREMENT, NOT A GUESS. Live
`orchestrator_runs`, 343 priced runs, 2026-08-26..09-02, all companies:

    p50 $0.1068 · p90 $0.4921 · p99 $1.0884 · max $4.3439 · MEAN $0.2143

The mean is roughly 2x the median — a fat tail, and the tail is what you
actually pay for. So every headroom calculation here uses the MEAN. Gotcha 37
is the standing warning against characterising this cost from a single day;
the window above spans eight.

⚠ Re-derive these before changing a price. `scripts/cost_drift_check.py` and
the query in this module's test are the two ways to do it. A price set against
a cost that has since moved is gotcha 48's expired-justification shape pointed
at revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The measurement this file is priced against. Kept as data so the test can
# re-derive the headroom claims instead of trusting the prose above.
MEASURED_MEAN_TURN_USD: Final = 0.2143
MEASURED_AT: Final = "2026-08-26..2026-09-02, n=343 priced runs"

TRIAL_DAYS: Final = 14
TRIAL_TURNS: Final = 150
"""Enough to evaluate the product, not enough to run a portfolio on for free.

⚠ 14 days rather than 7 because the onboarding wizard has an explicit "I need
my admin or partner to generate these keys" path — a customer waiting on
somebody else's Buildium admin can burn a 7-day clock before seeing a single
answer, and a trial that expires before first value is a trial that taught
them nothing.
"""


FEATURE_CRM: Final = "crm"
"""The Leasing pipeline — lead inbox, texts, calls, applicant tracking.

⚠ A feature key is a CAPABILITY THE SERVER ENFORCES, never a bullet on a
pricing card. `crm.py` refuses every authenticated Leasing route for a plan
that does not carry this, so adding a key here without a matching refusal
somewhere ships a promise the product does not keep — and adding a bullet to
`highlights` without a key ships one the product cannot revoke. Bullets live
in `highlights`; only enforced capabilities live in `features`.
"""

ALL_FEATURES: Final = (FEATURE_CRM,)


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    price_usd: int
    per_seat: bool
    turns_included: int
    """Turns per SEAT when `per_seat`, otherwise pooled across the account."""

    features: frozenset[str] = frozenset()
    """Enforced capabilities. See `FEATURE_CRM` — not marketing copy."""

    tagline: str = ""
    """One line: who this plan is FOR. The pricing page's whole job."""

    highlights: tuple[str, ...] = ()
    """What you get, in the customer's words.

    ⚠ NEVER restate a price or an allowance here. Those are rendered from
    `price_usd` / `turns_included` by whoever draws the card, so there is one
    copy of each number; a bullet spelling one out is the hand-copied price
    table that `test_plan_figures_have_one_source` exists to prevent, wearing
    a different hat. A test asserts no digit-bearing figure appears in this
    tuple.
    """

    max_seats: int = 50
    """Ceiling on billable/allowanced seats — a blast radius, not a product cap.

    ⚠ Seats multiply BOTH the price and the allowance, so an unbounded count
    (a buggy import, a script, an abusive signup) either bills someone
    thousands of dollars or hands out thousands of turns of model spend. A
    customer genuinely this large is a conversation, not a webhook.

    ⚠ The TRIAL's ceiling is the one that has to be tight, because there its
    seats multiply spend with NO revenue on the other side — see the trial
    entry below.
    """

    def turn_allowance(self, seats: int) -> int:
        return (
            self.turns_included * max(1, min(seats, self.max_seats))
            if self.per_seat
            else self.turns_included
        )

    def includes(self, feature: str) -> bool:
        return feature in self.features


PLANS: Final[dict[str, Plan]] = {
    # ⚠ NO LEASING ON THE TRIAL, and the reason is arithmetic rather than
    # packaging (founder call, 2026-09-04). Texting a lead requires A2P 10DLC
    # carrier approval, which takes ten to fifteen days; the trial is
    # fourteen. So a trialist granted Leasing gets a section that cannot send
    # anything before their trial ends — a feature that exists only as a
    # setup checklist they never finish.
    #
    # This reverses the decision made hours earlier in this same file, which
    # granted the trial every feature so nobody would fail to discover
    # Leasing. That reasoning was sound about DISCOVERY and wrong about
    # DELIVERY: showing somebody a door they cannot open in the time they have
    # is worse than describing the room. /features and /pricing describe it.
    # ⚠ PER-SEAT, and it is the only free plan that is (founder call,
    # 2026-09-04: "the trial needs seats, it may cost but anyone that gets to
    # that point may be good"). A team evaluating together used to SHARE 150
    # turns — 30 each across five people, which is not enough to form a view of
    # the product, and the shape of trial that loses the accounts most worth
    # winning.
    #
    # ⚠ `max_seats=5` IS THE WHOLE SAFETY OF THIS DECISION, because trial seats
    # multiply model spend against ZERO revenue. At the measured mean
    # ($0.2143/turn) five seats is 750 turns ≈ $161 of spend per trialling
    # company, and that is the deliberate ceiling. At the default 50 it would
    # be $1,607 per signup, which anyone could take by inviting fifty
    # addresses. Raising this number is buying leads with model spend — a
    # decision to take on purpose with a conversion rate in hand, never a
    # tidy-up.
    "trial": Plan(
        "trial",
        "Free trial",
        0,
        per_seat=True,
        max_seats=5,
        turns_included=TRIAL_TURNS,
        features=frozenset(),
        tagline="The everyday work, for two weeks.",
        highlights=(
            "Buildium, the Inbox, reporting and documents",
            "No card, no commitment",
        ),
    ),
    # $50 flat, 150 pooled — the same allowance as the trial, every month.
    #
    # Priced at the measured mean: 150 x $0.2143 = $32.15 of model spend
    # against $50, less ~$4.40 of per-customer fixed cost that does NOT
    # amortise under white-label (Stripe's 2.9% + $0.30, the A2P campaign's
    # $1.50/mo, a number at ~$1.15/mo). That leaves ~27% gross margin.
    #
    # ⚠ 27% is roughly HALF what Pro earns, and that is the honest cost of
    # a $50 entry point on Sonnet 5. Cutting the cap to 100 turns would return
    # ~48%, and the reason it is not 100 is the ladder: the free trial gives
    # 150, so a paid tier below that reads as a downgrade on the pricing page
    # even though the trial's 150 are once and these recur. Whoever changes
    # this number is choosing between margin and that first impression — it is
    # a product decision, not an oversight.
    #
    # ⚠ FLAT, never per-seat. Three per-seat $50 seats would be 450 turns for
    # $150 against Pro's 400 for $199, which prices the ladder backwards.
    # Pooled at 150, a team that shares it gets an allowance too small to work
    # on and moves up by itself — the cap does the segmenting, so no seat
    # limit is needed.
    #
    # ⚠ NO `FEATURE_CRM`. Leasing is the upgrade reason this tier exists to
    # create: at 27% margin Starter earns roughly half what Pro does, so the
    # cheapest rung has to buy something less, and a whole outbound-messaging
    # section is a cleaner line to draw than a smaller allowance nobody feels
    # until the month is over. It is also the honest one — Leasing carries a
    # per-customer A2P campaign and a phone number, real recurring cost that
    # $50 does not cover.
    "starter": Plan(
        "starter",
        "Starter",
        50,
        per_seat=False,
        turns_included=150,
        features=frozenset(),
        tagline="One person, the everyday work.",
        # ⚠ No "Leasing is not included" bullet, deliberately. What a plan
        # does and does not unlock is rendered from `features`, so the card
        # says it from the same set the API refuses on — a hand-written
        # exclusion is a claim that keeps its old wording the day the
        # capability moves rung.
        highlights=(
            "Ask anything about your Buildium portfolio",
            "Inbox, reminders, documents and reporting",
        ),
    ),
    # $199 / seat with 400 turns each is ~18 per business day — a daily driver
    # for one person. Cost at the measured mean is ~$85.72/seat, so ~57% gross
    # margin. That is thinner than SaaS-typical and it is honest: the lever
    # that widens it is cutting cost per turn, not raising the cap.
    "pro": Plan(
        "pro",
        "Pro",
        199,
        per_seat=True,
        turns_included=400,
        features=frozenset(ALL_FEATURES),
        tagline="A team working the whole portfolio.",
        highlights=(
            "Everything in Starter",
            "Your own number for texts and calls to leads",
            "The allowance is per person, so it grows as the team does",
        ),
    ),
    # $500 flat, marketed as unlimited, ceilinged at 1,200 pooled.
    #
    # ⚠ Do NOT sell this as literally unlimited: at the measured mean a flat
    # $500 breaks even near 2,300 turns/month, and an unlimited plan is
    # adversely selected by precisely the customers who would pass that — a
    # fixed price offered to your most expensive accounts.
    #
    # ⚠ THE CEILING MUST SIT BELOW BREAK-EVEN, and this shipped at 2,500 —
    # ABOVE it — for a few hours on 2026-09-02. At that cap a customer who
    # used everything they paid for cost $535.75 against $500: the plan was
    # profitable only because most people would not finish it, which is a
    # utilisation BET dressed as a guarantee. 1,200 costs $257.16, so every
    # Scale customer is profitable even at 100% use.
    #
    # 1,200 is also the floor the LADDER needs. Two Pro seats already
    # carry 800 turns for $398, so a Scale cap below that gives nobody a reason
    # to move up; at 1,200 the comparison is three Pro seats ($597) for
    # the same allowance, and Scale is plainly the better deal. A cap chosen for
    # margin alone would land near 700 and quietly kill the upgrade path.
    #
    # The durable fix is neither number: at $0.10/turn instead of $0.2143 this
    # cap could double at the same margin. Cost per turn is the lever
    # (task #21 — one specialist ships a 180k-token tool list).
    "scale": Plan(
        "scale",
        "Scale",
        500,
        per_seat=False,
        turns_included=1200,
        features=frozenset(ALL_FEATURES),
        tagline="High volume, one predictable bill.",
        highlights=(
            "Everything in Pro",
            "Add the whole team — the allowance is pooled, not charged per person",
            "One flat price, whatever the month looks like",
        ),
    ),
}

PAID_PLANS: Final = ("starter", "pro", "scale")
"""Purchasable, in LADDER ORDER — the billing panel renders them in this order.

⚠ Every entry needs a Stripe price id (`BillingSettings.price_ids`) or its
checkout 503s. `test_billing_plans.py` reads BOTH sides so a plan added here
without one fails a test rather than a customer's card.
"""


def plan_for(key: str) -> Plan:
    """Resolve a stored plan key, falling back to trial on anything unknown.

    ⚠ Falls back to the MOST RESTRICTIVE plan, never the most generous. A row
    carrying a plan this build does not know about is a row written by a newer
    deploy or corrupted; either way, handing it `scale`'s allowance would let an
    unknown state buy the largest ceiling we sell.
    """
    return PLANS.get(key, PLANS["trial"])


def cheapest_plan_with(feature: str) -> Plan | None:
    """The cheapest PURCHASABLE plan carrying `feature`, or `None` if none does.

    Derives the upgrade target so nothing has to restate it: the Leasing
    refusal used to name "Pro" as a hardcoded string, and
    `LeasingComplianceGate` had already worked out why that is wrong —
    "a client that wrote 'upgrade to Pro' itself would still say Pro". The
    server was the caller that rule never reached (gotchas 25/67).

    ⚠ MINIMUM BY PRICE, never the first `PAID_PLANS` entry that matches. That
    tuple's own docstring says it is ALSO the order the billing panel renders,
    so there is a live reason to reorder it — "put the popular tier first" —
    that has nothing to do with price. Taking the first match would then quote
    a dearer plan than the customer needs, silently, on the one sentence whose
    job is to tell them what to buy. The name of this function is the property
    it computes (`test_the_cheapest_carrier_is_actually_the_cheapest`).
    `test_the_cheapest_cap_does_not_read_as_a_downgrade_from_the_trial` had
    already reached for `min()` for the same reason.

    ⚠ Deliberately skips the trial. It is not purchasable, so it can never be
    an upgrade target, and naming it as one would tell a Starter customer to
    move DOWN to get a capability.
    """
    carriers = [PLANS[key] for key in PAID_PLANS if PLANS[key].includes(feature)]
    return min(carriers, key=lambda p: p.price_usd, default=None)


def plan_includes(key: str, feature: str) -> bool:
    """Does the plan stored on this company carry `feature`?

    Resolves through `plan_for`, so an unknown stored key inherits the TRIAL's
    features rather than the richest ones — and since 2026-09-04 the trial
    carries NONE, so an unreadable plan key closes every gated section.

    ⚠ That is the opposite direction from `gate.py`, which fails OPEN, and the
    difference is deliberate. `gate.py` guards the METER: an outage there
    blacks out a paying customer's whole product, so it lets turns through. A
    feature key guards a CAPABILITY the customer may simply not have bought,
    and the fail-open version of that is handing Leasing to a $50 account. The
    live path never depends on this anyway — `crm.py` reads the plan off a
    `BillingState` and treats an unreadable ROW as allow; this fallback only
    fires on a plan key no build recognises.
    """
    return plan_for(key).includes(feature)
