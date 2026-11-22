"""How current is the data on this screen?

The chat footer has said "as of <date>" since migration 0057, and every PAGE
rendering the same mirrored rows said nothing at all. A property manager
reading the Operations grid has no way to know they are looking at a cached
copy of Buildium, let alone how old it is — and gotcha 57 is two separate
sessions reading "0 paid bills" as a missing feature when it was a sync
window. Users make that mistake faster and with less context.

⚠ The VERDICT comes from `mirror_sync_state.assess`, shared with the chat
footer. Only the wording differs, because the audiences do (gotchas 39/42).
A second copy of "is this stale" would eventually have the footer and the grid
above it disagreeing about the same account on the same screen.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.pms_coverage import coverage_for
from helixis.db.repositories import mirror_sync_state as sync_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mirror", tags=["mirror"])


class FreshnessOut(BaseModel):
    as_of: datetime | None = None
    """Newest core sync. `null` means nothing has ever synced — which the
    client must render as "not synced yet", NEVER as "just now"."""
    stalest_table: str | None = None
    stalest_at: datetime | None = None
    stale_hours: int | None = None
    is_stale: bool = False
    """The server's own answer, not a threshold for the client to re-apply.
    Same rule as billing's `block` (gotcha 73): two numbers cannot be
    re-combined into one decision without eventually disagreeing with it."""
    has_data: bool = False
    pms: str | None = None
    """Which system the mirror is copied from: ``buildium`` or ``rentvine``.
    ``null`` when the lookup failed, never guessed. Member-readable, unlike
    the admin-only credential routes, because every page that says "sync
    from X" or offers a Re-sync needs to know which X."""
    pms_name: str | None = None


@router.get("/freshness", response_model=FreshnessOut)
async def mirror_freshness(
    user: AuthenticatedUser = Depends(get_current_user),
) -> FreshnessOut:
    """When each mirrored table last synced, for the caller's company."""
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")

    # Asked first and separately: a failed freshness read must not also cost
    # the page its PMS, and coverage_for never raises.
    cov = await coverage_for(get_pool(), user.company_ids[0])
    try:
        freshness = await sync_state.get_freshness(get_pool(), user.company_ids[0])
    except Exception:  # noqa: BLE001
        # ⚠ An empty verdict, not a 500. This decorates a page; a freshness
        # read that fails must cost the caption and never the grid the user
        # actually came for. `has_data=False` then renders as "sync status
        # unknown", which is honest — unlike a timestamp we could not read.
        logger.warning("mirror freshness read failed", exc_info=True)
        return FreshnessOut(pms=cov.pms, pms_name=cov.name)

    verdict = sync_state.assess(freshness)
    return FreshnessOut(
        as_of=verdict.as_of,
        stalest_table=verdict.stalest_table,
        stalest_at=verdict.stalest_at,
        stale_hours=verdict.stale_hours,
        is_stale=verdict.is_stale,
        has_data=verdict.has_data,
        pms=cov.pms,
        pms_name=cov.name,
    )
