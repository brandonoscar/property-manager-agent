"""Operational memory — deterministic entity signals (#534 Stage B).

Stage A (:mod:`helixis.memory.entity_memory`) added entity-anchored write/read
on top of mem0 and pure fact derivers. Stage B feeds OPERATIONAL signals
(late-payer, delinquency) into memory, anchored to the Buildium entity id, and
injects them into the "What Helixis noticed" enrichment card + drafted replies.

Design decision (RAM-constrained worker, gotcha #9): the webhook worker never
initializes mem0, so operational signals take the **cheaper deterministic
path** — derived from the mirror and stored in ``helixis_entity_signals``
(migration 0054), NOT via mem0. No mem0 in the worker → zero new RAM load; the
API process keeps mem0 for chat-learned facts.

Grounding (confirmed live against the sandbox 2026-07-11): ``LeaseTransaction``
has no due-date field, so charge→payment pairing would require GUESSING the due
date — a wrong "chronic late-payer" flag is harmful. The RELIABLE lateness
signal is Buildium's OWN aged-receivables computation in
``helixis_lease_balances`` (``balance_31_60`` / ``balance_61_90`` /
``balance_over_90``). There is one balance snapshot per lease (no month-by-month
history), so we derive current delinquency severity, not a "K of N months"
frequency. The snapshot is refreshed by the daily backfill, so on a payment
event it may be up to a day stale — acceptable for an advisory signal that
re-derives on every event and every daily sync.

Discipline (guardrails): signals are **advisory** (surfaced, never auto-acted);
PII-stripped before store; every read/write is **company-scoped**; each write
(and each auto-resolve) is audited via ``memory_audit`` (action ``operational``,
migration 0055); derivation is cheap + deterministic — NO per-event LLM call.
Best-effort throughout: any failure degrades to "no signal" and never breaks
the webhook pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from helixis.memory.audit import write_operational_audit
from helixis.memory.entity_memory import (
    AgingSummary,
    delinquency_severity,
    derive_delinquency_fact,
)
from helixis.security.pii import redact

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# The entity a payment/lease signal is anchored to. A lease maps 1:1 to a unit,
# and the balance snapshot is per-lease, so the lease is the natural anchor; the
# unit number is carried as the human label ("Unit 2B").
_ENTITY_TYPE_LEASE = "lease"
# One signal category per (entity) — the aging-derived late-payer flag. Kept
# distinct from the plain outstanding-balance fact the existing mirror context
# already surfaces (mirror_context._lease_facts), so the card isn't redundant.
_CATEGORY_DELINQUENCY = "payment_behavior"


@dataclass(frozen=True)
class LeaseDelinquency:
    """A lease's current aged-receivables snapshot + its human label, read from
    the mirror. ``aging`` is ``None`` when the lease has no balance row (current
    / caught-up) — the signal is then cleared."""

    lease_id: int
    label: str
    aging: AgingSummary | None


async def read_lease_delinquency(
    conn: asyncpg.Connection,
    *,
    company_id: str,
    lease_id: int,
) -> LeaseDelinquency | None:
    """Read a lease's aging buckets + unit label from the mirror (company-scoped
    raw SQL — there is no repository for these tables). Returns ``None`` only on
    a query failure; a lease with no balance row returns a ``LeaseDelinquency``
    with ``aging=None`` (caught-up → clear any stored signal)."""
    try:
        row = await conn.fetchrow(
            """
            SELECT b.total_balance, b.balance_31_60, b.balance_61_90,
                   b.balance_over_90, b.snapshot_at, b.unit_id, u.unit_number
              FROM helixis_lease_balances b
              LEFT JOIN helixis_units u
                ON u.buildium_id = b.unit_id
               AND u.company_id = b.company_id
             WHERE b.company_id = $1::uuid
               AND b.lease_id = $2
             LIMIT 1
            """,
            company_id,
            lease_id,
        )
    except Exception:  # noqa: BLE001 — best-effort; a probe failure yields no signal
        logger.warning("operational signals: balance read failed for lease %s", lease_id)
        return None

    unit_number = (row["unit_number"] if row else None) or ""
    label = f"Unit {unit_number}".strip() if unit_number else f"Lease {lease_id}"
    if row is None:
        return LeaseDelinquency(lease_id=lease_id, label=label, aging=None)

    snapshot = row["snapshot_at"]
    as_of = snapshot.date() if hasattr(snapshot, "date") else date.today()
    aging = AgingSummary(
        overdue_31_60=float(row["balance_31_60"] or 0),
        overdue_61_90=float(row["balance_61_90"] or 0),
        overdue_over_90=float(row["balance_over_90"] or 0),
        as_of=as_of,
    )
    return LeaseDelinquency(lease_id=lease_id, label=label, aging=aging)


async def _store_signal(
    conn: asyncpg.Connection,
    *,
    company_id: str,
    entity_id: str,
    entity_label: str,
    category: str,
    signal: str,
    severity: str,
    evidence: dict,
) -> str | None:
    """Upsert one entity signal (company-scoped) and return its id. PII-stripped
    before store. Best-effort — returns ``None`` on failure."""
    import json

    clean = redact(signal)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO helixis_entity_signals
                (company_id, entity_type, entity_id, entity_label, category,
                 signal, severity, source, evidence, first_seen_at, last_seen_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, 'operational', $8::jsonb, now(), now())
            ON CONFLICT (company_id, entity_type, entity_id, category)
            DO UPDATE SET signal = EXCLUDED.signal,
                          entity_label = EXCLUDED.entity_label,
                          severity = EXCLUDED.severity,
                          evidence = EXCLUDED.evidence,
                          last_seen_at = now()
            RETURNING id::text
            """,
            company_id,
            _ENTITY_TYPE_LEASE,
            entity_id,
            entity_label,
            category,
            clean,
            severity,
            json.dumps(evidence),
        )
    except Exception:  # noqa: BLE001 — best-effort; a store failure yields no signal
        logger.warning("operational signals: store failed for %s/%s", entity_id, category)
        return None
    return row["id"] if row else None


