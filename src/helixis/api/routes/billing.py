"""Billing — subscription status, Checkout, the portal, and Stripe's webhook.

* ``GET  /billing/status``           — any member. Plan, usage, and the BLOCK.
* ``GET  /billing/upcoming-invoice`` — any member. Stripe's own next-bill figure.
* ``GET  /billing/seat-quote``       — any member. What N more people costs, BEFORE inviting.
* ``POST /billing/checkout``         — admin. Returns a Stripe Checkout URL.
* ``POST /billing/change-plan``      — admin. Re-prices the live subscription.
* ``POST /billing/portal``           — admin. Returns a billing-portal URL.
* ``POST /billing/webhook``          — Stripe. Signature-verified, unauthenticated.

⚠ ``/upcoming-invoice`` is separate from ``/status`` ON PURPOSE. ``/status`` is
fetched by ``TrialBanner``, which renders on every page; a Stripe round trip in
it would put Stripe's latency and Stripe's outages in front of every page load.
Only the billing panel asks for the invoice.

⚠ ``/status`` returns the gate's OWN answer (``block``), never the ingredients
for the client to re-derive one. Gotcha 73 is this repo's record of exactly
that mistake: ``GET /sms/status`` handed back two booleans, the client guessed
which blocker had fired, and the panel's explanation diverged from the refusal
it was explaining. A status field the enforcement path disagrees with is worse
than no status field.

⚠ Checkout and the portal are ADMIN-only, and that is a stronger floor than
managing your own settings: both change what the company is charged. Reading
status is not — a member seeing "412 of 1200 requests used" needs no
privilege and prevents the support ticket.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from helixis.api.auth import (
    AuthenticatedUser,
    get_current_user,
    require_unambiguous_company,
)
from helixis.api.roles import ADMIN, normalize_role, role_at_least
from helixis.billing import stripe_signature
from helixis.billing.card_expiry import expires_before_charge, expiry_label
from helixis.billing.gate import check_turn_allowed, load_state
from helixis.billing.notices import BillingNotice, notice_for
from helixis.billing.plans import ALL_FEATURES, PAID_PLANS, PLANS, Plan, plan_for
from helixis.billing.quote import seat_change_quote
from helixis.billing.seats import billable_seats, count_members
from helixis.billing.stripe_client import StripeClient
from helixis.billing.stripe_errors import StripeFailure
from helixis.billing.stripe_status import storable_status, subscription_can_bill
from helixis.db.engine import get_pool
from helixis.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Where Stripe sends somebody back to after Checkout or the billing portal.
#
# ⚠ THE WEB APP, NOT THE MARKETING SITE. Every person who reaches Stripe from
# here is already signed in and mid-payment; landing them on the public landing
# page is a dead end at the exact moment they are handing us money. Before the
# 2026-09-15 domain swap the apex WAS this app, so the old literal was right and
# stopped being right without changing — which is why it is a named constant
# read in both places rather than a string typed twice (gotcha 25's fan-out: two
# copies of one fact, nothing comparing them).
#
# ⚠ It is only the FALLBACK. `billing.portal_return_url` overrides it and is
# unset in every config file here, so this literal is the live value unless
# somebody set that env var in the Render dashboard (gotchas 29/80 — a flag's
# live value is not what the repo says).
_RETURN_URL = "https://app.occupella.com"


def _settings() -> Settings:
    return Settings()


async def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=403, detail="No company on this account")
    return str(user.company_ids[0])


async def _spending_company_id(user: AuthenticatedUser) -> str:
    """For the two endpoints that move money — Checkout and the portal.

    ⚠ Deliberately NOT `_company_id` (which takes [0]). Metering does not use
    [0]: `/agent/run` takes `body.company_id`, validates membership, and
    `begin_run` writes the billable row against THAT company — which is what
    `gate._turns_used` counts. So a caller holding two companies could pay for
    one while burning the other's allowance, see plenty of headroom on the
    Billing page, and get a 402 in chat with nothing on screen explaining it.

    `/status` keeps [0] on purpose: reading a plan is harmless and a member
    seeing the wrong meter says something. Charging for the wrong one is what
    has to refuse.
    """
    return require_unambiguous_company(user)


async def _require_admin(user: AuthenticatedUser) -> str:
    """Admin floor, FAIL CLOSED — an unreadable role is refused.

    ⚠ The asymmetry against ``gate.py`` is deliberate and worth stating.
    The gate fails OPEN because a database blip must not lock a paying
    customer out of the product. This fails CLOSED because it authorises a
    CHARGE, and an unverifiable role must never be able to start one.
    """
    company_id = await _spending_company_id(user)
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            "select role from company_users where company_id = $1 and user_id = $2",
            company_id,
            str(user.user_id),
        )
    except Exception:
        logger.exception("billing: role lookup failed for user=%s", user.user_id)
        raise HTTPException(status_code=503, detail="Could not verify your role") from None
    role = normalize_role(row["role"]) if row else None
    if not role or not role_at_least(role, ADMIN):
        raise HTTPException(status_code=403, detail="Only an admin can change billing")
    return company_id


class PlanOut(BaseModel):
    """One purchasable plan, as FACTS rather than as marketing copy.

    ⚠ The client formats the sentence; the SERVER owns every number in it.
    Until 2026-09-02 the panel carried its own ``$199 / user / month`` and
    ``400 requests per user`` strings, hand-copied from ``plans.py``, with
    nothing holding the two in agreement — and ``plans.py``'s own docstring
    invites re-derivation before a price change. So the drift was invited on
    one side and invisible on the other: a customer would read the old price
    on the card and Stripe would charge the new one, which is a billing
    dispute rather than an ordinary bug.

    Gotchas 50/51/77/78 all end at the same move — put the field on the row
    and the join disappears. There is nothing to keep in sync now because
    there is only one copy.
    """

    key: str
    label: str
    price_usd: int
    per_seat: bool
    turns_included: int
    max_seats: int
    """The plan's seat ceiling — a clamp on BOTH the invoice and the allowance.

    ⚠ Surfaced because the invite screen has to state it. `billable_seats` and
    `Plan.turn_allowance` both clamp here, so a company past this number is
    neither billed nor allowanced for the extra people — which is a fact about
    what somebody is buying, not an implementation detail. It matters most on
    the TRIAL (5), where a sixth teammate silently adds no allowance at all;
    without this field the client cannot know that number exists.
    """

    tagline: str
    """Who the plan is for. Server-owned for the same reason the price is."""
    highlights: list[str]
    """What you get, as sentences. ⚠ Carries no price and no allowance — the
    client renders those from the numbers above, so there is one copy."""
    features: list[str]
    """Enforced capability keys (see `plans.FEATURE_CRM`). The client uses
    these to say what a plan unlocks; the SERVER uses the same set to refuse
    the routes. One source, so a card cannot promise a section the API
    declines."""


def _plan_out(p: Plan) -> PlanOut:
    """One plan as facts.

    ⚠ ONE projection, used by both the catalog and `current_plan`. Two copies
    would drift the day a field is added — and the field most likely to be added
    is a number a customer reads.
    """
    return PlanOut(
        key=p.key,
        label=p.label,
        price_usd=p.price_usd,
        per_seat=p.per_seat,
        turns_included=p.turns_included,
        max_seats=p.max_seats,
        tagline=p.tagline,
        highlights=list(p.highlights),
        features=sorted(p.features),
    )


def _plan_catalog() -> list[PlanOut]:
    """The purchasable plans, in ladder order.

    Trial is deliberately absent: it cannot be bought, and rendering it as a
    choice would offer a customer a downgrade into a 14-day clock.
    """
    return [_plan_out(PLANS[k]) for k in PAID_PLANS]


class NoticeOut(BaseModel):
    reason: str
    severity: str
    message: str
    action: str | None = None


class StatusOut(BaseModel):
    plan: str
    plan_label: str
    status: str
    seats: int
    turns_used: int
    turns_allowed: int
    turns_remaining: int
    trial_ends_at: str | None
    period_end: str | None
    past_due: bool
    block: str | None
    """The gate's own reason code, or null. NOT for the client to re-derive."""
    block_message: str | None
    enforced: bool
    """False while billing is wired but not yet turning anyone away."""
    has_billing_account: bool = False
    """Has this company ever been a Stripe customer? See `BillingState`.

    ⚠ It is a BOOLEAN and never the customer id: this answers one question and
    the id has no business on a payload the browser reads.

    ⚠ Do NOT let the client reconstruct it from `plan` + `status`. A cancelled
    subscription is stored as `plan='trial', status='canceled'` on purpose, so
    the two fields together are exactly the ambiguity this field removes — and
    gotcha 73 is this repo's record of a client deriving a billing state from
    two booleans and drifting from what the server meant.
    """
    features: list[str]
    """What THIS company's current plan unlocks — the server's own answer.

    ⚠ Same rule as `block` (gotcha 73): the client must not re-derive this by
    looking `plan` up in `plans` below. The catalog holds only PURCHASABLE
    plans, so a company on the trial would find no row for itself and conclude
    it has no features — and the day a customer is granted something off-ladder
    the two answers diverge with nothing failing. The enforcement path and the
    UI read one field.
    """
    plans: list[PlanOut]
    """What can be bought, priced by the server. See `PlanOut`."""
    current_plan: PlanOut
    """THIS company's plan as facts — including the trial, which `plans` omits.

    ⚠ The invite screen needs to know whether adding a teammate costs money,
    how much, and where the seat ceiling is. It cannot get that from `plans`:
    that list is the purchasable LADDER and deliberately has no trial row, so a
    trialling company looking itself up there finds nothing and would have to
    guess — and the trial is exactly the plan whose seat cap (5) is tight enough
    to matter.

    ⚠ It also cannot be derived from `seats`. That field is the quantity STRIPE
    is billing, synced back by the subscription webhook; it is not the plan's
    per-seat price and not its ceiling.

    Redundant with `plan` / `plan_label` on purpose — those two are read by
    existing callers and stay. This is one object with the whole shape rather
    than three more scalars that would each need keeping in step with `PlanOut`.
    """
    notice: NoticeOut | None = None
    """The standing warning BEFORE a block — trial ending, card failed.

    ⚠ Same rule as `block` and `features`: the client must NOT re-derive this
    from `trial_ends_at` + `past_due`. Those two fields cannot express the
    grace window's remaining days, and a client that computes its own deadline
    will eventually disagree with the one `check_turn_allowed` enforces — the
    UI counting down to a date the gate does not use.
    """
    can_change_plan: bool = False
    """Is there a subscription that `/billing/change-plan` can actually re-price?

    The panel has to choose between opening a Stripe Checkout and switching an
    existing subscription, and picking wrongly is a double charge in one
    direction and a dead end in the other. It asked `plan === 'trial'`, which
    is a different question and answers this one wrongly for the customer whose
    first payment expired: their plan is whatever they picked, so the panel
    headed the section "Change plan" and every button pushed a re-price at a
    subscription that never charged — a 502 reading "Could not change the plan
    with Stripe", when nothing is wrong with Stripe.

    ⚠ Same rule as `block`, `features` and `notice`: the client must NOT
    re-derive this from `plan` + `status`. The terminal-status set lives in
    `billing/stripe_status.py` next to the vocabulary it belongs to and next to
    the migration's CHECK constraint that is already pinned against it; a
    second copy in TypeScript is the drift gotcha 28 exists to stop, on the
    field that decides whether a customer is charged twice.
    """


