"""Progressive-disclosure envelope — W2 slice 6 (F9).

Three layers, derived server-side from what a reasoning-loop run has ALREADY
produced by the time it finishes — never model-emitted:

1. **answer** — the prose answer with the SYNTHESIZE footer stripped, so the
   frontend can render a clean reply and tuck the reasoning behind a toggle.
2. **reasoning** — the parsed footer fields (sources / confidence + reason /
   unverified, via :mod:`helixis.agents.synthesis_contract`) plus the ordered
   specialist-tool trail the orchestrator actually walked.
3. **audit** — run_id / trace_id pointers into telemetry (`orchestrator_runs`)
   and Langfuse, for the "show me everything" click.

Deriving (instead of asking the model for a structured envelope) is the same
bucket-C rejection recorded on ``synthesis_contract``: forcing ``final_output``
to JSON would break SSE token streaming for zero additional information — every
field here already exists in the prose, the footer, or run bookkeeping. The
envelope is published as an ADDITIVE ``disclosure`` SSE event in loop mode only
(clients that don't know the type ignore it — the tabular_data/map precedent).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from helixis.agents.synthesis_contract import parse_synthesis_footer

# A footer line as the frontend should treat it: one of the three contract
# labels at line start. Kept intentionally in sync with the parse regexes in
# synthesis_contract (same labels, same anchoring).
_FOOTER_LABEL_RE = re.compile(r"^\s*(Sources|Confidence|Unverified):", re.IGNORECASE)

# Defensive cap on the trail included in the payload. A runaway-length trail
# can't exist in practice (the loop's max_turns bounds it) — this only keeps a
# pathological case from bloating one SSE frame.
_MAX_TRAIL = 30


def strip_synthesis_footer(text: str) -> str:
    """Layer 1: the answer without its trailing SYNTHESIZE footer block.

    Strips only a TRAILING block of footer-labelled lines (blank lines between
    answer and footer go with it). A footer quoted mid-answer is left alone —
    only the end-of-text block is presentation, the rest is content. If the
    text is footer-only (no prose survives), the original text is returned so
    the client never renders an empty bubble.
    """
    lines = text.rstrip("\n").split("\n")
    idx = len(lines)
    labelled = 0
    while idx > 0:
        candidate = lines[idx - 1]
        if _FOOTER_LABEL_RE.match(candidate):
            labelled += 1
            idx -= 1
        elif not candidate.strip():
            idx -= 1
        else:
            break
    if labelled == 0:
        return text.rstrip()
    stripped = "\n".join(lines[:idx]).rstrip()
    return stripped if stripped else text.rstrip()


@dataclass
class DisclosureEnvelope:
    """The derived three-layer answer. ``to_payload`` is the wire shape."""

    answer: str
    sources: list[str] = field(default_factory=list)
    confidence: str | None = None
    confidence_reason: str = ""
    unverified: list[str] = field(default_factory=list)
    footer_well_formed: bool = False
    specialist_trail: list[str] = field(default_factory=list)
    run_id: str | None = None
    trace_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "reasoning": {
                "sources": self.sources,
                "confidence": self.confidence,
                "confidence_reason": self.confidence_reason,
                "unverified": self.unverified,
                "footer_well_formed": self.footer_well_formed,
                "specialist_trail": self.specialist_trail,
            },
            "audit": {
                "run_id": self.run_id,
                "trace_id": self.trace_id,
            },
        }


def build_disclosure_envelope(
    final_output: str,
    *,
    specialist_trail: Sequence[str] = (),
    run_id: str | None = None,
    trace_id: str | None = None,
) -> DisclosureEnvelope:
    """Derive the envelope from the finished run's artifacts.

    ``specialist_trail`` is the ordered list of specialist-tool names the
    orchestrator called (duplicates kept — a repeat IS information: the
    absence-recheck rule re-querying a source). A missing/malformed footer
    still yields an envelope (``footer_well_formed=False``, empty reasoning
    fields) — disclosure degrades, it never gates the answer.
    """
    footer = parse_synthesis_footer(final_output)
    return DisclosureEnvelope(
        answer=strip_synthesis_footer(final_output),
        sources=footer.sources,
        confidence=footer.confidence,
        confidence_reason=footer.confidence_reason,
        unverified=footer.unverified,
        footer_well_formed=footer.well_formed,
        specialist_trail=list(specialist_trail)[:_MAX_TRAIL],
        run_id=run_id,
        trace_id=trace_id,
    )


__all__ = [
    "DisclosureEnvelope",
    "build_disclosure_envelope",
    "strip_synthesis_footer",
]
