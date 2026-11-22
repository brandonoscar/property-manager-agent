"""How a step ENDED has to reach the wire, or the client cannot render it.

Three states, not two. A read that returned no rows is not evidence the record
does not exist (gotcha 26) — so ``empty`` must be distinguishable from ``ok``
at the wire, and a per-tool ``error`` must mark ONE row rather than poisoning a
turn that got three good answers and one failure.

⚠ And the correlation key. The SDK runs a turn's tool calls CONCURRENTLY
(gotcha 25), so a client pairing starts to results by name and arrival order
mis-attributes the failure when calls 1, 2, 4 return and 3 does not. ``call_id``
was already tracked server-side for telemetry and never put on the wire.
"""

from __future__ import annotations

import ast
from pathlib import Path

from helixis.common.events import (
    TOOL_STATUSES,
    EventType,
    classify_tool_result,
    tool_result_event,
    tool_start_event,
)

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


# ── the classifier ────────────────────────────────────────────────────


def test_nothing_is_empty_and_something_is_not() -> None:
    for nothing in ("", "   ", "[]", "{}", "null", "None", '""'):
        assert classify_tool_result(nothing) == "empty", nothing
    for something in ("3 rows", "0", "[{'id': 1}]", "false"):
        assert classify_tool_result(something) == "ok", something


def test_prose_explaining_a_miss_is_an_answer_not_an_empty() -> None:
    """⚠ A tool that says WHY it found nothing is giving a real answer, and
    dimming that row hides the explanation. This is the same distinction the
    truncation footer exists for: only unambiguously-nothing is nothing."""
    for prose in (
        "No open work orders were found at 200 E Colfax Ave.",
        "None on file — the mirror carries no paid bills.",
        "I searched all 179 tasks and none matched.",
    ):
        assert classify_tool_result(prose) == "ok", prose


def test_the_sdk_error_text_matched_here_is_the_text_the_sdk_actually_emits() -> None:
    """⚠ Reads the LIVE SDK, not a hand-written string (gotcha 45).

    ``error`` is only ever the Agents SDK's own substituted failure text — it
    swallows the exception and hands the string back as the tool's output, so
    by the time the orchestrator sees it the prefix IS the only signal. Those
    words belong to the SDK and can change on an upgrade, and the failure mode
    is SILENT: every errored tool would quietly re-classify as ``ok`` and every
    row would render as a clean finish.
    """
    from agents.tool import default_tool_error_function  # type: ignore[attr-defined]

    produced = default_tool_error_function(None, RuntimeError("boom"))
    assert classify_tool_result(produced) == "error", (
        f"the SDK now says {produced[:60]!r} — _SDK_TOOL_ERROR_PREFIXES in "
        "common/events.py no longer matches it, so every tool failure is "
        "being rendered to the user as a successful step"
    )


def test_a_tool_that_merely_mentions_an_error_is_still_ok() -> None:
    """The prefix is matched at the START. A tool explaining an upstream
    failure in prose is answering the question, not failing."""
    assert (
        classify_tool_result(
            "Buildium returned an error occurred while running the tool — retry later."
        )
        == "ok"
    )


def test_every_status_the_classifier_can_return_is_a_declared_status() -> None:
    produced = {classify_tool_result(s) for s in ("", "x", "[]")}
    produced.add("error")
    assert produced <= set(TOOL_STATUSES)


# ── the wire ──────────────────────────────────────────────────────────


def test_both_tool_events_carry_the_correlation_key() -> None:
    start = tool_start_event("buildium_agent", "Checking Buildium", "call_7")
    done = tool_result_event("buildium_agent", "ok", 0, "call_7", "empty")
    assert start.type == EventType.TOOL_START
    assert start.data["call_id"] == "call_7"
    assert done.data["call_id"] == "call_7"
    assert done.data["status"] == "empty"


def test_the_key_is_optional_so_a_replayed_stream_still_parses() -> None:
    """⚠ A resumed or stored stream replays events emitted before this
    existed. The client keeps a name+count fallback for exactly those, so the
    field must default rather than raise."""
    assert tool_start_event("t", "d").data["call_id"] == ""
    assert tool_result_event("t", "r", 0).data["status"] == "ok"


# ── the other side of the boundary (gotcha 28) ────────────────────────


def _orchestrator_calls(func_name: str) -> list[ast.Call]:
    tree = ast.parse(_ORCH.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == func_name
    ]


def test_the_orchestrator_actually_puts_the_id_on_both_events() -> None:
    """⚠ AST on the CALL SITE. Every field here existed server-side already;
    the defect was that nothing forwarded it, and a behavioural test of the
    event builders cannot see a caller that omits an optional argument
    (gotcha 62)."""
    starts = _orchestrator_calls("tool_start_event")
    results = _orchestrator_calls("tool_result_event")
    assert starts, "no tool_start_event call found — this scan is not reading the orchestrator"
    assert results, "no tool_result_event call found — this scan is not reading the orchestrator"
    for call in starts:
        assert len(call.args) >= 3, (
            "tool_start_event is being called without a call_id — the client "
            "falls back to pairing by name and arrival order, which puts a "
            "concurrent call's failure on the wrong row"
        )
    for call in results:
        assert len(call.args) >= 5, (
            "tool_result_event is being called without call_id + status — an "
            "empty read would render identically to a successful one"
        )


def test_the_status_the_orchestrator_sends_is_classified_not_hardcoded() -> None:
    """A literal ``"ok"`` at the emit site would make every step read as a
    clean finish forever, and nothing would fail to say so."""
    for call in _orchestrator_calls("tool_result_event"):
        status_arg = call.args[4]
        assert isinstance(status_arg, ast.Call), (
            "tool_result_event's status is a constant at the call site — it "
            "must come from classify_tool_result(...) or empty and errored "
            "steps both render as successes"
        )
        assert getattr(status_arg.func, "id", "") == "classify_tool_result"


def test_the_client_pairs_by_id_and_keeps_the_fallback() -> None:
    """⚠ The consumer half. Correlating by identity is the entire fix; losing
    the name+count fallback would leave every replayed row spinning."""
    src = _projection_source(_ROOT).read_text(encoding="utf-8")
    assert "resultById" in src, "runnerFold.ts no longer pairs tool calls by call_id"
    assert "call_id" in src
    assert "pairedSoFar" in src, "the pre-call_id name+count fallback was removed"
