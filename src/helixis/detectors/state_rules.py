"""State-rule detectors — pure SQL over the current mirror.

Detectors #1 (rent charged below the lease) and #2 (duplicate vendor bill),
ported verbatim from the original reconciliation engine into the shared
Detector framework. The SQL, thresholds, portfolio guard, conservative-gap
math, and evidence payloads are unchanged — the trust-contract unit tests
pin them.

Each rule is a ``Detector`` subclass registered into the framework registry
on import. Adding another state rule is one more class + ``register()``.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.registry import register

# ── thresholds (unchanged from the reconciliation engine) ────────────
RENT_GAP_TOLERANCE = 1.00
RENT_GAP_PORTFOLIO_MIN_AFFECTED = 5
RENT_GAP_PORTFOLIO_SHARE = 0.5
DUPLICATE_BILL_WINDOW_DAYS = 14
DUPLICATE_BILL_LOOKBACK_DAYS = 180


# ── Rule 1: rent charged below the lease ─────────────────────────────
_RENT_SQL = """
WITH bounds AS (
    SELECT date_trunc('month', now()) - interval '2 months' AS m0,
           date_trunc('month', now()) - interval '1 month'  AS m1,
           date_trunc('month', now())                        AS m_end
),
charges AS (
    SELECT t.lease_id,
           date_trunc('month', t.transaction_date) AS month,
           SUM(t.total_amount)                     AS charged
      FROM helixis_lease_transactions t, bounds b
     WHERE t.company_id = $1::uuid
       AND t.transaction_type = 'Charge'
       AND t.transaction_date >= b.m0
       AND t.transaction_date <  b.m_end
     GROUP BY 1, 2
)
SELECT l.buildium_id                    AS lease_id,
       l.property_id,
       l.unit_id,
       u.unit_number,
       l.rent::float                    AS rent,
       COALESCE(c0.charged, 0)::float   AS charged_m0,
       COALESCE(c1.charged, 0)::float   AS charged_m1,
       GREATEST(
           0,
           (EXTRACT(YEAR FROM age(now(), l.lease_from_date)) * 12
            + EXTRACT(MONTH FROM age(now(), l.lease_from_date)))::int
       )                                AS months_elapsed
  FROM helixis_leases l
  CROSS JOIN bounds b
  LEFT JOIN charges c0
    ON c0.lease_id = l.buildium_id AND c0.month = b.m0
  LEFT JOIN charges c1
    ON c1.lease_id = l.buildium_id AND c1.month = b.m1
  LEFT JOIN helixis_units u
    ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE l.company_id = $1::uuid
   AND l.status = 'Active'
   AND COALESCE(l.rent, 0) > 0
   AND l.lease_from_date < b.m0
   AND COALESCE(c0.charged, 0) < l.rent - $2
   AND COALESCE(c1.charged, 0) < l.rent - $2
"""

_RENT_ELIGIBLE_SQL = """
SELECT COUNT(*)
  FROM helixis_leases l
 WHERE l.company_id = $1::uuid
   AND l.status = 'Active'
   AND COALESCE(l.rent, 0) > 0
   AND l.lease_from_date < date_trunc('month', now()) - interval '2 months'
