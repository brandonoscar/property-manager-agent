"""The per-run call budget — the structural fix for the fan-out loop.

PR #850 tried to solve this with tool wording and lost: the live battery
showed 78 per-unit calls become 40 of those plus 72 individual work-order
fetches, and the turn still died on the wall clock. So these tests pin the
properties prose cannot give us — a hard cap, a memo, and (the part that
would be a real bug if it slipped) writes passing through untouched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from helixis.agents.call_budget import (
    _REPEAT_NOTE,
    CONNECTOR_READ_CAP,
    PER_RECORD_CAPS,
    RUN_RECORD_BUDGET,
    CallLedger,
    bind_call_ledger,
    budget_tool,
    budget_tools,
    reset_call_ledger,
)


def _tool(name: str, *, calls: list[str] | None = None, reply: str = "ok", delay: float = 0.0):
    """A minimal stand-in for the SDK's FunctionTool: a name and an invoker.

    ``delay`` yields to the event loop mid-call, which is what a real Buildium
    round-trip does — and what let ten concurrent calls slip past the cap.
    """
    seen = calls if calls is not None else []

    async def _invoke(_ctx, args_json: str) -> str:
        seen.append(args_json)
        if delay:
            await asyncio.sleep(delay)
        return f"{reply}:{len(seen)}"

    return SimpleNamespace(name=name, on_invoke_tool=_invoke, _calls=seen)


@pytest.fixture
def ledger():
    token = bind_call_ledger()
    try:
        yield
    finally:
        reset_call_ledger(token)


# ─── the memo ────────────────────────────────────────────────────────────


async def test_an_identical_read_is_answered_from_the_memo(ledger) -> None:
    """delinquency_report ×3 and list_open_work_orders ×8 in one turn were
    real, and every one of them was a fresh Buildium fetch."""
    tool = budget_tool(_tool("buildium_list_open_work_orders"))
    first = await tool.on_invoke_tool(None, "{}")
    second = await tool.on_invoke_tool(None, "{}")

    assert tool._calls == ["{}"]  # Buildium hit exactly once
    assert second.startswith(first)
    assert _REPEAT_NOTE in second


async def test_argument_order_does_not_defeat_the_memo(ledger) -> None:
    tool = budget_tool(_tool("buildium_list_gl_transactions"))
    await tool.on_invoke_tool(None, '{"start_date": "1", "end_date": "2"}')
    await tool.on_invoke_tool(None, '{"end_date": "2", "start_date": "1"}')
    assert len(tool._calls) == 1


async def test_different_arguments_are_different_calls(ledger) -> None:
    tool = budget_tool(_tool("buildium_get_work_order"))
    await tool.on_invoke_tool(None, '{"work_order_id": 1}')
    await tool.on_invoke_tool(None, '{"work_order_id": 2}')
    assert len(tool._calls) == 2


async def test_unparseable_arguments_never_produce_a_false_memo_hit(ledger) -> None:
    """A memo MISS is always safe; a false hit answers with someone else's
    data. Junk that can't be normalized must miss."""
    tool = budget_tool(_tool("buildium_list_tenants"))
    await tool.on_invoke_tool(None, "not json")
    await tool.on_invoke_tool(None, "also not json")
    assert len(tool._calls) == 2


# ─── the cap ─────────────────────────────────────────────────────────────


async def test_a_per_record_read_refuses_past_its_cap(ledger) -> None:
    cap = PER_RECORD_CAPS["buildium_get_work_order"].limit
    tool = budget_tool(_tool("buildium_get_work_order"))
    for i in range(cap):
        await tool.on_invoke_tool(None, f'{{"work_order_id": {i}}}')
    assert len(tool._calls) == cap

    refusal = await tool.on_invoke_tool(None, '{"work_order_id": 999}')
    assert len(tool._calls) == cap  # the call never reached Buildium
    assert "cap" in refusal
    # The refusal is only useful if it names where to go instead.
    assert "buildium_open_maintenance_by_unit" in refusal


async def test_a_capped_tool_still_serves_repeats_of_what_it_already_fetched(
    ledger,
) -> None:
    """The cap is on DISTINCT records. Re-asking for one already fetched is a
    memo hit, not a refusal — otherwise the guard would hide data the agent
    legitimately holds."""
    cap = PER_RECORD_CAPS["buildium_get_lease"].limit
    tool = budget_tool(_tool("buildium_get_lease"))
    for i in range(cap):
        await tool.on_invoke_tool(None, f'{{"lease_id": {i}}}')

    again = await tool.on_invoke_tool(None, '{"lease_id": 0}')
    assert "cap" not in again
    assert _REPEAT_NOTE in again


