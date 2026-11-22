"""May this company run a chat turn, and how much of its allowance is left.

Two rules run everything in this module, and both are asymmetric on purpose.

**1. Unreadable means ALLOW, not refuse.**
Every other fail-closed gate in this product (the consent ledger, the role
floor, the A2P check) guards an action that is unlawful or irreversible when
taken wrongly. This one guards revenue. A database blip that silently locks a
paying customer out of the product costs more than the handful of turns an
outage could give away, and it is invisible to us and infuriating to them.
So a lookup failure logs and lets the turn through.

**2. `past_due` keeps serving — for a bounded window, not forever.**
A failed card is a payment problem, not a permission problem — cards expire,
banks decline, and Stripe retries for days. Cutting service the hour an
invoice fails punishes the customer for their bank. Stripe moves the
subscription to `canceled` when it genuinely gives up, and THAT is normally
what stops service.

⚠ **But only if Stripe is configured to escalate.** Smart Retries run ~4
attempts over 2-3 weeks and then do whatever the dashboard says: cancel, mark
`unpaid`, or **leave the subscription `past_due` indefinitely**. On that last
setting — and it is a dashboard toggle nothing in this repo can see (gotcha
29's rule pointed at Stripe) — a customer whose card died keeps the full
product forever, because `past_due` is in `_SERVING_STATUSES` and
`past_due_since` was written by two webhook handlers and read by NOBODY.

So `PAYMENT_GRACE_DAYS` is a **backstop, not a policy**: it is deliberately
longer than Stripe's own retry window, so in every correctly-configured
deployment Stripe cancels first and this never fires. It exists for the
deployment where Stripe was left on "do nothing", which is the default nobody
changes.

⚠ A billable turn is a row in `orchestrator_runs`, counted live. There is no
second counter, deliberately: two sources of truth for the same number is the
class of bug gotchas 25 and 53 keep recording, and the one that decides
whether somebody can use what they paid for is the worst place to have it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from helixis.billing.plans import PAID_PLANS, Plan, plan_for
from helixis.billing.stripe_status import subscription_can_bill
from helixis.common.answered import CARD_TOOLS
from helixis.db import engine as db_engine

logger = logging.getLogger(__name__)

# Statuses that keep the product open. `past_due` is in here for rule 2.
_SERVING_STATUSES = frozenset({"trialing", "active", "past_due"})

#: How long a `past_due` company keeps full service. See rule 2 — this is a
#: backstop for a Stripe account configured never to escalate, so it is set
#: LONGER than Stripe's own ~2-3 week retry window on purpose. Shortening it
#: below that window turns a backstop into a second, competing dunning policy
#: and starts cutting customers off while their bank is still retrying.
PAYMENT_GRACE_DAYS = 21

#: How close to the end of the trial the in-app countdown starts. Days, not
#: hours: a property manager who opens Occupella on Monday and again on Friday
#: must see the warning at least once, and a 48-hour window can fall entirely
#: between two visits.
TRIAL_NOTICE_DAYS = 5


@dataclass(frozen=True)
class BillingState:
    plan: Plan
    status: str
    seats: int
    turns_used: int
    turns_allowed: int
    trial_ends_at: datetime | None
    period_end: datetime | None
    past_due: bool
    has_billing_account: bool = False
    """Has this company ever been a Stripe customer?

    ⚠ IT EXISTS BECAUSE A CANCELLED CUSTOMER READS AS A TRIALLING ONE. The
    `customer.subscription.deleted` handler sets `plan = 'trial'` deliberately
    (fall back to the smallest allowance, never `pro`), so `plan` alone cannot
    tell a company that has never paid from one that paid for a year and
    stopped. The billing panel hid the Stripe-portal link on that reading — so
    at the exact moment somebody most needs receipts, and when a chargeback is
    most likely, the only route to their invoices and payment history
    disappeared.

    ⚠ The client must NOT reconstruct this from `plan` + `status`. That is
    gotcha 73's rule: two fields the browser combines are two fields that drift
    from what the server means. This is the server's own answer.

    ⚠ Defaults FALSE, and that is the safe direction: an older row or a
    degraded read hides a link rather than offering a portal session for a
    customer Stripe has never heard of, which would open on an error.
    """

    past_due_since: datetime | None = None
    """When the first invoice failed. NULL while the card is working.

    ⚠ Defaulted so every existing construction site keeps compiling, but the
    default is the SAFE direction here: a state built without it reads as "not
    delinquent" and nobody is cut off by a field a caller forgot to pass.
    """

    has_subscription: bool = False
    """Is a Stripe subscription id stored on this row — live or dead?

    ⚠ ON ITS OWN THIS ANSWERS NOTHING USEFUL, which is the whole point of
    keeping it private to `can_change_plan` below. Three callers used to read
    the id straight off the row as though it meant "has a subscription", and an
    `incomplete_expired` one — a first payment that definitively failed, id
    still on the row since migration 0097 let the status be stored at all —
    satisfied every one of them.

    ⚠ Defaults FALSE so a degraded read reports no subscription, which sends a
    customer to Checkout. Being wrong that way costs Stripe a duplicate it will
    refuse to create for a customer who already has one; the other default
    strands somebody who is trying to start paying.
    """

    @property
    def turns_remaining(self) -> int:
        return max(self.turns_allowed - self.turns_used, 0)

    @property
    def can_change_plan(self) -> bool:
        """Is there a subscription `/billing/change-plan` could actually re-price?

        The one question the billing panel needs and the one it was not asked.
        It read `plan === 'trial'` instead — true of a company that never
        subscribed, and FALSE of one whose first payment expired, because the
        plan they picked is still on the row. So that customer was offered
        "Change plan", and every button under it pushed a re-price at a
        subscription that never charged.
        """
        return self.has_subscription and subscription_can_bill(self.status)

    @property
    def is_trial(self) -> bool:
        return self.plan.key == "trial"

    @property
    def trial_days_left(self) -> int | None:
        """Whole days until the trial ends, floored at 0. `None` when this is
        not a dated trial — an undated one has not started a clock, and
        rendering "0 days left" for it would announce an expiry that
        `_trial_expired` deliberately refuses to enforce."""
        if not self.is_trial or self.trial_ends_at is None:
            return None
        return max((self.trial_ends_at - datetime.now(UTC)).days, 0)

    @property
    def grace_days_left(self) -> int | None:
        """Whole days of service left on a failed card. `None` when the card
        is fine or the delinquency has no recorded start."""
        if not self.past_due or self.past_due_since is None:
            return None
        elapsed = (datetime.now(UTC) - self.past_due_since).days
        return max(PAYMENT_GRACE_DAYS - elapsed, 0)


@dataclass(frozen=True)
class BillingBlock:
    """Why a turn was refused. `None` from `check_turn_allowed` means allowed."""

    reason: str
    """Machine-readable: 'trial_expired' | 'subscription_canceled' | 'turns_exhausted'
    | 'payment_failed' | 'payment_incomplete' | 'subscription_unpaid'.

    ⚠ Mirrored by `BillingBlock` in `frontend/src/services/billing.ts`, which is
    a second copy TypeScript cannot import.

    ⚠ THIS DOCSTRING PREVIOUSLY NAMED `test_billing_plans.py` AS THE GUARD FOR
    THAT, AND IT WAS NOT ONE. That file compares PLAN KEYS against the client
    and has never looked at a reason code — so the sentence asserting gotcha
    28's both-sides rule was itself gotcha 48's shape: a claim that cannot
    fail, inside the note invoking the rule it was breaking. Found while adding
    `subscription_unpaid`, i.e. by the exact change the missing guard existed
    to catch. The real guard is `test_billing_block_reasons.py`, which walks
    THIS module's `BillingBlock(...)` call sites and compares them with the
    client's union."""
    message: str
    """What the user reads. Says what happened and what fixes it — never both blame and no exit."""


