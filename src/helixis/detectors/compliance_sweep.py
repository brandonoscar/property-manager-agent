"""#12 — on-demand parameterized compliance sweep.

Not scheduled. An operator triggers it with a rule change ("this fee is
prohibited as of date D", "leases in property X need a new addendum") and gets
back every currently-affected lease, each with a proposed remediation — the
high-volume output that batch disposition (a later UI stage) is built for.

Reachable surface (per the discovery report): the **lease** layer — status,
rent, dates, property. The mirror does NOT carry GL/memo on lease ledger
charges, and fee-schedules / recurring-charge schedules / notice-template
bodies aren't mirrored at all, so fee-*identity* targeting isn't possible here;
this sweeps by lease attributes and leaves fee-level rules to a live-API pass.

Findings persist under a per-sweep ``rule_key`` (``compliance:<label>``) in the
unified store, so re-running a sweep updates in place and resolves leases that
no longer match. Read-only: it proposes remediation text; it never acts.
"""

from __future__ import annotations

from datetime import date

import asyncpg

from helixis.detectors.base import ON_DEMAND, SOURCE_RULE, Detector, Finding
from helixis.detectors.store import persist_findings

_SWEEP_LEASES_SQL = """
SELECT l.buildium_id AS lease_id, l.property_id, l.unit_id, u.unit_number,
       l.rent::float AS rent, l.lease_from_date, l.lease_to_date
  FROM helixis_leases l
  LEFT JOIN helixis_units u ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
 WHERE l.company_id = $1::uuid
   AND l.status = 'Active'
   AND ($2::bigint[] IS NULL OR l.property_id = ANY($2::bigint[]))
   AND ($3::float IS NULL OR l.rent >= $3)
   AND ($4::float IS NULL OR l.rent <= $4)
   AND ($5::date IS NULL OR l.lease_from_date < $5)
"""


async def sweep_leases(
    conn: asyncpg.Connection,
    company_id: str,
    *,
    label: str,
    remediation: str,
    property_ids: list[int] | None = None,
    rent_min: float | None = None,
    rent_max: float | None = None,
    started_before: date | None = None,
) -> list[Finding]:
    """Return a Finding for every active lease matching the sweep filters,
    each carrying the proposed remediation. Pure read (no persistence)."""
    rule_key = f"compliance:{label}"
    rows = await conn.fetch(
        _SWEEP_LEASES_SQL,
        company_id,
        property_ids,
        rent_min,
        rent_max,
        started_before,
    )
    out: list[Finding] = []
    for r in rows:
        unit = f"unit {r['unit_number']}" if r["unit_number"] else f"lease #{r['lease_id']}"
        out.append(
            Finding(
                rule_key=rule_key,
                fingerprint=f"lease:{r['lease_id']}",
                severity="info",
                title=f"Compliance sweep '{label}' — {unit}",
                detail=(
                    f"Lease #{r['lease_id']} matches the '{label}' rule change. "
                    f"Proposed remediation: {remediation}"
                ),
                amount=None,
                property_id=r["property_id"],
                entity_type="lease",
                entity_id=r["lease_id"],
                evidence={
                    "sweep_label": label,
                    "remediation": remediation,
                    "lease_rent": r["rent"],
                    "lease_from_date": str(r["lease_from_date"]),
                    "filters": {
                        "property_ids": property_ids,
                        "rent_min": rent_min,
                        "rent_max": rent_max,
                        # ⚠ str(), like ``lease_from_date`` two lines up and
                        # for the same reason: ``persist_findings`` does
                        # ``json.dumps(f.evidence)`` and a ``datetime.date``
                        # raises there. Typing the parameter fixed the bind
                        # and would have broken the persist — the two live in
                        # different modules, so the working test selection
                        # would not have shown it.
                        "started_before": str(started_before) if started_before else None,
                    },
                },
            )
        )
    return out


class _SweepDetector(Detector):
    """A per-run detector identity so the on-demand sweep reuses the store's
    dedup + stale-resolve under this sweep's own ``compliance:<label>`` key."""

    detector_class = ON_DEMAND
    source = SOURCE_RULE
    default_enabled = False  # never in the scheduled cycle

    def __init__(self, label: str) -> None:
        self.detector_id = f"compliance_sweep:{label}"
        self.rule_keys = (f"compliance:{label}",)

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        raise NotImplementedError("compliance sweeps run via run_compliance_sweep, not the cycle")


async def run_compliance_sweep(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    label: str,
    remediation: str,
    property_ids: list[int] | None = None,
    rent_min: float | None = None,
    rent_max: float | None = None,
    started_before: date | None = None,
) -> list[Finding]:
    """Run the sweep and persist its findings into the unified store under
    ``compliance:<label>`` (re-running updates in place; leases that no longer
    match are auto-resolved). Returns the findings for the caller to surface."""
    async with pool.acquire() as conn:
        findings = await sweep_leases(
            conn,
            company_id,
            label=label,
            remediation=remediation,
            property_ids=property_ids,
            rent_min=rent_min,
            rent_max=rent_max,
            started_before=started_before,
        )
        await persist_findings(conn, company_id, _SweepDetector(label), findings)
    return findings


__all__ = ["run_compliance_sweep", "sweep_leases"]
