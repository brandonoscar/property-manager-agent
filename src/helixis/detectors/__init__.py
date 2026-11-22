"""Ambient detector framework.

One object repeated: a Detector reads mirrored data and emits typed,
idempotent, entity-linked Findings into the unified ``helixis_risk_flags``
store, with dedup, auto-resolve, dismissal-suppression, per-detector crash
isolation, and observability handled by the framework. Adding a detector is
one subclass + one ``register()`` call.

The reconciliation rules (#1 rent-under-charged, #2 duplicate-vendor-bill)
are the first detectors ported onto this layer; ``reconciliation`` now
delegates here so there is a single implementation.
"""

from __future__ import annotations

# Importing the built-ins registers them into the registry on package import.
from helixis.detectors import bridge_rules, mirror_state_rules, state_rules  # noqa: F401
from helixis.detectors.base import (
    AGGREGATE,
    COMMS,
    ON_DEMAND,
    STATE_RULE,
    Detector,
    Finding,
)
from helixis.detectors.compliance_sweep import run_compliance_sweep, sweep_leases
from helixis.detectors.registry import (
    all_detectors,
    get,
    register,
    state_rule_detectors,
)
from helixis.detectors.runner import (
    run_detector_cycle,
    run_detectors_for_company,
    run_state_rule_detectors,
)

__all__ = [
    "AGGREGATE",
    "COMMS",
    "ON_DEMAND",
    "STATE_RULE",
    "Detector",
    "Finding",
    "all_detectors",
    "get",
    "register",
    "run_compliance_sweep",
    "run_detector_cycle",
    "run_detectors_for_company",
    "run_state_rule_detectors",
    "state_rule_detectors",
    "sweep_leases",
]