async def test_the_run_budget_bounds_fan_out_across_tools(ledger) -> None:
    """Nine tools capped at eight is a 72-call budget — exactly the fan-out
    that killed the battery runs. The run-wide ceiling is the real bound."""
    tools = [
        budget_tool(_tool("buildium_get_work_order")),
        budget_tool(_tool("buildium_get_lease")),
        budget_tool(_tool("buildium_get_tenant")),
    ]
    made = 0
    refused = 0
    for i in range(30):
        reply = await tools[i % 3].on_invoke_tool(None, f'{{"id": {i}}}')
        # Two refusal shapes now: the cap message, and the escalated "you are
        # looping" one past RUN_REFUSAL_CEILING (battery v4 Q11 issued 52
        # refused calls without taking the hint). Both are refusals; classify
        # on the marker they share rather than on either one's wording.
        if "STOP" in reply or "cap" in reply:
            refused += 1
        else:
            made += 1
    assert made == RUN_RECORD_BUDGET
    assert refused == 30 - RUN_RECORD_BUDGET


async def test_bulk_reads_are_memoized_but_never_capped(ledger) -> None:
    """A portfolio-wide tool is the ANSWER to fan-out. Capping it would push
    the agent back toward record-at-a-time reads."""
    tool = budget_tool(_tool("buildium_list_unpaid_bills"))
    for i in range(40):
        reply = await tool.on_invoke_tool(None, f'{{"property_name": "p{i}"}}')
        assert "cap" not in reply
    assert len(tool._calls) == 40


# ─── concurrency (the v2 battery hole) ───────────────────────────────────


async def test_the_cap_holds_when_a_turn_fires_its_calls_in_parallel(ledger) -> None:
    """The bug battery v2 found. The SDK issues a turn's tool calls
    CONCURRENTLY; the cap recorded its key only AFTER the await, so ten
    parallel calls all read an empty ledger, all passed the gate, and all
    executed. Q7 made ten calls each to four per-record tools under a run
    budget of 14."""
    cap = PER_RECORD_CAPS["buildium_get_lease"].limit
    tool = budget_tool(_tool("buildium_get_lease", delay=0.01))

    replies = await asyncio.gather(
        *(tool.on_invoke_tool(None, f'{{"lease_id": {i}}}') for i in range(20))
    )
    assert len(tool._calls) == cap
    # Past RUN_REFUSAL_CEILING the refusal escalates to the looping message,
    # so count refusals of EITHER shape — the property under test is that
    # exactly ``cap`` calls executed, not which sentence came back.
    assert sum(("which is the cap" in r) or ("STOP" in r) for r in replies) == 20 - cap


async def test_the_run_budget_holds_across_tools_fired_in_parallel(ledger) -> None:
    """Q7's real shape: four per-record tools, ten leases, one turn."""
    tools = [
        budget_tool(_tool(name, delay=0.01))
        for name in (
            "buildium_get_lease",
            "buildium_list_recent_lease_transactions",
            "buildium_list_work_orders_for_unit",
        )
    ]
    await asyncio.gather(
        *(tool.on_invoke_tool(None, f'{{"id": {i}}}') for tool in tools for i in range(10))
    )
    assert sum(len(t._calls) for t in tools) == RUN_RECORD_BUDGET


async def test_concurrent_identical_calls_share_one_round_trip(ledger) -> None:
    """Duplicates in the same turn land together, so neither can see the
    other's memo entry — they have to await the in-flight call instead."""
    tool = budget_tool(_tool("buildium_list_open_work_orders", delay=0.01))
    replies = await asyncio.gather(*(tool.on_invoke_tool(None, "{}") for _ in range(8)))
    assert len(tool._calls) == 1
    assert sum(_REPEAT_NOTE in r for r in replies) == 7
    assert all(r.startswith("ok:1") for r in replies)


