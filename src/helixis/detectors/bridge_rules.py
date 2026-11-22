"""Bridge detectors — the already-coded signals, brought onto the unified layer.

#3 rent-not-collected, #4 chronic-late resident, and #5 promise-to-pay-broken
already exist as live signals in separate stores (the ``reminders`` producers,
``entity_signals``, and ``payment_promises``). These re-express them as
first-class Detectors so the unified ``helixis_risk_flags`` finding store carries
all twelve — pure SQL over the mirror, same shape as the other detectors.

They ship **dark** (``default_enabled=False``): the existing producers keep
running unchanged, so nothing double-alerts until these are switched on via
``HELIXIS_DETECTORS__ENABLED_IDS`` and the overlapping legacy producers are
retired (a later consolidation step, not this PR).

Note (#4): a true *frequency* "chronic" signal (K-of-N late months) isn't
derivable — the mirror keeps one balance snapshot per lease, no month-by-month
history (see the discovery report). This detects current serious aging (61-90 /
90+) as the reachable proxy, and says so in the finding.
"""

from __future__ import annotations

import asyncpg

from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.registry import register

# ── thresholds ───────────────────────────────────────────────────────
RENT_NOT_COLLECTED_GRACE_DAY = 5  # don't flag before the 5th of the month
# Days of recent outreach that suppress the card — mirrors the retired
# rent_missing producer's SUPPRESS_DAYS so the gate behaves identically.
RENT_NOT_COLLECTED_SUPPRESS_DAYS = 5
AGING_TOLERANCE = 1.00
PROMISE_GRACE_DAYS = 1


def _unit_label(unit_number: str | None, lease_id: int) -> str:
    return f"unit {unit_number}" if unit_number else f"lease #{lease_id}"


# ── #3 rent not collected this month ─────────────────────────────────
_RENT_NOT_COLLECTED_SQL = """
SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id, u.unit_number,
       l.rent::float AS rent
  FROM helixis_leases l
  LEFT JOIN helixis_units u ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE l.company_id = $1::uuid
   AND l.status = 'Active'
   AND COALESCE(l.rent, 0) > 0
   AND EXTRACT(DAY FROM now()) >= $2
   AND NOT EXISTS (
        SELECT 1
          FROM helixis_lease_transactions t
         WHERE t.company_id = l.company_id
           AND t.lease_id = l.buildium_id
           AND t.transaction_type = 'Payment'
           AND t.transaction_date >= date_trunc('month', now())
   )
   -- Neglect gate, ported from the retired ``rent_missing`` reminder
   -- producer (2026-08-21). Without it this detector flags every unpaid
   -- lease including the ones the team already chased this week — one
   -- card instead of 41, but a card that cries wolf. A detector that
   -- reports handled work is how a desk gets ignored.
   AND NOT EXISTS (
        SELECT 1
          FROM helixis_activity_log a
         WHERE a.company_id = l.company_id
           AND a.entity_type IN ('lease', 'tenant')
           AND a.entity_id = l.buildium_id
           AND a.created_at >= now() - make_interval(days => $3)
   )
"""


class RentNotCollectedDetector(Detector):
    detector_id = "rent_not_collected"
    detector_class = STATE_RULE
    # ON by default (2026-08-21). This is now the ONLY producer of the
    # "rent hasn't posted this month" signal — the ``rent_missing`` reminder
    # producer that duplicated it has been unregistered. Leaving this dark
    # would have deleted the signal rather than improved it.
    default_enabled = True
    rule_keys = ("rent_not_collected",)
    reads_tables = frozenset(
        {"helixis_activity_log", "helixis_lease_transactions", "helixis_leases", "helixis_units"}
    )

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(
            _RENT_NOT_COLLECTED_SQL,
            company_id,
            RENT_NOT_COLLECTED_GRACE_DAY,
            RENT_NOT_COLLECTED_SUPPRESS_DAYS,
        )
        out: list[Finding] = []
        for r in rows:
            unit = _unit_label(r["unit_number"], r["lease_id"])
            out.append(
                Finding(
                    rule_key="rent_not_collected",
                    fingerprint=f"lease:{r['lease_id']}",
                    severity="warning",
                    title=f"No rent collected this month on {unit}",
                    detail=(
                        f"Lease #{r['lease_id']} bills ${r['rent']:,.0f}/mo but no payment has "
                        "posted since the 1st. Follow up before it ages into delinquency."
                    ),
                    amount=r["rent"],
                    property_id=r["property_id"],
                    entity_type="lease",
                    entity_id=r["lease_id"],
                    evidence={"lease_rent": r["rent"], "unit_id": r["unit_id"]},
                )
            )
        return out


