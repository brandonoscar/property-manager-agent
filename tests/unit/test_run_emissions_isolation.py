"""One run's tables, maps, download links, drafts and cards reach ITS chat only.

Andrew's audit (2026-09-29), confirmed in code: the five queues a tool fills
and the stream loop drains were module-level. ``run_agent_streaming`` drains
all of them after every stream event (token deltas included) and publishes
what it finds under its OWN session id, and one uvicorn worker serves every
company. So company A's table, or the signed link to A's CSV, went to whichever
run streamed next, and A's model was told it had been shown (gotcha 175).

Two layers, because each can be right while the other is wrong:

* the queues: two bound runs cannot see each other's payloads, on all five
  channels, and a task a run spawns (the SDK's tool task, a specialist's
  nested run) still lands in that run;
* the real loop: two ``run_agent_streaming`` calls side by side, with run B
  streaming while run A's table waits to be published (gotcha 71's rule:
  this loop is tested by RUNNING it).
"""

from __future__ import annotations

import asyncio
import contextlib
import types
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from helixis.agents import orchestrator as orch
from helixis.agents import visual_tools as vt
from helixis.common.types import AgentRequest
from helixis.settings import Settings
from helixis.streaming.bus import EventBus

Push = Callable[[], Awaitable[Any]]
Drain = Callable[[], Awaitable[list[Any]]]


async def _push_table() -> Any:
    return await vt._show_table_impl("Alder Court tenants", ["Tenant"], [["Alice Alder"]])


async def _push_map() -> Any:
    return vt.queue_map([(40.0, -75.0)], caption="Alder Court")


async def _push_artifact() -> Any:
    return vt.queue_artifacts([{"name": "a.csv", "size": 9, "url": "https://signed.example/a"}])


async def _push_email_draft() -> Any:
    return await vt._show_email_draft_impl(
        ["tenant@alder.example"], ["Reminder"], ["Rent"], ["Your rent is due."], None
    )


async def _push_card() -> Any:
    return vt._queue_card("detail_card", {"title": "Alder Court 4B"})


CHANNELS: list[tuple[str, Push, Drain]] = [
    ("table", _push_table, vt.drain_pending_visuals),
    ("map", _push_map, vt.drain_pending_maps),
    ("download link", _push_artifact, vt.drain_pending_artifacts),
    ("email draft", _push_email_draft, vt.drain_pending_email_drafts),
    ("card", _push_card, vt.drain_pending_cards),
]


@pytest.fixture(autouse=True)
async def _clean_unbound_queues() -> Any:
    """The unbound fallback is module-level by design; keep it empty around
    each test so no test reads another's leftovers."""
    for _name, _push, drain in CHANNELS:
        await drain()
    yield
    for _name, _push, drain in CHANNELS:
        await drain()


# ── the queues ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "push", "drain"), CHANNELS, ids=[c[0] for c in CHANNELS])
async def test_another_runs_drain_never_sees_this_runs_payload(
    name: str, push: Push, drain: Drain
) -> None:
    """The exact interleaving that leaked: A queues, B drains before A does."""
    a_pushed, b_drained = asyncio.Event(), asyncio.Event()
    seen: dict[str, list[Any]] = {}

    async def run_a() -> None:
        token = vt.bind_run_emissions()
        try:
            await push()
            a_pushed.set()
            await b_drained.wait()
            seen["a"] = await drain()
        finally:
            vt.reset_run_emissions(token)

    async def run_b() -> None:
        token = vt.bind_run_emissions()
        try:
            await a_pushed.wait()
            seen["b"] = await drain()
            b_drained.set()
        finally:
            vt.reset_run_emissions(token)

    await asyncio.wait_for(asyncio.gather(run_a(), run_b()), 5)
    assert seen["b"] == [], f"run B received run A's {name}"
    assert len(seen["a"]) == 1, f"run A lost its own {name}"


@pytest.mark.parametrize(("name", "push", "drain"), CHANNELS, ids=[c[0] for c in CHANNELS])
async def test_a_task_the_run_spawns_still_lands_in_the_run(
    name: str, push: Push, drain: Drain
) -> None:
    """The SDK runs each tool, and a specialist's nested run, in a task it
    creates; a task copies its parent's context, so it reaches the SAME queues
    the run drains. Without this a per-run queue would drop every card a
    specialist renders."""
    token = vt.bind_run_emissions()
    try:
        await asyncio.create_task(push())
        assert len(await drain()) == 1, f"a {name} pushed from a child task never arrived"
    finally:
        vt.reset_run_emissions(token)


async def test_unbound_callers_keep_working_and_stay_out_of_bound_runs() -> None:
    """Unit tests and the non-streaming ``Runner.run`` callers bind nothing and
    still round-trip. What they leave behind is invisible to a real run."""
    await _push_table()
    token = vt.bind_run_emissions()
    try:
        assert await vt.drain_pending_visuals() == []
    finally:
        vt.reset_run_emissions(token)
    assert len(await vt.drain_pending_visuals()) == 1


# ── the real loop ─────────────────────────────────────────────────────


class _Delta:
    def __init__(self, delta: str) -> None:
        self.type, self.delta = "response.output_text.delta", delta


class _Raw:
    type = "raw_response_event"

    def __init__(self, delta: str) -> None:
        self.data = _Delta(delta)