async def test_a_failed_read_does_not_consume_a_slot(ledger) -> None:
    """A transient Buildium error must not spend budget the agent could use
    on a call that works."""
    boom = True

    async def _invoke(_ctx, _args):
        nonlocal boom
        if boom:
            boom = False
            raise RuntimeError("buildium 502")
        return "ok"

    tool = budget_tool(SimpleNamespace(name="buildium_get_lease", on_invoke_tool=_invoke))
    with pytest.raises(RuntimeError):
        await tool.on_invoke_tool(None, '{"lease_id": 1}')

    from helixis.agents.call_budget import current_call_ledger

    led = current_call_ledger()
    assert led is not None
    assert led.record_calls == 0
    assert await tool.on_invoke_tool(None, '{"lease_id": 1}') == "ok"


async def test_the_run_summary_reports_what_the_budget_actually_did(ledger) -> None:
    """The measurement channel. Refusals and repeats happen inside the
    specialist and never reach the SSE stream, so one summary event per run is
    the only way to judge the guard without inferring it from call counts."""
    from helixis.agents.call_budget import current_call_ledger

    tool = budget_tool(_tool("buildium_get_work_order"))
    cap = PER_RECORD_CAPS["buildium_get_work_order"].limit
    for i in range(cap + 2):
        await tool.on_invoke_tool(None, f'{{"work_order_id": {i}}}')
    await tool.on_invoke_tool(None, '{"work_order_id": 0}')

    led = current_call_ledger()
    assert led is not None
    summary = led.summary()
    assert summary["record_calls"] == cap
    assert summary["run_record_budget"] == RUN_RECORD_BUDGET
    assert summary["refusals"] == 2
    assert summary["memo_hits"] == 1
    assert summary["distinct_by_tool"]["buildium_get_work_order"] == cap


def test_the_refusal_code_is_in_the_reason_vocabulary() -> None:
    """An unlisted slug still emits but logs a warning on every firing. The
    cap fires often enough that this matters."""
    from helixis.observability.reason_codes import REASON_CODES

    assert "call_budget_refused" in REASON_CODES


async def test_memo_hits_and_refusals_are_counted_for_the_trace(ledger) -> None:
    """Battery v2 couldn't tell whether the cap fired — the strings never
    reached the client because specialist tool results don't surface in the
    SSE stream. The ledger counts them so the run can be measured, not
    inferred."""
    from helixis.agents.call_budget import current_call_ledger

    tool = budget_tool(_tool("buildium_get_work_order"))
    for i in range(PER_RECORD_CAPS["buildium_get_work_order"].limit + 3):
        await tool.on_invoke_tool(None, f'{{"work_order_id": {i}}}')
    await tool.on_invoke_tool(None, '{"work_order_id": 0}')

    led = current_call_ledger()
    assert led is not None
    assert led.refusals["buildium_get_work_order"] == 3
    assert led.memo_hits["buildium_get_work_order"] == 1


# ─── writes ──────────────────────────────────────────────────────────────


async def test_a_write_is_never_capped_and_never_memoized(ledger) -> None:
    """Repeat-suppression on a write belongs to its idempotency key and
    confirm gate. A budget that could silence one is a correctness hole."""
    tool = budget_tool(_tool("buildium_record_payment"))
    for _ in range(40):
        reply = await tool.on_invoke_tool(None, '{"lease_id": 1, "amount": 100}')
        assert "cap" not in reply
        assert _REPEAT_NOTE not in reply
    assert len(tool._calls) == 40


async def test_a_write_invalidates_the_memo(ledger) -> None:
    """Close a work order, re-read it: the agent must see the NEW state, not
    the pre-write snapshot."""
    read = budget_tool(_tool("buildium_get_work_order"))
    write = budget_tool(_tool("buildium_close_work_order"))

    before = await read.on_invoke_tool(None, '{"work_order_id": 9006}')
    await write.on_invoke_tool(None, '{"work_order_id": 9006}')
    after = await read.on_invoke_tool(None, '{"work_order_id": 9006}')

    assert _REPEAT_NOTE not in after
    assert after != before
    assert len(read._calls) == 2


def test_no_registered_write_tool_can_be_budgeted() -> None:
    """The read allowlist is the safety property. Pin it against the LIVE
    registry so a future write named against the convention fails here rather
    than silently getting capped in production."""
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.call_budget import _is_budgeted
    from helixis.agents.mirror_tools import mirror_tools
    from helixis.agents.sms_tools import sms_tools

    write_verbs = (
        "remember",
        "create",
        "update",
        "close",
        "post",
        "record",
        "add",
        "upload",
        "share",
        "renew",
        "send",
        "delete",
    )
    for tool in (*buildium_tools(), *sms_tools(), *mirror_tools):
        if any(verb in tool.name for verb in write_verbs):
            assert not _is_budgeted(tool.name), tool.name


