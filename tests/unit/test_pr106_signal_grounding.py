"""PR #106 — drop hallucinated planner signals.

The V2 planner prompt asks Gemini for a list of "signals" — short
facts about the event that render in the Inbox's "WHAT HELIXIS
NOTICED" section. Production observation: Gemini occasionally
fabricates numeric claims (e.g. "20 prior tickets on this unit in
the last 90 days" when the actual ``history_unit`` has 2).

The validator under test here is strict-drop: any signal that
makes a numeric claim about prior tickets/incidents/work-orders
on the unit or property gets dropped if the number doesn't match
the ground truth from ``EnrichedEvent.history_unit`` /
``history_property``.

Tests pin:
- Matching numbers → signal kept.
- Mismatched numbers → signal dropped.
- Property scope vs unit scope routing.
- Signals without numeric claims → kept (we can't refute what we
  can't measure).
- Signals with claims but no scope keyword → kept (no ground
  truth available; pass-through rather than aggressive drop).
- ``signals = None`` and ``signals = []`` → no-ops.
- The validator logs each drop with structured context.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from helixis.agent.planner.signal_validator import validate_signals


def _make_action_surface(signals: list[str] | None) -> Any:
    """Build the minimal ActionSurface-like object the validator
    consumes. Pydantic's ``model_copy`` lets us avoid constructing
    a full ActionSurface with valid actions / placeholder strings."""
    from helixis.agent.planner.models import ActionSurface

    return ActionSurface(
        schema_version=2,
        summary="Sarah Chen at 123 Main St unit 4B reported a leak.",
        actions=[],
        freeform_placeholder="or describe what you want me to do…",
        signals=signals,
    )


def _make_enriched(*, history_unit: int, history_property: int) -> Any:
    """Stand in for ``EnrichedEvent`` with just the surface the
    validator reads: ``history_unit`` / ``history_property`` (only
    lengths matter) + ``event_name`` and ``task.id`` for the log
    extras. Uses MagicMock-y duck typing so we don't have to satisfy
    the full Pydantic model."""
    from types import SimpleNamespace

    return SimpleNamespace(
        event_name="Task.Created",
        task=SimpleNamespace(id=17435),
        history_unit=[SimpleNamespace(id=i) for i in range(history_unit)],
        history_property=[SimpleNamespace(id=i) for i in range(history_property)],
    )


# ── Numeric claims on the UNIT scope ─────────────────────────────


def test_keeps_signal_when_unit_count_matches() -> None:
    surface = _make_action_surface(["3 prior tickets on this unit in the last 90 days"])
    enriched = _make_enriched(history_unit=3, history_property=0)
    out = validate_signals(surface, enriched)
    assert out.signals == ["3 prior tickets on this unit in the last 90 days"]


def test_drops_signal_when_unit_count_overstates() -> None:
    """The production failure case: '20 prior tickets on this unit'
    while actual history_unit has 2 → dropped."""
    surface = _make_action_surface(["20 prior tickets on this unit in the last 90 days"])
    enriched = _make_enriched(history_unit=2, history_property=2)
    out = validate_signals(surface, enriched)
    assert out.signals == []


def test_drops_signal_when_unit_count_understates() -> None:
    """A mismatch in either direction is suspect — we don't trust
    the LLM's count more than ground truth even when low."""
    surface = _make_action_surface(["1 prior ticket on this unit"])
    enriched = _make_enriched(history_unit=5, history_property=5)
    out = validate_signals(surface, enriched)
    assert out.signals == []


# ── Numeric claims on the PROPERTY scope ─────────────────────────


def test_keeps_signal_when_property_count_matches() -> None:
    surface = _make_action_surface(["7 prior work orders on this property in the last 30 days"])
    enriched = _make_enriched(history_unit=0, history_property=7)
    out = validate_signals(surface, enriched)
    assert out.signals == ["7 prior work orders on this property in the last 30 days"]


def test_drops_signal_when_property_count_mismatches() -> None:
    surface = _make_action_surface(["50 prior incidents on this property"])
    enriched = _make_enriched(history_unit=2, history_property=4)
    out = validate_signals(surface, enriched)
    assert out.signals == []


