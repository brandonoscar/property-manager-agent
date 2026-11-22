"""The toolkit connectors' sync cycle runs beside the worker's claims, never
in front of them.

The worker awaited every sync cycle inline, so no webhook event was claimed
while one ran. A Rent Manager or Propertyware pass reads for minutes under
its call budget, five companies a tick, so one tick could hold every claim
for up to an hour. ``worker._SingleFlight`` starts the cycle as a background
task, one at a time, and cancels it when the loop ends.

Pinned here: the runner's behaviour (starts at once, one at a time, never
raises, stops at shutdown), and the loop's wiring, read from its AST (rule
17): the cycle is started through the runner, never awaited in the loop, and
the runner is entered beside the Buildium client so every way out of the
loop stops it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap

import pytest

from helixis.webhooks import worker
from helixis.webhooks.worker import _SingleFlight


async def test_a_cycle_starts_at_once_and_runs_beside_the_caller() -> None:
    release = asyncio.Event()
    ran: list[str] = []

    async def cycle() -> None:
        ran.append("started")
        await release.wait()
        ran.append("finished")

    async with _SingleFlight("test cycle") as flight:
        assert flight.start(cycle) is True
        await asyncio.sleep(0)
        # The caller is free while the cycle waits: this is the claim loop.
        assert ran == ["started"]
        assert flight.busy()
        release.set()
        await asyncio.sleep(0.01)
        assert ran == ["started", "finished"]
        assert not flight.busy()


async def test_a_tick_that_finds_the_last_cycle_running_starts_nothing() -> None:
    release = asyncio.Event()
    calls: list[int] = []

    async def cycle() -> None:
        calls.append(1)
        await release.wait()

    async with _SingleFlight("test cycle") as flight:
        assert flight.start(cycle) is True
        await asyncio.sleep(0)
        assert flight.start(cycle) is False
        await asyncio.sleep(0)
        assert calls == [1], "a second cycle started while the first ran"
        release.set()
        await asyncio.sleep(0.01)
        # Once it is done, the next tick starts one again.
        assert flight.start(cycle) is True
        await asyncio.sleep(0.01)
        assert calls == [1, 1]


@pytest.mark.parametrize("error", [RuntimeError("db down"), SystemExit(1)])
async def test_a_failing_cycle_is_logged_and_never_raises(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    async def cycle() -> None:
        raise error

    with caplog.at_level(logging.ERROR, logger=worker.logger.name):
        async with _SingleFlight("test cycle") as flight:
            assert flight.start(cycle) is True
            await asyncio.sleep(0.01)
            assert not flight.busy()
    assert any("test cycle failed" in r.getMessage() for r in caplog.records)


async def test_leaving_the_loop_cancels_a_running_cycle_and_lets_it_unwind() -> None:
    unwound: list[str] = []

    async def cycle() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            # Where an open transaction rolls back.
            unwound.append("rolled back")

    async with _SingleFlight("test cycle") as flight:
        flight.start(cycle)
        await asyncio.sleep(0)
    assert unwound == ["rolled back"]
    assert not flight.busy()


async def test_a_cycle_that_will_not_stop_is_left_after_the_grace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Shutdown never hangs on a cycle: past the grace it logs and goes on."""
    monkeypatch.setattr(worker, "_BACKGROUND_STOP_GRACE_SECONDS", 0.05)
    stubborn = asyncio.Event()

    async def cycle() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await stubborn.wait()

    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        async with asyncio.timeout(2.0):
            async with _SingleFlight("test cycle") as flight:
                flight.start(cycle)
                await asyncio.sleep(0)
    assert any("did not stop" in r.getMessage() for r in caplog.records)
    stubborn.set()
    await asyncio.sleep(0.01)


# ── The loop's wiring, read from its AST ────────────────────────────────


def _run_worker() -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(worker.run_worker)))
    found = tree.body[0]
    assert isinstance(found, ast.AsyncFunctionDef)
    return found


def _calls_to(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_the_loop_never_awaits_the_pms_cycle_itself() -> None:
    run = _run_worker()
    awaited = [
        n
        for n in ast.walk(run)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "run_pms_sync_cycle"
    ]
    assert not awaited, "the PMS cycle is awaited in the claim loop again"
    calls = _calls_to(run, "run_pms_sync_cycle")
    assert calls, "the loop no longer runs the PMS cycle at all: this test proves nothing"
    # Every call sits in a lambda handed to the runner's start.
    lambdas = [
        n
        for n in ast.walk(run)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "start"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "pms_sync"
        and n.args
        and isinstance(n.args[0], ast.Lambda)
    ]
    inside = {id(c) for lam in lambdas for c in _calls_to(lam, "run_pms_sync_cycle")}
    assert {id(c) for c in calls} <= inside


def test_the_runner_is_entered_beside_the_buildium_client() -> None:
    """Every way out of the loop (the stop signal, the empty-poll exit, an
    exception) leaves this ``async with``, which is what stops the cycle
    before the pool closes."""
    run = _run_worker()
    withs = [n for n in ast.walk(run) if isinstance(n, ast.AsyncWith)]
    entered = [
        {
            item.context_expr.func.id
            for item in w.items
            if isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
        }
        for w in withs
    ]
    assert {"BuildiumClient", "_SingleFlight"} in entered
