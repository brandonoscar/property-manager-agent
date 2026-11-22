"""The anti-absence guard was watching a channel the signal does not travel on.

``claim_guard`` exists because of battery v2's Q9: the agent read a list showing
25 of 179 tasks, failed to find one in it, and told the user the task did not
exist "anywhere in the portfolio" — while "URGENT: water heater leaking into
the closet" sat in Buildium at priority Low (gotcha 26). The guard's input is
``saw_truncated_list``, set in ``run_agent_streaming`` by matching
``TRUNCATION_MARKER`` against each ``tool_output`` event on the parent stream.

**Every truncating list tool lives on a SPECIALIST, and a specialist consult is
a nested run.** Out on the parent stream the whole consult is ONE
``tool_output`` carrying the specialist's model-written prose, which has
paraphrased the footer away. Measured on the live mirror (Blue Ridge only,
gotcha 47): of **597** stored ``buildium_agent`` results, the literal marker
survives in **exactly one**. So for essentially every run that could have
needed it, the guard read clean and said nothing.

The fix routes the signal through ``ConsultLedger``, which is the one thing
that crosses: a nested run is a Task and a Task COPIES the context, so a
``ContextVar.set`` inside is invisible outside (gotcha 42) — but the copy holds
the same ledger OBJECT, so mutating it is visible. Same mechanism as the
by-reference ``_partial`` list beside it.

⚠ Which is why these tests run their OWN ``asyncio.run`` and do the mutation
inside a real Task. A test that called ``specialist_stream_capture`` inline
would pass against a ContextVar-based implementation that cannot work in
production — a test that cannot observe the failure is not covering it
(gotcha 36).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from helixis.agents.claim_guard import TRUNCATION_MARKER, saw_truncated_read
from helixis.agents.specialist_limits import (
    bind_consult_ledger,
    captured_partial,
    current_consult_ledger,
    open_partial_capture,
    reset_consult_ledger,
    specialist_stream_capture,
)


def _tool_output_event(output: str) -> dict:
    """The shape ``run_agent_streaming`` itself reads: a run-item event named
    ``tool_output`` whose ``item.output`` is the tool's return."""
    return {
        "event": SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(output=output),
        )
    }


def _delta_event(delta: str) -> dict:
    return {
        "event": SimpleNamespace(
            type="raw_response_event",
            data=SimpleNamespace(type="response.output_text.delta", delta=delta),
        )
    }


# A real truncation footer, quoted from ``buildium_list_tasks``.
TRUNCATED = (
    "- Fix the porch light [New] (id: 41102)\n\n"
    "… and 154 more not shown. This list is TRUNCATED — do NOT conclude a task "
    "is absent because it isn't here. Re-call with search='<keyword>' to search "
    "all 179 tasks."
)
COMPLETE = "- Fix the porch light [New] (id: 41102)\n- Replace the filter [New] (id: 41103)"


# ── the crossing ─────────────────────────────────────────────────────────


def test_a_truncated_read_inside_a_nested_task_reaches_the_outer_ledger() -> None:
    """The whole point. The consult runs in a Task; the flag has to survive it."""

    async def main() -> bool:
        token = bind_consult_ledger()
        try:

            async def nested() -> None:
                # Inside a Task: the context is a COPY, so anything set here
                # via ContextVar would be lost. Mutating the ledger is not.
                specialist_stream_capture(_tool_output_event(TRUNCATED))

            await asyncio.create_task(nested())
            ledger = current_consult_ledger()
            return bool(ledger and ledger.truncated_read)
        finally:
            reset_consult_ledger(token)

    assert asyncio.run(main()), (
        "a truncated read inside a specialist consult did not reach the "
        "orchestrator — claim_guard will report clean on exactly the runs it "
        "exists for"
    )


def test_a_complete_read_does_not_set_it() -> None:
    """The other direction (gotcha 38). A guard that always fires is not a
    guard — it would caveat every honest 'there are none' answer in the
    product, and a caveat nobody can trust is worse than none."""

    async def main() -> bool:
        token = bind_consult_ledger()
        try:
            specialist_stream_capture(_tool_output_event(COMPLETE))
            ledger = current_consult_ledger()
            return bool(ledger and ledger.truncated_read)
        finally:
            reset_consult_ledger(token)

    assert asyncio.run(main()) is False


