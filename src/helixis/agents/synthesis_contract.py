"""SYNTHESIZE footer contract — parse + validate (W2 slice 2, F6/F7).

The reasoning loop's SYNTHESIZE step must end every cross-source answer with a
three-line, machine-readable footer::

    Sources: buildium: $2,400 past due; lease §4: late fees capped at $50
    Confidence: likely — no corroborating payment record found
    Unverified: tenant's claimed payment on the 3rd

This module is the deterministic side of that contract: :func:`parse_synthesis_footer`
extracts the fields; :func:`validate_synthesis` returns violations (empty list =
compliant). Consumers today are the eval suite (mechanical grading of F6
calibration-shape and F7 receipts) and, later, an optional runtime enforcer —
deliberately NOT wired into the live reply path yet, mirroring how the
REVERSE_REPORT contract landed parser-first behind its flag (#271 lesson: no
hard runtime gate on model prose before a dogfood tuning pass).

Why a prose footer instead of SDK ``output_schema`` structured output: forcing
the orchestrator's final output to a JSON object would change ``final_output``'s
type and break the SSE token-streaming reply path the frontend renders — a
rip-and-replace of the reply pipeline (bucket C) for zero additional
checkability. The footer is regex-stable, streams as ordinary text, and the S6
progressive-disclosure slice is the right place for a structured envelope if
dogfood demands one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONFIDENCE_VOCABULARY = ("confident", "likely", "unverified")

#: The band the model uses to say it could not stand behind what it just wrote.
#: ``likely`` is ordinary calibration and is NOT this — an answer that hedges
#: proportionately is the system working, and counting it here would drown the
#: signal in normal turns.
LOW_CONFIDENCE = "unverified"

_SOURCES_RE = re.compile(r"^\s*Sources:\s*(?P<body>.+)$", re.MULTILINE)
_CONFIDENCE_RE = re.compile(
    r"^\s*Confidence:\s*(?P<word>\w+)\s*(?:[—–-]\s*(?P<why>.*))?$", re.MULTILINE
)
_UNVERIFIED_RE = re.compile(r"^\s*Unverified:\s*(?P<body>.+)$", re.MULTILINE)


@dataclass
class SynthesisFooter:
    """Parsed footer. ``well_formed`` is True only when all three lines were
    found AND the confidence word is in the vocabulary."""

    sources: list[str] = field(default_factory=list)
    confidence: str | None = None
    confidence_reason: str = ""
    unverified: list[str] = field(default_factory=list)
    well_formed: bool = False


def _split_semicolons(body: str) -> list[str]:
    return [part.strip() for part in body.split(";") if part.strip()]


def parse_synthesis_footer(text: str) -> SynthesisFooter:
    """Extract the footer from an answer. Tolerant of the footer being embedded
    at the end of longer prose (the normal case); uses the LAST match of each
    label so quoted examples earlier in a reply can't shadow the real footer."""
    footer = SynthesisFooter()

    sources_matches = list(_SOURCES_RE.finditer(text))
    if sources_matches:
        footer.sources = _split_semicolons(sources_matches[-1].group("body"))

    confidence_matches = list(_CONFIDENCE_RE.finditer(text))
    if confidence_matches:
        m = confidence_matches[-1]
        footer.confidence = m.group("word").lower()
        footer.confidence_reason = (m.group("why") or "").strip()

    unverified_matches = list(_UNVERIFIED_RE.finditer(text))
    if unverified_matches:
        body = unverified_matches[-1].group("body").strip()
        footer.unverified = [] if body.lower() in {"none", "none."} else _split_semicolons(body)

    footer.well_formed = bool(
        footer.sources and footer.confidence in CONFIDENCE_VOCABULARY and unverified_matches
    )
    return footer


def validate_synthesis(text: str) -> list[str]:
    """Violations of the footer contract; empty list means compliant.

    Deterministic and mechanical only — judging whether the *content* of the
    footer is honest (calibration) is the eval judge's job, not this function's.
    """
    violations: list[str] = []
    footer = parse_synthesis_footer(text)

    if not footer.sources:
        violations.append("missing 'Sources:' line (F7 — receipts)")
    if footer.confidence is None:
        violations.append("missing 'Confidence:' line (F6)")
    elif footer.confidence not in CONFIDENCE_VOCABULARY:
        violations.append(
            f"confidence word {footer.confidence!r} not in {CONFIDENCE_VOCABULARY} (F6)"
        )
    elif not footer.confidence_reason:
        violations.append("Confidence line has no reason clause (F6)")
    if not _UNVERIFIED_RE.search(text):
        violations.append("missing 'Unverified:' line (F6)")

    return violations


def volunteered_low_confidence(text: str) -> bool:
    """Did the MODEL itself end at :data:`LOW_CONFIDENCE`?

    ⚠ **Read this BEFORE the claim guards run, not after.** Every caveat in
    ``claim_guard`` rewrites the Confidence line to ``unverified`` when it
    fires, so reading the finished reply would count a scope downgrade, a
    source downgrade and a model-failover downgrade as the model volunteering
    doubt. Those are four different facts, three of them already have their own
    reason code, and merging them produces a number that answers no question.

    The word only — never ``confidence_reason``. That clause is model prose
    about the user's own portfolio, and the reason-code channel's contract
    (``observability.reason_codes``) is short internal strings, never message
    bodies. This function returning a bool rather than the reason is the
    enforcement of that: there is nothing to accidentally forward.
    """
    return parse_synthesis_footer(text).confidence == LOW_CONFIDENCE


__all__ = [
    "CONFIDENCE_VOCABULARY",
    "LOW_CONFIDENCE",
    "SynthesisFooter",
    "parse_synthesis_footer",
    "validate_synthesis",
    "volunteered_low_confidence",
]