async def load_state(company_id: str) -> BillingState | None:
    """Read a company's billing row. `None` means unreadable — callers ALLOW."""
    try:
        pool = db_engine.get_pool()
        row = await pool.fetchrow(
            """
            select plan, status, seats, trial_ends_at, period_start, period_end,
                   turn_limit_override, past_due_since,
                   -- ⚠ The BOOLEAN, never the id. This answers one question —
                   -- "has this company ever been a Stripe customer" — and the
                   -- id itself has no business on a status payload the browser
                   -- reads. See `BillingState.has_billing_account` for what
                   -- depends on it.
                   (stripe_customer_id is not null) as has_billing_account,
                   -- ⚠ The BOOLEAN again, same reason. Paired with `status`
                   -- it answers "is there a subscription anyone could still be
                   -- charged on", which is what decides between opening a
                   -- Checkout and re-pricing — see `can_change_plan`.
                   (stripe_subscription_id is not null) as has_subscription
            from helixis_billing
            where company_id = $1
            """,
            company_id,
        )
    except Exception:
        logger.exception("billing: could not read state for company=%s", company_id)
        return None

    if row is None:
        # No row at all. A company created before 0087's backfill, or one whose
        # provisioning half-failed. Treated as unreadable rather than as
        # "unpaid": inventing a lockout from a missing row would refuse a
        # customer we have no evidence against.
        # ⚠ ERROR, because THIS LINE IS THE FREE ACCOUNT. Gotcha 81 is the
        # record: a missing row plus this deliberate fail-open composes into
        # unmetered service that nothing anywhere reports. The allow is right
        # and stays; what was wrong is that it announced itself at a level
        # Sentry keeps as a breadcrumb rather than an event, so the one
        # observable symptom of the gap went to a log nobody reads.
        #
        # It fires per turn for an affected company, which Sentry groups into a
        # single issue — noisier than a warning and far cheaper than the state
        # it describes. `helixis_billing_missing` should hold this at zero;
        # this is the runtime half of that same invariant.
        logger.error("billing: no row for company=%s — allowing", company_id)
        return None

    plan = plan_for(row["plan"])
    seats = int(row["seats"] or 1)
    allowance = row["turn_limit_override"] or plan.turn_allowance(seats)

    used = await _turns_used(company_id, row["period_start"])

    return BillingState(
        plan=plan,
        status=str(row["status"]),
        seats=seats,
        turns_used=used,
        turns_allowed=int(allowance),
        trial_ends_at=row["trial_ends_at"],
        period_end=row["period_end"],
        past_due=str(row["status"]) == "past_due",
        # ⚠ `.get` is wrong on an asyncpg Record and a bare index raises on a
        # deploy that has not applied the column yet. Both halves of that
        # matter: raising here returns None from the caller, which ALLOWS
        # (rule 1) — so a missing column degrades to "not delinquent" rather
        # than to a lockout, which is the direction this module always takes.
        past_due_since=row["past_due_since"] if "past_due_since" in row else None,
        has_billing_account=(
            bool(row["has_billing_account"]) if "has_billing_account" in row else False
        ),
        has_subscription=(bool(row["has_subscription"]) if "has_subscription" in row else False),
    )


