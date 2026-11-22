"""Prior turns for a golden case — the one thing the corpus could not express.

**Measured 2026-09-06: 0 of 153 golden cases exercised more than one turn.**
Every case carried a single ``input.user_message``, and neither harness had a
key for anything else — so the whole multi-turn class was untestable by
construction, and nobody could have written the case that would have caught
it.

That is exactly where the product fails. The 2026-09-06 demo take ran three
follow-up questions inside one thread and two of them returned nothing, and
gotcha 42 records the same class from the live probe: a miss on one question
hardened into an established absence two questions later, then **generalised
to a source that turn never touched**. Both are conversational-state defects.
Neither could be caught by any case in the suite.

This module builds the input the Agents SDK takes for a multi-turn run, in the
SAME shape production uses.

⚠ **It mirrors ``chat_history.load_session_history``, and the mirroring is the
point** (gotcha 66: a harness that reproduces production setup owns every
binding in that setup). Production hands the SDK a list of
``{"role": "user" | "assistant", "content": str}`` items with the current
turn appended last. A case that fed a differently-shaped history would
measure a conversation the product never has.

⚠ **Deliberately NOT modelled: tool rows.** ``load_session_history`` collapses
persisted ``role='tool'`` rows into the preceding assistant turn as a
parenthetical note, because the SDK rejects a tool item with no matching
``tool_call_id`` (PR #72). A case that wants the model to have seen a tool
result writes it into the assistant turn's own text, which is what production
would have handed it anyway.
"""

from __future__ import annotations

from typing import Any, cast

from agents.items import TResponseInputItem

#: Roles a case may declare. Anything else is a case-authoring error rather
#: than something to coerce — silently dropping an unrecognised role would
#: shorten the conversation the case believes it is testing.
_ROLES = ("user", "assistant")


class HistoryError(ValueError):
    """A malformed ``input.history`` block. Surfaced as a case error.

    ⚠ Never swallowed into "no history". A case whose history failed to load
    would run as a single-turn case and PASS its assertions for the wrong
    reason — gotcha 35's "a case that has never run is not a passing case",
    arriving as "a case that ran half of itself".
    """


def build_run_input(current_message: str, history: Any) -> str | list[TResponseInputItem]:
    """The ``Runner.run(input=…)`` value for a case.

    Returns the bare string when there is no history — byte-for-byte the
    previous behaviour, so every existing case is unaffected — and the SDK's
    item list when there is.

    ``history`` is a list of ``{"role": ..., "content": ...}`` dicts in
    chronological order. The current turn is appended as the final ``user``
    item, exactly as ``run_agent_streaming`` does.
    """
    if not history:
        return current_message

    if not isinstance(history, list):
        raise HistoryError(f"input.history must be a list, got {type(history).__name__}")

    # ⚠ Typed as the SDK's own input item, not `dict[str, str]`.
    #
    # `Runner.run(input=...)` takes `str | list[TResponseInputItem]`, a union
    # of TypedDicts. A plain `dict[str, str]` is not assignable to it, and
    # mypy said so at the call site rather than here — the error landed in
    # `agent_flow.py`, four files away from the function that produced the
    # value, which is what made it read as a harness problem rather than a
    # signature one.
    #
    # The `cast` is honest: the dicts ARE `EasyInputMessageParam`-shaped
    # (`role` + `content`), and the validation above is what guarantees it —
    # every role is checked against `_ROLES` and every content against `str`
    # before it is appended. A TypedDict cannot express that at runtime, so
    # the check is the proof and the cast records where it happened.
    items: list[TResponseInputItem] = []
    for i, raw in enumerate(history):
        if not isinstance(raw, dict):
            raise HistoryError(f"input.history[{i}] must be an object, got {type(raw).__name__}")
        role = raw.get("role")
        if role not in _ROLES:
            raise HistoryError(
                f"input.history[{i}].role must be one of {_ROLES}, got {role!r}. "
                "A tool result belongs in the assistant turn's own text — the SDK "
                "rejects a bare tool item, so production collapses those too."
            )
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HistoryError(f"input.history[{i}].content must be a non-empty string")
        items.append(cast("TResponseInputItem", {"role": role, "content": content}))

    # ⚠ The FIRST turn must be the user's, and the LAST must be the
    # assistant's. A history ending on a user turn would put two user messages
    # back to back once the current one is appended, which is not a shape any
    # real thread produces — the case would be measuring a conversation the
    # product cannot have.
    if items[0].get("role") != "user":
        raise HistoryError("input.history must start with a user turn")
    if items[-1].get("role") != "assistant":
        raise HistoryError(
            "input.history must end with an assistant turn — the case's "
            "user_message is appended as the next user turn"
        )

    items.append(cast("TResponseInputItem", {"role": "user", "content": current_message}))
    return items


__all__ = ["HistoryError", "build_run_input"]
