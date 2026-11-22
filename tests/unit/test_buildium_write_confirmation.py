"""Write-confirmation enforcement for Buildium write tools.

The guarantee under test: a Buildium write (``buildium_create_to_do_task``)
performs its side effect ONLY after explicit user approval, enforced in code
via ``confirm_or_abort`` inside the tool body — NOT by prompt convention.

COVERAGE MATRIX (read this before copying these as a template)
--------------------------------------------------------------
(a) Tool HONORS a denied gate — boundary, stub-driven.
    ``test_timeout_does_not_execute_write`` stubs ``confirm_or_abort``→False
    and asserts ``api.tasks.create`` is not called. This isolates the tool's
    "if not approved: return" branch from the gate internals.

(b) REAL deny / approve round-trip — boundary, emission-coupled.
    ``test_approve_executes_write_once`` / ``test_deny_does_not_execute_write``
    / ``test_write_blocked_until_approval_resolves`` /
    ``test_concurrent_confirms_do_not_cross_resolve`` run the *real*
    ``confirm_or_abort`` and drive approval through the payload it emits
    (mirroring the ``/agent/confirm`` round-trip).
    ⚠ By construction these are NOT clean detectors of an "always-approve"
    mutation (e.g. ``confirm_or_abort`` returning True as its first line):
    such a mutation removes the very emission they wait on, so they fail at
    setup ("no confirm payload emitted"), not at an assert-not-called. That
    is irreducible — the suite still goes red, just for setup reasons. The
    clean detector for the *result-ignoring* class is (a) + the guard in the
    tool; see the result-ignoring mutation note in the PR.

(c) ``confirm_or_abort`` FAIL-CLOSED branches — direct, NOT stubbed.
    ``test_confirm_or_abort_returns_false_when_queue_full`` and
    ``test_confirm_or_abort_returns_false_on_wait_timeout`` exercise the real
    function so the ``QueueFull``→False and wait-timeout→False branches are
    actually executed (the stub in (a) never runs this code). Each is paired
    with a write-tool boundary assertion that ``api.tasks.create`` is not
    called under that condition.

The gate awaits a Future that the API layer resolves out-of-band. We drive it
by running the impl as a background task, yielding so it pushes its confirm
payload, then resolving via the captured confirm_id (mirrors what
``/agent/confirm`` does in production).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.agents import idempotency as idem
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context


def _ctx_company(company_id: str = "company-1") -> Any:
    return memory_context(company_id=company_id, user_id="u1", session_id="s1")


def _api_capturing_create() -> tuple[Any, Any, AsyncMock]:
    """Client + api whose ``tasks.create`` is an AsyncMock we can assert on.

    ``create`` returns a dict with an Id and ``tasks.get`` reads back a matching
    Task, so the cut-1 read-back verifier confirms the write on the approve
    path. The deny/timeout tests never reach create, so this is inert for them.
    """
    from helixis.buildium.schemas import Task

    create = AsyncMock(return_value={"Id": 7777, "Title": "x"})
    api_mock = MagicMock()
    api_mock.tasks = MagicMock()
    api_mock.tasks.create = create
    api_mock.tasks.get = AsyncMock(return_value=Task(Id=7777, Title="x"))

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, create


async def _next_confirm_id() -> str:
    """Yield until the in-flight tool pushes its confirm payload, then return
    its confirm_id (draining the emission queue the way the orchestrator does)."""
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]["confirm_id"]
    raise AssertionError("no confirm payload was emitted by the write tool")


@pytest.mark.asyncio
async def test_approve_executes_write_once() -> None:
    """confirm → approve: the Buildium create runs exactly once."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        task = asyncio.create_task(
            bt._create_to_do_task_impl("Fix sink in 4B", assigned_to_user_id=1)
        )
        confirm_id = await _next_confirm_id()
        # Nothing should have been written yet — still awaiting approval.
        create.assert_not_called()

        await agent_tools.resolve_confirmation(confirm_id, True)
        result = await asyncio.wait_for(task, timeout=1.0)

    create.assert_awaited_once()
    # Write-probe 2026-07-05: Buildium 422s /tasks/todorequests without
    # Priority AND TaskStatus — pin that the approved payload carries both.
    sent = create.await_args.kwargs["fields"]
    assert sent["Priority"] == "Normal"
    assert sent["TaskStatus"] == "New"
    assert "Task created in Buildium" in result