async def _turns_used(company_id: str, period_start: datetime | None) -> int:
    """Billable turns this period.

    ⚠ A turn the user never got an answer from is NOT billable. Gotcha 37
    measured 22% of runs hitting the deadline with nothing returned, and
    gotcha 56 gives the test for it: a run is blank only when `final_text` is
    empty AND no card tool ran, because a turn can legitimately answer
    entirely in cards. Charging for a blank turn bills somebody for our own
    timeout, which is the fastest way to make a usage meter feel dishonest.

    ⚠ THIS PARAGRAPH DESCRIBED A RULE THE QUERY BELOW DID NOT IMPLEMENT.
    The second arm read `jsonb_array_length(tool_calls) > 0` — ANY tool, not a
    CARD tool — so a deadline death that spent four minutes on specialist
    consults and returned zero characters counted as a billable turn. That is
    precisely the 22% population the paragraph cites, billed by the query that
    quotes it. The named set now comes from `common/answered.py`, which is the
    same set `evals/cost_drift.answered` uses, so the meter and the latency
    sample cannot drift into disagreeing about what an answer is.

    ⚠ BOUND, never interpolated. A hardcoded name list here is a second copy
    of the set one import away, and its failure mode is silent in the
    expensive direction: a card tool renamed upstream stops matching and every
    card-only turn starts reading as blank.

    ⚠ The alias is `tool_call`, not `call`. Whether Postgres accepts `call`
    there was not checkable from the session that wrote this — no database, no
    SQL parser installed — and a reserved-word collision would raise on the
    live metering path rather than anywhere a test could see it. Gotcha 79's
    rule: a remembered keyword list is not evidence, and the unambiguous
    spelling costs nothing.
    """
    if period_start is None:
        return 0
    try:
        pool = db_engine.get_pool()
        row = await pool.fetchrow(
            """
            select count(*) as n
            from orchestrator_runs
            where company_id = $1
              and started_at >= $2
              and (
                    coalesce(final_text, '') <> ''
                 or exists (
                        select 1
                          from jsonb_array_elements(
                                   coalesce(tool_calls, '[]'::jsonb)
                               ) as tool_call
                         where tool_call ->> 'name' = any($3::text[])
                    )
              )
            """,
            company_id,
            period_start,
            sorted(CARD_TOOLS),
        )
    except Exception:
        logger.exception("billing: could not count turns for company=%s", company_id)
        # Unreadable usage reads as ZERO, which allows. Same asymmetry as the
        # state read: never manufacture a lockout from a failed query.
        return 0
    return int(row["n"]) if row else 0