def _notice_out(notice: BillingNotice | None) -> NoticeOut | None:
    if notice is None:
        return None
    return NoticeOut(
        reason=notice.reason,
        severity=notice.severity,
        message=notice.message,
        action=notice.action,
    )


@router.get("/status", response_model=StatusOut)
async def billing_status(user: AuthenticatedUser = Depends(get_current_user)) -> StatusOut:
    settings = _settings()
    company_id = await _company_id(user)
    state = await load_state(company_id)

    if state is None:
        # Unreadable. Report the trial's shape rather than inventing a lockout
        # — same posture as the gate, which allows in this case.
        trial = PLANS["trial"]
        return StatusOut(
            plan="trial",
            plan_label=trial.label,
            status="trialing",
            seats=1,
            turns_used=0,
            turns_allowed=trial.turns_included,
            turns_remaining=trial.turns_included,
            trial_ends_at=None,
            period_end=None,
            past_due=False,
            # ⚠ FALSE on an unreadable row, deliberately. Offering a portal
            # link for a company Stripe may never have heard of opens on an
            # error; hiding one for a real customer costs a click through
            # Settings. Same fail-toward-the-recoverable-mistake direction as
            # every other default in this branch.
            has_billing_account=False,
            block=None,
            block_message=None,
            # ⚠ FALSE on an unreadable row, for the same reason as the portal
            # link above but with a sharper consequence: this branch reports
            # the TRIAL's shape, and offering "Change plan" against a
            # subscription we could not read sends a re-price at an id we do
            # not have. Checkout's own 409 is the backstop if a subscription
            # does turn out to exist.
            can_change_plan=False,
            enforced=settings.billing.enforce,
            # ⚠ EVERY capability, stated explicitly — NOT `trial.features`.
            #
            # This used to read the trial's set and was correct only because
            # the trial happened to carry everything. On 2026-09-04 the trial
            # dropped Leasing (carrier approval outlasts fourteen days), and
            # that one-line packaging change would silently have turned this
            # branch from fail-OPEN into fail-CLOSED: the panel hiding Leasing
            # while `crm._primary_company_id` — which allows on an unreadable
            # row — went on serving it. A UI that hides a section the API
            # answers is its own kind of wrong, and it would have shipped with
            # nothing failing.
            #
            # So the fail-open decision is made HERE rather than inherited from
            # whichever plan happens to be the fallback. A blip must not black
            # out a section somebody is paying for.
            features=sorted(ALL_FEATURES),
            plans=_plan_catalog(),
            # ⚠ The trial's REAL seat terms, not the fail-open feature set
            # above. Those diverge deliberately: `features` is widened here so a
            # blip cannot black out a section somebody pays for, while the seat
            # cap is a number the invite screen quotes to an admin — inflating
            # THAT on an unreadable row would tell them their trial covers more
            # people than it does, which is a promise rather than a permission.
            current_plan=_plan_out(trial),
        )

    block = await check_turn_allowed(company_id)
    return StatusOut(
        plan=state.plan.key,
        plan_label=state.plan.label,
        status=state.status,
        seats=state.seats,
        turns_used=state.turns_used,
        turns_allowed=state.turns_allowed,
        turns_remaining=state.turns_remaining,
        trial_ends_at=state.trial_ends_at.isoformat() if state.trial_ends_at else None,
        period_end=state.period_end.isoformat() if state.period_end else None,
        past_due=state.past_due,
        block=block.reason if block else None,
        block_message=block.message if block else None,
        enforced=settings.billing.enforce,
        features=sorted(state.plan.features),
        plans=_plan_catalog(),
        current_plan=_plan_out(state.plan),
        has_billing_account=state.has_billing_account,
        notice=_notice_out(notice_for(state)),
        can_change_plan=state.can_change_plan,
    )


