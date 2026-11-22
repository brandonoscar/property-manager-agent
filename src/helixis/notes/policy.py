"""Autonomous Buildium note policy — decide when Helixis records a note itself.

Helixis leaves an internal Buildium note WITHOUT asking when — and ONLY when —
it (a) learns a durable operational fact worth putting in the system of record,
or (b) takes a consequential action worth an audit line. The hard product rule
is that **not everything becomes a note**: chit-chat, one-off status, and
anything already visible on the task must never generate one. This module is the
deterministic gate that enforces exactly that.

The note-worthy classes (the allowlist the owner signed off on):

  - FINANCIAL_RESPONSIBILITY — who pays for what / spend policy
    ("owner won't cover electrical in 2026"; "cap repairs at $500 w/o approval")
  - COMMITMENT             — a promise/agreement ("tenant agreed to pay Friday";
                             "owner approved the HVAC quote")
  - ACCESS_FACT            — property/access fact ("gate code is now 4417";
                             "dog on premises")
  - CONTACT_PREFERENCE     — how someone wants to be reached ("owner prefers text")
  - ACTION_PROVENANCE      — a terse audit line for a consequential thing Helixis
                             did ("Helixis drafted WO from tenant text, 7/14")

Everything else (STATUS_UPDATE, PLEASANTRY, OTHER) is classified but never noted.

Two LIVE seams sit outside this module and are injected where it's wired:
  - the CLASSIFIER (LLM) that turns a source event/message into candidate
    facts with a class + confidence — non-deterministic, validated by evals;
  - the WRITER (Buildium notes API) that persists an accepted note. The exact
    notes endpoint per entity MUST be grounded in the real Buildium API, never
    guessed (repo gotcha #15), so it's a Protocol here — not a call.

This module owns only the deterministic middle — the allowlist, the accept/
reject rules, dedup, and note-body formatting — all unit-tested, zero network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# ── classes ─────────────────────────────────────────────────────


class NoteClass(StrEnum):
    """What a candidate fact/action is. Only ALLOWED_CLASSES ever get noted."""

    # Note-worthy (the approved allowlist).
    FINANCIAL_RESPONSIBILITY = "financial_responsibility"
    COMMITMENT = "commitment"
    ACCESS_FACT = "access_fact"
    CONTACT_PREFERENCE = "contact_preference"
    ACTION_PROVENANCE = "action_provenance"

    # Never noted — the anti-spam floor. The classifier may still emit these;
    # the gate drops them.
    STATUS_UPDATE = "status_update"
    PLEASANTRY = "pleasantry"
    OTHER = "other"


#: The classes that become Buildium notes. Anything outside this set is dropped
#: by :func:`evaluate` — this frozenset IS the "not everything is a note" rule.
ALLOWED_CLASSES: frozenset[NoteClass] = frozenset(
    {
        NoteClass.FINANCIAL_RESPONSIBILITY,
        NoteClass.COMMITMENT,
        NoteClass.ACCESS_FACT,
        NoteClass.CONTACT_PREFERENCE,
        NoteClass.ACTION_PROVENANCE,
    }
)

#: Human-facing label per class, used in the note body prefix.
_CLASS_LABEL: dict[NoteClass, str] = {
    NoteClass.FINANCIAL_RESPONSIBILITY: "Financial responsibility",
    NoteClass.COMMITMENT: "Commitment",
    NoteClass.ACCESS_FACT: "Access",
    NoteClass.CONTACT_PREFERENCE: "Contact preference",
    NoteClass.ACTION_PROVENANCE: "Action",
}

#: Confidence floor for a classifier-proposed fact to be noted. The classifier
#: returns a 0..1 confidence; below this we don't record it (a hedged guess is
#: not something to write into a landlord's system of record).
MIN_CONFIDENCE = 0.7

#: Safe upper bound on a note body. Buildium's exact limit isn't assumed here
#: (gotcha #15) — this is a conservative cap so we never post a wall of text.
MAX_NOTE_LEN = 500


# ── data ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityRef:
    """The Buildium entity a note attaches to.

    ``id`` is that entity's Buildium id. The WRITER seam maps (kind, id) to the
    correct notes endpoint — this module never guesses it.

    ⚠ ``kind`` was documented here as "task / lease / owner / tenant /
    property". That was a guess written before the endpoints were probed, and
    four of the five are wrong: the 2026-07-05 write-probes found note
    mechanisms for **vendor, task and work_order only** (see
    ``notes/writer.py`` for the per-kind mechanics). lease / owner / tenant /
    property have no known route, and ``BuildiumNoteWriter`` raises rather than
    inventing one. A docstring cannot fail, so this one went on naming three
    endpoints that do not exist until someone measured.
    """

    kind: str
    id: int | str


@dataclass
class NoteCandidate:
    """A classifier-proposed fact/action that MIGHT deserve a note."""

    note_class: NoteClass
    text: str
    target: EntityRef
    source: str = ""  # provenance, e.g. "email from owner 2026-07-14"
    confidence: float = 1.0


@dataclass(frozen=True)
class Decision:
    """The gate's verdict for one candidate."""

    accepted: bool
    reason: str


