"""Read the findings ledger and run it through the attention budget.

Thin DB layer over ``helixis_risk_flags``: pull the OPEN, unseen findings
(``status='new'`` — a ``confirmed`` finding is acknowledged and a ``dismissed``
one is suppressed, so neither re-notices) and hand them to the pure
``detectors.attention`` engine. Also computes per-detector dismissal rates for
the calibration report.

Best-effort like the rest of the "noticed" surface: a DB hiccup returns an
empty digest rather than raising — this is an enhancement, never load-bearing.
"""

from __future__ import annotations

import logging

import asyncpg

from helixis.detectors.attention import (
    DEFAULT_DAILY_CAP,
    CalibrationReport,
    NoticedDigest,
    NoticedFinding,
    detector_calibration,
    select_noticed,
)

logger = logging.getLogger(__name__)

_OPEN_FINDINGS_SQL = """
SELECT id, rule_key, severity, title, detail, amount, property_id,
       entity_type, entity_id, last_seen_at
  FROM helixis_risk_flags
 WHERE company_id = $1::uuid
   AND status = 'new'
 ORDER BY last_seen_at DESC
 LIMIT 500
"""

_CALIBRATION_SQL = """
SELECT rule_key,
       COUNT(*)                                   AS total,
       COUNT(*) FILTER (WHERE status = 'dismissed') AS dismissed
  FROM helixis_risk_flags
 WHERE company_id = $1::uuid
 GROUP BY rule_key
"""


def _to_finding(row: asyncpg.Record) -> NoticedFinding:
    amount = row["amount"]
    return NoticedFinding(
        id=str(row["id"]),
        rule_key=row["rule_key"],
        severity=row["severity"],
        title=row["title"],
        detail=row["detail"],
        amount=float(amount) if amount is not None else None,
        property_id=row["property_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        last_seen=row["last_seen_at"],
    )


async def load_noticed(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> NoticedDigest:
    """The company's attention-budgeted "Occupella noticed" digest."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_OPEN_FINDINGS_SQL, company_id)
    except (Exception, SystemExit):  # noqa: BLE001 — noticed is an enhancement, never load-bearing
        logger.warning("noticed digest unavailable; returning empty", exc_info=True)
        return NoticedDigest()
    findings = [_to_finding(r) for r in rows]
    return select_noticed(findings, daily_cap=daily_cap)


async def load_calibration(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    min_samples: int = 5,
) -> list[CalibrationReport]:
    """Per-detector dismissal rates; miscalibrated ones flagged."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_CALIBRATION_SQL, company_id)
    except (Exception, SystemExit):  # noqa: BLE001 — best-effort
        logger.warning("calibration report unavailable; returning empty", exc_info=True)
        return []
    counts = {r["rule_key"]: (int(r["dismissed"]), int(r["total"])) for r in rows}
    return detector_calibration(counts, min_samples=min_samples)


__all__ = ["load_calibration", "load_noticed"]