@pytest.mark.asyncio
async def test_deny_does_not_execute_write() -> None:
    """confirm → deny: the Buildium create is never called."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        task = asyncio.create_task(
            bt._create_to_do_task_impl("Fix sink in 4B", assigned_to_user_id=1)
        )
        confirm_id = await _next_confirm_id()
        await agent_tools.resolve_confirmation(confirm_id, False)
        result = await asyncio.wait_for(task, timeout=1.0)

    create.assert_not_called()
    assert "didn't create" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_timeout_does_not_execute_write() -> None:
    """No user response before timeout → treated as denial, no write.

    We patch ``confirm_or_abort`` AS SEEN BY the idempotency primitive (the
    gate now runs inside ``run_guarded``) to a fast timeout-equivalent
    (returns False) so the test doesn't wait 300s. This still exercises the
    real branch: not-approved ⇒ no API call.
    """
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    async def _timed_out(**_kwargs: Any) -> bool:
        return False

    with (
        _ctx_company(),
        patch.object(idem, "confirm_or_abort", new=_timed_out),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._create_to_do_task_impl("Fix sink in 4B", assigned_to_user_id=1)

    create.assert_not_called()
    assert "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_write_blocked_until_approval_resolves() -> None:
    """The regression that proves the gate: even when the tool is called
    'directly' (model skipped any separate confirm step), the side effect is
    BLOCKED — ``api.tasks.create`` does not run while the confirm is pending,
    and only runs once an approval resolves."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        task = asyncio.create_task(
            bt._create_to_do_task_impl("Direct call, no prior confirm", assigned_to_user_id=1)
        )
        confirm_id = await _next_confirm_id()

        # Give the task ample scheduler turns; it MUST stay blocked on the
        # gate and never reach the API while the confirm is unresolved.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done(), "write tool returned without waiting for approval"
        create.assert_not_called()

        # Once approval arrives, the write proceeds.
        await agent_tools.resolve_confirmation(confirm_id, True)
        await asyncio.wait_for(task, timeout=1.0)

    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_confirms_do_not_cross_resolve() -> None:
    """Two in-flight writes mint distinct confirm_ids; resolving one must not
    unblock the other (constraint 5)."""
    await agent_tools.drain_pending_confirmations()
    c1, a1, create1 = _api_capturing_create()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=c1),
        patch.object(bt, "BuildiumApi", return_value=a1),
    ):
        t1 = asyncio.create_task(bt._create_to_do_task_impl("Task one", assigned_to_user_id=1))
        id1 = await _next_confirm_id()
        t2 = asyncio.create_task(bt._create_to_do_task_impl("Task two", assigned_to_user_id=1))
        id2 = await _next_confirm_id()

        assert id1 != id2

        # Resolve only the first; the second must remain blocked.
        await agent_tools.resolve_confirmation(id1, True)
        await asyncio.wait_for(t1, timeout=1.0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not t2.done(), "resolving confirm #1 wrongly unblocked confirm #2"

        await agent_tools.resolve_confirmation(id2, False)
        await asyncio.wait_for(t2, timeout=1.0)


# ── (c) confirm_or_abort fail-closed branches — REAL function, not stubbed ──


@pytest.mark.asyncio
async def test_confirm_or_abort_returns_false_when_queue_full() -> None:
    """If the confirm payload can't be surfaced (emission queue full), the
    gate must fail CLOSED — return False, not silently proceed. Exercises the
    real ``except asyncio.QueueFull`` branch (the stub in (a) never runs it)."""
    await agent_tools.drain_pending_confirmations()

    with patch.object(
        agent_tools._pending_confirm_emissions,
        "put_nowait",
        side_effect=asyncio.QueueFull,
    ):
        approved = await agent_tools.confirm_or_abort(
            action="buildium_create_task",
            title="Create a task in Buildium?",
            details="...",
            items=[],
        )

    assert approved is False


@pytest.mark.asyncio
async def test_queue_full_blocks_write_at_boundary() -> None:
    """Boundary pairing for the queue-full branch: with emission failing,
    the write tool must NOT call ``api.tasks.create``."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
        patch.object(
            agent_tools._pending_confirm_emissions, "put_nowait", side_effect=asyncio.QueueFull
        ),
    ):
        result = await bt._create_to_do_task_impl("Fix sink in 4B", assigned_to_user_id=1)

    create.assert_not_called()
    assert "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_confirm_or_abort_returns_false_on_wait_timeout() -> None:
    """If the user never responds, the gate must fail CLOSED on timeout —
    return False. Exercises the real ``_wait_for_confirmation`` timeout path
    (TimeoutError → False) with a tiny timeout and no resolve."""
    await agent_tools.drain_pending_confirmations()

    approved = await agent_tools.confirm_or_abort(
        action="buildium_create_task",
        title="Create a task in Buildium?",
        details="...",
        items=[],
        timeout=0.01,
    )

    assert approved is False
    # The unresolved payload was still emitted — drain it so it doesn't leak
    # into another test's _next_confirm_id().
    await agent_tools.drain_pending_confirmations()


@pytest.mark.asyncio
async def test_wait_timeout_blocks_write_at_boundary() -> None:
    """Boundary pairing for the timeout branch: a real (un-stubbed) gate
    timeout must leave ``api.tasks.create`` uncalled. We patch only the
    timeout constant the tool passes — via a thin wrapper that forces a tiny
    timeout into the REAL confirm_or_abort — so the genuine timeout branch
    runs, not a stub that returns False directly."""
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    real_confirm = agent_tools.confirm_or_abort

    async def _fast_timeout(**kwargs: Any) -> bool:
        # Delegate to the REAL function but force an immediate timeout, so the
        # actual _wait_for_confirmation TimeoutError→False path executes.
        kwargs["timeout"] = 0.01
        return await real_confirm(**kwargs)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
        patch.object(idem, "confirm_or_abort", new=_fast_timeout),
    ):
        result = await bt._create_to_do_task_impl("Fix sink in 4B", assigned_to_user_id=1)

    create.assert_not_called()
    assert "nothing was changed" in result.lower()
    await agent_tools.drain_pending_confirmations()


@pytest.mark.asyncio
async def test_missing_assignee_refuses_before_the_gate() -> None:
    """A create with no assignee must fail BEFORE the claim, not at the POST.

    Live 2026-08-30, Blue Ridge: Buildium 422'd every create with
    ``{"Key":"AssignedToUserId","Value":"AssignedToUserId must be greater
    than 0."}``. That 422 is raised inside ``_do_create``, so ``run_guarded``
    reaps the claim ``abandoned_unknown`` -- never retried by design. One
    absent field therefore did not fail the call, it killed the action
    permanently, and the tool docstring was actively telling the model to omit
    the field ("Buildium will create the task unassigned").

    So the assertion is not merely "returns an error". It is that no gate was
    raised and no POST was attempted -- a PRE-side-effect refusal the agent can
    recover from by calling ``buildium_list_staff`` and trying again.
    """
    await agent_tools.drain_pending_confirmations()
    client_mock, api_mock, create = _api_capturing_create()

    gate_calls: list[dict[str, Any]] = []

    async def _recording_gate(**kwargs: Any) -> bool:
        gate_calls.append(kwargs)
        return True

    with (
        _ctx_company(),
        patch.object(idem, "confirm_or_abort", new=_recording_gate),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._create_to_do_task_impl("Fix sink in 4B")

    # No POST, and -- the load-bearing half -- no gate either. Approving a
    # write that cannot succeed spends a person's attention on nothing.
    create.assert_not_called()
    assert gate_calls == [], "a doomed write must not reach the confirm gate"

    # The reply has to route the agent to the fix, not just say "no".
    assert "buildium_list_staff" in result
    assert "assigned_to_user_id" in result
    assert "nothing was created" in result.lower()
