"""Purge the Propertyware-derived mirror for one company: the disconnect.

Disconnecting Propertyware deletes every mirror row for the company, by the
same rule and on the same table list as the Buildium and Rentvine disconnects
(``rentvine/purge.py`` gives the reasoning in full; it applies here unchanged).

⚠ **THIS DELEGATES RATHER THAN RE-LISTING.** Propertyware writes the same
``helixis_*`` tables every system writes (ADR 0010 Decision (a)), and the
one-PMS trigger (migration 0107, four-way) means a company holds one system's
credentials at a time, so every company-scoped mirror row belongs to the one
it connected. A hand-typed copy of
:data:`~helixis.buildium.purge.PURGE_TABLES` here would be the copy that
goes stale and silently stops deleting a table (gotcha 25).
``test_propertyware_routes`` pins the delegation, and that every table the
Propertyware pass writes is on the list.

⚠ **NEVER NARROWED WITH ``AND pms = 'propertyware'``.** Several purged
tables have no ``pms`` column, and ``purge_buildium_mirror`` tolerates a
missing column so a drifted environment can still disconnect; a ``pms``
filter would skip those tables in silence and report a clean purge (gotcha
112). ``company_id`` alone is correct and complete, because of the trigger.

⚠ **PURGE ONLY: the API key is not revoked.** It keeps working inside
Propertyware until the customer deletes it there (Setup → Administration
Setup → API Keys), so no surface may describe disconnecting as a revocation.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.purge import purge_buildium_mirror

logger = logging.getLogger(__name__)


async def purge_propertyware_mirror(pool: Any, company_id: str) -> dict[str, int]:
    """Delete every mirror row for ``company_id``. Idempotent; a real database
    error propagates, so the disconnect answers a retryable 500 (the Buildium
    purge's contract, which is what runs). Returns rows deleted per table."""
    deleted = await purge_buildium_mirror(pool, company_id)
    logger.info(
        "propertyware mirror purged for company %s: %d row(s) across %d table(s)",
        company_id,
        sum(deleted.values()),
        len(deleted),
    )
    return deleted


__all__ = ["purge_propertyware_mirror"]
