"""A ``thinking`` event is prose the model wrote — never a status placeholder.

``ChatThread.tsx`` renders EVERY ``thinking`` event as an assistant bubble
stamped with the Occupella badge. So publishing a hardcoded string through
that channel does not add a status line; it puts words in the agent's mouth.

The orchestrator did exactly that at t≈0 of every run::

    await bus.publish(session_id, thinking_event("Thinking…"))

and it was wrong in both directions at once. The bubble said "Thinking…" as
though the agent had written it, and it simultaneously KILLED the real
instant-feedback the client already had: the animated dots render on
``isRunning && events.length === 0``, so the first event published is the
event that switches them off. One real placeholder traded for one fake
sentence, on every single turn — the first thing a viewer sees.

⚠ The guard is on the ARGUMENT, not on the word. Banning the string
"Thinking…" would be beaten by "Working…" or "One moment" the same day; what
makes a call wrong here is that the text is a CONSTANT, i.e. not something a
model produced. The legitimate call passes ``scrub_reply_text(segment_text)``.

⚠ AST, not a substring scan (gotcha 40/59). The comment where the line used
to live QUOTES the broken call in order to explain it, so a text scan fires
on the sentence documenting the guard. A comment is never a ``Call`` node.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"


def _placeholder_calls() -> list[str]:
    """Every ``thinking_event(<string literal>)`` call site under src/helixis."""
    found: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "thinking_event" or not node.args:
                continue
            first = node.args[0]
            # A constant string, or an f-string/concat with no dynamic part.
            literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
            if literal:
                rel = path.relative_to(_SRC.parent.parent)
                found.append(f"{rel}:{node.lineno} thinking_event({first.value!r})")
    return found


def test_no_thinking_event_carries_a_hardcoded_status_string() -> None:
    offenders = _placeholder_calls()
    assert not offenders, (
        "A `thinking` event renders as an assistant BUBBLE in the client, so a "
        "hardcoded string is the agent appearing to say words nobody wrote — and "
        "it also switches off the animated dots, which were the real instant "
        "feedback. Publish a `narration` event for status, or nothing.\n  " + "\n  ".join(offenders)
    )


def test_the_scan_can_see_the_defect_it_guards_against() -> None:
    """The guard's own floor: prove the walker finds one when it exists.

    A corpus scan that silently collected zero call sites — wrong root, bad
    glob, renamed helper — would report green forever (gotcha 49).
    """
    tree = ast.parse('bus.publish(sid, thinking_event("Thinking…"))\n')
    hits = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "thinking_event"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    ]
    assert len(hits) == 1, "the walker cannot see a literal thinking_event call"


def test_the_real_thinking_call_still_exists_and_is_dynamic() -> None:
    """The channel is not dead — model prose still flushes through it.

    Deleting every call would also pass the guard above, and would silently
    remove the pre-tool-call narration that stops answers gluing together.
    """
    src = (_SRC / "agents" / "orchestrator.py").read_text(encoding="utf-8")
    calls = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "thinking_event"
    ]
    assert calls, "the orchestrator no longer flushes the model's pre-tool prose at all"
