"""Our own deadline must not cut a run between approval and the write landing.

Measured 2026-08-30 on the write harness's reversible tier. All three writes
came back `abandoned_unknown / ambiguous:RuntimeError`, the run row said

    error_type  SoftDeadlineSynthesized
    err         "wall-clock deadline 240s exceeded (stopped at the 168s soft
                 cutoff to write an answer)"

and 168s is exactly `SOFT_DEADLINE_FRACTION * 240`. The stop fired while
`run_guarded` was suspended in `await execute()`; its `except Exception`
reaped the claim as ambiguous; and `abandoned_unknown` is **never retried by
design**, because a duplicate write in a landlord's records is worse than a
missing one.

Net effect: a human approved a move-out against a real tenant's lease and
nobody — not the operator, not the ledger, not the harness — could say whether
it had happened.

The soft stops are right for READS (gotcha 37's 40% recovery). They have no
business firing once a person has approved a gate.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents import orchestrator as orch
from helixis.agents.write_guard import (
    WRITE_GRACE_MULTIPLIER,
    bind_write_marker,
    executing_write,
    unbind_write_marker,
    write_in_flight,
)

# ── the marker ────────────────────────────────────────────────────────


def test_unbound_is_a_no_op() -> None:
    """Tests, the webhook worker, and every path with no orchestrator run must
    behave byte-for-byte as before."""
    assert write_in_flight() is False
    with executing_write("buildium_post_charge"):
        assert write_in_flight() is False


def test_it_marks_only_for_the_duration_of_the_write() -> None:
    _state, token = bind_write_marker()
    try:
        assert not write_in_flight()
        with executing_write("buildium_record_move_out"):
            assert write_in_flight()
        assert not write_in_flight(), "the marker outlived the write it describes"
    finally:
        unbind_write_marker(token)


def test_concurrent_writes_do_not_unprotect_each_other() -> None:
    """A COUNTER, not a boolean. The SDK runs a turn's tool calls concurrently
    (gotcha 25), so two gated writes can overlap — and a boolean would be
    cleared by whichever finished first, cutting the other mid-flight."""
    _state, token = bind_write_marker()
    try:
        with executing_write("a"):
            with executing_write("b"):
                assert write_in_flight()
            assert write_in_flight(), "the inner write's exit unprotected the outer one"
        assert not write_in_flight()
    finally:
        unbind_write_marker(token)


@pytest.mark.asyncio
async def test_the_marker_crosses_the_as_tool_boundary() -> None:
    """A nested `.as_tool()` sub-run is a Task, and a Task COPIES the context —
    a `ContextVar.set` in there is invisible to the watchdog polling outside
    (gotchas 34/42). Mutating a shared dict is what crosses; rebinding is not.

    The write executes inside the sub-run and the watchdog reads from the
    parent, so if this does not hold the whole fix is inert.
    """
    _state, token = bind_write_marker()
    try:
        seen: list[bool] = []

        async def _nested_sub_run() -> None:
            with executing_write("buildium_post_charge"):
                seen.append(write_in_flight())
                await asyncio.sleep(0)

        await asyncio.create_task(_nested_sub_run())
        assert seen == [True], "the marker did not survive the Task boundary"
    finally:
        unbind_write_marker(token)


# ── the watchdog ──────────────────────────────────────────────────────


async def _drain(gen) -> int:
    n = 0
    async for _ in gen:
        n += 1
    return n


@pytest.mark.asyncio
async def test_the_cost_ceiling_does_not_cut_an_approved_write() -> None:
    """The exact shape that lost three writes: an expensive run that has
    already been approved. Before this, the ceiling raised mid-`execute()`."""
    _state, token = bind_write_marker()
    try:

        async def _events():
            for _ in range(5):
                yield object()
                await asyncio.sleep(0.01)

        # ⚠ The marker is entered AROUND the drain, not inside the generator.
        # In production `run_guarded` holds it for the whole of `execute()`
        # while the stream keeps running -- the watchdog polls on its own
        # timer and must see the flag already set. Marking it inside the
        # generator leaves the first poll unprotected, which is a property of
        # the test, not of the fix.
        with executing_write("buildium_post_charge"):
            seen = await _drain(
                orch._stream_with_stall_watchdog(
                    _events(), {"pending": False}, over_cost=lambda: True
                )
            )
        assert seen == 5, "the cost ceiling fired while a write was in flight"
    finally:
        unbind_write_marker(token)


@pytest.mark.asyncio
async def test_the_cost_ceiling_still_fires_when_no_write_is_running() -> None:
    """The other direction, and the one that matters for the bill. A guard that
    protects everything protects nothing — #947's ceiling exists because four
    runs of 213 burned 20% of a window's spend."""
    _state, token = bind_write_marker()
    try:

        async def _events():
            for _ in range(5):
                yield object()
                await asyncio.sleep(0.01)

        with pytest.raises(orch._SoftDeadlineReachedError):
            await _drain(
                orch._stream_with_stall_watchdog(
                    _events(), {"pending": False}, over_cost=lambda: True
                )
            )
    finally:
        unbind_write_marker(token)


