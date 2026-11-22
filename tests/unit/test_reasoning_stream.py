"""The model's reasoning was already on the stream and was being dropped.

A three-minute turn showed the user a spinner. The one signal that exists
during that window — a nested specialist consult publishes nothing between its
start and its result (gotcha 37: 195 of 232 tool calls were nested agent runs)
— is the model's own working-out, which the Agents SDK emits as raw stream
events whenever the provider returns reasoning content. ``run_agent_streaming``
handled ``response.output_text.delta`` and nothing else, so those events
arrived and went nowhere.

⚠ This is NOT dark code waiting on a flag. ``agents/effort.py`` already sets
``ModelSettings(reasoning=Reasoning(effort=...))`` on questions it classifies
trivial, LiteLLM maps ``reasoning_effort`` onto Anthropic extended thinking,
and the SDK's chat-completions handler turns the resulting deltas into these
events. The plumbing existed on both sides and one branch was missing.
"""

from __future__ import annotations

import ast
from pathlib import Path

from helixis.agents.orchestrator import _REASONING_DELTA_TYPES
from helixis.common.events import EventType, reasoning_event

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


# ── the SDK's own vocabulary (gotcha 45: read the source, not a memory) ──


def test_the_delta_types_we_listen_for_are_the_ones_the_sdk_emits() -> None:
    """⚠ Reads the LIVE SDK. These strings belong to the Agents SDK and can
    drift on an upgrade — silently, because the failure mode is a stream that
    simply carries nothing. Nothing would go red; the user would just be back
    to watching a spinner."""
    handler = (
        Path(__import__("agents").__file__).parent / "models" / "chatcmpl_stream_handler.py"
    ).read_text(encoding="utf-8")
    for delta_type in _REASONING_DELTA_TYPES:
        assert f'type="{delta_type}"' in handler, (
            f"the SDK no longer emits {delta_type!r} — the orchestrator is "
            "listening for an event that will never arrive, and the reasoning "
            "trail is silently empty again"
        )


def test_we_listen_for_both_of_the_sdk_reasoning_paths() -> None:
    """The SDK has two: a delta carrying ``reasoning_content`` becomes a
    summary delta, one carrying ``reasoning`` becomes a text delta. Which one
    fires is the provider's choice, so handling one works today and does
    nothing the day the mapping changes."""
    assert _REASONING_DELTA_TYPES == {
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    }


def test_the_effort_setting_that_makes_this_live_is_still_wired() -> None:
    """⚠ The evidence that this is not dark code, executable so it cannot
    expire (gotcha 48). If ``effort_settings`` stops asking for reasoning, no
    provider emits any, and every guard here goes on passing over a channel
    that carries nothing."""
    from helixis.agents.effort import effort_settings

    live = effort_settings("what's the balance on 4B", enabled=True)
    assert live is not None, "adaptive effort no longer recognises a trivial lookup"
    assert getattr(live, "reasoning", None) is not None, (
        "ModelSettings no longer carries a reasoning field — nothing asks the "
        "provider to think out loud, so the reasoning trail is permanently empty"
    )


# ── the event ─────────────────────────────────────────────────────────


def test_reasoning_is_its_own_event_type() -> None:
    """⚠ NOT folded into ``thinking``. That one is prose the model wrote FOR
    the user and renders at body size in a bubble; this is the working-out and
    renders dimmed in the activity trail. One type for both would put
    paragraphs of raw reasoning where the answer goes."""
    e = reasoning_event("The lease ends in March, so the renewal window is open.")
    assert e.type == EventType.REASONING
    assert EventType.REASONING != EventType.THINKING
    assert e.data["content"].startswith("The lease")


# ── the orchestrator (gotcha 62: a helper nothing calls) ──────────────


def _orch_tree() -> ast.Module:
    return ast.parse(_ORCH.read_text(encoding="utf-8"))


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_the_orchestrator_actually_publishes_reasoning() -> None:
    """A behavioural test of ``reasoning_event`` cannot see that nothing calls
    it — which is exactly the state this whole change found the stream in."""
    calls = _calls(_orch_tree(), "reasoning_event")
    assert calls, (
        "nothing in the orchestrator publishes a reasoning event — the SDK's "
        "deltas are being dropped again and the wait is a spinner"
    )


def test_every_reasoning_publish_goes_through_the_identifier_scrubber() -> None:
    """⚠ THE LEAK GUARD, and it has to be AST (gotcha 40 — the comments in
    that file quote the function names being discussed).

    Reasoning is dense with raw Buildium record numbers and this text reaches
    the screen. ``reply_hygiene`` exists because identifiers in user-facing
    prose is a live defect class (gotcha 38), and a new channel that bypasses
    it is the same leak wearing a different name.
    """
    for call in _calls(_orch_tree(), "reasoning_event"):
        assert call.args, "reasoning_event called with no content"
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "scrub_reply_text", (
            "a reasoning_event is being published with unscrubbed text — raw "
            "record numbers would reach the user"
        )


def test_the_reasoning_buffer_is_flushed_before_a_tool_call() -> None:
    """A buffer held to a boundary the model may never emit is text nobody
    sees. The last sentence of the working-out is the one that says WHY the
    next step is happening, so it has to land before that step's row."""
    src = _ORCH.read_text(encoding="utf-8")
    tool_called = src.index('event.name == "tool_called"')
    window = src[tool_called : tool_called + 2000]
    assert "reasoning_buffer" in window, (
        "the reasoning tail is not flushed at the tool call — the sentence "
        "explaining the step never reaches the screen"
    )


# ── the client (gotcha 28: read both sides) ───────────────────────────


def test_the_client_renders_reasoning_and_does_not_count_it_as_a_step() -> None:
    """⚠ Both halves. Passing it through is useless if the trail then reports
    "47 steps" for four tool calls — a count nobody can trust is worse than no
    count, and it is the summary line the user actually reads."""
    chat = _projection_source(_ROOT).read_text(encoding="utf-8")
    assert "'reasoning'" in chat, "runnerFold.ts drops reasoning events on the floor"

    thread = (
        _ROOT / "frontend" / "src" / "components" / "conversation" / "ConversationThread.tsx"
    ).read_text(encoding="utf-8")
    assert "case 'reasoning'" in thread, "the renderer has no reasoning branch"
    assert "l.kind === 'step'" in thread, (
        "the activity trail's summary is counting every line again — a "
        "thinking model would report dozens of 'steps' it never took"
    )
