"""Reconciliation — deterministic cross-examination of a company's books.

Phase 1 of the §7-rank-4 moat feature: rules, not agent. Every flag is
exact SQL over the entity mirror with the computed numbers attached as
evidence — no LLM anywhere in this package, so a flag can never be a
hallucination. Phase 2 (the nightly analyst) writes into the same
``helixis_risk_flags`` table with ``source='analyst'``.
"""

from helixis.reconciliation.engine import (
    RULE_KEYS,
    RiskFlag,
    run_reconciliation_cycle,
    scan_company,
)

__all__ = ["RULE_KEYS", "RiskFlag", "run_reconciliation_cycle", "scan_company"]
