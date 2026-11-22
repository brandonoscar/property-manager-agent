"""A2P rejection-code catalog — coverage, classification, and the
edit-vs-appeal-vs-recreate decision tree (deep research, 2026-08-19)."""

from __future__ import annotations

from helixis.comms.a2p_codes import (
    CATALOG,
    FEES,
    Generation,
    NextStep,
    catalog_is_stale_for,
    classify,
    next_step,
)


def test_catalog_covers_all_three_generations() -> None:
    gens = {e.generation for e in CATALOG.values()}
    assert gens == {Generation.LEGACY, Generation.MARCH_2026, Generation.SEPT_2026}
    # Sizes: 22 legacy, 51 March-2026, 30 Sept-2026 (the research names 30532-30567
    # non-contiguously — 30535/30538/30546/30548/30553/30554 are not documented).
    by_gen = {g: sum(1 for e in CATALOG.values() if e.generation == g) for g in gens}
    assert by_gen[Generation.LEGACY] == 22
    assert by_gen[Generation.MARCH_2026] == 51
    assert by_gen[Generation.SEPT_2026] == 30


def test_consent_codes_are_classified_resubmittable() -> None:
    # The consent family that most threatens a PM opt-in flow.
    for code in (30913, 30923, 30924, 30925, 30931, 30932):
        entry = classify(code)
        assert entry is not None, code
        assert entry.resubmittable, code


def test_appeal_bands_are_never_resubmittable() -> None:
    for code in range(30940, 30965):
        entry = classify(code)
        assert entry is not None, code
        assert not entry.resubmittable, code
        assert next_step(code) is NextStep.APPEAL


def test_decision_tree() -> None:
    # Field-level resubmittable failure → free edit-in-place.
    assert next_step(30924) is NextStep.EDIT_IN_PLACE
    # Wrong use case can't be edited → delete/recreate path.
    assert next_step(30924, wrong_use_case=True) is NextStep.FIX_BRAND
    # Brand-level problem always routes through the brand fix.
    assert next_step(30883, brand_level=True) is NextStep.FIX_BRAND
    # Appeal-only stays appeal even if flags are unset.
    assert next_step(30953) is NextStep.APPEAL
    # Unknown code in the A2P space → stale-catalog signal.
    assert next_step(30599) is NextStep.UNKNOWN_CODE
    assert catalog_is_stale_for(30599)
    assert not catalog_is_stale_for(30924)
    assert not catalog_is_stale_for(404)


def test_sept_wave_all_resubmittable_and_tagged() -> None:
    sept = [e for e in CATALOG.values() if e.generation == Generation.SEPT_2026]
    assert all(e.resubmittable for e in sept)
    assert classify(30539) is not None
    assert "Reply STOP to cancel" in classify(30539).remediation  # type: ignore[union-attr]


def test_fee_constants_match_the_2025_08_schedule() -> None:
    assert FEES["brand_standard"] == 46.00
    assert FEES["secondary_vetting"] == 41.50
    assert FEES["authentication_plus"] == 12.50
    assert FEES["campaign_vetting_one_time"] == 15.00
