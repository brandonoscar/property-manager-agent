"""Analyst tools (ADR 0007, slice 1) — schema lookup + guarded SQL.

Two tools, same idioms as mirror_tools: thin ``@function_tool`` wrappers over
un-decorated ``_impl`` coroutines (unit-testable without the SDK), pool via
``get_pool()``, tenancy via the ``current_memory_context()`` contextvar, and
graceful degradation — every failure is a structured JSON result the agent's
prompt teaches it to read, never an exception into the agent loop.

Self-gating: the feature ships dark. When ``settings.analysis.enabled`` is
false (the default), both tools return ``analysis_unavailable`` — same
tool-layer gating pattern as the web/browser tools, so the handoff can be
registered unconditionally.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents import function_tool

from helixis.analysis.executor import run_analysis_query
from helixis.analysis.sql_guard import ALLOWED_TABLES, MAX_ROWS, hidden_columns
from helixis.db.company_pms import company_pms
from helixis.db.engine import get_pool
from helixis.db.pms_coverage import coverage_for
from helixis.db.tenant_links import (
    CURRENT_LINK_STATUS,
    FUTURE_LINK_STATUS,
    MOVED_OUT_LINK_STATUS,
    current_link_sql,
)
from helixis.memory.context import current_memory_context
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

#: ⚠ **Names the alternative, because the version that did not caused a real
#: false claim.** Live run 2026-08-28, "what property do you think is at the
#: most risk and why?" — routed to this specialist, this tool refused, and the
#: answer was: *"The data-analysis capability isn't enabled on this deployment
#: right now, so I can't query your Buildium data (occupancy, delinquency,
#: work orders, financials, etc.)"*.
#:
#: The first clause is true and the second is FALSE. ``portfolio_overview``,
#: ``delinquency_report``, ``unpaid_bills``, ``expiring_leases`` and
#: ``buildium_open_maintenance_by_unit`` are all live mirror rollups and none
#: of them is behind this flag. The question was fully answerable; the user was
#: told the product cannot do something it does.
#:
#: Two lessons meeting. Gotcha 42: a limit of OURS reported as a fact about the
#: SOURCE — the message said "this tool is off" and the model rendered it as
#: "your data is unreachable". Gotcha 52: a prohibition with no permitted
#: substitute is an instruction to improvise, and the model improvised in the
#: only direction available, which was to generalise. So the refusal now says
#: what is still possible, in the same breath as what is not.
_UNAVAILABLE = json.dumps(
    {
        "ok": False,
        "error_kind": "analysis_unavailable",
        "error": (
            "Ad-hoc SQL analysis is not enabled on this deployment "
            "(HELIXIS_ANALYSIS__ENABLED is off). This limit is OURS and says "
            "NOTHING about whether the company's Buildium data is reachable — "
            "do not tell the user their data cannot be queried."
        ),
        "still_available": (
            "The mirror rollups are live and answer most portfolio questions "
            "without this tool: portfolio_overview, delinquency_report, "
            "unpaid_bills, expiring_leases, buildium_open_maintenance_by_unit. "
            "Hand back to the orchestrator and use those instead of declining."
        ),
    }
)
_NO_COMPANY = json.dumps(
    {
        "ok": False,
        "error_kind": "no_company",
        "error": "I couldn't determine which company this session belongs to.",
    }
)
_DB_UNAVAILABLE = json.dumps(
    {
        "ok": False,
        "error_kind": "db_unavailable",
        "error": "The database pool is not initialized in this process.",
    }
)


# ⚠ WHAT THE COLUMN LIST DOES NOT SAY. A bare inventory of names and types
# reads as though every column means what it is called, and two of them do not.
#
# ``created_at`` / ``updated_at`` are stamped when WE wrote the mirror row, not
# when anything happened in Buildium. Live-measured 2026-08-28 on Blue Ridge:
# leases, bills, tasks, tenants, properties, work orders and lease transactions
# ALL hold exactly ONE distinct ``updated_at`` date across every row — the last
# sync. So a "what changed this week" query over them returns the entire table
# or nothing at all, depending only on which side of the sync the window falls,
# and the analyst then cites that count as a finding. The dossier query shipped
# an ``age_days`` built this way and reported every open work order as 0 days
# old until it was deleted; the analyst can write the same expression itself in
# ``run_analysis``, and no code change can stop it — only knowing can.
#
# So the note ships WITH the schema, where the guess is made. It names what is
# unavailable and, more usefully, where the real dates are: pointing only at a
# prohibition is what makes a model improvise (gotcha 52's finding, one domain
# over). Every column named below was confirmed present in information_schema.
_SCHEMA_NOTES: list[str] = [
    "created_at and updated_at are SYNC timestamps — when Occupella last wrote "
    "the mirror row, not when the record was created or changed in Buildium. "
    "Every row in a table typically shares one updated_at (the last sync), so "
    "they cannot answer 'what changed', 'how old is this', or any 'since when' "
    "question. Never compute an age or a change window from them.",
    "The MIRROR tables keep no history: they hold the CURRENT state of each "
    "Buildium record with no prior versions, so 'what changed since <date>' "
    "has no answer from them. Say that rather than approximating it.",
    # ⚠ The note above used to end "...no answer here AT ALL", which became
    # false the moment the Occupella-native tables joined the allowlist — and a
    # caveat that is false steers the model away from a real answer while
    # teaching it to discount the true caveats beside it (gotcha 57). The two
    # halves of this read surface have genuinely different time semantics and
    # the notes have to say which is which.
    "The helixis-native tables are the EXCEPTION and they do carry real event "
    "time: helixis_work_order_status_history is an actual change log "
    "(previous_status → status at occurred_at); helixis_risk_flags has "
    "first_seen_at / last_seen_at / resolved_at; helixis_reminders has "
    "first_fired_at / last_fired_at; helixis_turnovers has move_out_date and "
    "per-step completion timestamps; helixis_renewal_offers has sent_at. On "
    "these tables created_at IS when the row was created, because Occupella "
    "created it — the sync-timestamp caveat applies only to the mirror.",
    "helixis_risk_flags holds findings from two sources, and the source "
    "column says which: 'rule' is a deterministic detector, 'analyst' is the "
    "nightly LLM pass. Check it before treating a flag as independent "
    "corroboration of your own conclusion — it may BE your own conclusion "
    "from a previous run.",
    "Dates that DO come from Buildium and are safe to reason about: "
    "helixis_bills.bill_date and .due_date; helixis_tasks.due_date; "
    "helixis_lease_transactions.transaction_date; helixis_leases.lease_from_date "
    "and .lease_to_date; helixis_lease_tenants.move_in_date. Work orders carry "
    "no date at all — Buildium's /workorders sends none.",
    # Re-pointed 2026-10-07: "holds UNPAID bills only" stopped being true when
    # Buildium's backfill began reading paid bills (2026-07-06), and Rentvine and
    # DoorLoop sync both kinds; a SUM over the table counted paid bills as owed.
    "helixis_bills.is_paid says whether a bill is paid, so what is still owed "
    "is is_paid IS NOT TRUE; never sum the whole table as money owed. Paid "
    "bills can be missing (Buildium's sync keeps only those paid within its "
    "lookback window), so a vendor with no paid bill on file is not evidence "
    "it was never paid. No table records when or how a bill was paid, so any "
    "query about payments made, payment history, or 'when did we last pay X' "
    "is unanswerable from this data. Report the gap; do not return an empty "
    "result as if it meant zero.",
    # The same rule the chat tools and Inbox readers follow (``db.tenant_links``).
    "helixis_lease_tenants.status is the LINK's status, not the lease's: "
    f"'{CURRENT_LINK_STATUS}' means the tenant lives under that lease now. A "
    "lease can stay Active after one of its tenants moves out; that person's "
    f"link reads '{MOVED_OUT_LINK_STATUS}', and '{FUTURE_LINK_STATUS}' is "
    "someone added who has not moved in. For who lives somewhere or who to "
    "contact about a lease, keep current links (a link with no status counts): "
    f"{current_link_sql('helixis_lease_tenants')}; for a tenant's history, read "
    "every link.",
]


def _company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


async def describe_mirror_schema_impl() -> str:
    """Column inventory for every analyst-readable mirror table.

    Runs OUR OWN query against information_schema (not agent SQL — the guard
    never sees this), filtered to the allowlist, so the schema the agent
    writes against is the live one, never a drifting hardcoded copy.

    ⚠ **That live read is exactly why migration 0101 needs a filter.** This is
    the one place in the product that enumerates COLUMNS, so a defaulted column
    added to 20 mirror tables would reach a Buildium customer's analyst as 20
    entries nobody added for them. ``sql_guard.hidden_columns`` is the one home
    for what is withheld, and ``executor.compose_scoped_sql`` reads the SAME set
    — telling the model a column does not exist while ``SELECT *`` hands it over
    would be worse than not filtering at all (gotcha 28).
    """
    if not get_settings().analysis.enabled:
        return _UNAVAILABLE
    try:
        pool = get_pool()
    except RuntimeError:
        return _DB_UNAVAILABLE
    hidden = hidden_columns(await company_pms(pool, _company_id()))
    # The tables the executor would refuse (``executor.run_analysis_query``,
    # the same ``Coverage.sees``) are left out of the schema and named, so the
    # model does not plan a query around a table that cannot answer.
    cov = await coverage_for(pool, _company_id())
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT table_name, column_name, data_type
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = ANY($1::text[])
                 ORDER BY table_name, ordinal_position
                """,
                sorted(ALLOWED_TABLES),
            )
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the loop
        logger.warning("describe_mirror_schema failed: %s", exc)
        return json.dumps({"ok": False, "error_kind": "query_error", "error": str(exc)[:400]})

    tables: dict[str, list[str]] = {}
    for r in rows:
        if r["column_name"] in hidden or not cov.sees(r["table_name"]):
            continue
        tables.setdefault(r["table_name"], []).append(f"{r['column_name']} {r['data_type']}")
    payload: dict[str, Any] = {"ok": True, "tables": tables, "notes": _SCHEMA_NOTES}
    unseen = sorted(t for t in ALLOWED_TABLES if not cov.sees(t))
    if unseen:
        payload["not_synced"] = unseen
    return json.dumps(payload)


