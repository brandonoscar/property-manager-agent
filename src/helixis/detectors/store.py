"""Persistence for Findings — the unified ``helixis_risk_flags`` store.

The upsert dedupes on ``(company_id, rule_key, fingerprint)`` and refreshes
``last_seen_at``; a previously-resolved condition that reappears is news
again; a PM's dismissal stays dismissed. The stale-resolve sweep marks
findings a detector no longer emits as resolved (dismissed ones untouched).

The SQL text + bind-arg order is byte-for-byte the original reconciliation
engine's (the trust-contract unit tests pin it). ``source`` is inlined from
a validated enum, never a bind param, so it can't shift the asserted arg
positions and can't be an injection vector.
"""

from __future__ import annotations

import json

import asyncpg

from helixis.detectors.base import SOURCE_ANALYST, SOURCE_RULE, Detector, Finding

_ALLOWED_SOURCES = frozenset({SOURCE_RULE, SOURCE_ANALYST})

_UPSERT_TMPL = """
INSERT INTO helixis_risk_flags
    (company_id, source, rule_key, fingerprint, severity, title, detail,
     amount, property_id, entity_type, entity_id, evidence)
VALUES ($1::uuid, '{source}', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
ON CONFLICT (company_id, rule_key, fingerprint) DO UPDATE SET
    severity     = EXCLUDED.severity,
    title        = EXCLUDED.title,
    detail       = EXCLUDED.detail,
    amount       = EXCLUDED.amount,
    evidence     = EXCLUDED.evidence,
    last_seen_at = now(),
    -- A previously-resolved condition that reappears is news again.
    -- A dismissal stays dismissed — the PM said "this is fine".
    status       = CASE
                     WHEN helixis_risk_flags.status = 'resolved' THEN 'new'
                     ELSE helixis_risk_flags.status
                   END,
    resolved_at  = CASE
                     WHEN helixis_risk_flags.status = 'resolved' THEN NULL
                     ELSE helixis_risk_flags.resolved_at
                   END
"""

# Findings of a rule_key the current scan no longer detects have healed —
# auto-resolve them (dismissed stay dismissed; already-resolved stay).
_RESOLVE_STALE_TMPL = """
UPDATE helixis_risk_flags
   SET status = 'resolved', resolved_at = now()
 WHERE company_id = $1::uuid
   AND source = '{source}'
   AND rule_key = $2
   AND status IN ('new', 'confirmed')
   AND NOT (fingerprint = ANY($3::text[]))
"""


def _source(detector: Detector) -> str:
    if detector.source not in _ALLOWED_SOURCES:
        raise ValueError(f"invalid finding source {detector.source!r} for {detector.detector_id!r}")
    return detector.source


async def persist_findings(
    conn: asyncpg.Connection,
    company_id: str,
    detector: Detector,
    findings: list[Finding],
) -> None:
    """Upsert the detector's current findings, then resolve stale ones.

    Idempotent: re-running with the same findings refreshes ``last_seen_at``
    and changes nothing else.
    """
    source = _source(detector)
    upsert = _UPSERT_TMPL.format(source=source)
    for f in findings:
        await conn.execute(
            upsert,
            company_id,
            f.rule_key,
            f.fingerprint,
            f.severity,
            f.title,
            f.detail,
            f.amount,
            f.property_id,
            f.entity_type,
            f.entity_id,
            json.dumps(f.evidence),
        )
    resolve = _RESOLVE_STALE_TMPL.format(source=source)
    for rule_key in detector.rule_keys:
        seen = [f.fingerprint for f in findings if f.rule_key == rule_key]
        await conn.execute(resolve, company_id, rule_key, seen)
