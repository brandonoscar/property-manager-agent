"""Actually RUN the streaming loop, and read what reaches the bus.

⚠ NOTHING DID THIS BEFORE. Every other test of ``run_agent_streaming`` is
static — an AST walk or a source grep — and that is the precise blind spot
gotcha 28 records: ``_queue_card("next_steps", …)`` was added without the
matching ``EventType`` member, so every substantive turn raised
``ValueError`` **behind an HTTP 200 SSE stream**. The client saw "something
went wrong", the traceback was Render-side only, and 4308 passing tests said
nothing, because the producer and the consumer were each well covered and
nobody ran the string through the enum.

Four features now live inside that same loop — the restatement dispatched
before the run, the reasoning deltas, the per-tool status and correlation id,
and the specialist substep emitter. Each is a branch whose failure mode is a
runtime error on a live turn. Ruff's F rules catch an undefined name; they
cannot catch an attribute that is not there or a branch that never fires.

So this drives the real function against a fake SDK stream and asserts on the
BYTES that reach the ``EventBus`` — the same surface the client reads.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from helixis.agents import orchestrator as orch
from helixis.agents.specialist_limits import specialist_stream_capture
from helixis.common.types import AgentRequest
from helixis.settings import Settings
from helixis.streaming.bus import EventBus

_TASK = "check whether 4B is behind on rent and what maintenance is open there"


# ── the fake SDK stream ───────────────────────────────────────────────


class _Delta:
    def __init__(self, dtype: str, delta: str) -> None:
        self.type, self.delta = dtype, delta


class _Raw:
    type = "raw_response_event"

    def __init__(self, data: Any) -> None:
        self.data = data


class _RunItem:
    type = "run_item_stream_event"

    def __init__(self, name: str, item: Any) -> None:
        self.name, self.item = name, item


class _Item:
    def __init__(self, raw: Any, output: Any = None) -> None:
        self.raw_item = raw
        if output is not None:
            self.output = output


class _FakeResult:
    """Stands in for ``Runner.run_streamed``'s return.

    ``nested`` fires ``specialist_stream_capture`` mid-stream, which is
    exactly when the live SDK fires it: while a consult is running and the
    OUTER loop is blocked. That is the condition the substep emitter exists
    for, so simulating it anywhere else would prove nothing.
    """

    final_output = "Here is the answer."
    last_agent = types.SimpleNamespace(name="orchestrator")

    def __init__(self, events: list[Any], nested: list[Any] | None = None) -> None:
        self._events = events
        self._nested = nested or []

    async def stream_events(self) -> Any:
        for event in self._events:
            await asyncio.sleep(0)
            if getattr(event, "name", None) == "tool_called":
                for nested in self._nested:
                    specialist_stream_capture(nested)
            yield event


def _nested_tool_call(name: str) -> dict[str, Any]:
    return {
        "event": _RunItem("tool_called", _Item({"name": name, "call_id": f"n-{name}"})),
    }


async def _drive(events: list[Any], nested: list[Any] | None = None) -> list[tuple[str, dict]]:
    """Run the real loop; return every event published, in order."""
    bus = EventBus()
    settings = Settings()
    request = AgentRequest(task=_TASK, company_id="co", session_id="s1")

    seen: list[tuple[str, dict]] = []
    real_publish = bus.publish

    async def spy(session_id: str, event: Any) -> Any:
        seen.append((str(event.type), dict(event.data)))
        return await real_publish(session_id, event)

    bus.publish = spy  # type: ignore[method-assign]

    async def fake_restate(*_: Any, **__: Any) -> str:
        return "Checking whether 4B is behind on rent."

    async def no_history(*_: Any, **__: Any) -> list[Any]:
        return []

    async def noop(*_: Any, **__: Any) -> None:
        return None

    with (
        patch.object(orch, "create_orchestrator", return_value=MagicMock()),
        patch.object(orch.Runner, "run_streamed", return_value=_FakeResult(events, nested)),
        patch.object(orch, "restate", new=fake_restate),
        patch.object(orch, "load_session_history", new=no_history),
        patch.object(orch, "save_message", new=noop),
        # No database in a unit run. The company-knowledge path already
        # degrades on this (it logs and continues); patching it here keeps the
        # test measuring the STREAM rather than the injection.
        patch.object(orch.db_engine, "get_pool", side_effect=RuntimeError("no pool")),
    ):
        await asyncio.wait_for(orch.run_agent_streaming(request, settings, bus), 30)
    return seen


_STANDARD = [
    _Raw(_Delta("response.reasoning_summary_text.delta", "The lease ends in March.\n")),
    _RunItem("tool_called", _Item({"name": "buildium_agent", "call_id": "c1"})),
    _RunItem("tool_output", _Item({"call_id": "c1"}, output="[]")),
    _Raw(_Delta("response.output_text.delta", "Answer text.")),
]


@pytest.fixture(scope="module")
def published() -> list[tuple[str, dict]]:
    return asyncio.run(_drive(_STANDARD, nested=[_nested_tool_call("buildium_get_property")]))


def _of(published: list[tuple[str, dict]], etype: str) -> list[dict]:
    return [data for t, data in published if t == etype]


# ── the turn survives at all ──────────────────────────────────────────


def test_the_run_completes_and_ends_with_done(published: list[tuple[str, dict]]) -> None:
    """⚠ The floor, and the one gotcha 28 needed. A `ValueError` raised in
    this loop does not fail loudly — it happens behind an HTTP 200 SSE stream,
    so the user gets a generic error and the traceback is server-side only."""
    assert published, "the loop published nothing at all"
    assert published[-1][0] == "done", f"the run did not finish cleanly: {published[-1]}"
    assert _of(published, "error") == [], "the loop raised into the stream"


def test_the_answer_still_reaches_the_user(published: list[tuple[str, dict]]) -> None:
    """Everything else here is decoration around this."""
    assert any(d.get("content") == "Answer text." for d in _of(published, "token"))


# ── each new branch, on a real execution ──────────────────────────────


def test_the_restatement_arrives_before_the_answer(published: list[tuple[str, dict]]) -> None:
    """⚠ Ordering is the whole feature. A restatement is worth publishing
    because it lands in the seconds after Enter; arriving behind the answer it
    is noise, which is why the run drops it once tokens start."""
    kinds = [t for t, _ in published]
    assert "restatement" in kinds, "the restatement never reached the bus"
    assert kinds.index("restatement") < kinds.index("token")


def test_reasoning_deltas_reach_the_bus_as_whole_lines(
    published: list[tuple[str, dict]],
) -> None:
    """⚠ These events were ALREADY on the SDK stream and were being dropped —
    the loop handled ``response.output_text.delta`` and nothing else. Whole
    lines, not raw tokens: a scrubber cannot redact half an identifier split
    across two deltas."""
    lines = [d["content"] for d in _of(published, "reasoning")]
    assert lines == ["The lease ends in March."]


def test_a_tool_call_carries_its_id_and_a_human_sentence(
    published: list[tuple[str, dict]],
) -> None:
    """The id was tracked server-side for telemetry and never put on the wire,
    so the client could only pair by name and arrival order — which lands a
    concurrent call's failure on the wrong row."""
    starts = _of(published, "tool_start")
    assert len(starts) == 1
    assert starts[0]["call_id"] == "c1"
    assert starts[0]["description"] == "Checking Buildium"
    assert "_" not in starts[0]["description"], "a machine name reached the user"