async def check_turn_allowed(company_id: str) -> BillingBlock | None:
    """`None` to proceed, a `BillingBlock` to refuse.

    Order is deliberate: subscription state before usage. Someone whose
    subscription is cancelled should be told that, not told they are out of
    turns — the two need opposite actions from them.
    """
    state = await load_state(company_id)
    if state is None:
        return None  # rule 1

    # ⚠ `incomplete` is NOT a cancellation and must never be described as one.
    # It means Stripe is still waiting on the FIRST payment — a card in 3-D
    # Secure verification, or an async payment method — and it lasts at most
    # 23 hours before becoming `incomplete_expired`. Telling somebody mid-bank-
    # redirect that their subscription "has ended" is both false and alarming,
    # and the action it gives them is the wrong one: starting another checkout
    # is what used to mint a SECOND subscription (create_checkout now 409s on
    # exactly that, so the old message sent them to an error instead).
    #
    # The pointer is the PORTAL rather than the plan picker, because the portal
    # is where the open invoice lives and where a payment can be retried. There
    # is nothing for us to do here but wait and say so accurately.
    if state.status == "incomplete":
        return BillingBlock(
            "payment_incomplete",
            "Your bank is still confirming the first payment, so Occupella is "
            "paused for a moment. This usually clears within a few minutes. If "
            "it doesn't, open Settings → Billing → Manage billing to finish or "
            "retry the payment — nothing has been charged twice.",
        )

    # The same first payment, definitively failed. "Ended" is still the wrong
    # word — it never started — and the action IS to begin again.
    if state.status == "incomplete_expired":
        return BillingBlock(
            "payment_incomplete",
            "That subscription was never completed — the first payment didn't "
            "go through in time. Pick a plan again in Settings → Billing; "
            "everything Occupella has already synced is still here.",
        )

    # ⚠ `unpaid` is not a cancellation either, and "start it again" is the
    # wrong instruction for it. It means Stripe exhausted dunning and stopped
    # collecting, leaving OPEN INVOICES on a subscription that still exists —
    # so the plan picker would try to mint a second one, which `create_checkout`
    # 409s on. The portal is where those invoices can actually be paid. Same
    # reasoning as the two `incomplete` branches above: the block is correct in
    # all three cases and only the sentence differs, because each one needs the
    # reader to do something different.
    #
    # ⚠ Reachable only since migration 0097. The CHECK constraint could not
    # store `unpaid`, so the row kept `past_due` — which IS in the serving set,
    # meaning the product was handed out free to a customer nobody was billing.
    # This branch is the half that refuses; the migration is the half that lets
    # the status arrive at all.
    if state.status == "unpaid":
        return BillingBlock(
            "subscription_unpaid",
            "We couldn't collect payment, so Occupella is paused and there's an "
            "unpaid invoice on the account. Open Settings → Billing → Manage "
            "billing to settle it — your data is exactly where you left it.",
        )

    # `paused` lands here. Stripe pauses collection through settings this
    # deployment does not configure, so it should never arrive; the generic
    # sentence is accurate if it does, and inventing a fourth message for a
    # state we do not create would be prose nobody can check.
    if state.status not in _SERVING_STATUSES:
        return BillingBlock(
            "subscription_canceled",
            "Your Occupella subscription has ended. Start it again in "
            "Settings → Billing and your data is exactly where you left it.",
        )

    if state.is_trial and _trial_expired(state):
        return BillingBlock(
            "trial_expired",
            "Your free trial has ended. Pick a plan in Settings → Billing to "
            "carry on — everything Occupella has already synced is still here.",
        )

    # AFTER cancellation and trial expiry, BEFORE the usage count. A customer
    # whose card is failing must be told that, not told they are out of turns:
    # one is fixed by updating a card and the other by buying more, and giving
    # the wrong instruction sends them to the wrong page.
    if state.grace_days_left == 0:
        return BillingBlock(
            "payment_failed",
            "We haven't been able to charge your card for "
            f"{PAYMENT_GRACE_DAYS} days, so Occupella is paused. Update your "
            "payment method in Settings → Billing and everything picks up "
            "where it left off — nothing has been deleted.",
        )

    if state.turns_remaining <= 0:
        if state.is_trial:
            return BillingBlock(
                "turns_exhausted",
                "You've reached the usage included in the free trial. Pick a plan in "
                "Settings → Billing to keep going.",
            )
        return BillingBlock(
            "turns_exhausted",
            f"You've reached this period's usage limit on the {state.plan.label} "
            "plan. Settings → Billing can add seats or move you up a plan.",
        )

    return None


def _trial_expired(state: BillingState) -> bool:
    """⚠ A trial with NO end date has not expired.

    A null `trial_ends_at` is a provisioning gap, not an expiry, and reading it
    as "expired" would lock out every company whose row was written before the
    field was set — turning one missing timestamp into a total outage.
    """
    if state.trial_ends_at is None:
        return False
    return datetime.now(UTC) >= state.trial_ends_at


def is_paid(state: BillingState | None) -> bool:
    """True only for a company on a paid plan in a serving state."""
    return state is not None and state.plan.key in PAID_PLANS and state.status in _SERVING_STATUSES