# ── #4 chronic-late resident (serious aging proxy) ───────────────────
_CHRONIC_LATE_SQL = """
SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id, u.unit_number,
       lb.total_balance::float                 AS balance,
       COALESCE(lb.balance_61_90, 0)::float    AS b61_90,
       COALESCE(lb.balance_over_90, 0)::float  AS b_over_90
  FROM helixis_lease_balances lb
  JOIN helixis_leases l ON l.buildium_id = lb.lease_id AND l.company_id = lb.company_id
  LEFT JOIN helixis_units u ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE lb.company_id = $1::uuid
   AND l.status = 'Active'
   AND (COALESCE(lb.balance_61_90, 0) > $2 OR COALESCE(lb.balance_over_90, 0) > $2)
"""


class ChronicLateResidentDetector(Detector):
    detector_id = "chronic_late_resident"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("chronic_late_resident",)
    reads_tables = frozenset({"helixis_lease_balances", "helixis_leases", "helixis_units"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(_CHRONIC_LATE_SQL, company_id, AGING_TOLERANCE)
        out: list[Finding] = []
        for r in rows:
            unit = _unit_label(r["unit_number"], r["lease_id"])
            over90 = r["b_over_90"] > AGING_TOLERANCE
            bucket = "90+ days" if over90 else "61-90 days"
            out.append(
                Finding(
                    rule_key="chronic_late_resident",
                    fingerprint=f"lease:{r['lease_id']}",
                    severity="critical" if over90 else "warning",
                    title=f"Seriously past-due balance on {unit} ({bucket})",
                    detail=(
                        f"Lease #{r['lease_id']} carries ${r['balance']:,.2f} past due, with "
                        f"${r['b_over_90']:,.2f} over 90 days and ${r['b61_90']:,.2f} at 61-90 "
                        "days. (Aging snapshot — month-by-month payment history isn't mirrored, "
                        "so this flags current severity, not lateness frequency.)"
                    ),
                    amount=round(r["balance"], 2),
                    property_id=r["property_id"],
                    entity_type="lease",
                    entity_id=r["lease_id"],
                    evidence={
                        "total_balance": r["balance"],
                        "balance_61_90": r["b61_90"],
                        "balance_over_90": r["b_over_90"],
                    },
                )
            )
        return out


# ── #5 promise-to-pay came due ───────────────────────────────────────
_PROMISE_BROKEN_SQL = """
SELECT email, promised_date
  FROM helixis_payment_promises
 WHERE company_id = $1::uuid
   AND promised_date < current_date - $2::int
"""


class PromiseToPayBrokenDetector(Detector):
    detector_id = "promise_to_pay_broken"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("promise_to_pay_broken",)
    reads_tables = frozenset({"helixis_payment_promises"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(_PROMISE_BROKEN_SQL, company_id, PROMISE_GRACE_DAYS)
        out: list[Finding] = []
        for r in rows:
            email = r["email"]
            out.append(
                Finding(
                    rule_key="promise_to_pay_broken",
                    fingerprint=f"promise:{email}:{r['promised_date']}",
                    severity="warning",
                    title=f"Promise-to-pay came due — {email}",
                    detail=(
                        f"{email} promised to pay by {r['promised_date']}, which has passed. "
                        "Confirm the payment landed or follow up. (Promises are keyed by sender "
                        "email; tie to the lease before acting.)"
                    ),
                    amount=None,
                    entity_type="contact",
                    entity_id=None,
                    evidence={"email": email, "promised_date": str(r["promised_date"])},
                )
            )
        return out


# Register the three bridges (all dark; enable via HELIXIS_DETECTORS__ENABLED_IDS).
for _det in (
    RentNotCollectedDetector(),
    ChronicLateResidentDetector(),
    PromiseToPayBrokenDetector(),
):
    register(_det)