def test_an_empty_result_is_labelled_empty_not_ok(published: list[tuple[str, dict]]) -> None:
    """⚠ A read that came back with no rows is not evidence the record does
    not exist (gotcha 26). The wire has to carry the difference or the client
    cannot render it, and a blank renders as an answer."""
    results = _of(published, "tool_result")
    assert len(results) == 1
    assert results[0]["call_id"] == "c1"
    assert results[0]["status"] == "empty"


def test_a_specialists_nested_step_reaches_the_bus(published: list[tuple[str, dict]]) -> None:
    """⚠ Fired from INSIDE the consult, which is the only condition that
    matters: the outer loop is blocked there, so a queue drained between outer
    events would not drain until the consult had already finished (gotcha 4's
    deadlock, in a new place). Proves the emitter is bound and live at exactly
    that moment."""
    subs = [d["content"] for d in _of(published, "substep")]
    assert subs == ["Looking up the property"], f"nested steps did not reach the bus: {subs}"


# ── the branch that must NOT fire ─────────────────────────────────────


def test_a_trivial_question_pays_for_no_restatement() -> None:
    """⚠ Measured on a REAL run, not on the classifier. ``worth_restating``
    is unit-tested, but a run that calls it and ignores the answer would pass
    that test and still spend a model call on every one-fact lookup —
    gotcha 62's rule, applied to a cost control."""

    async def go() -> list[tuple[str, dict]]:
        bus = EventBus()
        request = AgentRequest(task="what's the rent on 4B", company_id="co", session_id="s2")
        seen: list[tuple[str, dict]] = []
        real = bus.publish

        async def spy(sid: str, ev: Any) -> Any:
            seen.append((str(ev.type), dict(ev.data)))
            return await real(sid, ev)

        bus.publish = spy  # type: ignore[method-assign]
        dispatched = {"count": 0}

        async def counting_restate(*_: Any, **__: Any) -> str:
            dispatched["count"] += 1
            return "Looking up the rent on 4B."

        async def no_history(*_: Any, **__: Any) -> list[Any]:
            return []

        async def noop(*_: Any, **__: Any) -> None:
            return None

        with (
            patch.object(orch, "create_orchestrator", return_value=MagicMock()),
            patch.object(orch.Runner, "run_streamed", return_value=_FakeResult(_STANDARD)),
            patch.object(orch, "restate", new=counting_restate),
            patch.object(orch, "load_session_history", new=no_history),
            patch.object(orch, "save_message", new=noop),
            patch.object(orch.db_engine, "get_pool", side_effect=RuntimeError("no pool")),
        ):
            await asyncio.wait_for(orch.run_agent_streaming(request, Settings(), bus), 30)
        assert dispatched["count"] == 0, "a one-fact lookup paid for a restatement call"
        return seen

    seen = asyncio.run(go())
    assert not [t for t, _ in seen if t == "restatement"]
    assert seen[-1][0] == "done", "the trivial path did not finish cleanly"


