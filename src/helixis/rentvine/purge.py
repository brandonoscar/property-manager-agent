"""Purge the Rentvine-derived mirror for one company. ADR 0010 Decision (e).

Disconnecting Rentvine deletes every mirror row that came from the Rentvine
API, by the same rule and on the same table list as the Buildium disconnect.
ADR 0010 e1 records why the rule is applied without having read Rentvine's
terms: ``*.rentvine.com`` is egress-blocked from the build container, and the
safe side of an unanswered contractual question is to delete MORE. Keeping data
a vendor requires removed is a breach discovered from a lawyer; deleting data a
vendor would have let us keep costs one re-sync of a cache.

⚠ **THIS DELEGATES RATHER THAN RE-LISTING, AND THAT IS THE WHOLE MODULE.**
The tables are identical because the mirror is identical — ADR 0010 Decision
(a) writes Rentvine records into the SAME ``helixis_*`` tables Buildium writes,
and a1 (enforced by migration 0100's ``assert_one_pms_per_company`` trigger)
means a company holds ONE PMS, so every company-scoped mirror row belongs to
whichever one it connected. A second hand-typed copy of
:data:`~helixis.buildium.purge.PURGE_TABLES` here is gotcha 25's two-homes
drift pointed at a compliance obligation: the copy that goes stale is the one
that silently stops deleting a table. ``test_rentvine_purge.py`` pins the
delegation so a later "tidy-up" cannot inline a list.

⚠ **DO NOT NARROW THE DELETE WITH ``AND pms = 'rentvine'``.** It reads as the
obviously-correct tightening and it is the incomplete-sweep-certified-complete
failure (gotcha 112). Migration 0101 added ``pms`` to **21** tables; the purge
list holds **30**, so nine of them — ``buildium_events``,
``buildium_sync_state``, ``helixis_reminders``, ``helixis_risk_flags``,
``helixis_action_idempotency``, ``helixis_entity_aliases``,
``helixis_entity_signals``, ``helixis_mirror_sync_state`` and
``property_assignments`` — have no such column (measured, not assumed).
``purge_buildium_mirror`` deliberately TOLERATES ``UndefinedColumnError`` so a
deploy-drifted environment can still disconnect, which means the narrowed
predicate would skip those nine in silence and report a successful purge with
the event log and the Inbox reminders still sitting there. ``company_id``
alone is both correct and complete, *because* of a1.

⚠ **PURGE ONLY — this does not revoke the API key** (ADR 0010 e2). The key
keeps working inside Rentvine until the customer deletes it there, so no
surface may describe disconnecting as a revocation. Buildium's path has the
same property.
"""

from __future__ import annotations

import logging

import asyncpg

from helixis.buildium.purge import purge_buildium_mirror

logger = logging.getLogger(__name__)


async def purge_rentvine_mirror(pool: asyncpg.Pool, company_id: str) -> dict[str, int]:
    """Delete every Rentvine-derived row for ``company_id``.

    Idempotent, and it propagates a real database error so the caller can
    surface a retryable 500 — the same contract as the Buildium purge, which is
    what it runs.

    Returns rows-deleted per table (only tables that had rows).
    """
    deleted = await purge_buildium_mirror(pool, company_id)
    logger.info(
        "rentvine mirror purged for company %s: %d row(s) across %d table(s)",
        company_id,
        sum(deleted.values()),
        len(deleted),
    )
    return deleted


__all__ = ["purge_rentvine_mirror"]
