"""New state-rule detectors over the current Buildium mirror (#6-#11).

Pure SQL over the mirrored entities — the same class as the ported
reconciliation rules, added onto the shared framework. Every one ships
**dark** (``default_enabled = False``): the scheduled cycle skips it until
its id is listed in ``HELIXIS_DETECTORS__ENABLED_IDS``, so merging cannot
flood the findings list before the attention-budget layer exists.

Detectors:
- #6  active_lease_no_rent          — Active lease with no rent set at all.
- #7  invoice_no_work_order         — Unpaid maintenance invoice, no work order.
- #8  lease_credit_balance          — Active lease carrying a credit balance
                                      (overpayment / misapplied payment).
- #9  utilities_owner_paid_occupied — Owner-paid utility bills on an OCCUPIED
                                      unit (recoverable; utility GL identified by
                                      a configured id list or a name heuristic).
- #10 invoice_above_norm            — A recent bill materially above the vendor's
                                      trailing median.
- #11 same_day_trip_charges         — Multiple same-day charges from one vendor
                                      at one property (batching would have saved
                                      trip charges).

Each carries its evidence (the exact ids/amounts) so a finding is never
"this looks wrong" without the numbers behind it.
"""

from __future__ import annotations

import os

import asyncpg

from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.registry import register
from helixis.pms import registry as pms_registry

# ── thresholds (conservative; findings are advisory) ─────────────────
INVOICE_NO_WO_MIN_AMOUNT = 200.0
INVOICE_NO_WO_LOOKBACK_DAYS = 180
MAINTENANCE_NAME_PATTERNS = [
    "%repair%",
    "%mainten%",
    "%plumb%",
    "%electric%",
    "%hvac%",
    "%landscap%",
    "%clean%",
    "%pest%",
    "%roof%",
    "%applianc%",
]
LEASE_CREDIT_TOLERANCE = 5.00
UTILITY_LOOKBACK_DAYS = 120
UTILITY_NAME_PATTERNS = [
    "%utilit%",
    "%electric%",
    "%water%",
    "%sewer%",
    "%gas%",
    "%trash%",
    "%garbage%",
]
INVOICE_NORM_MIN_HISTORY = 5
INVOICE_NORM_LOOKBACK_DAYS = 90
INVOICE_NORM_MULTIPLE = 2.0
INVOICE_NORM_MIN_GAP = 150.0
SAME_DAY_TRIPS_LOOKBACK_DAYS = 90

_UTILITY_GL_IDS_ENV = "HELIXIS_DETECTORS__UTILITY_GL_ACCOUNT_IDS"