# ── the paragraph that rendered twice ────────────────────────────────────
#
# A live run showed the model's pre-tool narration in the trail AND again at
# the top of the answer. `reply_scrubber` publishes per LINE, so narration
# that stops mid-sentence to call a tool — the normal case — has published
# NOTHING and is sitting whole in its buffer. The `tool_called` branch reset
# `current_text` and `segment_text` and never touched the scrubber, so that
# buffer crossed the boundary and was re-emitted as answer tokens.
#
# ⚠ Has to RUN the loop. The scrubber is correct in isolation and the reset is
# correct in isolation; the defect is that one does not know about the other,
# which only an executed stream can show (gotcha 71).

_NARRATION = (
    "Let me check whether all the leases are being fully enforced, starting "
    "with the delinquency report and then the open work orders"  # no newline
)

_NARRATE_THEN_ANSWER = [
    _Raw(_Delta("response.output_text.delta", _NARRATION)),
    _RunItem("tool_called", _Item({"name": "buildium_agent", "call_id": "c1"})),
    _RunItem("tool_output", _Item({"call_id": "c1"}, output="[]")),
    _Raw(_Delta("response.output_text.delta", "\n\nHere is the answer.\n")),
]


@pytest.fixture(scope="module")
def narrated() -> list[tuple[str, dict]]:
    return asyncio.run(_drive(_NARRATE_THEN_ANSWER))


def _payloads(published: list[tuple[str, dict]], kind: str) -> list[str]:
    return [str(d.get("content") or "") for t, d in published if t == kind]