# ── the gate ────────────────────────────────────────────────────


def evaluate(candidate: NoteCandidate, *, min_confidence: float = MIN_CONFIDENCE) -> Decision:
    """Decide whether ``candidate`` becomes a Buildium note.

    Accept only when ALL hold: the class is in :data:`ALLOWED_CLASSES`, the
    confidence clears ``min_confidence``, and the text is non-empty. Rejections
    carry a machine-readable reason so the caller can log why a fact was NOT
    noted (useful when tuning the classifier). Never raises.
    """
    if candidate.note_class not in ALLOWED_CLASSES:
        return Decision(False, "class_not_allowed")
    if candidate.confidence < min_confidence:
        return Decision(False, "low_confidence")
    if not candidate.text or not candidate.text.strip():
        return Decision(False, "empty_text")
    return Decision(True, "accepted")


_WS = re.compile(r"\s+")


def dedup_key(candidate: NoteCandidate) -> str:
    """Stable key so the same fact about the same entity isn't re-noted.

    Keyed on (class, target kind+id, whitespace/case-normalized text). Two
    candidates that say the same thing about the same entity collapse to one
    key; the caller keeps a ``seen`` set of already-written keys.
    """
    text = _WS.sub(" ", candidate.text.strip().casefold())
    return f"{candidate.note_class.value}|{candidate.target.kind}:{candidate.target.id}|{text}"


def format_note_body(candidate: NoteCandidate) -> str:
    """Render the note text a human PM sees in Buildium.

    Shape: ``[Helixis] <Label> — <fact>`` plus a trailing source line when
    present. Prefixed so it's unmistakably Helixis-authored (not the PM's own
    note), and truncated to :data:`MAX_NOTE_LEN`.
    """
    label = _CLASS_LABEL.get(candidate.note_class, "Note")
    fact = _WS.sub(" ", candidate.text.strip())
    body = f"[Occupella] {label} — {fact}"
    if candidate.source.strip():
        body = f"{body} (source: {candidate.source.strip()})"
    if len(body) > MAX_NOTE_LEN:
        body = body[: MAX_NOTE_LEN - 1].rstrip() + "…"
    return body


def select_new(
    candidates: list[NoteCandidate],
    seen: set[str],
    *,
    min_confidence: float = MIN_CONFIDENCE,
) -> tuple[list[NoteCandidate], list[tuple[NoteCandidate, str]]]:
    """Split candidates into (to-write, dropped-with-reason).

    Applies :func:`evaluate`, then dedups accepted candidates against ``seen``
    (already-written keys) AND within this batch. ``seen`` is NOT mutated — the
    caller adds keys only after the WRITER actually persists, so a failed write
    can be retried. Dropped entries pair the candidate with its reason
    (``class_not_allowed`` / ``low_confidence`` / ``empty_text`` / ``duplicate``).
    """
    to_write: list[NoteCandidate] = []
    dropped: list[tuple[NoteCandidate, str]] = []
    batch_keys: set[str] = set()
    for c in candidates:
        decision = evaluate(c, min_confidence=min_confidence)
        if not decision.accepted:
            dropped.append((c, decision.reason))
            continue
        key = dedup_key(c)
        if key in seen or key in batch_keys:
            dropped.append((c, "duplicate"))
            continue
        batch_keys.add(key)
        to_write.append(c)
    return to_write, dropped


# ── live seams (injected where this is wired; not implemented here) ──


@runtime_checkable
class FactClassifier(Protocol):
    """LLM-backed: turn a source message/event into note candidates.

    Non-deterministic — its precision (does it only surface allowlisted,
    high-confidence facts?) is what the eval suite must pin. This module's gate
    is the deterministic backstop that drops whatever slips through.
    """

    async def classify(self, source_text: str, target: EntityRef) -> list[NoteCandidate]: ...


@runtime_checkable
class NoteWriter(Protocol):
    """Buildium-backed: persist an accepted note body to the target entity.

    The (target.kind, target.id) → notes-endpoint mapping MUST come from the
    real Buildium API, never guessed (gotcha #15). Returns the written note's id
    (or any handle) so the caller can record the dedup key as truly written.
    """

    async def write(self, target: EntityRef, body: str) -> str: ...


@dataclass
class NoteRun:
    """Result of a full autonomous-note pass (for logging/telemetry)."""

    written: list[str] = field(default_factory=list)  # dedup keys actually written
    dropped: list[tuple[NoteCandidate, str]] = field(default_factory=list)


__all__ = [
    "ALLOWED_CLASSES",
    "MAX_NOTE_LEN",
    "MIN_CONFIDENCE",
    "Decision",
    "EntityRef",
    "FactClassifier",
    "NoteCandidate",
    "NoteClass",
    "NoteRun",
    "NoteWriter",
    "dedup_key",
    "evaluate",
    "format_note_body",
    "select_new",
]