def test_the_mirror_read_list_matches_the_live_mirror_registry() -> None:
    """Named one by one because ``remember_contact_alias`` shares the registry
    and writes. A rename would silently un-budget a tool, so pin both ways."""
    from helixis.agents.call_budget import _MIRROR_READS, _is_budgeted
    from helixis.agents.mirror_tools import mirror_tools
    from helixis.agents.places_tools import places_tools

    registered = {t.name for t in (*mirror_tools, *places_tools)}
    assert _MIRROR_READS <= registered, sorted(_MIRROR_READS - registered)
    assert registered - _MIRROR_READS == {"remember_contact_alias"}
    assert not _is_budgeted("remember_contact_alias")


def test_every_cap_names_a_real_bulk_tool_to_use_instead() -> None:
    """A refusal that doesn't say where to go is a dead end for the model."""
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.mirror_tools import mirror_tools

    # Capped tools live in BOTH registries — ``property_snapshot`` is a mirror
    # read, and it was the one uncapped per-record tool battery v4's Q7 died
    # on (10 calls, one property each, nothing bounding it).
    registered = {t.name for t in (*buildium_tools(), *mirror_tools)}
    for name, cap in PER_RECORD_CAPS.items():
        assert name in registered, f"{name} is capped but not registered"
        suggested = [w.strip("`.,") for w in cap.steer.split() if w.startswith("`buildium_")]
        assert suggested, name
        for alternative in suggested:
            assert alternative in registered, f"{name} steers to unknown {alternative}"


# ─── wiring ──────────────────────────────────────────────────────────────


def test_wrapping_preserves_the_tool_contract_and_the_singletons() -> None:
    """Names, descriptions and schemas are what the golden cases and the
    snapshot guard pin — the wrap must be invisible to them, and must not
    mutate the module-level tools."""
    from helixis.agents.buildium_tools import buildium_tools

    original = buildium_tools()
    wrapped = budget_tools(original)

    assert [t.name for t in wrapped] == [t.name for t in original]
    assert [t.description for t in wrapped] == [t.description for t in original]
    assert all(w is not o for w, o in zip(wrapped, original, strict=True))
    assert all(
        w.on_invoke_tool is not o.on_invoke_tool for w, o in zip(wrapped, original, strict=True)
    )
    # The singletons keep their real implementations.
    assert [t.on_invoke_tool for t in buildium_tools()] == [t.on_invoke_tool for t in original]


def test_the_buildium_agent_gets_budgeted_tools() -> None:
    """The guard is worthless if it isn't wired to the agent that fans out."""
    from helixis.agents.buildium_agent import create_buildium_agent
    from helixis.agents.buildium_tools import buildium_get_work_order
    from helixis.settings import Settings

    agent = create_buildium_agent(Settings())
    live = {t.name: t for t in agent.tools if hasattr(t, "on_invoke_tool")}
    assert "buildium_get_work_order" in live
    assert live["buildium_get_work_order"].on_invoke_tool is not (
        buildium_get_work_order.on_invoke_tool
    )


async def test_with_no_ledger_bound_the_guard_is_inert() -> None:
    """Scripts, direct tool tests and the eval fixtures run without a bound
    ledger; they must behave exactly as before."""
    tool = budget_tool(_tool("buildium_get_work_order"))
    for i in range(30):
        reply = await tool.on_invoke_tool(None, f'{{"work_order_id": {i}}}')
        assert "cap" not in reply
    assert len(tool._calls) == 30


def test_the_ledger_counts_only_capped_tools_toward_the_run_budget() -> None:
    led = CallLedger()
    led.distinct["buildium_get_work_order"] = {"a", "b"}
    led.distinct["buildium_list_tenants"] = {"a", "b", "c", "d"}
    assert led.record_calls == 2


# ── the connector standard ──────────────────────────────────────────────
#
# Every one of the 15 entries in PER_RECORD_CAPS is a Buildium tool. Gmail,
# Drive, Calendar, Docs and Sheets had a memo and nothing else — and a memo
# only collapses IDENTICAL arguments, so a model that varies its search terms
# sails straight through. A live turn asking "are there any leases not being
# fully enforced" issued ~40 GMAIL_FETCH_EMAILS calls: none refused, and none
# counted toward RUN_RECORD_BUDGET, because that sum tested membership of the
# Buildium registry.
#
# ⚠ These pin the RULE, not a list. Composio tools are fetched per user at
# runtime — drive alone carries ~186 schemas — so an enumerated cap registry
# would be stale the day a toolkit ships a new verb. A connector added next
# month has to be bounded on arrival with nobody remembering to do it.