def test_it_latches_across_several_consults() -> None:
    """One truncated read anywhere in a run makes an absence claim unsafe for
    the whole answer, so a later complete read must not clear it."""

    async def main() -> bool:
        token = bind_consult_ledger()
        try:
            specialist_stream_capture(_tool_output_event(TRUNCATED))
            specialist_stream_capture(_tool_output_event(COMPLETE))
            ledger = current_consult_ledger()
            return bool(ledger and ledger.truncated_read)
        finally:
            reset_consult_ledger(token)

    assert asyncio.run(main()) is True


# ── the arm that already existed must still work ─────────────────────────


def test_the_text_delta_capture_is_unbroken() -> None:
    """``specialist_stream_capture`` gained a second arm; the first one is what
    keeps seven good turns of an exhausted consult from being discarded
    (gotcha 42). A regression here is silent — the consult just returns less."""
    token = open_partial_capture("drive_agent")
    try:
        specialist_stream_capture(_delta_event("found the "))
        specialist_stream_capture(_delta_event("SOP"))
        assert captured_partial() == "found the SOP"
    finally:
        from helixis.agents.specialist_limits import close_partial_capture

        close_partial_capture(token)


def test_a_tool_output_event_does_not_pollute_the_partial_buffer() -> None:
    """The two arms must not read each other's events. A tool result appended
    to the partial buffer would be handed back as 'what the specialist said'."""
    token = open_partial_capture("drive_agent")
    try:
        specialist_stream_capture(_tool_output_event(TRUNCATED))
        assert captured_partial() == ""
    finally:
        from helixis.agents.specialist_limits import close_partial_capture

        close_partial_capture(token)


# ── both sides of the marker ─────────────────────────────────────────────


def test_the_producer_and_the_consumer_agree_on_the_marker() -> None:
    """Gotcha 28's rule. The footer is written in ``buildium_tools`` and matched
    in ``claim_guard``; testing either alone lets them drift into a guard that
    can never match — which, given the finding above, has a precedent in this
    exact code path."""
    import inspect

    from helixis.agents import buildium_tools

    src = inspect.getsource(buildium_tools)
    assert TRUNCATION_MARKER in src, (
        f"claim_guard matches {TRUNCATION_MARKER!r} and no truncating tool "
        "emits it any more — the guard is now unreachable from either channel"
    )
    assert saw_truncated_read(TRUNCATED)
    assert not saw_truncated_read(COMPLETE)


def test_a_missing_or_odd_event_shape_costs_the_signal_and_not_the_turn() -> None:
    """Defensive, per the function's own contract: an SDK shape change must not
    raise into a live consult."""

    async def main() -> None:
        token = bind_consult_ledger()
        try:
            for junk in (None, {}, {"event": None}, {"event": SimpleNamespace(type="other")}, "x"):
                specialist_stream_capture(junk)
        finally:
            reset_consult_ledger(token)

    asyncio.run(main())


def test_the_orchestrator_actually_reads_the_ledger_it_is_handed() -> None:
    """⚠ Everything above tests the LEDGER. All of it passes with
    ``run_agent_streaming`` never consulting it — which is the shipped state
    this change exists to fix, so the suite would have been green against the
    bug. Mutation caught exactly that: reverting the call site to
    ``truncated_anywhere = saw_truncated_list`` left every other test passing.

    Third time the same shape has bitten in two days (the owner join, the
    vendor fetch, now this): a helper is well-tested and the caller drops it.

    AST rather than a substring — ``ruff format`` reflows this line (gotcha 40).
    """
    import ast
    import inspect

    from helixis.agents import orchestrator

    tree = ast.parse(inspect.getsource(orchestrator.run_agent_streaming))
    # The signal must reach ``needs_scope_caveat``'s ``truncated_reads``, and it
    # must not be the bare parent-stream flag — that is the channel measured at
    # 1-in-597.
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "needs_scope_caveat"
    ]
    assert calls, "run_agent_streaming no longer applies the scope caveat at all"
    for call in calls:
        arg = next((k.value for k in call.keywords if k.arg == "truncated_reads"), None)
        assert arg is not None, "needs_scope_caveat called without truncated_reads"
        assert not (isinstance(arg, ast.Name) and arg.id == "saw_truncated_list"), (
            "the caveat is fed ONLY by the parent stream again — the marker "
            "survives 1 of 597 specialist consults on that channel, so this "
            "reports clean for essentially every run that needs it"
        )
    # And the ledger has to be consulted somewhere in the function.
    reads_ledger = any(
        isinstance(n, ast.Attribute) and n.attr == "truncated_read" for n in ast.walk(tree)
    )
    assert reads_ledger, "the consult ledger's truncated_read is never read"
