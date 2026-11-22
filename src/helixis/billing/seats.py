"""How many seats a company is billed for, and keeping Stripe told.

⚠ THE BUG THIS EXISTS TO CLOSE. ``helixis_billing.seats`` was written in
exactly one place — the Stripe subscription webhook, which copies the
subscription item's quantity into it. That quantity came from a number the
BROWSER sent at checkout (``payload.seats``) and was never touched again. So:

  * inviting somebody never told Stripe, leaving the company under-billed AND
    under-allowanced (Pro's allowance is per seat, so a five-person team on a
    one-seat subscription shared 400 turns instead of 2,000);
  * removing somebody never told Stripe either, so they kept paying;
  * and the seat count at checkout was whatever the client claimed.

Membership is the truth. This module derives the seat count from it and pushes
it to Stripe, and Stripe's webhook then syncs back into ``seats`` — one
direction of truth, with the existing webhook as the return leg.

⚠ WHY THIS CANNOT BE TRANSACTIONAL, and what is done instead. There is no
distributed transaction across Postgres and Stripe, so "the membership change
and the billing change happen together" is achievable in intent and not in
mechanism. The choice made here: **the membership change always wins.** A team
edit must not fail because a payment processor is slow, and an admin who
cannot remove somebody during a Stripe outage has a worse problem than an
incorrect invoice. Every sync is therefore best-effort, and
:func:`reconcile_company_seats` is what makes it eventually correct — it
recomputes from membership and repairs drift in either direction.

⚠ A SEAT IS COUNTED AT INVITE, NOT AT ACCEPTANCE, because that is what the
data model already says: ``POST /company/invite`` creates the ``company_users``
row up front so the membership exists before the invitee first signs in. Making
billing wait for acceptance means adding an ``accepted_at`` column and teaching
this module to ignore rows without one — a real option, deliberately not taken
tonight, and a one-place change if the answer flips. State it out loud on the
invite screen either way: charging for a seat nobody has used yet is defensible
only if it is disclosed.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from helixis.billing.plans import PLANS, Plan
from helixis.billing.stripe_client import StripeClient
from helixis.settings import Settings

logger = logging.getLogger(__name__)


async def count_members(pool: asyncpg.Pool, company_id: str) -> int:
    """Live membership count for a company — the number seats derive FROM.

    Scoped to the one company (gotcha 47): a seat count is per-tenant by
    definition and an unscoped count here would bill somebody for the whole
    deployment.
    """
    row = await pool.fetchrow(
        "select count(*)::int as n from public.company_users where company_id = $1",
        company_id,
    )
    return int(row["n"]) if row and row["n"] is not None else 0


def billable_seats(plan: Plan, member_count: int) -> int:
    """Seats to bill for ``member_count`` people on ``plan``.

    ⚠ A FLAT plan is always quantity 1, no matter how many people are in the
    company. Starter and Scale are priced per ACCOUNT and say so on the pricing
    page — Scale's own highlight is "the allowance is pooled, not charged per
    person". Multiplying a flat plan by headcount would silently make that
    sentence false and overcharge every Scale customer with a team.

    ⚠ The ceiling is the PLAN'S (`max_seats`), not one constant for everything.
    The trial's is deliberately far tighter than a paid plan's, because trial
    seats multiply model spend with no revenue behind them.

    ⚠ This must agree with `Plan.turn_allowance`, which applies the same clamp
    to decide the ALLOWANCE. If they ever disagree, a customer is billed for a
    number of seats they are not given — pinned by a test that drives both.
    """
    if not plan.per_seat:
        return 1
    return max(1, min(member_count, plan.max_seats))


async def target_seats(pool: asyncpg.Pool, company_id: str) -> tuple[int, Plan] | None:
    """(seats, plan) this company SHOULD be at, or ``None`` if unanswerable.

    ``None`` rather than a guess when the billing row is missing or names a
    plan this build does not know — pushing a quantity computed from a plan we
    cannot resolve is how a customer ends up on the wrong number with no trace
    of why (gotcha 55's decline-to-assert, pointed at money).

    ⚠ MEMBERSHIP IS CHECKED DIRECTLY, not via ``plan_for``. That helper falls
    back to ``trial`` on an unknown key — correct for a GATE, where the most
    restrictive answer is the safe one, and wrong here: trial is flat, so an
    unrecognised plan would quietly resolve to quantity 1 and UNDER-bill a
    paying customer while looking like a clean sync. The safe direction for an
    allowance and the safe direction for an invoice are opposites, which is
    why this does not reuse the gate's resolver.
    """
    row = await pool.fetchrow(
        "select plan from public.helixis_billing where company_id = $1",
        company_id,
    )
    if row is None:
        # ERROR for the same reason as `gate.py`'s missing-row branch: seats
        # stop syncing to Stripe from here on, silently, so the company is
        # billed for whatever quantity it last had while its team grows. The
        # level is what turns that into an alert instead of a log line.
        logger.error("seats: no billing row for company=%s — not syncing", company_id)
        return None
    key = str(row["plan"])
    if key not in PLANS:
        logger.error("seats: company=%s names unknown plan %r — not syncing", company_id, key)
        return None
    plan = PLANS[key]
    return billable_seats(plan, await count_members(pool, company_id)), plan


async def sync_company_seats(
    pool: asyncpg.Pool, settings: Settings, company_id: str, *, reason: str
) -> dict[str, Any] | None:
    """Push this company's real seat count to Stripe. Best-effort, never raises.

    ⚠ CALL THIS AFTER the membership write, never before. Ordered the other way
    a Stripe success followed by a failed insert bills for somebody who is not
    on the team — and that direction is not self-healing, because the
    reconciler derives from membership and would happily leave the overcharge
    in place until the next edit.

    Returns Stripe's subscription object on a successful change, ``None``
    otherwise (including "nothing to do"), and logs the reason either way. The
    caller does not branch on it: a team edit is not conditional on billing.
    """
    try:
        computed = await target_seats(pool, company_id)
        if computed is None:
            return None
        seats, plan = computed

        row = await pool.fetchrow(
            "select stripe_subscription_id from public.helixis_billing where company_id = $1",
            company_id,
        )
        subscription_id = (row or {}).get("stripe_subscription_id")
        if not subscription_id:
            # A trial or a never-checked-out company has no subscription to
            # re-quantity. Not an error, and not a gap either: checkout reads
            # the live membership count when it happens, so the first bill is
            # already right. Debug rather than warning — every trial company
            # takes this path on every invite.
            logger.debug(
                "seats: company=%s plan=%s has no subscription yet (%s)",
                company_id,
                plan.key,
                reason,
            )
            return None

        client = StripeClient(settings.billing.stripe_secret_key)
        result = await client.update_subscription_quantity(str(subscription_id), quantity=seats)
        if result is None:
            # Loud: this is the state the reconciler exists to repair, and an
            # operator reading the log needs the company and the target.
            logger.error(
                "seats: FAILED to sync company=%s to %d seat(s) (%s) — "
                "Stripe and membership now disagree until reconciliation",
                company_id,
                seats,
                reason,
            )
        else:
            logger.info("seats: company=%s -> %d seat(s) (%s)", company_id, seats, reason)
        return result
    except Exception:
        # Belt and braces on top of StripeClient's own swallowing. Nothing in
        # this module may propagate into a team-management request.
        logger.exception("seats: sync raised for company=%s (%s)", company_id, reason)
        return None


async def reconcile_company_seats(pool: asyncpg.Pool, settings: Settings, company_id: str) -> bool:
    """Repair drift between membership and the billed quantity. Idempotent.

    Returns True when a change was pushed. Exists because every sync above is
    best-effort: a Stripe outage during an invite leaves the company under- or
    over-billed with nothing else to notice. Recomputes from membership, so it
    corrects in both directions and a re-run is a no-op.
    """
    return await sync_company_seats(pool, settings, company_id, reason="reconcile") is not None


__all__ = [
    "billable_seats",
    "count_members",
    "reconcile_company_seats",
    "sync_company_seats",
    "target_seats",
]
