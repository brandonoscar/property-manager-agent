"""Delete everything Occupella synced from Rent Manager for one company.

Disconnecting Rent Manager deletes the credentials row and then every mirror
row, by the same rule and on the same table list as the Buildium and Rentvine
disconnects (ADR 0010 e1). The terms (``design/rentmanager-terms.md``, rule 9)
make the disconnect the answer to a suspected compromise, so it is a purge,
and nothing about it is optional.

⚠ **IT DELEGATES RATHER THAN RE-LISTING, AND THAT IS THE WHOLE MODULE.** Rent
Manager writes the same ``helixis_*`` tables every other system does, and the
one-PMS trigger (migration 0107, four ways) means a company holds ONE system,
so every company-scoped mirror row is this system's. A second copy of
:data:`~helixis.buildium.purge.PURGE_TABLES` here is the copy that goes stale
and silently stops deleting a table (gotcha 25), on a compliance path.

⚠ **NEVER NARROW IT WITH ``AND pms = 'rentmanager'``** (gotcha 112). Several
tables on the list have no ``pms`` column, and ``purge_buildium_mirror``
tolerates a missing column so a drifted environment can still disconnect: a
narrowed delete would skip those tables in silence and report a clean purge.
``company_id`` alone is correct and complete, because of the one-PMS rule.

⚠ **PURGE ONLY: nothing changes inside Rent Manager.** The user Occupella
signed in as, and its password, keep working there until the customer
changes them, so no surface may describe disconnecting as revoking access.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.purge import purge_buildium_mirror

logger = logging.getLogger(__name__)


async def purge_rentmanager_mirror(pool: Any, company_id: str) -> dict[str, int]:
    """Delete every Rent Manager-derived row for ``company_id``.

    Idempotent, and a real database error propagates so the caller can answer
    with a retryable error: the contract of the Buildium purge it runs.
    Returns the rows deleted per table (only tables that had rows).
    """
    deleted = await purge_buildium_mirror(pool, company_id)
    logger.info(
        "rentmanager mirror purged for company %s: %d row(s) across %d table(s)",
        company_id,
        sum(deleted.values()),
        len(deleted),
    )
    return deleted


__all__ = ["purge_rentmanager_mirror"]
