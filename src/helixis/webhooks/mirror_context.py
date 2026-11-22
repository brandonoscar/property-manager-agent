"""Cross-record context for "What Helixis noticed" — the mirror payoff.

The noticed section's job is to surface what a PM CAN'T read in the
message itself. A maintenance request says "the sink leaks"; the mirror
knows the same unit had three tickets in 90 days, the tenant hasn't paid
this month, and the lease ends in six weeks. Those cross-record facts
are what make the card substance instead of a restatement.

Everything here is local SQL against the entity mirror (migrations
0038–0047) — zero Buildium API calls, so it runs on every planner pass
for free. Facts are DETERMINISTIC strings: the worker feeds them to the
planner as verified grounding AND merges them into the final signals as
a floor, so the section has substance even when the LLM under-delivers.

Failure posture: every probe is individually contained and the gatherer
never raises — a missing table (unapplied migration) or a bad query
just means fewer facts, never a blocked card.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.pms_coverage import coverage_for
from helixis.db.tenant_links import current_link_sql
from helixis.reminders.promises import promise_pending

logger = logging.getLogger(__name__)

# Cards read best with a handful of sharp facts, not a dossier.
MAX_FACTS = 5

# Lease-expiry lookahead mirroring the renewals window.
_LEASE_ENDING_DAYS = 90

# Skip the "no rent this month" fact in the first days of a month —
# before then it's true of almost every lease and reads as noise.
_RENT_FACT_MIN_DAY = 5


async def gather_mirror_context(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    property_id: int | None = None,
    unit_id: int | None = None,
    lease_id: int | None = None,
    tenant_emails: tuple[str, ...] | list[str] = (),
    exclude_task_id: int | None = None,
    max_facts: int = MAX_FACTS,
    today: dt.date | None = None,
) -> list[str]:
    """Return up to ``max_facts`` cross-record facts about the entities an
    event touches, ordered by how much a PM would care. Never raises."""
    facts: list[str] = []
    today = today or dt.date.today()
    emails = sorted({e.strip().lower() for e in tenant_emails if e and e.strip()})
    # "No rent payment has posted" is a claim about ABSENCE, so it is made only
    # where the ledger is known to be complete (``pms_coverage``): a Rentvine
    # history that has not landed has no Payment row for every lease.
    payments_known = (await coverage_for(pool, company_id)).sees("helixis_lease_transactions")
    try:
        async with pool.acquire() as conn:
            # An email event carries only a sender — resolve their active
            # lease so the money/lease facts light up for emails too.
            # ⚠ Through a CURRENT link only (``db.tenant_links``): a tenant who
            # moved out keeps a MovedOut link on the lease the others still
            # hold, and their email must not light up that lease's money.
            if lease_id is None and emails:
                row = await _probe(
                    conn.fetchrow(
                        f"""
                        SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id
                          FROM helixis_tenants t
                          JOIN helixis_lease_tenants lt
                            ON lt.tenant_buildium_id = t.buildium_id
                           AND lt.company_id = t.company_id
                          JOIN helixis_leases l
                            ON l.buildium_id = lt.lease_buildium_id
                           AND l.company_id = t.company_id
                         WHERE t.company_id = $1::uuid
                           AND lower(t.email) = ANY($2::text[])
                           AND l.status = 'Active'
                           AND {current_link_sql()}
                         LIMIT 1
                        """,
                        company_id,
                        emails,
                    ),
                    "sender lease resolve",
                )
                if row:
                    lease_id = row["lease_id"]
                    property_id = property_id or row["property_id"]
                    unit_id = unit_id or row["unit_id"]

            if lease_id is not None and not emails:
                rows = await _probe(
                    conn.fetch(
                        f"""
                        SELECT DISTINCT lower(t.email) AS email
                          FROM helixis_lease_tenants lt
                          JOIN helixis_tenants t
                            ON t.buildium_id = lt.tenant_buildium_id
                           AND t.company_id = lt.company_id
                         WHERE lt.company_id = $1::uuid
                           AND lt.lease_buildium_id = $2
                           AND {current_link_sql()}
                           AND t.email IS NOT NULL
                        """,
                        company_id,
                        lease_id,
                    ),
                    "lease tenant emails",
                )
                emails = [r["email"] for r in rows or []]

            if lease_id is not None:
                # Operational memory (#534 Stage B) first — the stored
                # late-payer / delinquency signal is the sharpest thing a PM
                # can't read off the message, so it leads the noticed card and
                # the drafted-reply context.
                await _entity_signal_facts(conn, facts, company_id, lease_id)
                await _lease_facts(
                    conn, facts, company_id, lease_id, today, payments_known=payments_known
                )
            if emails:
                await _promise_fact(conn, facts, company_id, emails, today)
            if unit_id is not None:
                await _unit_facts(conn, facts, company_id, unit_id, exclude_task_id)
            if lease_id is not None:
                await _turnover_fact(conn, facts, company_id, lease_id)
            if property_id is not None:
                await _property_fact(conn, facts, company_id, property_id, exclude_task_id)
    except (Exception, SystemExit):
        logger.exception("mirror context gather failed — planning without it")
    return facts[:max_facts]


async def _probe(coro: Any, label: str) -> Any:
    """Await one probe, containing its failure (a fact is never worth
    losing the card over)."""
    try:
        return await coro
    except (Exception, SystemExit):
        logger.warning("mirror context probe failed: %s", label, exc_info=True)
        return None


async def _entity_signal_facts(conn: Any, facts: list[str], company_id: str, lease_id: int) -> None:
    """Inject stored operational memory (#534 Stage B) — the deterministic
    late-payer / delinquency signal derived from the aged-receivables and
    anchored to the lease. Read-only + best-effort (its own try via _probe);
    an unapplied migration or empty result just contributes nothing."""
    from helixis.webhooks.operational_signals import get_lease_entity_signals

    signals = await _probe(
        get_lease_entity_signals(conn, company_id=company_id, lease_id=lease_id),
        "entity signals",
    )
    for signal in signals or []:
        if signal and signal not in facts:
            facts.append(signal)


async def _lease_facts(
    conn: Any,
    facts: list[str],
    company_id: str,
    lease_id: int,
    today: dt.date,
    *,
    payments_known: bool = True,
) -> None:
    row = await _probe(
        conn.fetchrow(
            """
            SELECT b.total_balance,
                   l.lease_to_date,
                   EXISTS (
                       SELECT 1 FROM helixis_lease_transactions t
                        WHERE t.company_id = l.company_id
                          AND t.lease_id = l.buildium_id
                          AND t.transaction_type = 'Payment'
                          AND t.transaction_date >= date_trunc('month', now())
                   ) AS paid_this_month
              FROM helixis_leases l
              LEFT JOIN helixis_lease_balances b
                ON b.company_id = l.company_id AND b.lease_id = l.buildium_id
             WHERE l.company_id = $1::uuid AND l.buildium_id = $2
            """,
            company_id,
            lease_id,
        ),
        "lease facts",
    )
    if not row:
        return
    balance = float(row["total_balance"] or 0)
    if balance > 0:
        facts.append(f"This lease carries an outstanding balance of ${balance:,.2f}.")
    if payments_known and not row["paid_this_month"] and today.day >= _RENT_FACT_MIN_DAY:
        facts.append("No rent payment has posted on this lease this month.")
    end = row["lease_to_date"]
    if end is not None:
        end_date = end.date() if isinstance(end, dt.datetime) else end
        days_left = (end_date - today).days
        if 0 <= days_left <= _LEASE_ENDING_DAYS:
            facts.append(
                f"The lease ends {end_date.isoformat()} — {days_left} days out, "
                "inside the renewal window."
            )


async def _promise_fact(
    conn: Any, facts: list[str], company_id: str, emails: list[str], today: dt.date
) -> None:
    row = await _probe(
        conn.fetchrow(
            """
            SELECT MAX(promised_date) AS promised
              FROM helixis_payment_promises
             WHERE company_id = $1::uuid AND email = ANY($2::text[])
            """,
            company_id,
            emails,
        ),
        "payment promise",
    )
    promised = row["promised"] if row else None
    if promised is None:
        return
    if promise_pending(promised, today=today):
        facts.append(f"The tenant promised payment by {promised.isoformat()} (from an email).")
    elif (today - promised).days <= 31:
        facts.append(
            f"The tenant promised payment by {promised.isoformat()} — that date has passed."
        )


async def _unit_facts(
    conn: Any, facts: list[str], company_id: str, unit_id: int, exclude_task_id: int | None
) -> None:
    row = await _probe(
        conn.fetchrow(
            f"""
            SELECT (SELECT count(*) FROM helixis_work_orders wo
                     WHERE wo.company_id = $1::uuid AND wo.unit_id = $2
                       AND {open_status_sql("wo.status")}) AS open_wos,
                   (SELECT count(*) FROM helixis_tasks tk
                     WHERE tk.company_id = $1::uuid AND tk.unit_id = $2
                       AND tk.buildium_id IS DISTINCT FROM $3
                       AND tk.updated_at >= now() - interval '90 days') AS recent_tasks
            """,
            company_id,
            unit_id,
            exclude_task_id,
        ),
        "unit facts",
    )
    if not row:
        return
    open_wos = row["open_wos"] or 0
    if open_wos:
        facts.append(
            f"{open_wos} work order{'s are' if open_wos > 1 else ' is'} already open on this unit."
        )
    recent = row["recent_tasks"] or 0
    if recent >= 2:
        facts.append(f"This unit has had {recent} other tasks in the last 90 days.")


async def _turnover_fact(conn: Any, facts: list[str], company_id: str, lease_id: int) -> None:
    row = await _probe(
        conn.fetchrow(
            """
            SELECT move_out_date FROM helixis_turnovers
             WHERE company_id = $1::uuid AND lease_buildium_id = $2
               AND status = 'active'
            """,
            company_id,
            lease_id,
        ),
        "turnover",
    )
    if row:
        when = f" (move-out {row['move_out_date'].isoformat()})" if row["move_out_date"] else ""
        facts.append(f"A turnover is in progress on this lease{when}.")


async def _property_fact(
    conn: Any, facts: list[str], company_id: str, property_id: int, exclude_task_id: int | None
) -> None:
    row = await _probe(
        conn.fetchrow(
            f"""
            SELECT count(*) AS overdue FROM helixis_tasks
             WHERE company_id = $1::uuid AND property_id = $2
               AND buildium_id IS DISTINCT FROM $3
               AND {open_status_sql("status")}
               AND due_date < current_date
            """,
            company_id,
            property_id,
            exclude_task_id,
        ),
        "property facts",
    )
    overdue = (row["overdue"] or 0) if row else 0
    if overdue:
        facts.append(
            f"{overdue} other task{'s are' if overdue > 1 else ' is'} overdue at this property."
        )


__all__ = ["MAX_FACTS", "gather_mirror_context"]
