"""Every queued card type must be a publishable EventType.

#859 added ``_queue_card("next_steps", …)`` and never added the enum member.
The drain loop in ``run_agent_streaming`` calls ``EventType(etype)`` on
whatever comes off the queue, so the missing member raised ``ValueError``
straight out of the run — every substantive turn 500'd behind an HTTP 200 SSE
stream. It reached production and was found by the battery v4 preflight, not
by 4306 passing tests, because nothing exercised the *string* the tool queues
against the *enum* the orchestrator resolves.

The two tests below are the pair that closes it: one reads the literals out of
the call sites (so a new card can't skip the enum), the other proves an
unknown type can no longer kill a run.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from helixis.common.events import CARD_HISTORY_PREFIX, EventType

_AGENTS = Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents"


def _queued_card_types() -> set[str]:
    """The literal first argument of every ``_queue_card(...)`` call.

    Parsed rather than imported: the point is to see what the SOURCE queues,
    including a card type whose code path no test happens to execute — that
    is exactly the gap that let ``next_steps`` through.
    """
    found: set[str] = set()
    for path in _AGENTS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "_queue_card":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def test_card_types_are_publishable() -> None:
    """The pin. A queued card type with no enum member is an outage, not a
    missing card — the drain path resolves it on the live request path."""
    queued = _queued_card_types()
    assert queued, "found no _queue_card call sites — the AST scan has drifted"
    assert "next_steps" in queued, "the #859 regression case should be among them"

    publishable = {e.value for e in EventType}
    missing = sorted(queued - publishable)
    assert not missing, (
        f"card type(s) queued with no EventType member: {missing}. "
        f"Add them to EventType in common/events.py — otherwise EventType(etype) "
        f"raises inside run_agent_streaming and kills the whole turn."
    )


def test_an_unknown_card_type_cannot_kill_a_run() -> None:
    """Defence in depth. The enum pin above stops the known cause; this stops
    the class. A card we can't publish should cost us the card, never the
    answer the user is waiting for."""
    unknown = "definitely_not_a_real_card_type"
    assert unknown not in {e.value for e in EventType}

    # The drain loop's guard, in isolation: resolve-or-skip, never raise.
    published = []
    for etype in ("detail_card", unknown, "next_steps"):
        try:
            resolved = EventType(etype)
        except ValueError:
            continue
        published.append(resolved)

    assert published == [EventType.DETAIL_CARD, EventType.NEXT_STEPS]


_THREAD_TSX = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "conversation"
    / "ConversationThread.tsx"
)

#: Card types deliberately not rendered by ConversationThread, with the reason.
#: An entry here is a decision; an unlisted absence is the bug this test finds.
_NOT_RENDERED_IN_THREAD: dict[str, str] = {}


def test_every_queued_card_is_handled_by_the_client() -> None:
    """The OTHER half of gotcha 28, which that entry names and nothing pinned.

    ``EventType`` only proves the backend can PUBLISH the card. The user sees
    it only if ``ConversationThread`` also switches on the same string, and
    the failure there is silent in the opposite direction: the turn succeeds,
    the event arrives, and the card is dropped on the floor with no error
    anywhere. The model then believes it showed a form the user never saw —
    and for ``request_details`` that means it stops and waits for an answer
    to a question that was never asked.

    Read from the SOURCE on both sides, per that gotcha's own rule: testing
    the producer and the consumer separately is what let ``next_steps``
    through in the first place.
    """
    if not _THREAD_TSX.exists():  # pragma: no cover - backend-only checkouts
        import pytest

        pytest.skip(f"frontend not present at {_THREAD_TSX}")

    source = _THREAD_TSX.read_text(encoding="utf-8")
    # `case 'form':` / `case 'form': {` — the literals the renderer dispatches
    # on. A substring search for the bare word would match the import, the
    # type name and the comments explaining it (gotcha 40).
    handled = set(re.findall(r"case\s+'([a-z_]+)'\s*:", source))
    assert handled, "found no case labels — the ConversationThread scan has drifted"

    queued = _queued_card_types()
    missing = sorted(queued - handled - set(_NOT_RENDERED_IN_THREAD))
    assert not missing, (
        f"card type(s) the backend queues and the client never renders: {missing}. "
        f"Add a `case '<type>':` to ConversationThread.tsx, or record it in "
        f"_NOT_RENDERED_IN_THREAD with the reason. A dropped card is silent — "
        f"the turn succeeds and the user simply never sees it."
    )


_TYPES_TS = _THREAD_TSX.parent / "types.ts"


def _ts_literal(name: str, source: str) -> str:
    """The string a `export const NAME = '...'` declares, or ''."""
    m = re.search(rf"export const {name}\s*=\s*['\"]([^'\"]*)['\"]", source)
    return m.group(1) if m else ""


def test_the_card_history_prefix_is_the_same_string_on_both_sides() -> None:
    """The backend WRITES `card:<type>` rows; the client HYDRATES on the same
    literal. A drift of one character restores nothing — reloading a thread
    silently goes back to dropping every card, with no error anywhere and no
    failing test unless something reads both files. Gotcha 28's rule, applied
    to a string that crosses a language boundary rather than an enum."""
    if not _TYPES_TS.exists():  # pragma: no cover - backend-only checkouts
        import pytest

        pytest.skip(f"frontend not present at {_TYPES_TS}")

    ts = _ts_literal("CARD_HISTORY_PREFIX", _TYPES_TS.read_text(encoding="utf-8"))
    assert ts, "CARD_HISTORY_PREFIX not found in conversation/types.ts"
    assert ts == CARD_HISTORY_PREFIX, (
        f"prefix drift: python writes {CARD_HISTORY_PREFIX!r}, the client reads {ts!r}. "
        f"A stored card is then never restored on reload — silently."
    )


def test_every_queued_card_survives_the_client_fold() -> None:
    """⚠ The SECOND list on the client side, and the one that actually bit.

    `ConversationThread` can draw a card and still never see it: `ChatThread`
    rebuilds a turn's events from the stored ones and only re-emits what
    `CARD_EVENT_TYPES` names. Measured 2026-09-01, before that constant
    existed: `next_steps`, `map` and `form` were published by the backend,
    kept by the store, renderable by the thread — and dropped between them by
    four hardcoded filters. So handling the card is not enough; the fold has
    to carry it.
    """
    if not _TYPES_TS.exists():  # pragma: no cover - backend-only checkouts
        import pytest

        pytest.skip(f"frontend not present at {_TYPES_TS}")

    source = _TYPES_TS.read_text(encoding="utf-8")
    block = re.search(r"export const CARD_EVENT_TYPES = \[(.*?)\] as const;", source, re.S)
    assert block, "CARD_EVENT_TYPES not found in conversation/types.ts"
    folded = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert folded, "CARD_EVENT_TYPES parsed empty — the scan has drifted"

    queued = _queued_card_types()
    missing = sorted(queued - folded - set(_NOT_RENDERED_IN_THREAD))
    assert not missing, (
        f"card type(s) the backend queues that ChatThread's fold never re-emits: "
        f"{missing}. Add them to CARD_EVENT_TYPES in conversation/types.ts — a "
        f"handled `case` alone is not enough, and the drop is silent."
    )
