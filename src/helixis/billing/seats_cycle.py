"""The seat reconciler's sweep — repairs Stripe/membership drift, unattended.

⚠ WHY THIS FILE EXISTS AT ALL. ``billing/seats.py`` documents
``reconcile_company_seats`` as the thing that "makes it eventually correct",
and for one commit nothing anywhere called it. That is precisely the failure
this repo keeps re-learning — ``comms_triage`` shipped complete, unit-tested
and unreachable; ``notes/policy`` shipped with both live seams unbound; gotcha
62 caught a reconciliation call deleted from a cycle with every unit test still
green. A helper whose docstring promises eventual consistency, with no caller,
promises nothing. So the sweep and its call site are the deliverable, not the
function.

⚠ IT SWEEPS ONLY COMPANIES WITH A SUBSCRIPTION, not every company. A trial or
never-checked-out company has no Stripe quantity to correct, so visiting one
costs a query and can never change anything; the sync itself already returns
early for them. Deriving the population from the thing being repaired is
gotcha 62's other half — that finding was a cleanup sweep whose population
query silently excluded the companies that needed it.
"""

from __future__ import annotations

import logging

import asyncpg

from helixis.billing.seats import reconcile_company_seats
from helixis.settings import Settings

logger = logging.getLogger(__name__)


async def run_seat_reconcile_cycle(pool: asyncpg.Pool, settings: Settings) -> int:
    """Re-push every subscribed company's true seat count. Returns rows visited.

    Idempotent by construction: each company's target is recomputed from live
    membership, and ``update_subscription_quantity`` skips the write when
    Stripe already holds that number. So a sweep over a healthy account makes
    no API calls at all and leaves no proration lines behind.

    Never raises — the worker's other cycles must not die because a billing
    repair did.
    """
    if not settings.billing.stripe_secret_key:
        # No keys, nothing to reconcile against. Debug, not warning: this is
        # the normal state of a dev box and of CI.
        logger.debug("seats: no Stripe key configured — reconcile skipped")
        return 0

    try:
        rows = await pool.fetch(
            # Deliberately CROSS-COMPANY: this is a fleet-wide janitor, the
            # same shape as the other worker sweeps (gotcha 47 asks that a
            # global read say so out loud). Each company is then reconciled
            # individually and scoped by its own id.
            """
            select company_id
              from public.helixis_billing
             where stripe_subscription_id is not null
            """
        )
    except Exception:
        logger.exception("seats: could not list subscribed companies")
        return 0

    visited = 0
    for row in rows:
        company_id = str(row["company_id"])
        try:
            await reconcile_company_seats(pool, settings, company_id)
        except Exception:
            # Already swallowed one level down; belt and braces so one bad
            # company cannot end the sweep for the rest of the fleet.
            logger.exception("seats: reconcile raised for company=%s", company_id)
        visited += 1
    return visited


__all__ = ["run_seat_reconcile_cycle"]
