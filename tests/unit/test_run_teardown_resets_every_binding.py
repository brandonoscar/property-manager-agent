"""A run resets its write marker and table verdict on every exit.

Andrew's audit (2026-09-29), confirmed in code (gotcha 179): #972 put the restatement's
``cancel()`` branch above ``unbind_write_marker`` and ``reset_table_policy``
in the run's ``finally``, and the two resets ended up inside it. They ran
only while a restatement was still pending. In the usual case (it had landed,
or the question was never worth restating) the table verdict outlived its run.
``bind_table_policy`` binds nothing for a question it doesn't recognise, so
the next run in the same task (the eval runner, the browser sweep, the
battery) inherited the last verdict and had its tables refused.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.agents import orchestrator as orch
from helixis.agents import table_policy, write_guard
from helixis.common.types import AgentRequest
from helixis.settings import Settings
from helixis.streaming.bus import EventBus

_JUDGMENT = "Marcus says our repair window is 30 days. Is he right?"
_UNRECOGNISED = "Summarise the Maple Court portfolio"


class _Raw:
    type = "raw_response_event"

    def __init__(self, delta: str) -> None:
        self.data = types.SimpleNamespace(type="response.output_text.delta", delta=delta)


class _FakeResult:
    final_output = "Done."
    last_agent = types.SimpleNamespace(name="orchestrator")

    async def stream_events(self) -> Any:
        yield _Raw("Done.")


async def _run(task: str, seen: list[bool], *, restate: Any = None) -> None:
    """One real ``run_agent_streaming`` call in the CALLER's context, recording
    whether tables were refused while the model ran."""

    def run_streamed(*_: Any, **__: Any) -> _FakeResult:
        seen.append(table_policy.tables_are_surplus())
        return _FakeResult()

    async def no_restate(*_: Any, **__: Any) -> str:
        return ""

    async def no_history(*_: Any, **__: Any) -> list[Any]:
        return []

    async def noop(*_: Any, **__: Any) -> None:
        return None

    with (
        patch.object(orch, "create_orchestrator", return_value=MagicMock()),
        patch.object(orch.Runner, "run_streamed", run_streamed),
        patch.object(orch, "restate", new=restate or no_restate),
        patch.object(orch, "load_session_history", new=no_history),
        patch.object(orch, "save_message", new=noop),
        patch.object(orch.db_engine, "get_pool", MagicMock(side_effect=RuntimeError("no pool"))),
        patch("helixis.db.repositories.company_knowledge.load", AsyncMock(return_value=None)),
        patch.object(orch, "_run_timezone_for", AsyncMock(return_value=None)),
        patch.object(orch, "resolve_run_pms", AsyncMock(return_value=None)),
    ):
        request = AgentRequest(task=task, company_id="co", session_id=f"s-{len(seen)}")
        await orch.run_agent_streaming(request, Settings(), EventBus())


async def test_a_judgment_runs_verdict_does_not_reach_the_next_run() -> None:
    seen: list[bool] = []
    await _run(_JUDGMENT, seen)
    await _run(_UNRECOGNISED, seen)
    assert seen == [True, False], "the second run inherited the first run's table verdict"


async def test_a_finished_run_leaves_neither_binding_behind() -> None:
    await _run(_JUDGMENT, [])
    assert table_policy._policy.get() is None
    assert write_guard._write_state.get() is None


async def test_the_resets_also_run_while_a_restatement_is_still_pending() -> None:
    """The path that always worked, pinned so a fix can't move the resets into
    an ``else`` and break it."""
    hung = asyncio.Event()

    async def slow_restate(*_: Any, **__: Any) -> str:
        await hung.wait()
        return ""

    with patch.object(orch, "worth_restating", return_value=True):
        await _run(_JUDGMENT, [], restate=slow_restate)
    assert table_policy._policy.get() is None
    assert write_guard._write_state.get() is None