def test_a_connector_read_is_capped_without_anyone_registering_it() -> None:
    from helixis.agents.call_budget import CONNECTOR_READ_CAP, cap_for

    # Deliberately a toolkit this repo has never heard of.
    cap = cap_for("FRESHBOOKS_LIST_INVOICES")
    assert cap is not None, "a new connector shipped uncapped — the 40-call bug"
    assert cap.limit == CONNECTOR_READ_CAP
    assert cap.steer, "a refusal with no steer just makes the model guess again"


def test_one_connector_gets_one_budget_however_many_verbs_it_ships() -> None:
    # ⚠ The live run used BOTH GMAIL_FETCH_EMAILS and
    # GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID. Capping per TOOL would have given it
    # six each; a toolkit must not buy headroom by adding a verb.
    from helixis.agents.call_budget import cap_group

    assert cap_group("GMAIL_FETCH_EMAILS") == "GMAIL"
    assert cap_group("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID") == "GMAIL"
    # Ours are unchanged: each Buildium tool still budgets alone.
    assert cap_group("buildium_get_property") == "buildium_get_property"


def test_a_connector_write_is_never_capped() -> None:
    # The safety property the whole module is built around (gotcha 25): a cap
    # that could silence a write is a correctness hole, not a cost saving.
    from helixis.agents.call_budget import cap_for

    assert cap_for("GMAIL_SEND_EMAIL") is None
    assert cap_for("GOOGLECALENDAR_CREATE_EVENT") is None
    assert cap_for("buildium_create_work_order") is None


def test_connector_reads_count_against_the_run_wide_ceiling() -> None:
    # ⚠ The quieter half of the bug. `record_calls` summed names present in
    # PER_RECORD_CAPS, so connector reads were invisible to the run budget
    # even while they were the thing consuming the turn.
    from helixis.agents.call_budget import CallLedger

    ledger = CallLedger()
    ledger.distinct["GMAIL"] = {f"GMAIL_FETCH_EMAILS:q{i}" for i in range(5)}
    assert ledger.record_calls == 5


@pytest.mark.asyncio
async def test_the_forty_gmail_searches_that_prompted_this(ledger) -> None:
    """The live incident, replayed.

    "are there any leases not being fully enforced" — a question Buildium and
    the mirror rollups answer on their own — issued ~40 ``GMAIL_FETCH_EMAILS``
    calls with varying search terms. The memo could not help: it collapses
    IDENTICAL arguments, and every search was different. Nothing else was
    watching.
    """
    calls: list[str] = []
    tool = budget_tool(_tool("GMAIL_FETCH_EMAILS", calls=calls))
    for i in range(40):
        await tool.on_invoke_tool(SimpleNamespace(), f'{{"query": "lease violation {i}"}}')

    assert len(calls) == CONNECTOR_READ_CAP, (
        f"{len(calls)} Gmail searches executed against a cap of {CONNECTOR_READ_CAP}"
    )


@pytest.mark.asyncio
async def test_a_toolkits_verbs_share_one_budget(ledger) -> None:
    """⚠ The run used two Gmail tools. Per-TOOL caps would have given it six
    each — a toolkit must not raise its own ceiling by shipping a verb."""
    calls: list[str] = []
    fetch = budget_tool(_tool("GMAIL_FETCH_EMAILS", calls=calls))
    by_id = budget_tool(_tool("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", calls=calls))
    for i in range(10):
        await fetch.on_invoke_tool(SimpleNamespace(), f'{{"q": "{i}"}}')
        await by_id.on_invoke_tool(SimpleNamespace(), f'{{"id": "{i}"}}')

    assert len(calls) == CONNECTOR_READ_CAP


@pytest.mark.asyncio
async def test_a_connector_write_still_goes_through_every_time(ledger) -> None:
    """The safety property, in the direction that matters: capping a send
    would silence real mail. Ten sends, ten sends."""
    calls: list[str] = []
    send = budget_tool(_tool("GMAIL_SEND_EMAIL", calls=calls))
    for i in range(10):
        await send.on_invoke_tool(SimpleNamespace(), f'{{"to": "a{i}@example.com"}}')

    assert len(calls) == 10