"""


async def _rule_rent_under_charged(conn: asyncpg.Connection, company_id: str) -> list[Finding]:
    rows = await conn.fetch(_RENT_SQL, company_id, RENT_GAP_TOLERANCE)

    # Portfolio guard: most of the book tripping at once = missing data,
    # not individual leaks. Only consult the denominator when enough
    # leases tripped to matter.
    if len(rows) >= RENT_GAP_PORTFOLIO_MIN_AFFECTED:
        eligible = await conn.fetchval(_RENT_ELIGIBLE_SQL, company_id)
        if eligible and len(rows) / eligible >= RENT_GAP_PORTFOLIO_SHARE:
            return [
                Finding(
                    rule_key="rent_under_charged",
                    fingerprint="portfolio:charge_gap",
                    severity="warning",
                    title="Rent charges look missing across the portfolio",
                    detail=(
                        f"{len(rows)} of {eligible} active leases show no or short rent "
                        f"charges in the last two full months. When most of the portfolio "
                        f"trips at once it usually means charge data isn't reaching Occupella "
                        f"(sync gap) or recurring charges stopped posting in Buildium — "
                        f"not {len(rows)} separate leaks. Check the Buildium connection and "
                        f"recurring-charge setup before chasing individual units."
                    ),
                    evidence={
                        "affected_leases": len(rows),
                        "eligible_leases": eligible,
                        "share": round(len(rows) / eligible, 3),
                    },
                )
            ]

    flags: list[Finding] = []
    for r in rows:
        unit = f"unit {r['unit_number']}" if r["unit_number"] else f"lease #{r['lease_id']}"
        never_charged = r["charged_m0"] == 0 and r["charged_m1"] == 0
        gap = round(r["rent"] - max(r["charged_m0"], r["charged_m1"]), 2)
        cumulative = round(gap * r["months_elapsed"], 2)
        if never_charged:
            title = f"No rent charged on {unit} for two months"
            detail = (
                f"The lease says ${r['rent']:,.0f}/mo but no charges posted in either of "
                f"the last two full months. If this has run since the lease began "
                f"({r['months_elapsed']} months), that is ≈${cumulative:,.0f} never billed."
            )
        else:
            title = f"Rent charged below the lease on {unit}"
            detail = (
                f"The lease says ${r['rent']:,.0f}/mo but the last two months posted "
                f"${r['charged_m0']:,.0f} and ${r['charged_m1']:,.0f} in charges — "
                f"${gap:,.0f}/mo short. If this gap has run since the lease began "
                f"({r['months_elapsed']} months), that is ≈${cumulative:,.0f} uncollected."
            )
        flags.append(
            Finding(
                rule_key="rent_under_charged",
                fingerprint=f"lease:{r['lease_id']}",
                severity="critical" if never_charged else "warning",
                title=title,
                detail=detail,
                amount=gap,
                property_id=r["property_id"],
                entity_type="lease",
                entity_id=r["lease_id"],
                evidence={
                    "lease_rent": r["rent"],
                    "charged_last_full_month": r["charged_m1"],
                    "charged_prior_month": r["charged_m0"],
                    "monthly_gap": gap,
                    "months_elapsed": r["months_elapsed"],
                    "estimated_cumulative": cumulative,
                },
            )
        )
    return flags


# ── Rule 2: duplicate vendor bills ───────────────────────────────────
_DUP_BILLS_SQL = """
SELECT b1.buildium_id            AS bill_a,
       b2.buildium_id            AS bill_b,
       b1.vendor_id,
       v.name                    AS vendor_name,
       b1.total_amount::float    AS amount,
       b1.bill_date              AS date_a,
       b2.bill_date              AS date_b,
       b1.is_paid                AS paid_a,
       b2.is_paid                AS paid_b
  FROM helixis_bills b1
  JOIN helixis_bills b2
    ON b2.company_id = b1.company_id
   AND b2.vendor_id = b1.vendor_id
   AND b2.total_amount = b1.total_amount
   AND b2.buildium_id > b1.buildium_id
   AND ABS(b2.bill_date - b1.bill_date) <= $2
  LEFT JOIN helixis_vendors v
    ON v.buildium_id = b1.vendor_id AND v.company_id = b1.company_id
 WHERE b1.company_id = $1::uuid
   AND COALESCE(b1.total_amount, 0) > 0
   AND b1.vendor_id IS NOT NULL
   AND b1.bill_date >= current_date - $3::int
   AND NOT (
       b1.reference_number IS NOT NULL
       AND b2.reference_number IS NOT NULL
       AND b1.reference_number <> b2.reference_number
   )
"""


async def _rule_duplicate_vendor_bills(conn: asyncpg.Connection, company_id: str) -> list[Finding]:
    rows = await conn.fetch(
        _DUP_BILLS_SQL, company_id, DUPLICATE_BILL_WINDOW_DAYS, DUPLICATE_BILL_LOOKBACK_DAYS
    )
    flags: list[Finding] = []
    for r in rows:
        vendor = r["vendor_name"] or f"vendor #{r['vendor_id']}"
        both_paid = bool(r["paid_a"]) and bool(r["paid_b"])
        detail = (
            f"Two bills from {vendor} for ${r['amount']:,.2f} were entered "
            f"{abs((r['date_b'] - r['date_a']).days)} day(s) apart "
            f"(#{r['bill_a']} on {r['date_a']} and #{r['bill_b']} on {r['date_b']})."
        )
        if both_paid:
            detail += " Both are marked paid — if this is one invoice, it was paid twice."
        flags.append(
            Finding(
                rule_key="duplicate_vendor_bill",
                fingerprint=f"bills:{r['bill_a']}:{r['bill_b']}",
                severity="critical" if both_paid else "warning",
                title=f"Possible duplicate bill from {vendor} (${r['amount']:,.2f})",
                detail=detail,
                amount=r["amount"],
                entity_type="bill",
                entity_id=r["bill_b"],
                evidence={
                    "bill_ids": [r["bill_a"], r["bill_b"]],
                    "vendor_id": r["vendor_id"],
                    "amount": r["amount"],
                    "dates": [str(r["date_a"]), str(r["date_b"])],
                    "both_paid": both_paid,
                },
            )
        )
    return flags


# ── Detector wrappers + registration ─────────────────────────────────
class RentUnderChargedDetector(Detector):
    detector_id = "rent_under_charged"
    detector_class = STATE_RULE
    rule_keys = ("rent_under_charged",)
    reads_tables = frozenset({"helixis_lease_transactions", "helixis_leases", "helixis_units"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        return await _rule_rent_under_charged(conn, company_id)


class DuplicateVendorBillDetector(Detector):
    detector_id = "duplicate_vendor_bill"
    detector_class = STATE_RULE
    rule_keys = ("duplicate_vendor_bill",)
    reads_tables = frozenset({"helixis_bills", "helixis_vendors"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        return await _rule_duplicate_vendor_bills(conn, company_id)


# Registration order is load-bearing: rent then duplicate (the reconciliation
# tests assert the scan runs rules in this sequence).
_RENT: Any = register(RentUnderChargedDetector())
_DUP: Any = register(DuplicateVendorBillDetector())