class CheckoutIn(BaseModel):
    plan: str

    # ⚠ `seats` IS GONE FROM THIS MODEL, deliberately (2026-09-04). It used to
    # be a number the browser sent, which meant the bill was whatever the
    # client claimed and had no relationship to the actual team. Same move as
    # the A2P form's server-filled fields: a value a caller can override is a
    # value that can be wrong, and REMOVING the field removes the failure
    # rather than validating it. The quantity is now derived from membership
    # at the moment of checkout.
    #
    # Removing it also settles the UX complaint that started this — there is no
    # quantity stepper to render, because there is no number to ask for.


class UrlOut(BaseModel):
    url: str


@router.post("/checkout", response_model=UrlOut)
async def create_checkout(
    payload: CheckoutIn, user: AuthenticatedUser = Depends(get_current_user)
) -> UrlOut:
    settings = _settings()
    if not settings.billing.configured:
        raise HTTPException(status_code=503, detail="Billing is not set up on this deployment")

    company_id = await _require_admin(user)

    plan = plan_for(payload.plan)
    if plan.key not in PAID_PLANS:
        # ⚠ `plan_for` falls back to TRIAL on anything unknown, so this also
        # catches a junk plan key — it arrives as "trial", which is not
        # purchasable. Never name the plans in the message: the catalog is
        # `_plan_catalog()` and a hand-written list drifts the moment one is
        # added, which is the drift PlanOut's docstring exists to stop.
        raise HTTPException(status_code=422, detail="Pick a plan from the billing page")

    price_id = settings.billing.price_ids.get(plan.key, "")
    if not price_id:
        # ⚠ Refuse rather than sending Stripe a blank price. Gotcha 77's rule:
        # a body known to fail should not be spent on a round trip that
        # returns an error naming a field the customer cannot act on.
        logger.error("billing: no Stripe price id configured for plan=%s", plan.key)
        raise HTTPException(status_code=503, detail="That plan is not available yet")

    pool = get_pool()

    # Derived from who is ACTUALLY in the company, never from the request.
    # `billable_seats` also applies the flat-plan rule (Starter and Scale are
    # per-account, so always quantity 1) and the plan's own ceiling.
    quantity = billable_seats(plan, await count_members(pool, company_id))
    row = await pool.fetchrow(
        """
        select b.stripe_customer_id, b.stripe_subscription_id, b.status, c.name
        from helixis_billing b join companies c on c.id = b.company_id
        where b.company_id = $1
        """,
        company_id,
    )
    company_name = (row["name"] if row else None) or "Occupella customer"
    customer_id = row["stripe_customer_id"] if row else None

    # ⚠ REFUSE A SECOND SUBSCRIPTION. Checkout CREATES; it cannot change a plan,
    # and Stripe will happily give one customer two live subscriptions. The
    # panel reads "Change plan" for anyone already paying and offers a button on
    # every other plan, so this was one click away: the customer pays for both,
    # and the resulting `customer.subscription.created` overwrites our
    # `stripe_subscription_id` with the new one — after which the ORIGINAL is
    # invisible to this product and bills forever with nothing in the app able
    # to see it. Switching is `/billing/change-plan`, which re-prices the
    # subscription that already exists.
    #
    # ⚠ AND THE REASON IS "BILLS FOREVER", so the test is whether the stored
    # subscription can still bill — NOT whether we stored an id. A first
    # payment that expired (`incomplete_expired`) leaves the id on the row with
    # a subscription that never charged and never will, and refusing here told
    # that customer "you already have a subscription" while `gate.py`, one file
    # over, was telling them "that subscription was never completed — pick a
    # plan again". Their other exit is `change_plan`, which would push a
    # re-price at a dead subscription and 502; its own comment already says a
    # "lapsed company" belongs in checkout. Both doors shut, on somebody trying
    # to start paying us.
    if row is not None and row["stripe_subscription_id"] and subscription_can_bill(row["status"]):
        raise HTTPException(
            status_code=409,
            detail=(
                "You already have a subscription — change your plan instead of starting a new one."
            ),
        )

    client = StripeClient(settings.billing.stripe_secret_key)

    if not customer_id:
        customer = await client.create_customer(
            company_id=company_id, name=company_name, email=user.email or ""
        )
        if not customer:
            raise HTTPException(status_code=502, detail="Could not reach Stripe")
        customer_id = customer["id"]
        # UPSERT, not UPDATE. A plain update touches nothing when the row is
        # missing, so the Stripe customer id would be silently discarded and
        # the NEXT checkout would mint a second Stripe customer for the same
        # company — after which the webhook's stripe_customer_id lookup
        # resolves nothing. 0088's trigger means the row is always there; this
        # is the second line, because the failure is invisible and lands on a
        # customer in the act of paying us. Same shape as gotcha 73's STOP
        # revocation with no consent row to land on.
        #
        # The insert names no trial fields: the table's own defaults supply
        # plan/status/period_start, and a NULL trial_ends_at reads as "no
        # expiry recorded" (gate._trial_expired), which allows. The trial
        # LENGTH stays in exactly one place — migration 0088.
        await pool.execute(
            "insert into helixis_billing (company_id, stripe_customer_id) "
            "values ($1, $2) "
            "on conflict (company_id) do update set "
            "stripe_customer_id = excluded.stripe_customer_id, updated_at = now()",
            company_id,
            customer_id,
        )

    base = settings.billing.portal_return_url or _RETURN_URL

    async def _open_checkout(cid: str, failure: StripeFailure | None = None) -> dict | None:
        return await client.create_checkout_session(
            customer_id=cid,
            price_id=price_id,
            quantity=quantity,
            success_url=f"{base}?billing=success",
            cancel_url=f"{base}?billing=cancelled",
            company_id=company_id,
            failure=failure,
        )

    failure = StripeFailure()
    session = await _open_checkout(str(customer_id), failure)

    # ⚠ A STORED CUSTOMER ID STRIPE HAS NEVER HEARD OF IS NOT FALSY, so the
    # `if not customer_id` branch above — the one that already knows how to
    # mint a customer and record it — could never fire for the company that
    # needed it most. Measured (Sentry PYTHON-FASTAPI-3N): an id created under
    # a TEST key, read back under a LIVE one, which Stripe refuses by name.
    # Every billing call for that company then failed identically and forever,
    # and this route answered 502 "Could not start checkout" — which reads as
    # *Stripe is down, try later* to somebody in the act of paying us. It never
    # clears. Retrying fails the same way every time.
    #
    # The row is a CACHE of a Stripe fact; Stripe is the source of truth. When
    # Stripe positively says the cached id names nothing, the cache is wrong
    # and re-minting is the same act the empty-id branch already performs.
    #
    # ⚠ ONLY on positive evidence. `customer_gone` recognises one error shape
    # and declines on timeouts, 500s and anything unrecognised (see
    # billing/stripe_errors.py) — because a FALSE positive mints a second
    # Stripe customer for a company that already has a working one, which is
    # gotcha 81's duplicate-customer harm: the webhook's customer lookup then
    # resolves nothing. A missed one costs only the 502 we already had.
    #
    # ⚠ Reachable only when there is NO stored subscription id — the 409 above
    # returns first. A company carrying BOTH a stale customer and a stale
    # subscription is stuck there, and is deliberately not repaired here: see
    # the note on `portal`.
    if session is None and failure.customer_gone:
        logger.warning(
            "billing: stored stripe customer for company=%s no longer exists; re-minting",
            company_id,
        )
        customer = await client.create_customer(
            company_id=company_id, name=company_name, email=user.email or ""
        )
        if not customer:
            raise HTTPException(status_code=502, detail="Could not reach Stripe")
        customer_id = customer["id"]
        await pool.execute(
            "insert into helixis_billing (company_id, stripe_customer_id) "
            "values ($1, $2) "
            "on conflict (company_id) do update set "
            "stripe_customer_id = excluded.stripe_customer_id, updated_at = now()",
            company_id,
            customer_id,
        )
        # ⚠ Retried ONCE, never in a loop. A second `customer_gone` against an
        # id Stripe handed us seconds earlier is not a stale cache — it is
        # something we do not understand, and looping would mint a fresh
        # customer per attempt.
        session = await _open_checkout(str(customer_id))

    if not session or not session.get("url"):
        raise HTTPException(status_code=502, detail="Could not start checkout")
    return UrlOut(url=str(session["url"]))


