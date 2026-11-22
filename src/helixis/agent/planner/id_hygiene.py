"""Keep Buildium internal identifiers out of the action surface's prose.

The summarizer has run its output through an ID sanitizer since PR #92. The
ACTION SURFACE never did — and it is just as user-facing. Pulled from the live
Inbox 2026-08-25, 13 identifier leaks across the cards, and **9 of them were
here**, not in the summary:

    action.draft    "This is a follow-up on task 39503 ('Kitchen faucet
                     dripping constantly'), which was never dispatched"
    action.trigger  "Work order 9023 has property_id and unit_id both null"
    action.draft    "Work order 9023 ... has no property_id or unit_id"

A draft is the worst place for it: it is the pre-filled body of a note or a
message, so an un-edited click sends "task 39503" to a vendor or a resident.

**Redact, do not reject.** The summarizer can afford to reject because it has a
correction-nudge retry loop and, failing that, a one-line fail-safe. The
planner has neither, and an action surface is not one sentence — throwing away
a whole plan of suggestions because one rationale said "task 39503" is a much
worse trade than shipping that plan with the number removed. The valuable part
of the card is the actions.

⚠ **``parameters`` is deliberately untouched.** The handlers execute on those
ids — ``{"task_id": 39503}`` is how ``add_internal_note`` finds the task.
Cleaning them would break execution, so this walks named prose fields only and
never recurses into a parameter dict. That asymmetry is the whole design and is
pinned by a test.

The rule itself lives once — the same patterns that judge the chat reply judge
these. Sharing the import beats a second copy that drifts: two hand-maintained
ID tables is exactly the bug this file exists to clean up after.

⚠ **And that sharing was HALF DONE, which is worse than not sharing at all.**
#897 found the chat surface leaking shapes ``redact_id_references`` cannot
see — a plural ``IDs 11314, 11320`` heading a run, a bare parenthesised
``(11362, 11369)``, a lone ``11314`` in prose — and added them in
``agents.reply_hygiene``. That fix never reached HERE, so the inbox kept
running the older, weaker rule while chat ran the stronger one, and the same
card could be clean in one surface and leaking in the other.

This now calls ``scrub_reply_text``, which is a strict superset: it applies
``redact_id_references`` first and then the three shapes it misses. Gotcha 25's
rule, applied to the rule itself — *fix a field-shape bug at EVERY layer that
reads it.*
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from helixis.agent.summarizer.sanitizer import (
    SummarySanitizationError,
    sanitize_summary_text,
)
from helixis.agents.reply_hygiene import scrub_reply_text

if TYPE_CHECKING:
    from helixis.agent.planner.models import ActionSurface

logger = logging.getLogger(__name__)


def _clean(value: str | None) -> str | None:
    """Redact one prose string, keeping ``None`` and blanks as they are.

    Returns the ORIGINAL when redaction would empty it. Several of these
    fields carry a pydantic ``min_length``, so an emptied string fails
    re-validation and costs the whole surface — the opposite of the point.
    A field that is nothing but an identifier is vanishingly rare and a
    stray number beats a dropped card.
    """
    if not value or not value.strip():
        return value
    cleaned = scrub_reply_text(value)
    if not cleaned.strip():
        logger.warning("id redaction emptied a prose field; keeping the original")
        return value
    return cleaned


def _clean_list(values: list[str] | None) -> list[str] | None:
    if not values:
        return values
    return [c for v in values if (c := _clean(v)) is not None]


#: Keys whose VALUES are prose a person reads. Everything not listed is left
#: alone, which is the safe default: an unknown key might be a parameter.
_PROSE_KEYS = frozenset(
    {
        "summary",
        "freeform_placeholder",
        "label",
        "intent",
        "rationale",
        "alternative",
        "draft",
        "prompt",
        "note_body",
        "description",
        "title",
        "body",
        "message",
    }
)

#: Keys whose values are LISTS of prose strings.
_PROSE_LIST_KEYS = frozenset({"signals", "triggers", "summary_highlights"})

#: Subtrees the walker must NOT descend into. These carry the ids the
#: handlers execute on — ``{"task_id": 39503}`` is how ``add_internal_note``
#: finds the task. Cleaning them yields a beautiful card whose buttons do
#: nothing.
#:
#: ⚠ ``parameters`` also holds ``note_body`` / ``description`` / ``title``,
#: which ARE prose and DO reach the user (the frontend pre-fills the editor
#: from them). So this is not a blanket skip: scalar prose keys inside are
#: still cleaned, ids are not. See ``_scrub_parameters``.
_ID_BEARING_KEYS = frozenset({"parameter_patch"})


def _scrub_parameters(params: dict[str, Any]) -> dict[str, Any]:
    """Clean the prose INSIDE a parameter dict while leaving ids intact.

    ``{"task_id": 39503, "note_body": "follow-up on task 39503"}`` becomes
    ``{"task_id": 39503, "note_body": "follow-up on task"}`` — the handler
    still finds the task, and the text it writes into Buildium no longer
    quotes an internal id back at whoever reads it.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in _PROSE_KEYS and isinstance(value, str):
            out[key] = _clean(value)
        else:
            out[key] = value
    return out