def test_property_scope_dominates_when_signal_mentions_both() -> None:
    """If a signal name-drops both 'unit' and 'property', the
    property check takes precedence (broader scope dominates)."""
    surface = _make_action_surface(["5 prior tickets on this property and unit"])
    # The PROPERTY count matches (5), unit count would NOT match (2).
    # Property-dominates means the signal passes.
    enriched = _make_enriched(history_unit=2, history_property=5)
    out = validate_signals(surface, enriched)
    assert out.signals == ["5 prior tickets on this property and unit"]


# ── Pass-through cases ───────────────────────────────────────────


def test_passes_through_signals_without_numeric_claims() -> None:
    """Qualitative signals can't be ground-checked — keep them."""
    surface = _make_action_surface(
        [
            "Loud crashing description matches fixture-failure pattern",
            "Tenant reported during business hours (likely available)",
        ]
    )
    enriched = _make_enriched(history_unit=0, history_property=0)
    out = validate_signals(surface, enriched)
    assert out.signals == [
        "Loud crashing description matches fixture-failure pattern",
        "Tenant reported during business hours (likely available)",
    ]


def test_passes_through_when_claim_has_no_unit_or_property_scope() -> None:
    """Numeric claim but no scope keyword → no ground truth → pass."""
    surface = _make_action_surface(["3 photos attached to the description"])
    enriched = _make_enriched(history_unit=0, history_property=0)
    out = validate_signals(surface, enriched)
    assert out.signals == ["3 photos attached to the description"]


def test_no_op_when_signals_none() -> None:
    surface = _make_action_surface(None)
    enriched = _make_enriched(history_unit=10, history_property=10)
    out = validate_signals(surface, enriched)
    assert out.signals is None


def test_no_op_when_signals_empty() -> None:
    surface = _make_action_surface([])
    enriched = _make_enriched(history_unit=10, history_property=10)
    out = validate_signals(surface, enriched)
    assert out.signals == []


# ── Mixed valid + invalid signals ────────────────────────────────


def test_drops_only_the_offending_signal_from_a_mixed_list() -> None:
    """Two valid signals + one hallucinated → exactly the
    hallucinated one is dropped; the valid two pass through in
    original order."""
    surface = _make_action_surface(
        [
            "Loud crashing description matches fixture-failure pattern",
            "20 prior tickets on this unit in the last 90 days",  # bad
            "Tenant reported during business hours",
        ]
    )
    enriched = _make_enriched(history_unit=2, history_property=2)
    out = validate_signals(surface, enriched)
    assert out.signals == [
        "Loud crashing description matches fixture-failure pattern",
        "Tenant reported during business hours",
    ]


# ── Observability ────────────────────────────────────────────────


def test_emits_warning_log_for_each_dropped_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    surface = _make_action_surface(
        ["20 prior tickets on this unit", "9 prior incidents on this property"]
    )
    enriched = _make_enriched(history_unit=1, history_property=1)
    with caplog.at_level(logging.WARNING, logger="helixis.agent.planner.signal_validator"):
        out = validate_signals(surface, enriched)
    assert out.signals == []
    # Filter to the validator's own logger — the loose-ends batch added a
    # second WARNING from observability.reason_codes on the same drop.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "helixis.agent.planner.signal_validator"
    ]
    assert len(warnings) == 1  # one log call covering all drops
    msg = warnings[0].getMessage()
    assert "hallucinated" in msg


def test_no_log_when_nothing_dropped(caplog: pytest.LogCaptureFixture) -> None:
    surface = _make_action_surface(["3 prior tickets on this unit"])
    enriched = _make_enriched(history_unit=3, history_property=3)
    with caplog.at_level(logging.WARNING, logger="helixis.agent.planner.signal_validator"):
        out = validate_signals(surface, enriched)
    assert out.signals == ["3 prior tickets on this unit"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


# ── Case insensitivity ───────────────────────────────────────────


def test_regex_is_case_insensitive() -> None:
    """LLMs sometimes capitalize 'Prior' or 'Tickets'. The regex
    must catch the claim regardless."""
    surface = _make_action_surface(["20 Prior TICKETS on this Unit in the last 90 days"])
    enriched = _make_enriched(history_unit=2, history_property=2)
    out = validate_signals(surface, enriched)
    assert out.signals == []