class ChangePlanOut(BaseModel):
    plan: str
    plan_label: str
    seats: int


@router.post("/change-plan", response_model=ChangePlanOut)
async def change_plan(
    payload: CheckoutIn, user: AuthenticatedUser = Depends(get_current_user)
) -> ChangePlanOut:
    """Move an existing subscription onto a different plan.

    ⚠ SEPARATE FROM CHECKOUT ON PURPOSE. Checkout creates; this one re-prices
    what already exists. Collapsing them into one endpoint is exactly how the
    double-subscription bug happened — a single "buy" path cannot tell a first
    purchase from an upgrade, and Stripe does not stop you having both.

    ⚠ The RESPONSE is the new plan, not a URL. There is nowhere to redirect: the
    change lands on Stripe immediately and the panel re-reads `/billing/status`.
    A caller that got a URL back would open a Checkout it must not open.

    ⚠ Our own row is NOT written here. `customer.subscription.updated` is the
    single writer of plan/status/seats, and having two writers is how a failed
    Stripe call leaves the app showing a plan the customer is not on. The
    response reports what we ASKED Stripe for, and `/billing/status` reports
    what actually happened once the webhook lands — which is why the panel
    refreshes rather than trusting this body.
    """
    settings = _settings()
    if not settings.billing.configured:
        raise HTTPException(status_code=503, detail="Billing is not set up on this deployment")

    company_id = await _require_admin(user)

    plan = plan_for(payload.plan)
    if plan.key not in PAID_PLANS:
        raise HTTPException(status_code=422, detail="Pick a plan from the billing page")

    price_id = settings.billing.price_ids.get(plan.key, "")
    if not price_id:
        logger.error("billing: no Stripe price id configured for plan=%s", plan.key)
        raise HTTPException(status_code=503, detail="That plan is not available yet")

    pool = get_pool()
    row = await pool.fetchrow(
        "select stripe_subscription_id, status from helixis_billing where company_id = $1",
        company_id,
    )
    subscription_id = row["stripe_subscription_id"] if row else None
    # ⚠ A DEAD SUBSCRIPTION IS THE SAME ANSWER AS NO SUBSCRIPTION, and the
    # comment below already said so — "a trial or LAPSED company" — while the
    # condition only ever caught the null. `customer.subscription.deleted`
    # nulls the id, so `canceled` reached here correctly; `incomplete_expired`
    # keeps its id and fell straight through to a Stripe re-price of a
    # subscription that never charged, which comes back as the 502 below. That
    # is the wrong sentence: nothing is wrong with Stripe.
    if not subscription_id or not subscription_can_bill(row["status"] if row else None):
        # A trial or lapsed company has nothing to re-price. Sending them to
        # checkout is the correct answer and the panel knows to do that, but
        # say so rather than 500ing on a null.
        raise HTTPException(
            status_code=409,
            detail="There's no active subscription to change — pick a plan to start one.",
        )

    # ⚠ Computed for the TARGET plan, not carried over. `billable_seats` applies
    # that plan's own rules: Pro is per-seat so quantity is the headcount, while
    # Starter and Scale are per-account so quantity is 1. Reusing the current
    # subscription's quantity across a Pro → Scale move would bill five Scales.
    quantity = billable_seats(plan, await count_members(pool, company_id))

    client = StripeClient(settings.billing.stripe_secret_key)
    result = await client.switch_subscription_price(
        str(subscription_id), price_id=price_id, quantity=quantity
    )
    if result is None:
        raise HTTPException(status_code=502, detail="Could not change the plan with Stripe")

    logger.info(
        "billing: company=%s switched to plan=%s quantity=%d", company_id, plan.key, quantity
    )
    return ChangePlanOut(plan=plan.key, plan_label=plan.label, seats=quantity)