class _RunItem:
    type = "run_item_stream_event"

    def __init__(self, name: str, raw: Any, output: Any = None) -> None:
        self.name = name
        self.item = types.SimpleNamespace(raw_item=raw)
        if output is not None:
            self.item.output = output


_END = object()


class _FakeResult:
    """Stands in for ``Runner.run_streamed``'s return, with the one property
    this test depends on: like the SDK, the run's work starts in a task
    created INSIDE ``run_streamed``, so it carries the context as it was at
    that call. A binding made after ``run_streamed`` never reaches the tools."""

    final_output = "Done."
    last_agent = types.SimpleNamespace(name="orchestrator")

    def __init__(self, events: Callable[[], Any]) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

        async def run() -> None:
            try:
                async for event in events():
                    await self._queue.put(event)
            finally:
                await self._queue.put(_END)

        self._task = asyncio.get_running_loop().create_task(run())

    async def stream_events(self) -> Any:
        while (event := await self._queue.get()) is not _END:
            yield event


def _no_model_no_db(run_streamed: Callable[..., _FakeResult]) -> contextlib.ExitStack:
    async def fake_restate(*_: Any, **__: Any) -> str:
        return ""

    async def no_history(*_: Any, **__: Any) -> list[Any]:
        return []

    async def noop(*_: Any, **__: Any) -> None:
        return None

    stack = contextlib.ExitStack()
    for p in (
        patch.object(orch, "create_orchestrator", return_value=MagicMock()),
        patch.object(orch.Runner, "run_streamed", side_effect=run_streamed),
        patch.object(orch, "restate", new=fake_restate),
        patch.object(orch, "load_session_history", new=no_history),
        patch.object(orch, "save_message", new=noop),
        patch.object(orch.db_engine, "get_pool", side_effect=RuntimeError("no pool")),
    ):
        stack.enter_context(p)
    return stack


async def test_two_live_runs_side_by_side_each_get_only_their_own_visuals() -> None:
    """Run A renders a table and a download link mid-tool-call; while they wait
    to be published, run B streams five events, and each one runs the loop's
    drain. Before the fix B published both under its own session."""
    bus = EventBus()
    published: dict[str, list[tuple[str, dict]]] = {"sA": [], "sB": []}
    real_publish = bus.publish
    a_tool_started, a_pushed, b_streamed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def spy(session_id: str, event: Any) -> Any:
        published.setdefault(session_id, []).append((str(event.type), dict(event.data)))
        if session_id == "sA" and str(event.type) == "tool_start":
            a_tool_started.set()
        return await real_publish(session_id, event)

    bus.publish = spy  # type: ignore[method-assign]

    async def stream_a() -> Any:
        yield _RunItem("tool_called", {"name": "show_table", "call_id": "a1"})
        # Queue only once A's loop has finished with that event, so the
        # payload is waiting when B's drains run (the leaking interleaving).
        await a_tool_started.wait()
        await asyncio.sleep(0.02)
        await _push_table()
        await _push_artifact()
        a_pushed.set()
        await b_streamed.wait()
        yield _RunItem("tool_output", {"call_id": "a1"}, output="Displayed a table.")
        yield _Raw("Here are the tenants.")

    async def stream_b() -> Any:
        await a_pushed.wait()
        for i in range(5):
            await asyncio.sleep(0)
            yield _Raw(f"b{i} ")
        b_streamed.set()

    def fake_run_streamed(*_: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult(stream_a if "Alder" in str(kwargs.get("input")) else stream_b)

    settings = Settings()
    run_a = AgentRequest(
        task="list every tenant at Alder Court", company_id="co-a", session_id="sA"
    )
    run_b = AgentRequest(task="what is open at Birch Lane", company_id="co-b", session_id="sB")
    with _no_model_no_db(fake_run_streamed):
        await asyncio.wait_for(
            asyncio.gather(
                orch.run_agent_streaming(run_a, settings, bus),
                orch.run_agent_streaming(run_b, settings, bus),
            ),
            30,
        )

    def kinds(session: str) -> list[str]:
        return [t for t, _ in published[session]]

    assert kinds("sA")[-1] == "done" and kinds("sB")[-1] == "done"
    assert "tabular_data" not in kinds("sB"), "run A's table reached run B's chat"
    assert "artifacts" not in kinds("sB"), "run A's download link reached run B's chat"
    tables = [d for t, d in published["sA"] if t == "tabular_data"]
    links = [d for t, d in published["sA"] if t == "artifacts"]
    assert [t["title"] for t in tables] == ["Alder Court tenants"]
    assert links == [
        {"artifacts": [{"name": "a.csv", "size": 9, "url": "https://signed.example/a"}]}
    ]


async def test_a_finished_run_leaves_no_binding_behind() -> None:
    """Unbound in ``finally``, like the confirm emitter: whatever runs next in
    the caller's context must not push into a finished run's queues."""

    async def quiet() -> Any:
        yield _Raw("ok")

    request = AgentRequest(task="what is open at Birch Lane", company_id="co-b", session_id="sQ")
    # Awaited in THIS task (``asyncio.timeout``, not ``wait_for``, which would
    # run it in a task of its own and hide a missing reset).
    with _no_model_no_db(lambda *_a, **_k: _FakeResult(quiet)):
        async with asyncio.timeout(30):
            await orch.run_agent_streaming(request, Settings(), EventBus())
    assert vt._run_emissions.get() is None