def _configured_utility_gl_ids() -> list[int]:
    """The operator's one-time 'which GL accounts are utilities' tag, from a
    comma-separated env list. Empty → fall back to the name heuristic."""
    raw = os.getenv(_UTILITY_GL_IDS_ENV, "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                continue
    return ids


def _unit_label(unit_number: str | None, lease_or_unit_id: int) -> str:
    return f"unit {unit_number}" if unit_number else f"#{lease_or_unit_id}"


# ── #6 active lease with no rent set ─────────────────────────────────
_ACTIVE_LEASE_NO_RENT_SQL = """
SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id, u.unit_number
  FROM helixis_leases l
  LEFT JOIN helixis_units u ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE l.company_id = $1::uuid
   AND l.status = 'Active'
   -- A rent of $0, never a rent Occupella has not read. NULL is unknown:
   -- Rent Manager keeps rent on recurring charges, not on the lease
   -- (rentmanager/mapper.py, RM-11), so every one of its leases has NULL
   -- rent, and reading NULL as $0 would flag them all (rule 14). Measured
   -- 2026-10-05: no active Buildium or Rentvine lease has a NULL rent.
   AND l.rent = 0
"""


class ActiveLeaseNoRentDetector(Detector):
    detector_id = "active_lease_no_rent"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("active_lease_no_rent",)
    reads_tables = frozenset({"helixis_leases", "helixis_units"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(_ACTIVE_LEASE_NO_RENT_SQL, company_id)
        out: list[Finding] = []
        for r in rows:
            unit = _unit_label(r["unit_number"], r["lease_id"])
            out.append(
                Finding(
                    rule_key="active_lease_no_rent",
                    fingerprint=f"lease:{r['lease_id']}",
                    severity="warning",
                    title=f"Active lease with no rent set on {unit}",
                    detail=(
                        f"Lease #{r['lease_id']} is Active but its scheduled rent is $0. "
                        "Either the rent was never entered or the lease record is incomplete — "
                        "nothing will bill until it's set."
                    ),
                    amount=None,
                    property_id=r["property_id"],
                    entity_type="lease",
                    entity_id=r["lease_id"],
                    evidence={"lease_rent": 0, "unit_id": r["unit_id"]},
                )
            )
        return out


# ── #7 unpaid maintenance invoice with no work order ─────────────────
_INVOICE_NO_WO_SQL = """
SELECT DISTINCT b.buildium_id AS bill_id, b.vendor_id, v.name AS vendor_name,
       b.total_amount::float AS amount, b.bill_date
  FROM helixis_bills b
  JOIN helixis_bill_lines bl ON bl.bill_id = b.buildium_id AND bl.company_id = b.company_id
  JOIN helixis_gl_accounts g ON g.buildium_id = bl.gl_account_id AND g.company_id = b.company_id
  LEFT JOIN helixis_vendors v ON v.buildium_id = b.vendor_id AND v.company_id = b.company_id
 WHERE b.company_id = $1::uuid
   AND b.work_order_id IS NULL
   -- Only systems whose bill rows carry the work-order link
   -- (bill_link_systems): elsewhere NULL is unknown, not absent.
   AND b.pms = ANY($5::text[])
   AND b.is_paid = false
   AND COALESCE(b.total_amount, 0) >= $2
   AND b.bill_date >= current_date - $3::int
   AND g.name ILIKE ANY($4::text[])
"""


def bill_link_systems() -> list[str]:
    """The systems whose bill rows carry the work-order link, so a NULL there
    means the bill has no work order.

    Buildium's do. Any other system says so in its mapper
    (``BILL_WORK_ORDER_LINK = True``), and one that does not is left out: its
    NULL is unknown, never "no work order" (rule 14). Rentvine's payables row
    carries no ``workOrderID`` and Rent Manager's mapper writes ``None`` for
    every bill, so either would accuse every maintenance bill it holds. It
    used to be ``pms <> 'rentvine'``, which let each new system in by default.
    """
    found = [pms_registry.BUILDIUM]
    for key in pms_registry.PMS:
        if key != pms_registry.BUILDIUM and (
            pms_registry.connector_attr(key, "mapper", "BILL_WORK_ORDER_LINK") is True
        ):
            found.append(key)
    return found


class InvoiceNoWorkOrderDetector(Detector):
    detector_id = "invoice_no_work_order"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("invoice_no_work_order",)
    reads_tables = frozenset(
        {"helixis_bill_lines", "helixis_bills", "helixis_gl_accounts", "helixis_vendors"}
    )

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(
            _INVOICE_NO_WO_SQL,
            company_id,
            INVOICE_NO_WO_MIN_AMOUNT,
            INVOICE_NO_WO_LOOKBACK_DAYS,
            MAINTENANCE_NAME_PATTERNS,
            bill_link_systems(),
        )
        out: list[Finding] = []
        for r in rows:
            vendor = r["vendor_name"] or f"vendor #{r['vendor_id']}"
            out.append(
                Finding(
                    rule_key="invoice_no_work_order",
                    fingerprint=f"bill:{r['bill_id']}",
                    severity="info",
                    title=f"Unpaid invoice, no work order — {vendor} (${r['amount']:,.2f})",
                    detail=(
                        f"Bill #{r['bill_id']} from {vendor} for ${r['amount']:,.2f} "
                        f"({r['bill_date']}) is unpaid and has no linked work order. Verify the "
                        "work was authorized and completed before paying."
                    ),
                    amount=r["amount"],
                    entity_type="bill",
                    entity_id=r["bill_id"],
                    evidence={
                        "bill_id": r["bill_id"],
                        "vendor_id": r["vendor_id"],
                        "amount": r["amount"],
                        "bill_date": str(r["bill_date"]),
                        "is_paid": False,
                    },
                )
            )
        return out


# ── #8 active lease carrying a credit balance ────────────────────────
_LEASE_CREDIT_SQL = """
SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id, u.unit_number,
       lb.total_balance::float AS balance
  FROM helixis_lease_balances lb
  JOIN helixis_leases l ON l.buildium_id = lb.lease_id AND l.company_id = lb.company_id
  LEFT JOIN helixis_units u ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE lb.company_id = $1::uuid
   AND l.status = 'Active'
   AND lb.total_balance < -$2
"""


class LeaseCreditBalanceDetector(Detector):
    detector_id = "lease_credit_balance"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("lease_credit_balance",)
    reads_tables = frozenset({"helixis_lease_balances", "helixis_leases", "helixis_units"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(_LEASE_CREDIT_SQL, company_id, LEASE_CREDIT_TOLERANCE)
        out: list[Finding] = []
        for r in rows:
            unit = _unit_label(r["unit_number"], r["lease_id"])
            credit = round(-r["balance"], 2)
            out.append(
                Finding(
                    rule_key="lease_credit_balance",
                    fingerprint=f"lease:{r['lease_id']}",
                    severity="warning",
                    title=f"Credit balance on {unit} — payment may not reconcile",
                    detail=(
                        f"The ledger shows a ${credit:,.2f} credit on an active lease "
                        f"(#{r['lease_id']}). A lingering credit usually means an overpayment or "
                        "a payment applied to the wrong charge — reconcile it against the ledger."
                    ),
                    amount=credit,
                    property_id=r["property_id"],
                    entity_type="lease",
                    entity_id=r["lease_id"],
                    evidence={"total_balance": r["balance"], "credit": credit},
                )
            )
        return out


# ── #9 owner-paid utilities on an occupied unit ──────────────────────
_UTILITIES_OWNER_PAID_SQL = """
SELECT bl.unit_id, u.unit_number, u.property_id,
       COUNT(DISTINCT b.buildium_id) AS bill_count,
       SUM(bl.amount)::float         AS utility_total,
       MAX(b.bill_date)              AS latest_bill,
       array_agg(DISTINCT g.name)    AS gl_names
  FROM helixis_bill_lines bl
  JOIN helixis_bills b ON b.buildium_id = bl.bill_id AND b.company_id = bl.company_id
  JOIN helixis_units u ON u.buildium_id = bl.unit_id AND u.company_id = bl.company_id
  JOIN helixis_gl_accounts g ON g.buildium_id = bl.gl_account_id AND g.company_id = bl.company_id
 WHERE bl.company_id = $1::uuid
   AND u.is_occupied = true
   AND b.bill_date >= current_date - $2::int
   AND (
        (cardinality($3::bigint[]) > 0 AND g.buildium_id = ANY($3::bigint[]))
        OR (cardinality($3::bigint[]) = 0 AND g.name ILIKE ANY($4::text[]))
   )
 GROUP BY bl.unit_id, u.unit_number, u.property_id
"""


class UtilitiesOwnerPaidOccupiedDetector(Detector):
    detector_id = "utilities_owner_paid_occupied"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("utilities_owner_paid_occupied",)
    reads_tables = frozenset(
        {"helixis_bill_lines", "helixis_bills", "helixis_gl_accounts", "helixis_units"}
    )

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        gl_ids = _configured_utility_gl_ids()
        rows = await conn.fetch(
            _UTILITIES_OWNER_PAID_SQL,
            company_id,
            UTILITY_LOOKBACK_DAYS,
            gl_ids,
            UTILITY_NAME_PATTERNS,
        )
        out: list[Finding] = []
        for r in rows:
            unit = _unit_label(r["unit_number"], r["unit_id"])
            total = round(r["utility_total"] or 0.0, 2)
            gl_names = [n for n in (r["gl_names"] or []) if n]
            out.append(
                Finding(
                    rule_key="utilities_owner_paid_occupied",
                    fingerprint=f"unit:{r['unit_id']}",
                    severity="warning",
                    title=f"Owner-paid utilities on occupied {unit} (${total:,.2f})",
                    detail=(
                        f"{unit} is occupied, but ${total:,.2f} in utility bills "
                        f"({r['bill_count']} bill(s), latest {r['latest_bill']}) posted to the "
                        "owner's books in the last few months. If the resident should be paying, "
                        "the account may still be in the owner's name — recoverable cost."
                    ),
                    amount=total,
                    property_id=r["property_id"],
                    entity_type="unit",
                    entity_id=r["unit_id"],
                    evidence={
                        "unit_id": r["unit_id"],
                        "bill_count": r["bill_count"],
                        "utility_total": total,
                        "latest_bill": str(r["latest_bill"]),
                        "gl_accounts": gl_names,
                    },
                )
            )
        return out


# ── #10 invoice materially above the vendor's trailing norm ──────────
_INVOICE_ABOVE_NORM_SQL = """
WITH vendor_stats AS (
    SELECT vendor_id,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY total_amount) AS median_amount,
           COUNT(*) AS n
      FROM helixis_bills
     WHERE company_id = $1::uuid
       AND vendor_id IS NOT NULL
       AND COALESCE(total_amount, 0) > 0
     GROUP BY vendor_id
    HAVING COUNT(*) >= $2
)
SELECT b.buildium_id AS bill_id, b.vendor_id, v.name AS vendor_name,
       b.total_amount::float   AS amount, b.bill_date,
       s.median_amount::float  AS median, s.n
  FROM helixis_bills b
  JOIN vendor_stats s ON s.vendor_id = b.vendor_id
  LEFT JOIN helixis_vendors v ON v.buildium_id = b.vendor_id AND v.company_id = b.company_id
 WHERE b.company_id = $1::uuid
   AND b.bill_date >= current_date - $3::int
   AND COALESCE(b.total_amount, 0) > 0
   AND b.total_amount >= s.median_amount * $4
   AND b.total_amount - s.median_amount >= $5
"""


class InvoiceAboveNormDetector(Detector):
    detector_id = "invoice_above_norm"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("invoice_above_norm",)
    reads_tables = frozenset({"helixis_bills", "helixis_vendors"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(
            _INVOICE_ABOVE_NORM_SQL,
            company_id,
            INVOICE_NORM_MIN_HISTORY,
            INVOICE_NORM_LOOKBACK_DAYS,
            INVOICE_NORM_MULTIPLE,
            INVOICE_NORM_MIN_GAP,
        )
        out: list[Finding] = []
        for r in rows:
            vendor = r["vendor_name"] or f"vendor #{r['vendor_id']}"
            excess = round(r["amount"] - r["median"], 2)
            multiple = round(r["amount"] / r["median"], 1) if r["median"] else None
            out.append(
                Finding(
                    rule_key="invoice_above_norm",
                    fingerprint=f"bill:{r['bill_id']}",
                    severity="warning",
                    title=f"Invoice above the norm for {vendor} (${r['amount']:,.2f})",
                    detail=(
                        f"Bill #{r['bill_id']} from {vendor} is ${r['amount']:,.2f} "
                        f"({r['bill_date']}) against a trailing median of ${r['median']:,.2f} "
                        f"over {r['n']} bills — about {multiple}× the usual. "
                        "Worth a look before paying."
                    ),
                    amount=excess,
                    entity_type="bill",
                    entity_id=r["bill_id"],
                    evidence={
                        "amount": r["amount"],
                        "vendor_median": r["median"],
                        "multiple": multiple,
                        "sample_size": r["n"],
                        "bill_date": str(r["bill_date"]),
                        "vendor_id": r["vendor_id"],
                    },
                )
            )
        return out


# ── #11 multiple same-day charges from one vendor at one property ─────
_SAME_DAY_TRIPS_SQL = """
WITH bill_props AS (
    SELECT DISTINCT b.buildium_id, b.vendor_id, b.bill_date, b.total_amount, bl.property_id
      FROM helixis_bills b
      JOIN helixis_bill_lines bl ON bl.bill_id = b.buildium_id AND bl.company_id = b.company_id
     WHERE b.company_id = $1::uuid
       AND b.vendor_id IS NOT NULL
       AND bl.property_id IS NOT NULL
       AND b.bill_date >= current_date - $2::int
)
SELECT bp.vendor_id, v.name AS vendor_name, bp.property_id, bp.bill_date,
       COUNT(*)                                  AS bill_count,
       array_agg(bp.buildium_id ORDER BY bp.buildium_id) AS bill_ids,
       SUM(bp.total_amount)::float               AS total
  FROM bill_props bp
  LEFT JOIN helixis_vendors v ON v.buildium_id = bp.vendor_id AND v.company_id = $1::uuid
 GROUP BY bp.vendor_id, v.name, bp.property_id, bp.bill_date
HAVING COUNT(*) >= 2
"""


class SameDayTripChargesDetector(Detector):
    detector_id = "same_day_trip_charges"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("same_day_trip_charges",)
    reads_tables = frozenset({"helixis_bill_lines", "helixis_bills", "helixis_vendors"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        rows = await conn.fetch(_SAME_DAY_TRIPS_SQL, company_id, SAME_DAY_TRIPS_LOOKBACK_DAYS)
        out: list[Finding] = []
        for r in rows:
            vendor = r["vendor_name"] or f"vendor #{r['vendor_id']}"
            total = round(r["total"] or 0.0, 2)
            out.append(
                Finding(
                    rule_key="same_day_trip_charges",
                    fingerprint=f"vendor:{r['vendor_id']}:prop:{r['property_id']}:date:{r['bill_date']}",
                    severity="info",
                    title=f"{r['bill_count']} same-day charges from {vendor} at one property",
                    detail=(
                        f"{vendor} billed {r['bill_count']} times at property #{r['property_id']} "
                        f"on {r['bill_date']} (${total:,.2f} total). "
                        "Batching same-day visits usually avoids extra trip charges."
                    ),
                    amount=total,
                    property_id=r["property_id"],
                    entity_type="property",
                    entity_id=r["property_id"],
                    evidence={
                        "vendor_id": r["vendor_id"],
                        "property_id": r["property_id"],
                        "bill_date": str(r["bill_date"]),
                        "bill_count": r["bill_count"],
                        "bill_ids": list(r["bill_ids"]),
                    },
                )
            )
        return out


# Register the six (all dark by default; enable via HELIXIS_DETECTORS__ENABLED_IDS).
for _det in (
    ActiveLeaseNoRentDetector(),
    InvoiceNoWorkOrderDetector(),
    LeaseCreditBalanceDetector(),
    UtilitiesOwnerPaidOccupiedDetector(),
    InvoiceAboveNormDetector(),
    SameDayTripChargesDetector(),
):
    register(_det)