class UpcomingInvoiceOut(BaseModel):
    """What Stripe says the next bill is — or nothing, said plainly.

    ⚠ Every field is optional and `None` means UNKNOWN, never zero. A customer
    with no subscription, a Stripe outage and a 404 all land here, and reporting
    any of them as "$0.00 due" would be a false statement about money. The panel
    falls back to its own estimate, which is what it already showed.
    """

    total_usd: float | None = None
    """The invoice total AFTER discounts and proration, in dollars.

    ⚠ Stripe sends cents. Dividing here rather than in the browser keeps one
    answer to "how much" — a client doing its own arithmetic on a currency is
    how a bill ends up off by 100x on the one screen nobody would forgive it
    on."""
    at: str | None = None
    """When it will be charged (ISO). Stripe's own date for THIS invoice, which
    can differ from the subscription's period end during a plan change."""
    discounted: bool = False
    """Whether a coupon or promotion code is reducing the total.

    ⚠ Reported so the panel can say the number came from Stripe rather than
    from list price. It exists because enabling promotion codes made the
    panel's own arithmetic capable of overstating a bill."""
    card_expires_first: bool = False
    """The card on file dies BEFORE that charge, so the renewal will decline.

    ⚠ `False` covers both "the card is fine" and "we could not tell", which is
    the one place this model collapses a three-valued answer — and it is safe
    only in this direction: the field decides whether to show a warning, and
    the failure it under-reports is already handled by the dunning path
    (`past_due` + the payment_failed notice). Over-reporting would be a false
    alarm about somebody's money on the screen they trust least."""
    card_expiry: str | None = None
    """`09/2026`, only when the warning fires.

    The format printed on the card, so the reader can compare it against the
    physical card without translating. `None` whenever there is nothing to
    warn about — the panel must not render an expiry it was not asked to act
    on, which would read as a defect rather than information."""


@router.get("/upcoming-invoice", response_model=UpcomingInvoiceOut)
async def upcoming_invoice(
    user: AuthenticatedUser = Depends(get_current_user),
) -> UpcomingInvoiceOut:
    """The real next invoice, on its OWN endpoint.

    ⚠ Deliberately NOT folded into `/billing/status`. That endpoint is fetched
    by `TrialBanner`, which renders on every page, so putting a Stripe round
    trip in it would place Stripe's latency — and Stripe's outages — in front
    of every page load. This is the same lesson as gotcha 37: a check placed on
    the wrong path is paid for by everything that path serves.

    ⚠ Never raises for the ordinary "cannot say" cases. A billing panel that
    errors because a projection was unavailable is worse than one that shows
    the estimate it showed yesterday.
    """
    settings = _settings()
    company_id = await _company_id(user)
    if not settings.billing.configured:
        return UpcomingInvoiceOut()

    pool = get_pool()
    # Company-scoped (gotcha 47): an unscoped read here would hand one
    # customer another's invoice.
    row = await pool.fetchrow(
        "select stripe_customer_id from helixis_billing where company_id = $1", company_id
    )
    customer_id = row["stripe_customer_id"] if row else None
    if not customer_id:
        return UpcomingInvoiceOut()

    client = StripeClient(settings.billing.stripe_secret_key)
    invoice = await client.get_upcoming_invoice(str(customer_id))
    if not invoice:
        return UpcomingInvoiceOut()

    cents = invoice.get("amount_due")
    if cents is None:
        cents = invoice.get("total")
    if not isinstance(cents, int):
        # A total we cannot read is not a total of zero.
        return UpcomingInvoiceOut()

    at = invoice.get("next_payment_attempt") or invoice.get("period_end")
    when = datetime.fromtimestamp(at, tz=UTC).isoformat() if isinstance(at, int | float) else None
    # ⚠ Stripe moved from a single `discount` to a `discounts` list; both
    # spellings are read because which one arrives depends on the API version
    # pinned on the account, which is a dashboard setting and not visible here.
    discounts = invoice.get("discounts") or (
        [invoice["discount"]] if invoice.get("discount") else []
    )

    # ⚠ The card check rides on THIS endpoint and not on `/billing/status`, for
    # the reason stated above: status is fetched by `TrialBanner` on every page
    # load, and a second Stripe round trip there would put Stripe's latency in
    # front of the whole app (gotcha 37 — a check on the wrong path is paid for
    # by everything that path serves). Here it costs one extra call on a page
    # somebody deliberately opened to look at their billing.
    #
    # ⚠ It NEVER fails the response. An unreadable card is not a reason to
    # withhold the invoice total the customer came for, and `expires_before_
    # charge` already answers `None` for every uncertainty.
    expires_first = False
    expiry_text: str | None = None
    charge_at = datetime.fromtimestamp(at, tz=UTC) if isinstance(at, int | float) else None
    try:
        expiry = await client.get_default_card_expiry(str(customer_id))
        if expires_before_charge(expiry, charge_at) and expiry is not None:
            expires_first = True
            expiry_text = expiry_label(expiry)
    except Exception:  # noqa: BLE001 — a warning must never cost the answer
        logger.warning("billing: could not read the card expiry for company=%s", company_id)

    return UpcomingInvoiceOut(
        total_usd=round(cents / 100, 2),
        at=when,
        discounted=bool(discounts),
        card_expires_first=expires_first,
        card_expiry=expiry_text,
    )


class SeatQuoteOut(BaseModel):
    """What inviting ``adding`` more people does to this company's bill."""

    adding: int
    seats_now: int
    seats_after: int
    monthly_now_usd: int
    monthly_after_usd: int
    billable: bool
    prorated_now_usd: float | None = None
    days_left: int | None = None
    period_end: str | None = None
    reason: str


