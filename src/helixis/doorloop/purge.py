"""Purge the DoorLoop-derived mirror for one company: the disconnect.

Propertyware's ``purge.py`` gives the reasoning, and it applies here unchanged:

⚠ **THIS DELEGATES RATHER THAN RE-LISTING.** DoorLoop will write the same
``helixis_*`` tables every system writes (ADR 0010 Decision (a)), and the
one-PMS trigger (five-way since migration 0109) means a company holds one
system's credentials at a time, so every company-scoped mirror row belongs to
the one it connected. Every table ``doorloop.sync`` writes is on Buildium's
list (``test_doorloop_routes``).

⚠ **NEVER NARROWED WITH ``AND pms = 'doorloop'``.** Several purged tables have
no ``pms`` column; ``company_id`` alone is correct and complete, because of the
trigger (gotcha 112).

⚠ **PURGE ONLY: the API key is not revoked.** It keeps working inside DoorLoop
until the customer deletes it there (Settings → "Zapier & API Keys"), so no
surface may describe disconnecting as a revocation.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.purge import purge_buildium_mirror

logger = logging.getLogger(__name__)


async def purge_doorloop_mirror(pool: Any, company_id: str) -> dict[str, int]:
    """Delete every mirror row for ``company_id``. Idempotent; a real database
    error propagates, so the disconnect answers a retryable 500. Returns rows
    deleted per table."""
    deleted = await purge_buildium_mirror(pool, company_id)
    logger.info(
        "doorloop mirror purged for company %s: %d row(s) across %d table(s)",
        company_id,
        sum(deleted.values()),
        len(deleted),
    )
    return deleted


__all__ = ["purge_doorloop_mirror"]