def test_pre_tool_narration_is_published_once_as_a_thought(narrated) -> None:
    thoughts = [
        p for p in _payloads(narrated, "thinking") if "leases are being fully enforced" in p
    ]
    assert len(thoughts) == 1, f"narration appeared in {len(thoughts)} thinking events"


def test_the_same_narration_never_arrives_again_as_answer_tokens(narrated) -> None:
    """⚠ THE BUG. The trail bubble and the answer bubble both showed it."""
    leaked = [p for p in _payloads(narrated, "token") if "leases are being fully enforced" in p]
    assert not leaked, f"narration re-published as answer text: {leaked!r}"


def test_the_real_answer_still_streams(narrated) -> None:
    """Both directions (gotcha 38): dropping the buffer must not eat the
    answer written AFTER the tool call."""
    tokens = "".join(_payloads(narrated, "token"))
    assert "Here is the answer." in tokens


# ── cards are written down, not just streamed ────────────────────────


def test_a_card_is_persisted_so_a_reload_can_restore_it() -> None:
    """⚠ Cards used to exist ONLY on the live stream.

    Reloading a thread replays `helixis_chat_messages`, so every card was
    dropped and only the prose came back. For a FORM that is a dead end: the
    model said "fill that in", is waiting, and was told not to re-ask in
    prose — so the user sees a sentence about a form that is not there.

    This runs the real loop (gotcha 71 — a failure inside it hides behind a
    2xx) and asserts the row that makes the restore possible actually gets
    written, with the SCRUBBED payload.
    """
    from helixis.agents import visual_tools
    from helixis.common.events import CARD_HISTORY_PREFIX

    saved: list[dict[str, Any]] = []

    async def go() -> None:
        bus = EventBus()
        settings = Settings()
        request = AgentRequest(task=_TASK, company_id="co", session_id="s-cards")

        async def fake_restate(*_: Any, **__: Any) -> str:
            return "Setting up a task."

        async def no_history(*_: Any, **__: Any) -> list[Any]:
            return []

        async def capture(**kwargs: Any) -> None:
            saved.append(kwargs)

        class _FormMidRun(_FakeResult):
            """Queue a form the way `request_details` does, mid-run: the tool
            body runs inside the run, so the card lands in THAT run's queue.
            Queued before the run started it would sit in no run's queue at
            all, since each run drains only its own (gotcha 175)."""

            async def stream_events(self) -> Any:
                async for event in super().stream_events():
                    yield event
                    if getattr(event, "name", None) == "tool_called":
                        visual_tools._request_details_impl(
                            "New maintenance task",
                            [["unit", "Which unit"], ["what", "What needs doing"]],
                            submit_label="Create task",
                        )

        with (
            patch.object(orch, "create_orchestrator", return_value=MagicMock()),
            patch.object(orch.Runner, "run_streamed", return_value=_FormMidRun(_STANDARD, None)),
            patch.object(orch, "restate", new=fake_restate),
            patch.object(orch, "load_session_history", new=no_history),
            patch.object(orch, "save_message", new=capture),
            patch.object(orch.db_engine, "get_pool", side_effect=RuntimeError("no pool")),
        ):
            await asyncio.wait_for(orch.run_agent_streaming(request, settings, bus), 30)

    asyncio.run(go())

    card_rows = [r for r in saved if str(r.get("tool_name", "")).startswith(CARD_HISTORY_PREFIX)]
    assert card_rows, (
        "the streaming loop published a card and saved no row for it — a reload "
        "will drop it, which is the defect this test exists for"
    )
    row = card_rows[0]
    assert row["tool_name"] == f"{CARD_HISTORY_PREFIX}form"
    assert row["role"] == "tool"
    # The payload has to be the CARD, not a placeholder — a row that restores
    # an empty card is the same dead end wearing a different costume.
    assert row["tool_result"]["title"] == "New maintenance task"
    assert [f["name"] for f in row["tool_result"]["fields"]] == ["unit", "what"]
