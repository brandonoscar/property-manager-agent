"""The morning brief — "the 5 things that need you today" (roadmap §7 rank 2).

Deterministic mirror + CRM rollups, severity-ordered, capped at five. Same
grounding rule as the first scan: no LLM in this path — every line is a row
count, so the brief can never invent urgency. Items with a zero count are
dropped entirely (an empty brief is a legitimate, good morning).

Severity order (hidden leaks → money → stuck work → windows → leads):
  1. open risk flags from the reconciliation sweep (0051)
  2. payment promises due or passed
  3. delinquent balances
  4. stalled work orders (open, untouched > 7 days)
  5. leases ending inside the renewal window
  6. CRM leads going cold
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.repositories.first_scan import EXPIRING_WINDOW_DAYS, STALLED_AFTER_DAYS
from helixis.db.repositories.leads import count_cold_leads

MAX_ITEMS = 5

_BRIEF_SQL = f"""
SELECT
    (SELECT COUNT(*) FROM helixis_risk_flags
      WHERE company_id = $1::uuid AND status = 'new')                     AS risk_flags,
    (SELECT COUNT(*) FROM helixis_payment_promises
      WHERE company_id = $1::uuid
        AND promised_date <= current_date + 1)                            AS promises_due,
    (SELECT COUNT(*) FROM helixis_lease_balances
      WHERE company_id = $1::uuid AND COALESCE(total_balance, 0) > 0)     AS delinquent_leases,
    (SELECT COALESCE(SUM(total_balance), 0) FROM helixis_lease_balances
      WHERE company_id = $1::uuid AND COALESCE(total_balance, 0) > 0)     AS delinquent_total,
    (SELECT COUNT(*) FROM helixis_work_orders
      WHERE company_id = $1::uuid AND {open_status_sql("status")}
        AND COALESCE(pms_updated_at, updated_at)
            < now() - make_interval(days => $3))                          AS stalled_work_orders,
    (SELECT COUNT(*) FROM helixis_leases
      WHERE company_id = $1::uuid AND status = 'Active'
        AND lease_to_date BETWEEN now()
            AND now() + make_interval(days => $2))                        AS expiring_leases
"""


@dataclass
class BriefItem:
    key: str
    count: int
    label: str
    detail: str
    view: str  # frontend navigation target: chat | inbox | properties | crm
    amount: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def build_morning_brief(pool: asyncpg.Pool, company_id: str) -> list[BriefItem]:
    """Compute today's brief for one company. Read-only."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_BRIEF_SQL, company_id, EXPIRING_WINDOW_DAYS, STALLED_AFTER_DAYS)
    cold = await count_cold_leads(pool, company_id=company_id)

    items: list[BriefItem] = []

    if row["risk_flags"] > 0:
        n = row["risk_flags"]
        items.append(
            BriefItem(
                key="risk_flags",
                count=n,
                label=f"{_plural(n, 'hidden risk')} found in your books",
                detail="Rent charged below the lease, possible duplicate bills — review each flag.",
                view="chat",
            )
        )

    if row["promises_due"] > 0:
        n = row["promises_due"]
        items.append(
            BriefItem(
                key="promises_due",
                count=n,
                label=f"{_plural(n, 'payment promise')} due or passed",
                detail="Tenants who said they'd pay — today is the day to follow up.",
                view="inbox",
            )
        )

    if row["delinquent_leases"] > 0:
        n = row["delinquent_leases"]
        total = float(row["delinquent_total"] or 0)
        items.append(
            BriefItem(
                key="delinquency",
                count=n,
                amount=total,
                label=f"${total:,.0f} owed across {_plural(n, 'lease')}",
                detail="Ask “who's behind on rent?” to see who actually needs the nudge.",
                view="chat",
            )
        )

    if row["stalled_work_orders"] > 0:
        n = row["stalled_work_orders"]
        items.append(
            BriefItem(
                key="stalled_work_orders",
                count=n,
                label=f"{_plural(n, 'work order')} stalled over {STALLED_AFTER_DAYS} days",
                detail="No update from anyone — residents are waiting.",
                view="inbox",
            )
        )

    if row["expiring_leases"] > 0:
        n = row["expiring_leases"]
        items.append(
            BriefItem(
                key="expiring_leases",
                count=n,
                label=f"{_plural(n, 'lease')} ending within {EXPIRING_WINDOW_DAYS} days",
                detail="Renewal windows open — decide before they become turnovers.",
                view="properties",
            )
        )

    if cold > 0:
        items.append(
            BriefItem(
                key="cold_leads",
                count=cold,
                label=f"{_plural(cold, 'lead')} going cold",
                detail="Quiet 3+ days — a nudge today keeps them in play.",
                view="crm",
            )
        )

    return items[:MAX_ITEMS]


__all__ = ["MAX_ITEMS", "BriefItem", "build_morning_brief"]