async def run_analysis_impl(sql: str) -> str:
    if not get_settings().analysis.enabled:
        return _UNAVAILABLE
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    try:
        pool = get_pool()
    except RuntimeError:
        return _DB_UNAVAILABLE
    result = await run_analysis_query(sql, company_id, pool)
    return json.dumps(result.to_payload())


@function_tool
async def describe_mirror_schema() -> str:
    """List every table and column you may query with ``run_analysis``.

    Call this FIRST whenever you are not certain of a table or column name —
    guessing produces a query_error round-trip. The result maps each readable
    table to its columns with types.

    Returns:
        JSON: {"ok": true, "tables": {"helixis_bills": ["buildium_id bigint",
        "total_amount numeric", ...], ...}} or a structured error.
    """
    return await describe_mirror_schema_impl()


@function_tool
async def run_analysis(sql: str) -> str:
    """Run ONE read-only SQL query over the company's synced Buildium data.

    Rules the query must follow (violations come back as
    ``rejected_by_guard`` with the reason):
    - A single SELECT (or WITH … SELECT). No writes, no DDL, no comments,
      no ``$`` parameters, no schema prefixes — write ``helixis_bills``,
      never ``public.helixis_bills``.
    - Only the mirror tables from ``describe_mirror_schema``.
    - Results are capped at 500 rows (``truncated: true`` when the cap hit —
      aggregate instead of re-fetching).

    Scoping is automatic: you only ever see this company's rows. On
    ``query_error``, read the database's message, fix the query, and retry
    (at most twice).

    Args:
        sql: The complete SQL query text.

    Returns:
        JSON: {"ok": true, "columns": [...], "rows": [...], "row_count": n,
        "truncated": bool, "executed_sql": "..."} or a structured error.
    """
    return await run_analysis_impl(sql)


__all__ = [
    "MAX_ROWS",
    "describe_mirror_schema",
    "describe_mirror_schema_impl",
    "run_analysis",
    "run_analysis_impl",
]