@router.get("/seat-quote", response_model=SeatQuoteOut)
async def seat_quote(
    adding: int = 1,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SeatQuoteOut:
    """Price an invite BEFORE it is sent.

    ⚠ **The invite dialog is required to call this and show the answer.** Pro
    is per-seat, so four invites took a test account from $199/month to $995
    with no screen ever naming a figure — the charge was right and the consent
    was missing (founder report, 2026-09-09).

    ⚠ **No Stripe call.** The monthly figures come from `PLANS` and the
    proration from the period this row already stores, so the quote cannot be
    blocked by a Stripe outage on a path where failing would either stop a
    legitimate invite or let one through unpriced. Stripe stays the authority
    on what is actually charged; this is the estimate the person agrees to.

    ⚠ **Readable by any member**, not admin-gated. It exposes this company's
    own plan price and headcount, both of which the Billing panel already
    shows to everyone, and gating it would leave the invite dialog unable to
    price the very action a MANAGER is allowed to take.
    """
    company_id = await _company_id(user)
    pool = get_pool()
    # Company-scoped (gotcha 47) — an unscoped read prices another tenant's
    # plan against this one's headcount.
    row = await pool.fetchrow(
        "select plan, stripe_subscription_id, status, period_start, period_end "
        "from helixis_billing where company_id = $1",
        company_id,
    )
    # ⚠ A missing row resolves to the TRIAL, matching `gate.py`'s fail-open
    # default. The alternative — refusing to quote — would block the invite
    # dialog entirely for a company whose billing row has not landed yet
    # (gotcha 81's gap), which is a worse failure than quoting the free plan
    # somebody in that state is genuinely on.
    plan = plan_for(str(row["plan"]) if row and row["plan"] else "trial")
    members = await count_members(pool, company_id)
    q = seat_change_quote(
        plan,
        member_count=members,
        adding=max(0, min(adding, 50)),
        # ⚠ A dead subscription cannot be prorated onto. `seat_change_quote`
        # reads this to choose between "charged a part-month today" and
        # "included in the first bill of the next subscription", and for a
        # lapsed company it is always the second — their next act is a fresh
        # checkout, which reads live membership. Passing True here quoted the
        # invite dialog a proration that could never be charged.
        has_subscription=bool(row and row["stripe_subscription_id"])
        and subscription_can_bill(row["status"] if row else None),
        period_end=row["period_end"] if row else None,
        period_start=row["period_start"] if row else None,
    )
    return SeatQuoteOut(
        adding=q.adding,
        seats_now=q.seats_now,
        seats_after=q.seats_after,
        monthly_now_usd=q.monthly_now_usd,
        monthly_after_usd=q.monthly_after_usd,
        billable=q.billable,
        prorated_now_usd=q.prorated_now_usd,
        days_left=q.days_left,
        period_end=q.period_end,
        reason=q.reason,
    )


@router.post("/portal", response_model=UrlOut)
async def create_portal(user: AuthenticatedUser = Depends(get_current_user)) -> UrlOut:
    settings = _settings()
    if not settings.billing.configured:
        raise HTTPException(status_code=503, detail="Billing is not set up on this deployment")

    company_id = await _require_admin(user)
    pool = get_pool()
    row = await pool.fetchrow(
        "select stripe_customer_id from helixis_billing where company_id = $1", company_id
    )
    customer_id = row["stripe_customer_id"] if row else None
    if not customer_id:
        raise HTTPException(status_code=409, detail="No subscription to manage yet")

    client = StripeClient(settings.billing.stripe_secret_key)
    failure = StripeFailure()
    session = await client.create_portal_session(
        customer_id=str(customer_id),
        return_url=settings.billing.portal_return_url or _RETURN_URL,
        failure=failure,
    )
    # ⚠ THE PORTAL DOES NOT RE-MINT, and the asymmetry with checkout is the
    # point. Checkout is about to CREATE a subscription, so a fresh customer is
    # exactly what it needs. The portal exists to manage one that already
    # exists — handing it a brand-new empty customer would open a billing page
    # showing no subscription, no card and no invoices to somebody who is
    # paying us, which is a worse lie than the error.
    #
    # So it says the true thing instead. This is also the one message the
    # company stuck at checkout's 409 (stale customer AND stale subscription
    # id) can actually reach, which is why it names the state rather than
    # blaming Stripe.
    #
    # ⚠ "Retrying will not clear it" is the load-bearing clause. The 502 below
    # reads as *Stripe is down* and sends an admin back to click again forever
    # (gotchas 102/106: an instruction that cannot work is worse than none).
    # Nothing in the product can reset this linkage today, so the message does
    # not pretend otherwise or invent a self-serve step.
    if session is None and failure.customer_gone:
        logger.warning(
            "billing: stored stripe customer for company=%s no longer exists; portal unavailable",
            company_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Your saved Stripe billing link is no longer valid, so the portal "
                "cannot open. Retrying will not clear it — this needs to be reset "
                "on our side."
            ),
        )
    if not session or not session.get("url"):
        raise HTTPException(status_code=502, detail="Could not open the billing portal")
    return UrlOut(url=str(session["url"]))


# ── Stripe webhook ────────────────────────────────────────────────────────


