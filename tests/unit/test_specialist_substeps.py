"""The ninety seconds a consult row sits there, finally narrated.

Out on the parent stream a whole ``buildium_agent`` consult is ONE
``tool_start`` and, much later, one ``tool_result``. Nothing is published in
between, so the client renders a single row that does not move — and gotcha 37
measured that this is most of a long turn, not an edge case: 195 of 232 tool
calls were nested agent runs.

The nested run is meanwhile calling real tools, and ``specialist_stream_capture``
was ALREADY watching every one of its stream events (for the truncation signal)
and throwing them away. Forwarding them beats any synthetic heartbeat: a pulse
says "still alive", which the user already assumed and cannot act on, while
"Listing open work orders" answers the question they are actually asking.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from helixis.agents.specialist_limits import (
    _nested_tool_name,
    reset_substep_emitter,
    set_substep_emitter,
    specialist_stream_capture,
)
from helixis.common.events import EventType, substep_event

_ROOT = Path(__file__).resolve().parents[2]


def _projection_source(root: Path) -> Path:
    """The store/stream → conversation projection chat AND the Inbox render.

    It lived in ChatThread.tsx until 2026-09-30, when it moved to
    runnerFold.ts so the Inbox could use it too. Re-pointed, not relaxed: the
    consumers are asserted here so a projection nobody calls cannot pass.
    """
    components = root / "frontend" / "src" / "components"
    chat = (components / "ChatThread.tsx").read_text(encoding="utf-8")
    inbox = (components / "InboxPage.tsx").read_text(encoding="utf-8")
    assert "foldMessagesToConversation(" in chat, "ChatThread no longer uses the projection"
    assert "foldInboxStream(" in inbox, "the Inbox no longer uses the projection"
    return components / "conversation" / "runnerFold.ts"


_ORCH = _ROOT / "src" / "helixis" / "agents" / "orchestrator.py"


class _Item:
    def __init__(self, raw: Any) -> None:
        self.raw_item = raw


class _Event:
    def __init__(self, etype: str, name: str, item: Any) -> None:
        self.type = etype
        self.name = name
        self.item = item


def _nested_call(tool: str, *, as_dict: bool = False) -> dict[str, Any]:
    raw: Any = {"name": tool} if as_dict else type("Raw", (), {"name": tool})()
    return {"event": _Event("run_item_stream_event", "tool_called", _Item(raw))}


# ── the capture forwards the nested step ──────────────────────────────


def test_a_nested_tool_call_reaches_the_emitter_as_a_sentence() -> None:
    seen: list[str] = []
    token = set_substep_emitter(seen.append)
    try:
        specialist_stream_capture(_nested_call("buildium_list_open_work_orders"))
    finally:
        reset_substep_emitter(token)
    # The SAME sentence generator the parent's own rows use — one voice, not
    # two descriptions of the same kind of thing.
    assert seen == ["Listing the open work orders"]


def test_both_live_sdk_shapes_are_read() -> None:
    """⚠ The name lives on ``raw_item.name`` for an object and
    ``raw_item["name"]`` for a TypedDict, and the live SDK produces both —
    which is exactly why ``orchestrator.register_tool_call`` handles the pair.
    Reading one shape works until the day it doesn't, silently."""
    assert _nested_tool_name(_Item({"name": "buildium_get_property"})) == "buildium_get_property"
    assert (
        _nested_tool_name(_Item(type("R", (), {"name": "buildium_get_property"})()))
        == "buildium_get_property"
    )
    assert _nested_tool_name(_Item(None)) == ""
    assert _nested_tool_name(None) == ""


def test_an_unnameable_step_emits_nothing_rather_than_a_placeholder() -> None:
    """A row reading "Working on it" says less than no row. The fallback is
    right for a tool nobody classified; it is wrong for a call whose name we
    could not read at all, because it adds a line carrying zero information to
    the one place the user is looking for information."""
    seen: list[str] = []
    token = set_substep_emitter(seen.append)
    try:
        specialist_stream_capture({"event": _Event("run_item_stream_event", "tool_called", None)})
    finally:
        reset_substep_emitter(token)
    assert seen == []


