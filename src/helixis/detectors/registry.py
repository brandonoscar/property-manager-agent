"""The detector registry — add a detector with one ``register()`` call.

Registration order is preserved (dict insertion order) so a scan runs
detectors in a deterministic sequence — the reconciliation unit tests rely
on rent-then-duplicate ordering.
"""

from __future__ import annotations

from helixis.detectors.base import STATE_RULE, Detector

_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    """Register a detector instance. Returns it so it can be used as a
    module-level ``FOO = register(FooDetector())`` binding."""
    if not getattr(detector, "detector_id", None):
        raise ValueError("detector is missing a detector_id")
    if detector.detector_id in _REGISTRY:
        raise ValueError(f"duplicate detector_id {detector.detector_id!r}")
    if not detector.rule_keys:
        raise ValueError(
            f"detector {detector.detector_id!r} declares no rule_keys — "
            "the framework needs them to auto-resolve stale findings"
        )
    if detector.reads_tables is None:
        raise ValueError(
            f"detector {detector.detector_id!r} declares no reads_tables — the "
            "cycle needs them to skip a company whose PMS never fills a table "
            "this rule reads (db/pms_coverage.py)"
        )
    _REGISTRY[detector.detector_id] = detector
    return detector


def get(detector_id: str) -> Detector | None:
    return _REGISTRY.get(detector_id)


def all_detectors() -> list[Detector]:
    return list(_REGISTRY.values())


def detectors_for_class(detector_class: str) -> list[Detector]:
    return [d for d in _REGISTRY.values() if d.detector_class == detector_class]


def state_rule_detectors() -> list[Detector]:
    return detectors_for_class(STATE_RULE)


def enabled_state_rule_detectors(extra_ids: frozenset[str] = frozenset()) -> list[Detector]:
    """State-rule detectors the scheduled cycle should run: the
    ``default_enabled`` ones plus any explicitly allow-listed by id."""
    return [d for d in state_rule_detectors() if d.default_enabled or d.detector_id in extra_ids]


def dark_state_rule_detector_ids(extra_ids: frozenset[str] = frozenset()) -> list[str]:
    """Registered state-rule detectors that will NOT run on this config.

    ⚠ **Because a dark detector and a clean portfolio are the same silence.**
    A detector that never runs contributes nothing to the cycle's counts, and
    neither does one that ran and found no problems — so the only question an
    operator actually has, *is this thing switched off or is the book clean?*,
    had no answer anywhere in the process.

    Measured 2026-08-29: 3 of the 12 registered state-rule detectors are
    default-enabled and 9 are dark. Of the three that do run, one
    (``duplicate_vendor_bill``) has produced zero findings in its lifetime —
    and without this, "working and finding nothing" is indistinguishable from
    "quietly broken since the day it merged". Same shape as the reminder
    producers' dark gate, and as gotcha 45's rule that a control which has
    silently stopped working looks exactly like one that is working.
    """
    on = {d.detector_id for d in enabled_state_rule_detectors(extra_ids)}
    return sorted(d.detector_id for d in state_rule_detectors() if d.detector_id not in on)


def reset_for_tests() -> None:
    """Clear the registry — unit-test isolation only."""
    _REGISTRY.clear()
