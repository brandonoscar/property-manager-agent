"""The reconciliation engine — now a thin shim over the detector framework.

The "hidden money leak" rules (a lease that says $1,000 while the recurring
charge quietly posts $950; a vendor invoice entered twice a week apart) were
the first two ambient detectors. They have been generalised into the shared
framework (``helixis.detectors``): the rule logic + SQL now live in
``detectors.state_rules``, persistence + dedup in ``detectors.store``, and
crash-isolated execution in ``detectors.runner``.

This module stays as the stable public surface everything already imports
(``scan_company``, ``run_reconciliation_cycle``, ``RiskFlag``, ``RULE_KEYS``)
so the webhook worker and the reconciliation routes/tests keep working
unchanged — it just delegates. New detectors are added in
``helixis.detectors``, not here.

``RiskFlag`` is an alias of ``detectors.Finding`` (identical fields); the
Phase-2 nightly LLM analyst still writes ``source='analyst'`` rows into the
same ``helixis_risk_flags`` table via ``reconciliation.analyst``.
"""

from __future__ import annotations

import asyncpg

from helixis.detectors.base import Finding as RiskFlag
from helixis.detectors.runner import run_detector_cycle, run_state_rule_detectors

# The state-rule detectors' rule_key namespaces, kept for back-compat.
RULE_KEYS = ("rent_under_charged", "duplicate_vendor_bill")


async def scan_company(pool: asyncpg.Pool, company_id: str) -> dict[str, int]:
    """Run every state-rule detector for one company; upsert findings,
    resolve healed ones. Returns per-detector current-detection counts.

    Delegates to the detector framework — see ``helixis.detectors.runner``.
    """
    return await run_state_rule_detectors(pool, company_id)


async def run_reconciliation_cycle(pool: asyncpg.Pool) -> None:
    """One sweep over every company with mirrored books. Called by the
    webhook worker on its own cadence; delegates to the detector cycle."""
    await run_detector_cycle(pool)


__all__ = ["RULE_KEYS", "RiskFlag", "run_reconciliation_cycle", "scan_company"]