# Price id -> plan key is resolved from settings at call time rather than
# stored, so rotating a price in the dashboard does not orphan live rows.
def _plan_for_price(settings: Settings, price_id: str | None) -> str | None:
    """Which plan a Stripe price id belongs to, or None if we cannot say.

    ⚠ THIS IS THE REVERSE OF `BillingSettings.price_ids`, AND IT IS DERIVED
    FROM IT — never an if-chain. That property's own docstring records this
    exact defect being fixed on the FORWARD side:

        "It used to carry `price_starter if key == "starter" else price_pro`,
        which is a branch that silently mis-charges the moment a third plan
        exists — the new plan falls to the `else` and buys Pro."

    The fix reached checkout and stopped there. This function kept the shape:
    `if starter … if pro … return None`, with no `scale` branch. So a Scale
    customer's `customer.subscription.created` resolved to None, the SQL below
    does `plan = coalesce($3, plan)`, and the row KEPT ITS OLD PLAN — trial.
    Stripe charges $500/month; the gate hands them the trial's 150 requests and
    no Leasing, then locks them out at `turns_exhausted`. That is gotcha 25
    stated exactly: fix a field-shape bug at EVERY layer that reads it.

    ⚠ The guard beside it could not see this. `test_every_purchasable_plan_has_
    a_price_id_field` compares `price_ids` against `PAID_PLANS` — both sides of
    the FORWARD map — and is structurally blind to the reverse one. There is a
    round-trip test now (`test_billing_plans.py`).

    ⚠ An UNRESOLVED price on a live subscription is logged loudly. It means
    Stripe is billing somebody for a price this build cannot name, so the plan
    silently stays whatever it was — which is the failure above. It is not
    raised: the caller must still 2xx or Stripe retries for days and disables
    the endpoint (see `stripe_webhook`).
    """
    if not price_id:
        return None
    # ⚠ EVERY price id that means this plan, not just today's — that is the
    # GRANDFATHERING half. Raising a price means creating a NEW Stripe price
    # (an existing one's amount cannot be edited), and existing subscriptions
    # keep the old id forever. Matching only the configured id therefore makes
    # every long-standing customer resolve to NO plan the moment a price
    # changes: an ERROR on every one of their events, and — worse —
    # `subscription_reconcile` unable to restore a plan it cannot name, so a
    # grandfathered customer who loses a webhook stays on the trial allowance
    # while their card is charged. See `BillingSettings.legacy_price_ids`.
    for key, known in settings.billing.price_ids_by_plan.items():
        if price_id in known:
            return key
    logger.error(
        "billing: price %s maps to no plan this build knows — the company's plan "
        "will keep its previous value. Configure the price id (or add it to "
        "HELIXIS_BILLING__LEGACY_PRICE_IDS if it is a retired price a customer is "
        "grandfathered on), or the customer is paying for one plan and holding "
        "another.",
        price_id,
    )
    return None


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Apply a Stripe subscription event.

    ⚠ Reads the RAW body before parsing. The signature covers the exact bytes
    Stripe sent; parsing and re-serialising changes whitespace and key order,
    so the HMAC would never match and every legitimate event would be
    rejected.

    ⚠ Always returns 2xx once the signature verifies, even when the event is
    one we ignore. A non-2xx makes Stripe retry for days and eventually
    disable the endpoint — so an event type we do not handle must be
    acknowledged, not refused.
    """
    settings = _settings()
    secret = settings.billing.stripe_webhook_secret
    if not secret:
        # No bypass exists here, deliberately (gotchas 68 and 74 both record a
        # test flag reaching a real webhook). Unconfigured means every event
        # is refused, which is the safe direction for the endpoint that
        # decides who has paid.
        raise HTTPException(status_code=403, detail="Billing webhook is not configured")

    raw = await request.body()
    header = request.headers.get("Stripe-Signature", "")
    if not stripe_signature.verify(secret, header, raw):
        logger.warning("billing: rejected a webhook with a bad signature")
        raise HTTPException(status_code=403, detail="Bad signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed event") from None

    etype = str(event.get("type", ""))
    obj: dict[str, Any] = event.get("data", {}).get("object", {}) or {}

    try:
        await _apply_event(settings, etype, obj, event.get("created"))
    except Exception:
        # Logged, then acknowledged. Raising would make Stripe retry an event
        # our own bug cannot process, for days, and eventually disable the
        # endpoint — losing the events that WOULD have applied.
        logger.exception("billing: failed to apply %s", etype)

    return {"received": "true"}


def _warn_if_nothing_matched(etype: str, tag: Any, key: str, value: Any) -> None:
    """An `update … where` that matches no row is this repo's recurring silent
    bug (gotchas 73, 76, 81) and here it decides whether somebody who paid can
    use what they bought.

    Every statement below keys on a STRIPE id rather than on our own
    `company_id`, so "no such row" is a reachable state rather than an
    impossible one — and `pool.execute` reports it as an ordinary success. The
    warning is the only thing that can distinguish "applied" from "landed on
    nothing", so it names the key it looked the row up by; without that the log
    line says an event failed and not which customer to go and repair.

    ⚠ Advisory only. It must never raise: the caller already swallows
    exceptions so Stripe is not made to retry an event our own bug cannot
    process, and a diagnostic that can break the handler it observes is worse
    than no diagnostic. So an unparseable tag is skipped rather than guessed
    at — a fake pool returns a MagicMock, and reading that as "0 rows" would
    make every test emit a false alarm about a row it never had.
    """
    if not isinstance(tag, str) or not tag.upper().startswith("UPDATE "):
        return
    try:
        touched = int(tag.split()[1])
    except (IndexError, ValueError):  # pragma: no cover — asyncpg always counts
        return
    if touched == 0:
        # ⚠ ERROR, not warning, and the level is the whole point of this
        # function. Sentry's logging integration events at ERROR and keeps
        # WARNING as a breadcrumb, so every detector in this file that means
        # "billing state is now wrong and will NOT self-correct" was writing to
        # a log nobody reads — the failure it describes is silent by nature,
        # which is exactly why it needed an alert rather than a line.
        logger.error(
            "billing: %s matched no billing row (%s=%s) — that company's "
            "subscription state is now stale and will not self-correct",
            etype,
            key,
            value,
        )


def _is_subscription_line(line: dict[str, Any]) -> bool:
    """Is this invoice line the RECURRING subscription charge?

    ⚠ Three shapes, because Stripe changed this and we cannot see which API
    version an account is pinned to (stripe.com is egress-blocked from this
    container — gotcha 75, so these came from documentation and not a probe):
    older versions carry `type: "subscription"`, 2025-era ones moved it to
    `parent.type: "subscription_item_details"`, and a `subscription` id on the
    line is a third tell. Any one of them is enough; matching none is not
    proof of the opposite, which is why the caller falls back rather than
    declining.
    """
    if line.get("type") == "subscription":
        return True
    if (line.get("parent") or {}).get("type") == "subscription_item_details":
        return True
    return bool(line.get("subscription"))


def _billing_period_from_invoice(obj: dict[str, Any]) -> dict[str, Any]:
    """The subscription line's period — NOT `lines.data[0]`.

    ⚠ THIS BECAME REACHABLE THE DAY change-plan SHIPPED. An invoice's `lines`
    array is not just the subscription: it also holds PRORATIONS, and a
    proration's `period` is the slice of the cycle being credited or charged
    (say the eleven days left in the month), not the billing period. Taking
    `data[0]` blindly therefore writes a stub window into `period_start` /
    `period_end` — and that pair is what `gate.py` counts turns against, so a
    customer who upgrades mid-cycle gets their allowance reset against a few
    days instead of a month, and `period_end` can land in the PAST. Stripe puts
    prorations first often enough that this is the common case, not the edge.

    ⚠ It falls back to `data[0]` rather than declining, and the asymmetry is
    deliberate. This statement is the one that ROLLS THE METERING WINDOW, and
    the handler's own comment calls a miss here "the costliest of the four": a
    company it does not update keeps last period's `period_start` and exhausts
    one allowance permanently while paying every month. So an unrecognised
    shape must behave exactly as it did before — a correction where we can see,
    a no-op where we cannot (gotcha 24's rule for `_only_for_property`).
    """
    lines = (obj.get("lines") or {}).get("data") or []
    if not lines:
        return {}
    for line in lines:
        if _is_subscription_line(line):
            return line.get("period") or {}
    if len(lines) > 1:
        # Worth a line in the log: we picked one of several and could not tell
        # which was the subscription, so the window may have rolled to a
        # proration's slice. Silent would leave nothing to correlate against.
        logger.warning(
            "billing: invoice %s has %d lines and none identifiable as the "
            "subscription — rolling the metering window from the first line, "
            "which may be a proration",
            obj.get("id"),
            len(lines),
        )
    return lines[0].get("period") or {}


async def _is_delayed_retry(pool: Any, obj: dict[str, Any], created: Any) -> bool:
    """Has a NEWER Stripe event already been applied to this customer's row?

    See `0093_billing_event_ordering.sql` for the failure: a queued
    `subscription.updated` landing an hour after `subscription.deleted` hands a
    cancelled customer their paid plan back, permanently and silently, because
    every handler is an unconditional UPDATE.

    Keyed on `stripe_customer_id` ALONE, which is the one identifier all four
    handled objects carry — subscriptions and invoices both. It is read across
    companies on purpose: a Stripe customer id is globally unique and the event
    carries no company of its own (gotcha 47 asks for that to be said out loud).

    ⚠ Every uncertain answer is FALSE — proceed. No customer on the event, no
    usable timestamp, no row yet, a read that raises: all of them mean this
    function cannot see a reason to refuse, and refusing on a blind guess
    silently drops the event that rolls the metering window or records a
    signup. The defect this guards against is rare; a false positive here is a
    paying customer locked out, and that asymmetry decides every branch.

    ⚠ STRICTLY greater-than. Stripe emits `invoice.paid` and
    `customer.subscription.updated` in the same second off one charge, so
    refusing on equality would drop whichever arrived second.
    """
    customer = obj.get("customer")
    if not customer or created is None:
        return False
    try:
        seconds = float(created)
    except (TypeError, ValueError):
        return False
    try:
        # Deliberately cross-tenant: Stripe ids are global and the event names
        # no company. `max` is the conservative read — if ANY row for this
        # customer has seen something newer, treat the event as a late retry.
        newest = await pool.fetchval(
            """
            select max(last_stripe_event_at)
              from helixis_billing
             where stripe_customer_id = $1
            """,
            customer,
        )
    except Exception:
        logger.exception("billing: could not read event ordering for customer=%s", customer)
        return False
    if not isinstance(newest, datetime):
        return False
    return newest > datetime.fromtimestamp(seconds, tz=UTC)


async def _apply_event(
    settings: Settings, etype: str, obj: dict[str, Any], created: Any = None
) -> None:
    pool = get_pool()

    if await _is_delayed_retry(pool, obj, created):
        logger.warning(
            "billing: ignoring %s for customer=%s — a newer event has already "
            "been applied, so this is a delayed Stripe retry arriving out of "
            "order (see migration 0093)",
            etype,
            obj.get("customer"),
        )
        return

    # Written by every handler below, so the ordering guard has something to
    # compare against. `null` when the event carried no usable timestamp, which
    # COALESCE keeps out of the column rather than writing a false stamp.
    stamp = None
    if created is not None:
        try:
            stamp = float(created)
        except (TypeError, ValueError):
            stamp = None

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        company_id = (obj.get("metadata") or {}).get("company_id")
        items = (obj.get("items") or {}).get("data") or [{}]
        price_id = ((items[0].get("price") or {}).get("id")) if items else None
        plan = _plan_for_price(settings, price_id)
        quantity = int(items[0].get("quantity") or 1) if items else 1
        # ⚠ NULL rather than a guess when Stripe sends a status this build (or
        # the CHECK constraint) does not know. The column is COALESCEd below,
        # so the row keeps its last known value and every OTHER field in the
        # event still lands. Writing the raw string instead raises 23514, and
        # `stripe_webhook` swallows that — so one unstorable status used to
        # discard the subscription id, the plan, the seat count AND the
        # metering window with it. See billing/stripe_status.py.
        status = storable_status(obj.get("status"))
        if status is None:
            logger.error(
                "billing: %s for customer=%s carried status %r, which this build "
                "cannot store — the company's status stays as it was and will NOT "
                "self-correct. Add it to STRIPE_SUBSCRIPTION_STATUSES and widen "
                "helixis_billing_status_known.",
                etype,
                obj.get("customer"),
                obj.get("status"),
            )

        tag = await pool.execute(
            """
            update helixis_billing set
                stripe_subscription_id = $2,
                -- LEARN the customer id here — the first event that carries
                -- it. Both invoice handlers below key on this column ALONE,
                -- so a row that never receives it is unreachable for the life
                -- of the account: the metering window stops rolling and a
                -- monthly payer is locked out with `turns_exhausted`.
                -- COALESCE so an object without `customer` cannot erase it;
                -- the WHERE reads the OLD row, so this cannot affect matching.
                -- Full reasoning + regression: test_billing_webhook.py.
                stripe_customer_id = coalesce($8, stripe_customer_id),
                plan   = coalesce($3, plan),
                status = coalesce($4, status),
                seats  = $5,
                period_start = coalesce(to_timestamp($6), period_start),
                period_end   = coalesce(to_timestamp($7), period_end),
                -- Clearing this on any non-past_due status is what ENDS a
                -- dunning window once the customer's card works again.
                --
                -- ⚠ Keyed on the EFFECTIVE status, not on $4. A bare `status`
                -- in a SET expression reads the PRE-UPDATE row, so
                -- `coalesce($4, status)` is the value actually being stored —
                -- and an unrecognised status (NULL $4) therefore leaves the
                -- dunning window exactly as it was instead of silently
                -- erasing when collection started.
                past_due_since = case when coalesce($4, status) = 'past_due'
                                      then coalesce(past_due_since, now()) else null end,
                last_stripe_event_at = coalesce(to_timestamp($9), last_stripe_event_at),
                updated_at = now()
            where company_id = $1 or stripe_customer_id = $8
            """,
            company_id,
            obj.get("id"),
            plan,
            status,
            quantity,
            obj.get("current_period_start"),
            obj.get("current_period_end"),
            obj.get("customer"),
            stamp,
        )
        # BOTH keys, because this statement matches on either and the operator
        # reading the log has to know which lookups were tried. Naming only the
        # one that happened to be non-null sends them to check a company_id
        # while the row is actually missing under its customer id, or the
        # reverse.
        _warn_if_nothing_matched(
            etype, tag, "company_id/customer", f"{company_id}/{obj.get('customer')}"
        )
        return

    if etype == "customer.subscription.deleted":
        # Back to 'trial' as the PLAN with 'canceled' as the STATUS. The gate
        # reads status first, so this is a lockout — but if the row is ever
        # revived the allowance it falls back to is the smallest we sell,
        # never `pro` (plans.py's same fall-back-restrictive rule).
        tag = await pool.execute(
            """
            update helixis_billing
               set status = 'canceled', plan = 'trial',
                   stripe_subscription_id = null,
                   last_stripe_event_at = coalesce(to_timestamp($3), last_stripe_event_at),
                   updated_at = now()
             where stripe_subscription_id = $1 or stripe_customer_id = $2
            """,
            obj.get("id"),
            obj.get("customer"),
            stamp,
        )
        _warn_if_nothing_matched(etype, tag, "customer", obj.get("customer"))
        return

    if etype == "invoice.payment_failed":
        # Marks the row and nothing else. gate.py keeps serving a past_due
        # company on purpose: a declined card is the customer's bank, not
        # their intent, and Stripe retries for days before giving up.
        tag = await pool.execute(
            """
            update helixis_billing
               set status = 'past_due',
                   past_due_since = coalesce(past_due_since, now()),
                   last_stripe_event_at = coalesce(to_timestamp($2), last_stripe_event_at),
                   updated_at = now()
             where stripe_customer_id = $1
            """,
            obj.get("customer"),
            stamp,
        )
        _warn_if_nothing_matched(etype, tag, "customer", obj.get("customer"))
        return

    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        # A successful invoice both clears dunning AND rolls the metering
        # window, which is what makes the turn allowance reset when the
        # customer is actually billed rather than on an arbitrary date.
        span = _billing_period_from_invoice(obj)
        tag = await pool.execute(
            """
            update helixis_billing
               set status = 'active', past_due_since = null,
                   period_start = coalesce(to_timestamp($2), period_start),
                   period_end   = coalesce(to_timestamp($3), period_end),
                   last_stripe_event_at = coalesce(to_timestamp($4), last_stripe_event_at),
                   updated_at = now()
             where stripe_customer_id = $1
            """,
            obj.get("customer"),
            span.get("start"),
            span.get("end"),
            stamp,
        )
        # The costliest miss of the four: this statement is what ROLLS THE
        # METERING WINDOW, so a company it does not match keeps last period's
        # `period_start` and exhausts one allowance permanently while paying
        # every month.
        _warn_if_nothing_matched(etype, tag, "customer", obj.get("customer"))
        return

    logger.info("billing: ignoring unhandled event %s", etype)
