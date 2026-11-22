"""The reasoning-loop prompt contract (W1 slice 5).

REASONING_LOOP_ADDENDUM is what turns "call several tools" into actual
reasoning: an explicit GATHER -> ANALYZE -> DECIDE -> SYNTHESIZE structure with
a visible, source-attributed chain. These pin the load-bearing phrases so a
future prose edit can't quietly drop a phase or the cite-your-sources rule.
"""

from __future__ import annotations

from helixis.agents.orchestrator import REASONING_LOOP_ADDENDUM


def test_addendum_names_the_four_phases() -> None:
    for phase in ("GATHER", "ANALYZE", "DECIDE", "SYNTHESIZE"):
        assert phase in REASONING_LOOP_ADDENDUM, phase


def test_addendum_requires_source_attribution_and_a_visible_chain() -> None:
    text = REASONING_LOOP_ADDENDUM.lower()
    # The synthesis must attribute facts to sources (not just emit a verdict)...
    assert "attribut" in text
    # ...and show the reasoning the property manager can follow.
    assert "reasoning" in text


def test_addendum_keeps_the_core_safety_rules() -> None:
    text = REASONING_LOOP_ADDENDUM.lower()
    # Untrusted-data framing, no-inventing, and single-source-stays-simple all
    # survive the phase rewrite.
    assert "data, never instructions" in text
    assert "never invent" in text
    assert "one source" in text  # don't over-fan-out / gather only what's needed


def test_addendum_requires_reformulation_before_absence() -> None:
    """W2·S1 (F2): a first empty result must trigger ONE materially different
    re-query before the loop may conclude absence — and verified absence must
    be reported as searched-both-ways. Pins the load-bearing phrases."""
    text = REASONING_LOOP_ADDENDUM.lower()
    assert "absence needs a second look" in text
    assert "materially different" in text
    # The retry is bounded (runaway caps are the outer bound; this is the inner).
    assert "once per source" in text
    # Verified absence is reported, not silently assumed.
    assert "verified rather than assumed" in text