async def _clear_signal(
    conn: asyncpg.Connection, *, company_id: str, entity_id: str, category: str
) -> bool:
    """Delete a stored signal (a lease that caught up auto-resolves). Returns
    True when a row was removed. Best-effort."""
    try:
        result = await conn.execute(
            """
            DELETE FROM helixis_entity_signals
             WHERE company_id = $1::uuid AND entity_type = $2
               AND entity_id = $3 AND category = $4
            """,
            company_id,
            _ENTITY_TYPE_LEASE,
            entity_id,
            category,
        )
    except Exception:  # noqa: BLE001
        logger.warning("operational signals: clear failed for %s/%s", entity_id, category)
        return False
    # asyncpg returns e.g. "DELETE 1"
    return result.endswith(("1", "2", "3", "4", "5", "6", "7", "8", "9")) and not result.endswith(
        " 0"
    )


async def derive_and_store_lease_signals(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    lease_id: int,
) -> str | None:
    """Derive the deterministic late-payer signal for a lease from the mirror,
    store it (or clear it when the lease is caught up), and audit the write.
    Returns the stored signal text, or ``None`` when nothing is flagged.

    Called best-effort from the LeaseTransaction webhook handler AFTER the
    mirror is updated. Never raises — a signal is a bonus on top of the mirror
    sync, never allowed to break the pipeline."""
    try:
        async with pool.acquire() as conn:
            delinquency = await read_lease_delinquency(
                conn, company_id=company_id, lease_id=lease_id
            )
            if delinquency is None:
                return None

            entity_id = str(lease_id)
            fact = (
                derive_delinquency_fact(delinquency.aging)
                if delinquency.aging is not None
                else None
            )

            if fact is None:
                # Caught up (or no balance row) → auto-resolve any stale flag.
                cleared = await _clear_signal(
                    conn, company_id=company_id, entity_id=entity_id, category=_CATEGORY_DELINQUENCY
                )
                if cleared:
                    await write_operational_audit(
                        company_id=company_id,
                        content=f"cleared late-payer signal for {delinquency.label}",
                        metadata={
                            "entity_type": _ENTITY_TYPE_LEASE,
                            "entity_id": entity_id,
                            "category": _CATEGORY_DELINQUENCY,
                            "op": "clear",
                        },
                    )
                return None

            aging = delinquency.aging
            assert aging is not None  # fact is not None ⇒ aging present
            severity = delinquency_severity(aging)
            evidence = {
                "overdue_31_60": aging.overdue_31_60,
                "overdue_61_90": aging.overdue_61_90,
                "overdue_over_90": aging.overdue_over_90,
                "as_of": aging.as_of.isoformat(),
            }
            signal_id = await _store_signal(
                conn,
                company_id=company_id,
                entity_id=entity_id,
                entity_label=delinquency.label,
                category=_CATEGORY_DELINQUENCY,
                signal=fact,
                severity=severity,
                evidence=evidence,
            )
            if signal_id is not None:
                await write_operational_audit(
                    company_id=company_id,
                    content=f"{delinquency.label}: {fact}",
                    memory_id=signal_id,
                    metadata={
                        "entity_type": _ENTITY_TYPE_LEASE,
                        "entity_id": entity_id,
                        "category": _CATEGORY_DELINQUENCY,
                        "severity": severity,
                        "source": "operational",
                        "op": "store",
                        "evidence": evidence,
                    },
                )
            return fact
    except Exception:  # noqa: BLE001 — defensive; every step is already best-effort
        logger.warning("operational signals: derive/store failed for lease %s", lease_id)
        return None


async def get_lease_entity_signals(
    conn: asyncpg.Connection,
    *,
    company_id: str,
    lease_id: int,
) -> list[str]:
    """Read stored operational signal strings for a lease, prefixed with the
    entity label ("Unit 2B: chronic late-payer …") for the noticed card / draft
    context. Company-scoped; most-severe first. Best-effort — returns ``[]`` on
    failure."""
    try:
        rows = await conn.fetch(
            """
            SELECT entity_label, signal
              FROM helixis_entity_signals
             WHERE company_id = $1::uuid
               AND entity_type = $2
               AND entity_id = $3
             ORDER BY CASE severity
                        WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                      last_seen_at DESC
            """,
            company_id,
            _ENTITY_TYPE_LEASE,
            str(lease_id),
        )
    except Exception:  # noqa: BLE001 — best-effort; no signal beats a broken card
        logger.warning("operational signals: read failed for lease %s", lease_id)
        return []
    out: list[str] = []
    for row in rows or []:
        label = (row["entity_label"] or "").strip()
        signal = (row["signal"] or "").strip()
        if not signal:
            continue
        out.append(f"{label}: {signal}" if label else signal)
    return out


__all__ = [
    "LeaseDelinquency",
    "derive_and_store_lease_signals",
    "get_lease_entity_signals",
    "read_lease_delinquency",
]