@pytest.mark.asyncio
async def test_the_refusal_ceiling_does_not_cut_an_approved_write() -> None:
    _state, token = bind_write_marker()
    try:

        async def _events():
            for _ in range(4):
                yield object()
                await asyncio.sleep(0.01)

        with executing_write("buildium_record_payment"):
            seen = await _drain(
                orch._stream_with_stall_watchdog(
                    _events(), {"pending": False}, is_looping=lambda: True
                )
            )
        assert seen == 4
    finally:
        unbind_write_marker(token)


def test_the_hard_deadline_is_stretched_not_removed() -> None:
    """⚠ A wedged write must still die. Suppressing the hard clock outright
    would let one hung HTTP call pin a worker forever — the same
    'stretched, not removed' bargain the watchdog already strikes for a tool in
    flight. Asserted on the source because the branch is the contract."""
    import inspect

    src = inspect.getsource(orch._stream_with_stall_watchdog)
    assert "WRITE_GRACE_MULTIPLIER if write_in_flight() else 1.0" in src, (
        "the hard deadline no longer stretches for a write -- either it was "
        "suppressed outright (a hung write pins the worker) or the grace was "
        "dropped (back to cutting approved writes)"
    )
    assert 1.0 < WRITE_GRACE_MULTIPLIER <= 3.0, (
        f"{WRITE_GRACE_MULTIPLIER} is not a bounded grace -- at 1.0 it does "
        "nothing, and a large value is immunity wearing a multiplier's costume"
    )


def test_both_execute_sites_are_wrapped() -> None:
    """`run_guarded` has TWO `await execute()` call sites — the store-backed
    path and the store-unavailable passthrough. Wrapping one and not the other
    is gotcha 25's every-layer rule, and the passthrough is exactly the
    degraded state where an interrupted write is least recoverable."""
    import ast
    import inspect

    from helixis.agents import idempotency

    tree = ast.parse(inspect.getsource(idempotency))
    awaited_bare = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", "") == "execute"
    ]
    assert len(awaited_bare) == 2, f"expected 2 execute() sites, found {len(awaited_bare)}"

    guarded = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.With)
        and any(
            getattr(getattr(item.context_expr, "func", None), "id", "") == "executing_write"
            for item in n.items
        )
    ]
    assert len(guarded) == 2, (
        f"only {len(guarded)} of 2 execute() sites are inside executing_write -- "
        "the unwrapped one still gets cut mid-write"
    )


# ── the ledger keeps WHY, not just WHAT ───────────────────────────────


def test_an_ambiguous_failure_records_the_reason_not_just_the_class() -> None:
    """`abandoned_unknown` is never retried, so whatever is stored here is the
    ONLY durable record of why a write did not land.

    It stored the exception class alone. Both `create_*` tools reaped as a bare
    `ambiguous:BuildiumApiError` on 2026-08-30 and the cause was gone for good
    -- the model's reply was a GUESS ("the most common missing field that trips
    up Buildium's task creation"), not a record. Gotcha 59's lesson: the status
    came back, the diagnosis did not.
    """
    from helixis.agents.idempotency import _ambiguous_detail
    from helixis.buildium.errors import BuildiumApiError

    detail = _ambiguous_detail(
        BuildiumApiError(422, "Title is required", code="Validation", path="/tasks/todorequests")
    )
    assert detail.startswith("ambiguous:BuildiumApiError"), "the class must still lead"
    assert "status=422" in detail
    assert "/tasks/todorequests" in detail
    assert "Title is required" in detail


def test_a_plain_exception_still_reads_as_before() -> None:
    """No regression for an error carrying no detail -- the label alone is
    still what gets stored, so nothing downstream sees a new shape."""
    from helixis.agents.idempotency import _ambiguous_detail

    assert _ambiguous_detail(RuntimeError()) == "ambiguous:RuntimeError"


def test_the_detail_is_bounded() -> None:
    """A breadcrumb, not a log. An unbounded provider body in a column nothing
    truncates is how a diagnostic aid becomes a storage problem."""
    from helixis.agents.idempotency import _ambiguous_detail
    from helixis.buildium.errors import BuildiumApiError

    assert len(_ambiguous_detail(BuildiumApiError(500, "x" * 5000))) <= 500
