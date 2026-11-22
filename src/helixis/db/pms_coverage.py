"""Which mirror tables does each PMS actually fill? One home for the answer.

ADR 0010 Decision (a) puts Rentvine's records in the SAME ``helixis_*`` tables
Buildium writes. That is what lets every mirror reader serve both connectors
unchanged. It is also a trap: a reader that finds no rows cannot tell "this
company has none" from "this company's PMS never sends any".

The Rentvine sync mirrors seven entities. It writes no ledger transactions,
no bills, no tasks and no lease balances. So a mirror-reading detector asking
"which active leases have no Payment this month?" gets every active Rentvine
lease back. Nothing in that query is wrong, but every answer it gives about a
Rentvine company is a false claim about a tenant's money.

A reader that depends on a table the company's PMS does not fill must
decline, never answer (gotcha 55: a guard that cannot see must not report
emptiness). This module is what lets it know.

⚠ **Only PMS-FED tables count.** Several ``helixis_*`` tables are written by
Occupella itself: the activity log, reminders, renewal offers drafted in the
app, property intel from public geodata, CRM leads. Those exist for every
company whatever its PMS, so they are never a reason to decline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from helixis.db.company_pms import company_pms_strict
from helixis.db.repositories.mirror_sync_state import get_freshness
from helixis.pms import registry
from helixis.rentvine.sync import DERIVED_TABLES, SYNC_STATE_STAGE

logger = logging.getLogger(__name__)

BUILDIUM = registry.BUILDIUM
RENTVINE = registry.RENTVINE

#: Every table whose rows come FROM a property-management system. Migration
#: 0101 put a ``pms`` column on 21 tables; this is those minus the two that
#: Occupella writes itself (``helixis_renewal_offers``, drafted in the app, and
#: ``helixis_property_intel``, from public geodata), plus Buildium's webhook
#: event log, which only a Buildium feed writes.
#: ``test_pms_coverage.py`` pins this against 0101's own list.
PMS_FED_TABLES: frozenset[str] = frozenset(
    {
        "helixis_properties",
        "helixis_units",
        "helixis_tenants",
        "helixis_leases",
        "helixis_lease_transactions",
        "helixis_work_orders",
        "helixis_applicants",
        "helixis_vendors",
        "helixis_owners",
        "helixis_tasks",
        "helixis_gl_accounts",
        "helixis_bills",
        "helixis_bank_accounts",
        "helixis_work_order_status_history",
        "helixis_lease_tenants",
        "helixis_turnovers",
        "buildium_staff",
        "helixis_bill_lines",
        "helixis_lease_balances",
        "buildium_events",
    }
)

#: What each PMS fills. Buildium fills all of them.
#:
#: ⚠ Rentvine's set is DERIVED from the table the sync stamps
#: (``SYNC_STATE_STAGE``), never typed out a second time. When the sync
#: learns a new entity, readers that need it start running for Rentvine
#: companies on the same commit, and not before.
FED_BY: dict[str, frozenset[str]] = {
    BUILDIUM: PMS_FED_TABLES,
    RENTVINE: frozenset(SYNC_STATE_STAGE) | DERIVED_TABLES,
}


def _stage_map(pms: str | None) -> dict[str, str]:
    """Table → sync stage for a mirror-only system: Rentvine's, or the one a
    newer connector's package exposes (``sync.SYNC_STATE_STAGE``, found through
    ``pms.registry`` so that connector never edits this file). Empty while that
    package does not exist."""
    if pms == RENTVINE:
        return SYNC_STATE_STAGE
    found = registry.connector_attr(pms, "sync", "SYNC_STATE_STAGE")
    return dict(found) if isinstance(found, dict) else {}


def fed_tables(pms: str | None) -> frozenset[str]:
    """The tables ``pms`` fills. :data:`FED_BY` for Buildium and Rentvine; for
    another system, its connector's stage map and derived tables, and nothing
    at all until that connector exists (every PMS-fed read is then blind)."""
    if pms in FED_BY:
        return FED_BY[pms]
    derived = registry.connector_attr(pms, "sync", "DERIVED_TABLES")
    return frozenset(_stage_map(pms)) | (frozenset(derived) if derived else frozenset())


def blind_tables(pms: str, reads: Iterable[str]) -> frozenset[str]:
    """The tables in ``reads`` that ``pms`` is expected to fill and does not.

    Empty means the reader can run. An unknown ``pms`` is treated as filling
    nothing, so every PMS-fed read is blind: running a money rule for a
    connector nobody has mapped is the defect this module exists to stop.
    """
    fed = fed_tables(pms)
    return frozenset(t for t in reads if t in PMS_FED_TABLES and t not in fed)


def unlanded_tables(pms: str, reads: Iterable[str], landed: Iterable[str]) -> frozenset[str]:
    """The Rentvine tables in ``reads`` whose sync stage has never been stamped
    (``landed`` is the company's stamped stage names, ``get_freshness``).

    ⚠ A fed table that has not landed is UNKNOWN, not empty. Rentvine's lease
    transactions fill a few hundred leases a pass, so for the first passes a
    lease with no Payment row may simply not have been read yet, and a detector
    reading it would call that tenant unpaid. A stage is stamped only when its
    table came through clean (``sync._record_freshness``), so waiting for the
    stamp is waiting for the whole table.

    Every system but Buildium: Buildium's readers predate the stamps
    (migration 0057) and run as they always have. Rentvine was the first to
    need it; Rent Manager and Propertyware fill their tables the same way.
    """
    if pms == BUILDIUM:
        return frozenset()
    stages = _stage_map(pms)
    stamped = set(landed)
    return frozenset(t for t in reads if t in stages and stages[t] not in stamped)


#: How a PMS is named to a customer.
PMS_NAME: dict[str, str] = {key: found.name for key, found in registry.PMS.items()}


@dataclass(frozen=True)
class Coverage:
    """What one company's PMS fills, resolved once per request.

    ``pms`` is ``None`` when the lookup failed. Then every PMS-fed table
    reads as unseen: a failed lookup must not turn into "nobody owes rent".
    """

    pms: str | None
    #: The company's stamped stage names (``get_freshness``), or ``None`` when
    #: they were not looked up. :func:`coverage_for` always looks them up for
    #: every system but Buildium, so a fed table that has not landed reads as
    #: unseen (:func:`unlanded_tables`).
    landed: frozenset[str] | None = None

    def sees(self, table: str) -> bool:
        if table not in PMS_FED_TABLES:
            return True
        if self.pms is None or blind_tables(self.pms, {table}):
            return False
        return self.landed is None or not unlanded_tables(self.pms, {table}, self.landed)

    @property
    def name(self) -> str | None:
        """The system's name for customer-facing copy, or ``None`` if unknown."""
        return PMS_NAME.get(self.pms) if self.pms is not None else None

    def not_sent(self, what: str, where: str) -> str:
        """The sentence a reader returns instead of an empty answer.

        ``what`` is the data in the customer's words ("rent balances"), and
        ``where`` is where they can check it themselves ("each tenant's
        ledger"), so the sentence names an action (rule 16).
        """
        if self.name is None:
            return (
                f"I can't read {what} right now, so I can't say either way. "
                f"Check {where} in your property management system."
            )
        # "Occupella doesn't sync", not "Rentvine doesn't have": Rentvine keeps
        # ledgers; the connector just doesn't mirror them yet.
        return (
            f"Occupella doesn't sync {what} from {self.name} yet, so I can't say "
            f"either way. Check {where} in {self.name}."
        )


async def coverage_for(pool: Any, company_id: str) -> Coverage:
    """This company's :class:`Coverage`. Never raises."""
    try:
        pms = await company_pms_strict(pool, company_id)
        if pms == BUILDIUM:
            return Coverage(pms)
        # Empty on any failure, which reads every fed table as unseen: the
        # conservative direction (rule 14).
        return Coverage(pms, frozenset(await get_freshness(pool, company_id)))
    except Exception:  # noqa: BLE001 — degrade the claim, never the page
        logger.warning("coverage_for: PMS lookup failed for %s", company_id, exc_info=True)
        return Coverage(None)


__all__ = [
    "BUILDIUM",
    "FED_BY",
    "fed_tables",
    "PMS_FED_TABLES",
    "PMS_NAME",
    "RENTVINE",
    "Coverage",
    "blind_tables",
    "coverage_for",
    "unlanded_tables",
]
