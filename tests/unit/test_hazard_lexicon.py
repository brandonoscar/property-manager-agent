"""Unit tests for the deterministic hazard lexicon (loose-ends batch).

The false-positive matrix is the hard part and the point: a PM who gets an
"EMERGENCY" flag on every gas bill stops reading the flag. Every pattern
added to ``_HAZARD_PATTERNS`` needs rows in BOTH matrices.
"""

from __future__ import annotations

import pytest

from helixis.security.hazard_lexicon import detect_hazards, hazard_label

# ── positives: emergencies that MUST flag ────────────────────────────

POSITIVES: list[tuple[str, str]] = [
    ("GAS LEAK in unit 4B please help", "gas_leak"),
    ("i smell gas in the hallway", "gas_leak"),
    ("It smells like gas near the stove", "gas_leak"),
    ("the boiler is leaking gas", "gas_leak"),
    ("carbon monoxide alarm went off twice", "carbon_monoxide"),
    ("the CO detector is going off", "carbon_monoxide"),
    ("the stove caught fire", "fire"),
    ("there's a fire in the laundry room", "fire"),
    ("my kitchen is on fire", "fire"),
    ("fire in my unit!!", "fire"),
    ("I smell smoke coming from next door", "smoke"),
    ("smoke in the hallway on 3", "smoke"),
    ("smoke filling the apartment", "smoke"),
    ("a pipe burst under the sink", "flooding"),
    ("burst pipe in the ceiling", "flooding"),
    ("the basement is flooding", "flooding"),
    ("bathroom flooded overnight", "flooding"),
    ("water pouring from the light fixture", "flooding"),
    ("the outlet is sparking", "electrical"),
    ("sparks coming from the panel", "electrical"),
    ("electrical burning smell in the kitchen", "electrical"),
    ("there's a burning smell from the heater", "electrical"),
    ("sewage backed up into the tub", "sewage"),
    ("sewage backing up again", "sewage"),
    ("no heat since last night and it's freezing", "no_heat"),
    ("the heat is out in the whole building", "no_heat"),
    ("heat not working, kids are cold", "no_heat"),
]


@pytest.mark.parametrize(("text", "expected_slug"), POSITIVES)
def test_emergencies_flag(text: str, expected_slug: str) -> None:
    assert expected_slug in detect_hazards(text)


def test_multiple_hazards_dedupe_and_keep_priority_order() -> None:
    hits = detect_hazards(
        "I smell gas, there is smoke in the hall, the outlet is sparking. smell gas!!"
    )
    assert hits == ["gas_leak", "smoke", "electrical"]


def test_case_insensitive() -> None:
    assert detect_hazards("GAS LEAK") == ["gas_leak"]


# ── negatives: everyday texts that must STAY QUIET ───────────────────

NEGATIVES: list[str] = [
    # gas, the utility / the fuel
    "my gas bill seems high this month",
    "is gas included in the rent?",
    "the gas company is coming tuesday",
    # fire, the verb / the fixture / routine safety
    "we fired the vendor last week",
    "does the unit have a fireplace?",
    "the fire extinguisher inspection is scheduled",
    "when is the fire alarm testing?",
    # smoke, the detector / the policy
    "the smoke detector battery is chirping",
    "smoke detector needs a new battery",
    "is this a smoke free building?",
    # water/plumbing, non-emergency
    "the faucet drips a little",
    "when will the water heater be replaced?",
    # heat, non-total-loss
    "can you check the thermostat settings?",
    "the heater is a bit noisy",
    # sewage adjacency without a backup
    "what day is sewage billed?",
    # electrical, non-emergency
    "the outlet cover is cracked",
    # generic
    "hi, following up on my application",
    "",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_everyday_texts_stay_quiet(text: str) -> None:
    assert detect_hazards(text) == []


def test_none_is_safe() -> None:
    assert detect_hazards(None) == []


def test_hazard_label() -> None:
    assert hazard_label(["gas_leak", "smoke"]) == "gas_leak, smoke"
