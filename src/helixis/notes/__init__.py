"""Autonomous Buildium notes — the deterministic gate (``policy``), the
Buildium binding (``writer``), and the pass that runs them (``runner``).

Dark until ``HELIXIS_NOTES__AUTONOMOUS_ENABLED=true``; the confirm-gated
``buildium_add_note`` agent tool is a separate, live path.
"""

from helixis.notes.policy import (
    ALLOWED_CLASSES,
    Decision,
    EntityRef,
    NoteCandidate,
    NoteClass,
    NoteRun,
    dedup_key,
    evaluate,
    format_note_body,
    select_new,
)
from helixis.notes.runner import NOTE_ACTION_TYPE, run_note_pass
from helixis.notes.writer import (
    AUTONOMOUS_KINDS,
    WRITABLE_KINDS,
    BuildiumNoteWriter,
    UnsupportedNoteTargetError,
    autonomous_writable,
)

__all__ = [
    "ALLOWED_CLASSES",
    "AUTONOMOUS_KINDS",
    "NOTE_ACTION_TYPE",
    "WRITABLE_KINDS",
    "BuildiumNoteWriter",
    "Decision",
    "EntityRef",
    "NoteCandidate",
    "NoteClass",
    "NoteRun",
    "UnsupportedNoteTargetError",
    "autonomous_writable",
    "dedup_key",
    "evaluate",
    "format_note_body",
    "run_note_pass",
    "select_new",
]
