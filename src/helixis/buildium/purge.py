"""Purge the Buildium-derived mirror for one company (Section 18 compliance).

Buildium's Terms of Service (§18, the API license) obligate the customer
to "immediately remove" API-derived data from their systems when their
API license ends. Occupella holds that data on the customer's behalf, so
we carry the obligation for them: disconnecting the Buildium API key
(``DELETE /api/v1/buildium/credentials``) purges every Buildium-derived
row for that company. Occupella's ToS commits to this behavior.

Scope — deliberately narrower than the right-to-erasure eraser
(``scripts/purge_company_data.py``, which deletes the whole company):
this removes ONLY data derived from the Buildium API — the mirror
tables, the webhook event log, sync cursors, and analytics computed
FROM the mirror (risk flags, reminders, entity signals, renewal
offers, property intel). It does NOT touch the Occupella account
itself: users, chat history, documents, SMS consent, Gmail state, or
agent memory. (Memory can contain conversation-level references to
Buildium entities; full memory erasure remains the
``purge_company_memory`` path — noted in the ToS.)

Reconnecting later is cheap by design: the mirror is a cache of
Buildium's data, so a fresh key + Test connection rebuilds it via the
initial sync. That is why disconnect ALWAYS purges — there is no
"keep my data but drop the key" mode, because that is exactly the
state §18 forbids.

The table list is an explicit, ordered allowlist (children before
parents so the deletes hold under any FK configuration), pinned by
``tests/unit/test_buildium_purge.py`` against the live migration set so
a new mirror table can't silently escape the purge.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

# Ordered child-first. Every entry is company-scoped (`company_id`
# column) and exclusively Buildium-API-derived. When a new mirror table
# lands, add it here — test_buildium_purge cross-checks this list
# against the migrations so forgetting fails CI, not compliance.
PURGE_TABLES: tuple[str, ...] = (
    # Work-order / task lineage (history → work orders → tasks)
    "helixis_work_order_status_history",
    "helixis_work_orders",
    "helixis_tasks",
    # Financials (lines before bills; transactions before leases)
    "helixis_bill_lines",
    "helixis_bills",
    "helixis_lease_transactions",
    "helixis_lease_balances",
    # Lease lineage
    "helixis_lease_tenants",
    "helixis_turnovers",
    "helixis_renewal_offers",
    "helixis_leases",
    # Applicants
    "helixis_applicants",
    # Property lineage (intel + units before properties)
    "helixis_property_intel",
    "helixis_units",
    "helixis_properties",
    # People / reference data
    "helixis_tenants",
    "helixis_owners",
    "helixis_vendors",
    "helixis_gl_accounts",
    "helixis_bank_accounts",
    "buildium_staff",
    # Derived analytics computed FROM the mirror
    "property_assignments",  # routing rules keyed by Buildium property ids
    "helixis_entity_aliases",
    "helixis_entity_signals",
    "helixis_risk_flags",
    "helixis_reminders",
    # Operational ledgers that carry Buildium entity identifiers
    "helixis_action_idempotency",
    # The webhook/event log — payloads ARE Buildium API data
    "buildium_events",
    # Rentvine deliveries the receiver refused, kept verbatim (migration 0106)
    # — payloads ARE Rentvine API data
    "helixis_rentvine_webhook_deliveries",
    # Sync cursors
    "buildium_sync_state",
    "helixis_mirror_sync_state",
    # The helixis.pms connectors' incremental watermarks (migration 0108)
    "helixis_pms_sync_watermarks",
)


async def purge_buildium_mirror(pool: asyncpg.Pool, company_id: str) -> dict[str, int]:
    """Delete every Buildium-derived row for ``company_id``.

    Idempotent (a re-run deletes nothing new). Per-table missing-table /
    missing-column errors are tolerated (deploy drift between code and an
    unmigrated environment must never make disconnect fail); any other
    database error propagates so the caller can surface a retryable 500 —
    the purge being idempotent makes a retry safe.

    Returns rows-deleted per table (only tables that had rows).
    """
    deleted: dict[str, int] = {}
    async with pool.acquire() as conn:
        for table in PURGE_TABLES:
            try:
                result = await conn.execute(
                    f"DELETE FROM public.{table} WHERE company_id = $1",  # noqa: S608 — table from the pinned literal allowlist above
                    company_id,
                )
                # asyncpg returns a status tag string ("DELETE 3"); be
                # defensive about anything else (test fakes, driver drift).
                count = int(result.split()[-1]) if isinstance(result, str) else 0
                if count:
                    deleted[table] = count
            except (
                asyncpg.exceptions.UndefinedTableError,
                asyncpg.exceptions.UndefinedColumnError,
            ) as exc:
                logger.warning("purge: skipping %s (%s)", table, type(exc).__name__)
    total = sum(deleted.values())
    logger.info(
        "buildium mirror purged for company %s: %d row(s) across %d table(s)",
        company_id,
        total,
        len(deleted),
    )
    return deleted


__all__ = ["PURGE_TABLES", "purge_buildium_mirror"]
