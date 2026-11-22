"""Deterministic hazard detection over inbound tenant text (SMS, voicemail
transcriptions).

A tenant texting "GAS LEAK" at 2am must never depend on an LLM being up,
in-budget, or in the loop — this is a pure, deterministic pattern scan that
runs at ingest time and flags life-safety emergencies for the property
manager. It NEVER acts (no auto-reply, no auto-dispatch): the output is a
list of hazard slugs the caller logs and surfaces on the activity feed.

Design constraints:
* PRECISE over broad. A false "EMERGENCY" flag on every "gas bill" text
  trains the PM to ignore the flag, which is worse than not having one.
  Patterns anchor on the emergency phrasing, not the bare noun — "fire" only
  fires standalone, so "fired the vendor" / "fireplace" / "fire extinguisher
  inspection" stay quiet (see the negative matrix in the unit tests).
* Pure + stateless: ``detect_hazards(text) -> list[str]`` with no I/O, so it
  is trivially unit-testable and safe at any call site.
* Slugs are stable identifiers (they land in activity rows); add new ones to
  ``_HAZARD_PATTERNS`` with both positive and negative test cases.
"""

from __future__ import annotations

import re

# slug -> compiled pattern. Order = display priority (most acute first).
_HAZARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "gas_leak",
        re.compile(
            r"\bgas\s+leak|\bsmell(?:s|ing|ed)?\s+(?:of\s+|like\s+)?gas\b|\bleaking\s+gas\b", re.I
        ),
    ),
    (
        "carbon_monoxide",
        re.compile(
            r"\bcarbon\s+monoxide\b|\bco\s+(?:alarm|detector)\s+(?:is\s+)?going\s+off\b", re.I
        ),
    ),
    (
        "fire",
        # "fire" the noun-in-progress, not the verb ("fired the vendor") or
        # fixtures ("fireplace", "fire extinguisher", "fire alarm testing").
        re.compile(
            r"\b(?:on\s+fire|caught\s+fire|there(?:'s|\s+is)\s+a\s+fire|fire\s+in\s+(?:the|my|our))\b",
            re.I,
        ),
    ),
    (
        "smoke",
        re.compile(
            r"\bsmell(?:s|ing|ed)?\s+(?:of\s+|like\s+)?smoke\b"
            r"|\bsmoke\s+(?:in|coming|filling|everywhere)\b",
            re.I,
        ),
    ),
    (
        "flooding",
        re.compile(
            r"\bburst\s+pipe\b|\bpipe\s+burst\b|\bflood(?:ing|ed)?\b"
            r"|\bwater\s+(?:everywhere|pouring|gushing)\b",
            re.I,
        ),
    ),
    (
        "electrical",
        re.compile(
            r"\bsparking\b|\bsparks\s+(?:from|coming)\b"
            r"|\belectrical\s+(?:fire|burning|smell)\b|\bburning\s+smell\b",
            re.I,
        ),
    ),
    (
        "sewage",
        re.compile(r"\bsewage\s+back(?:ed|ing)?\s*up\b|\bsewage\s+(?:in|overflow)", re.I),
    ),
    (
        "no_heat",
        # Heat loss is a habitability emergency in winter; keep the pattern
        # tight to total-loss phrasing, not thermostat complaints.
        re.compile(r"\bno\s+heat\b|\bheat\s+(?:is\s+)?(?:out|not\s+working)\b", re.I),
    ),
)


def detect_hazards(text: str | None) -> list[str]:
    """Return the hazard slugs found in ``text`` (empty list = none).

    Deduped, in ``_HAZARD_PATTERNS`` priority order. Safe on None/empty.
    """
    if not text:
        return []
    return [slug for slug, pattern in _HAZARD_PATTERNS if pattern.search(text)]


def hazard_label(slugs: list[str]) -> str:
    """A short human label for an activity line, e.g. ``gas_leak, smoke``."""
    return ", ".join(slugs)


__all__ = ["detect_hazards", "hazard_label"]