def scrub_surface_dict(surface: Any) -> Any:
    """Redact identifiers from every prose field of a raw surface dict.

    Works on the DICT rather than the model because the model is not the only
    shape in play: the risk floor writes ``{"kind": "risk", "risk": {...}}``
    and ``ActionSurface`` is ``extra="forbid"``, so those surfaces cannot be
    parsed at all. There are five producers (the planner, the risk bridge,
    email triage, comms triage, the routing surface builder) and one write
    boundary, so the walker goes at the boundary.
    """
    if isinstance(surface, list):
        return [scrub_surface_dict(item) for item in surface]
    if not isinstance(surface, dict):
        return surface

    out: dict[str, Any] = {}
    for key, value in surface.items():
        if key == "parameters" and isinstance(value, dict):
            out[key] = _scrub_parameters(value)
        elif key in _ID_BEARING_KEYS:
            out[key] = value
        elif key in _PROSE_KEYS and isinstance(value, str):
            out[key] = _clean(value)
        elif key in _PROSE_LIST_KEYS and isinstance(value, list):
            out[key] = [_clean(v) if isinstance(v, str) else v for v in value]
        else:
            out[key] = scrub_surface_dict(value)

    # Orphaned highlights, same reasoning as the model path.
    summary = out.get("summary")
    highlights = out.get("summary_highlights")
    if isinstance(summary, str) and isinstance(highlights, list):
        kept = [h for h in highlights if isinstance(h, str) and h in summary]
        out["summary_highlights"] = kept or None
    return out


def scrub_action_surface(surface: ActionSurface) -> ActionSurface:
    """Return ``surface`` with identifiers redacted from every prose field.

    Walks the named human-readable fields only. Anything the frontend renders
    as text is in scope; anything the backend executes on is not.
    """
    actions = []
    for action in surface.actions:
        update: dict[str, object] = {
            "label": _clean(action.label),
            "intent": _clean(action.intent),
            "rationale": _clean(action.rationale),
            "alternative": _clean(action.alternative),
            "triggers": _clean_list(action.triggers),
        }
        if action.primary_action is not None:
            update["primary_action"] = action.primary_action.model_copy(
                update={
                    "label": _clean(action.primary_action.label),
                    "draft": _clean(action.primary_action.draft),
                }
            )
        if action.first_followup is not None:
            update["first_followup"] = action.first_followup.model_copy(
                update={
                    "prompt": _clean(action.first_followup.prompt),
                    "options": [
                        # ``parameter_patch`` rides along untouched — same
                        # reason as ``parameters``: the click executes on it.
                        opt.model_copy(update={"label": _clean(opt.label)})
                        for opt in action.first_followup.options
                    ],
                }
            )
        actions.append(action.model_copy(update=update))

    summary = _clean(surface.summary)

    # ⚠ ``summary_highlights`` are EXACT substrings of ``summary`` that the
    # inbox wraps in violet. Redacting the summary silently orphans any
    # highlight whose span contained the identifier — the phrase no longer
    # occurs, so the frontend highlights nothing and the emphasis quietly
    # disappears. Drop the ones that no longer match rather than leaving
    # dangling references, and re-clean the survivors (a highlight can carry
    # an id of its own).
    highlights = _clean_list(surface.summary_highlights)
    if highlights is not None and summary is not None:
        highlights = [h for h in highlights if h in summary]

    return surface.model_copy(
        update={
            "summary": summary,
            "freeform_placeholder": _clean(surface.freeform_placeholder),
            "signals": _clean_list(surface.signals),
            "summary_highlights": highlights or None,
            "actions": actions,
        }
    )


def action_surface_id_leaks(surface: ActionSurface) -> list[tuple[str, str]]:
    """Every ``(field_path, offending_text)`` still failing the ID rule.

    The inspector behind the guard tests: it reads the SAME fields
    :func:`scrub_action_surface` writes, so a field added to the model and
    missed by the scrubber shows up here instead of shipping quietly. Used to
    assert the scrub is total, and available for a diagnostic sweep over
    stored rows.
    """
    leaks: list[tuple[str, str]] = []

    def check(path: str, value: str | None) -> None:
        if not value:
            return
        try:
            sanitize_summary_text(value)
        except SummarySanitizationError as exc:
            leaks.append((path, str(exc)))

    check("summary", surface.summary)
    check("freeform_placeholder", surface.freeform_placeholder)
    for i, signal in enumerate(surface.signals or []):
        check(f"signals[{i}]", signal)
    for i, highlight in enumerate(surface.summary_highlights or []):
        check(f"summary_highlights[{i}]", highlight)
    for i, action in enumerate(surface.actions):
        check(f"actions[{i}].label", action.label)
        check(f"actions[{i}].intent", action.intent)
        check(f"actions[{i}].rationale", action.rationale)
        check(f"actions[{i}].alternative", action.alternative)
        for j, trigger in enumerate(action.triggers or []):
            check(f"actions[{i}].triggers[{j}]", trigger)
        if action.primary_action is not None:
            check(f"actions[{i}].primary_action.label", action.primary_action.label)
            check(f"actions[{i}].primary_action.draft", action.primary_action.draft)
        if action.first_followup is not None:
            check(f"actions[{i}].first_followup.prompt", action.first_followup.prompt)
            for j, opt in enumerate(action.first_followup.options):
                check(f"actions[{i}].first_followup.options[{j}].label", opt.label)
    return leaks


__all__ = ["action_surface_id_leaks", "scrub_action_surface", "scrub_surface_dict"]