def test_a_failing_emitter_costs_the_line_not_the_consult(caplog: Any) -> None:
    """⚠ This runs inside a live nested agent run. A publish that raises here
    would take down a consult the user is waiting on, to fail at drawing a
    status line — the same rule ``inject_tool_examples`` follows for a drifted
    example (gotcha 27).

    ⚠ AND IT MUST BE ATTRIBUTED TO THE EMIT. ``specialist_stream_capture`` has
    its own outer catch-all, so deleting the inner one still swallows the
    error — which is why the first mutation of this branch survived (gotcha 58:
    check whether the mutation changes behaviour before writing a test for
    it). It DOES change something, just not liveness: the outer handler logs
    *"specialist stream capture failed"*, and that sentence is false. The
    capture worked perfectly; the BUS was down. A diagnostic that names the
    wrong subsystem sends the next reader to read the SDK shape handling,
    which is exactly the trap gotcha 42 records — an hour spent on a component
    that was never broken.
    """

    def boom(_: str) -> None:
        raise RuntimeError("bus down")

    token = set_substep_emitter(boom)
    try:
        with caplog.at_level(logging.DEBUG, logger="helixis.agents.specialist_limits"):
            specialist_stream_capture(_nested_call("buildium_get_property"))
    finally:
        reset_substep_emitter(token)

    messages = [r.message for r in caplog.records]
    assert any("substep emit failed" in m for m in messages), (
        f"the emit failure is not attributed to the emit: {messages}"
    )
    assert not any("stream capture failed" in m for m in messages), (
        "a bus failure is being reported as an SDK stream-shape failure — the "
        "next reader is sent to debug the wrong component"
    )


def test_nothing_is_emitted_without_an_emitter_bound() -> None:
    """Unbound is the default in every non-streaming caller (tests, the eval
    harness). The capture must behave exactly as it did before."""
    specialist_stream_capture(_nested_call("buildium_get_property"))


def test_the_truncation_arm_still_works() -> None:
    """⚠ The tool_called branch was inserted AHEAD of it, and an early return
    in the wrong place would silently disable the one channel the truncation
    signal travels on (measured at 1-in-597 on the parent stream)."""
    from helixis.agents.claim_guard import TRUNCATION_MARKER
    from helixis.agents.specialist_limits import (
        bind_consult_ledger,
        current_consult_ledger,
        reset_consult_ledger,
    )

    token = bind_consult_ledger()
    try:
        out = _Item(None)
        out.output = f"showing 25 of 179. {TRUNCATION_MARKER}"  # type: ignore[attr-defined]
        specialist_stream_capture({"event": _Event("run_item_stream_event", "tool_output", out)})
        ledger = current_consult_ledger()
        assert ledger is not None and ledger.truncated_read
    finally:
        reset_consult_ledger(token)


# ── the wire ──────────────────────────────────────────────────────────


def test_substep_is_its_own_event_type() -> None:
    e = substep_event("Listing open work orders")
    assert e.type == EventType.SUBSTEP
    # NOT a tool_start: it must not open a row of its own with its own
    # pairing, and it must not be counted as a step the ORCHESTRATOR took.
    assert EventType.SUBSTEP not in (EventType.TOOL_START, EventType.REASONING)


# ── the call site (gotcha 62: a helper nothing calls) ─────────────────


def _orch_calls(name: str) -> list[ast.Call]:
    tree = ast.parse(_ORCH.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_the_orchestrator_binds_the_emitter_and_unbinds_it() -> None:
    """⚠ The whole feature is one binding. ``specialist_stream_capture`` was
    already wired and already saw these events; without this call it goes on
    seeing them and dropping them, and every test above passes over a channel
    that reaches nobody."""
    assert _orch_calls("set_substep_emitter"), (
        "nothing binds the substep emitter — nested steps are being watched "
        "and discarded again, and a consult is a silent row for its whole life"
    )
    assert _orch_calls("reset_substep_emitter"), "the per-run emitter is never unbound"


def test_the_published_line_goes_through_the_identifier_scrubber() -> None:
    """A specialist's step names carry record identifiers, and this text
    reaches the screen (gotcha 38)."""
    for call in _orch_calls("substep_event"):
        assert call.args, "substep_event called with no content"
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "scrub_reply_text", (
            "a substep is published unscrubbed — raw record numbers reach the user"
        )


# ── the client (gotcha 28: read both sides) ───────────────────────────


def test_the_client_renders_substeps_and_does_not_count_them_as_steps() -> None:
    """⚠ A `buildium_agent` consult can make eight nested calls. Counting them
    as the parent's steps turns "1 step" into "9 steps" for one consult, and a
    summary nobody can trust is worse than no summary."""
    chat = _projection_source(_ROOT).read_text(encoding="utf-8")
    assert "'substep'" in chat, "runnerFold.ts drops substep events on the floor"

    thread = (
        _ROOT / "frontend" / "src" / "components" / "conversation" / "ConversationThread.tsx"
    ).read_text(encoding="utf-8")
    assert "case 'substep'" in thread, "the renderer has no substep branch"
    assert "'step' | 'thought' | 'substep'" in thread, (
        "the trail line no longer distinguishes a nested step — it is being "
        "counted as the orchestrator's own work"
    )
