"""Unit tests for the autonomous Buildium note policy (notes/policy.py)."""

from __future__ import annotations

from helixis.notes.policy import (
    ALLOWED_CLASSES,
    MAX_NOTE_LEN,
    EntityRef,
    NoteCandidate,
    NoteClass,
    dedup_key,
    evaluate,
    format_note_body,
    select_new,
)

_TASK = EntityRef("task", 4471)


def _cand(cls: NoteClass, text: str = "x", *, conf: float = 1.0, target: EntityRef = _TASK):
    return NoteCandidate(note_class=cls, text=text, target=target, confidence=conf)


# ── allowlist ───────────────────────────────────────────────────


def test_allowlist_is_exactly_the_five_approved_classes() -> None:
    assert ALLOWED_CLASSES == {
        NoteClass.FINANCIAL_RESPONSIBILITY,
        NoteClass.COMMITMENT,
        NoteClass.ACCESS_FACT,
        NoteClass.CONTACT_PREFERENCE,
        NoteClass.ACTION_PROVENANCE,
    }


def test_every_allowed_class_accepts() -> None:
    for cls in ALLOWED_CLASSES:
        assert evaluate(_cand(cls, "real fact")).accepted, cls


def test_non_allowlisted_classes_are_dropped() -> None:
    # The anti-spam floor: chit-chat / status / other never become notes.
    for cls in (NoteClass.STATUS_UPDATE, NoteClass.PLEASANTRY, NoteClass.OTHER):
        d = evaluate(_cand(cls, "tenant said thanks"))
        assert not d.accepted and d.reason == "class_not_allowed", cls


# ── confidence + empties ────────────────────────────────────────


def test_low_confidence_rejected() -> None:
    d = evaluate(_cand(NoteClass.COMMITMENT, "maybe pays friday", conf=0.5))
    assert not d.accepted and d.reason == "low_confidence"


def test_confidence_boundary_inclusive() -> None:
    assert evaluate(_cand(NoteClass.COMMITMENT, "pays friday", conf=0.7)).accepted


def test_custom_min_confidence() -> None:
    assert not evaluate(
        _cand(NoteClass.COMMITMENT, "pays friday", conf=0.8), min_confidence=0.9
    ).accepted


def test_empty_text_rejected() -> None:
    for text in ("", "   ", "\n\t"):
        d = evaluate(_cand(NoteClass.ACCESS_FACT, text))
        assert not d.accepted and d.reason == "empty_text"


# ── dedup ───────────────────────────────────────────────────────


def test_dedup_key_ignores_case_and_whitespace() -> None:
    a = _cand(NoteClass.ACCESS_FACT, "Gate code is  4417")
    b = _cand(NoteClass.ACCESS_FACT, "gate code is 4417")
    assert dedup_key(a) == dedup_key(b)


def test_dedup_key_differs_by_class_and_entity() -> None:
    base = _cand(NoteClass.ACCESS_FACT, "gate code 4417")
    other_class = _cand(NoteClass.COMMITMENT, "gate code 4417")
    other_entity = _cand(NoteClass.ACCESS_FACT, "gate code 4417", target=EntityRef("task", 9))
    assert dedup_key(base) != dedup_key(other_class)
    assert dedup_key(base) != dedup_key(other_entity)


# ── select_new (the batch gate) ─────────────────────────────────


def test_select_new_splits_accepted_and_dropped() -> None:
    cands = [
        _cand(NoteClass.FINANCIAL_RESPONSIBILITY, "owner won't cover electric 2026"),
        _cand(NoteClass.PLEASANTRY, "thanks!"),  # class dropped
        _cand(NoteClass.COMMITMENT, "pays friday", conf=0.3),  # low conf dropped
    ]
    to_write, dropped = select_new(cands, seen=set())
    assert [c.note_class for c in to_write] == [NoteClass.FINANCIAL_RESPONSIBILITY]
    reasons = {r for _, r in dropped}
    assert reasons == {"class_not_allowed", "low_confidence"}


def test_select_new_dedups_against_seen() -> None:
    c = _cand(NoteClass.ACCESS_FACT, "gate code 4417")
    to_write, dropped = select_new([c], seen={dedup_key(c)})
    assert to_write == []
    assert dropped == [(c, "duplicate")]


def test_select_new_dedups_within_batch() -> None:
    c1 = _cand(NoteClass.ACCESS_FACT, "Gate code 4417")
    c2 = _cand(NoteClass.ACCESS_FACT, "gate code  4417")  # same fact, diff spacing
    to_write, dropped = select_new([c1, c2], seen=set())
    assert len(to_write) == 1
    assert dropped == [(c2, "duplicate")]


def test_select_new_does_not_mutate_seen() -> None:
    c = _cand(NoteClass.COMMITMENT, "pays friday")
    seen: set[str] = set()
    select_new([c], seen=seen)
    # seen is only updated by the caller AFTER a successful write.
    assert seen == set()


# ── note body ───────────────────────────────────────────────────


def test_format_note_body_prefixes_and_labels() -> None:
    body = format_note_body(
        NoteCandidate(
            NoteClass.FINANCIAL_RESPONSIBILITY,
            "Owner will not cover electrical repairs in 2026.",
            _TASK,
            source="email from owner 2026-07-14",
        )
    )
    assert body.startswith("[Occupella] Financial responsibility — ")
    assert "electrical repairs" in body
    assert "source: email from owner 2026-07-14" in body


def test_format_note_body_without_source() -> None:
    body = format_note_body(_cand(NoteClass.ACCESS_FACT, "Gate code is 4417"))
    assert body == "[Occupella] Access — Gate code is 4417"
    assert "source:" not in body


def test_format_note_body_truncates_long_text() -> None:
    body = format_note_body(_cand(NoteClass.COMMITMENT, "x" * 1000))
    assert len(body) <= MAX_NOTE_LEN
    assert body.endswith("…")
