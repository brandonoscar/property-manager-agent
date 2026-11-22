"""A Rentvine workspace's chat never reaches for a Buildium tool.

Found 2026-09-25 by ``scripts/rentvine_ai_e2e.py``, the first run of the real
agent against a Rentvine workspace: 7 of 10 questions were handed to the PMS
specialist, which called live Buildium tools, each of which answered "Buildium
is not connected ... connect Buildium". The customer was told to connect a
system they do not use, and "list the open work orders" had no working answer,
because no mirror tool listed work orders.

Pinned here, both directions (gotchas 38/51):

* on a Rentvine run the live Buildium tools are HIDDEN, read through the SDK's
  own ``get_all_tools`` rather than our flag;
* on a Buildium run, and on a run whose PMS did not resolve, nothing is hidden;
* the module-level tool singletons are untouched (the 32x negative cases pin
  that registry by name);
* the run binds and resets the PMS, and adds the Rentvine note only on
  Rentvine (AST, gotcha 40);
* ``open_work_orders`` lists what Rentvine's sync writes, fenced, with the
  vendor, and says "unknown" where the row cannot say (rule 14).
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents import RunContextWrapper

from helixis.agents import mirror_tools, run_pms
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.buildium_tools import buildium_tools
from helixis.settings import Settings

BUILDIUM_NAMES = {t.name for t in buildium_tools()}


async def _visible(pms: str | None) -> set[str]:
    agent = create_buildium_agent(Settings())
    token = run_pms.bind_run_pms(pms)
    try:
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
    finally:
        run_pms.reset_run_pms(token)
    return {t.name for t in tools}


# ── the specialist's tool surface ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rentvine_run_cannot_see_any_live_buildium_tool() -> None:
    visible = await _visible("rentvine")
    assert visible & BUILDIUM_NAMES == set()
    # ...and keeps what works for Rentvine.
    assert {"open_work_orders", "contact_lookup", "property_snapshot"} <= visible
    assert "rentvine_update_work_order_status" in visible


@pytest.mark.asyncio
@pytest.mark.parametrize("pms", ["buildium", None])
async def test_a_buildium_or_unresolved_run_sees_every_buildium_tool(pms: str | None) -> None:
    """None is a failed lookup. It must hide nothing: hiding a Buildium
    customer's tools on a database blip would take their product away."""
    assert BUILDIUM_NAMES <= await _visible(pms)


def test_the_module_level_tools_are_not_changed() -> None:
    create_buildium_agent(Settings())
    assert all(t.is_enabled is True for t in buildium_tools())


def test_the_hidden_copies_are_copies() -> None:
    originals = buildium_tools()[:2]
    hidden = run_pms.hide_on_rentvine(originals)
    assert [h.name for h in hidden] == [o.name for o in originals]
    assert all(h is not o for h, o in zip(hidden, originals, strict=True))


# ── resolving the PMS ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_never_raises_and_says_none_without_a_pool() -> None:
    with patch("helixis.db.engine.get_pool", side_effect=RuntimeError("no pool")):
        assert await run_pms.resolve_run_pms("co-1") is None
    assert await run_pms.resolve_run_pms("") is None


@pytest.mark.asyncio
async def test_resolve_reads_the_one_home_for_which_pms() -> None:
    with (
        patch("helixis.db.engine.get_pool", return_value=object()),
        patch("helixis.db.company_pms.company_pms", AsyncMock(return_value="rentvine")),
    ):
        assert await run_pms.resolve_run_pms("co-1") == "rentvine"


def _run_agent_streaming_tree() -> ast.AST:
    from helixis.agents import orchestrator

    return ast.parse(inspect.getsource(orchestrator.run_agent_streaming))


def _called(node: ast.AST) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_the_run_binds_the_pms_and_resets_it_in_finally() -> None:
    tree = _run_agent_streaming_tree()
    assert {"bind_run_pms", "resolve_run_pms"} <= _called(tree)
    finals = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.finalbody]
    assert any(
        "reset_run_pms" in _called(ast.Module(body=t.finalbody, type_ignores=[])) for t in finals
    )


def test_the_rentvine_note_is_added_only_on_a_rentvine_run() -> None:
    tree = _run_agent_streaming_tree()
    guarded = [
        n for n in ast.walk(tree) if isinstance(n, ast.If) and "run_on_rentvine" in _called(n.test)
    ]
    assert guarded, "the note is no longer behind run_on_rentvine()"
    names_in_body = {
        n.id for g in guarded for s in g.body for n in ast.walk(s) if isinstance(n, ast.Name)
    }
    assert {"context_block", "RENTVINE_RUN_NOTE"} <= names_in_body
    # Nowhere else: an unconditional note would tell Buildium customers they
    # are on Rentvine.
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "RENTVINE_RUN_NOTE"]
    assert len(uses) == 1


def test_the_note_names_the_system_and_forbids_the_wrong_instruction() -> None:
    # Re-pointed 2026-10-01: the note used to forbid "reconnect Buildium" by
    # naming it, and the model echoed the name back ("Rentvine, not Buildium").
    note = run_pms.RENTVINE_RUN_NOTE
    assert "Rentvine" in note and "buildium" not in note.lower()
    lowered = note.lower()
    # Re-pointed 2026-10-05: "reconnect" left the forbidden list. Rentvine has
    # had a card since #1075, and a rejected key's answer says to reconnect;
    # test_run_note_tool_advice holds the note and that answer to one rule.
    for dead_end in ("restart", "re-route", "switch assistants"):
        assert dead_end in lowered, dead_end
    # Re-pointed 2026-10-01 (lease export): both sides are read. What coverage
    # says Rentvine FILLS, the note must not call unavailable — and the reverse.
    from helixis.db.pms_coverage import Coverage

    rentvine = Coverage(run_pms.RENTVINE)
    missing = note.split("Not available in Occupella yet:", 1)[1].split(".", 1)[0]
    assert rentvine.sees("helixis_lease_tenants")
    assert "tenant" not in missing and "rent amount" not in note
    assert "which current tenant is on which lease" in note
    # Re-pointed 2026-10-01 (balances): balances sync from the lease export,
    # payments (lease transactions) do not.
    assert rentvine.sees("helixis_lease_balances") and "balances" not in missing
    assert "rent balances" in note
    # Re-pointed 2026-10-02: payments sync too (getLease includes=transactions);
    # what is still missing is named, each read against coverage.
    assert rentvine.sees("helixis_lease_transactions") and "payment" not in missing
    assert "rent payments" in note
    for table, word in (
        ("helixis_tasks", "tasks"),
        ("helixis_bank_accounts", "how much is in each bank account"),
    ):
        assert not rentvine.sees(table) and word in missing, table
    # Re-pointed 2026-10-02: turnovers open from the lease export.
    assert rentvine.sees("helixis_turnovers") and "turnover" not in missing
    assert "turnovers (the make-ready checklist" in note
    # Re-pointed 2026-10-01 (bills): bills sync, their LINES do not, so the
    # note names vendor bills as available and spend by category/property as
    # missing, read against coverage on both sides.
    assert rentvine.sees("helixis_bills") and "bills" not in missing
    assert "vendor bills" in note
    # Re-pointed 2026-10-02: the lines sync (migration 0104's ledger columns).
    assert rentvine.sees("helixis_bill_lines") and "spending" not in missing
    assert "Spending by category and by property is available" in note
    # Re-pointed 2026-10-01: owner-property links now sync, so the note names
    # them as available, and a property with no owner shown must read as "not
    # known", never "unowned" (rule 14).
    assert "which owner owns which property" not in note
    assert "which properties each owns" in note
    assert "never say the property has no owner" in note


# ── nothing a Rentvine run reads or shows names the other PMS ──────────────
#
# Sandbox run 2026-10-01: told "You are the Buildium Agent", the specialist
# answered a Rentvine owner question with "I'm the Buildium specialist ...
# restart your question so it routes to the Rentvine-aware assistant".
# Both sides are read: what the agents are TOLD (every prompt, on both chat
# paths) and what the user is SHOWN (the handoff label).


def _prose_names_buildium(text: str) -> bool:
    """Prose only: ``buildium_agent`` / ``buildium_list_*`` are registered
    tool and agent names the user never sees, and must stay as registered."""
    import re

    return re.search(r"Buildium", text) is not None


def _with_pms(pms: str | None, build: Any) -> Any:
    token = run_pms.bind_run_pms(pms)
    try:
        return build()
    finally:
        run_pms.reset_run_pms(token)


@pytest.mark.parametrize("loop_mode", [False, True])
def test_a_rentvine_run_tells_no_agent_it_is_buildium(loop_mode: bool) -> None:
    from helixis.agents.orchestrator import create_orchestrator

    orch = _with_pms("rentvine", lambda: create_orchestrator(Settings(), loop_mode=loop_mode))
    agents = [orch, *(h for h in orch.handoffs if hasattr(h, "instructions"))]
    if not loop_mode:
        assert any(a.name == "buildium_agent" for a in agents)  # the specialist is checked
    for agent in agents:
        assert not _prose_names_buildium(agent.instructions), agent.name
    specialist = next((a for a in agents if a.name == "buildium_agent"), None)
    if specialist is not None:
        assert "You are the Rentvine Agent" in specialist.instructions


def test_a_buildium_run_is_told_exactly_what_it_was() -> None:
    from helixis.agents.orchestrator import create_orchestrator

    for pms in ("buildium", None):
        orch = _with_pms(pms, lambda: create_orchestrator(Settings(), loop_mode=False))
        specialist = next(h for h in orch.handoffs if getattr(h, "name", "") == "buildium_agent")
        assert "You are the Buildium Agent" in specialist.instructions


def test_the_fast_routed_specialist_is_reworded_too() -> None:
    from helixis.agents.analysis_agent import create_analysis_agent
    from helixis.agents.orchestrator import harden_fast_routed

    for factory in (create_buildium_agent, create_analysis_agent):

        def build(factory: Any = factory) -> Any:
            agent = harden_fast_routed(factory(Settings()))
            run_pms.reword_for_rentvine([agent])
            return agent

        assert not _prose_names_buildium(_with_pms("rentvine", build).instructions)
    # ...and the run actually does that on BOTH fast branches (AST, gotcha 40).
    tree = _run_agent_streaming_tree()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    hardened = sum(c.func.id == "harden_fast_routed" for c in calls)  # type: ignore[attr-defined]
    reworded = sum(c.func.id == "reword_for_rentvine" for c in calls)  # type: ignore[attr-defined]
    assert hardened and reworded == hardened


def test_create_orchestrator_rewords_after_every_prompt_is_built() -> None:
    from helixis.agents import orchestrator

    body = ast.parse(inspect.getsource(orchestrator.create_orchestrator)).body[0].body  # type: ignore[attr-defined]
    last_call = body[-2]
    assert isinstance(body[-1], ast.Return)
    assert "reword_for_rentvine" in _called(last_call)


def test_the_handoff_label_a_rentvine_user_sees_is_not_buildium() -> None:
    from helixis.agents.orchestrator import AGENT_DISPLAY_NAMES, _display_name

    labels = _with_pms("rentvine", lambda: [_display_name(n) for n in AGENT_DISPLAY_NAMES])
    assert "Rentvine Agent" in labels
    assert not any(_prose_names_buildium(label) for label in labels)
    assert _with_pms("buildium", lambda: _display_name("buildium_agent")) == "Buildium Agent"


# ── open_work_orders ───────────────────────────────────────────────────────


class _CM:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *_a: Any) -> None:
        return None


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "buildium_id": 990000701,
        "title": "Kitchen sink leaking under the cabinet",
        "status": "InProgress",
        "priority": "Medium",
        "property_name": "Synthetic Maple Court",
        "unit_number": "M-101",
        "has_vendor_key": True,
        "vendor_ref": "990000501",
        "vendor_name": "Synthetic Plumbing Co",
    }
    row.update(over)
    return row


async def _list(rows: list[dict[str, Any]], *, properties: bool = True, **kw: Any) -> str:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _CM(conn))
    ctx = MagicMock(company_id="co-1")
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="FRESH")),
        patch.object(mirror_tools, "_not_synced", AsyncMock(return_value="NOT SYNCED")),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[object()] if properties else []),
        ),
    ):
        out = await mirror_tools._open_work_orders_impl(**kw)
    assert conn.fetch.await_args.args[1] == "co-1"
    return out


@pytest.mark.asyncio
async def test_it_lists_id_place_status_and_vendor_inside_the_fence() -> None:
    out = await _list([_row()])
    line = (
        "- #990000701 Kitchen sink leaking under the cabinet — Synthetic Maple Court, "
        "unit M-101 (In progress, Medium priority, vendor: Synthetic Plumbing Co)"
    )
    assert line in out
    begin, end = out.index("BEGIN_UNTRUSTED"), out.index("END_UNTRUSTED")
    assert begin < out.index(line) < end
    assert out.rstrip().endswith("FRESH")


@pytest.mark.asyncio
async def test_what_the_row_cannot_say_is_said_as_unknown() -> None:
    out = await _list(
        [
            _row(buildium_id=1, vendor_name=None, vendor_ref="77"),
            _row(buildium_id=2, vendor_name=None, vendor_ref=None, has_vendor_key=True),
            _row(buildium_id=3, vendor_name=None, vendor_ref=None, has_vendor_key=False),
            _row(buildium_id=4, status=None, unit_number=None),
        ]
    )
    assert "vendor #77 (not among the synced vendors)" in out
    assert "#2 " in out and "no vendor assigned" in out
    line3 = next(ln for ln in out.splitlines() if ln.startswith("- #3 "))
    assert "vendor" not in line3  # key absent: unknown, never "no vendor"
    line4 = next(ln for ln in out.splitlines() if ln.startswith("- #4 "))
    assert "status unknown" in line4 and "unit" not in line4


@pytest.mark.asyncio
async def test_search_and_property_filters() -> None:
    rows = [
        _row(),
        _row(buildium_id=2, title="Hallway light", property_name="Synthetic Birch House"),
    ]
    by_word = await _list(rows, search="sink")
    assert "#990000701" in by_word and "#2 " not in by_word
    by_place = await _list(rows, property_query="birch")
    assert "#2 " in by_place and "#990000701" not in by_place
    none = await _list(rows, search="roof")
    assert none.startswith("No open work orders match that.")


@pytest.mark.asyncio
async def test_empty_is_told_apart_from_never_synced() -> None:
    assert await _list([], properties=False) == "NOT SYNCED"
    assert (await _list([])).startswith("No open work orders on file. FRESH")


@pytest.mark.asyncio
async def test_a_long_list_names_what_it_left_out() -> None:
    rows = [_row(buildium_id=i) for i in range(1, mirror_tools._WORK_ORDER_LIST_CAP + 6)]
    out = await _list(rows)
    assert "(+5 more not listed." in out
    assert f"- #{mirror_tools._WORK_ORDER_LIST_CAP + 1} " not in out


def test_the_query_is_company_scoped_and_keeps_unknown_status_open() -> None:
    sql = mirror_tools._OPEN_WORK_ORDERS_SQL
    assert "w.company_id = $1::uuid" in sql
    assert "w.status IS NULL OR w.status NOT IN (" in sql
    for alias in ("p", "u", "v"):
        assert f"{alias}.company_id = w.company_id" in sql


def test_the_vendor_key_has_one_home() -> None:
    """Operations and the chat must agree on where a work order names its
    vendor (gotcha 25)."""
    from helixis.db.repositories import operations

    ref = operations.vendor_ref_sql("wo")
    assert ref in operations._VENDORS_SQL
    assert operations.vendor_ref_sql("w") in mirror_tools._OPEN_WORK_ORDERS_SQL
    assert "'vendorContactID'" in ref and "'VendorId'" in ref


def test_the_specialist_is_shown_as_the_rentvine_agent_only_on_rentvine() -> None:
    """ "Handing off to Buildium Agent" is user-visible (the thinking line)."""
    from helixis.agents.orchestrator import _display_name

    for pms, want in (
        ("rentvine", "Rentvine Agent"),
        ("buildium", "Buildium Agent"),
        (None, "Buildium Agent"),
    ):
        token = run_pms.bind_run_pms(pms)
        try:
            assert _display_name("buildium_agent") == want
            assert _display_name("gmail_agent") == "Gmail Agent"
        finally:
            run_pms.reset_run_pms(token)


# ── every tool a Rentvine run's prompt names is one it can call ─────────────
#
# Sandbox run 2026-10-01: reworded, "``buildium_list_active_leases`` — current
# rent roll" read as advice for a Rentvine run, the model called the hidden
# tool, and the turn died. Not "no hidden name" but the stronger rule: every
# tool name the prompt mentions is registered AND visible for that run.


async def _tool_names(agent: Any) -> set[str]:
    return {t.name for t in await agent.get_all_tools(RunContextWrapper(context=None))}


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_mode", [False, True])
async def test_every_tool_a_rentvine_prompt_names_is_one_the_run_can_call(loop_mode: bool) -> None:
    import re

    from helixis.agents.orchestrator import create_orchestrator

    def agents_of(orch: Any) -> list[Any]:
        return [orch, *(h for h in orch.handoffs if hasattr(h, "instructions"))]

    # Every tool name the product registers on this path, hidden ones included.
    buildium_run = _with_pms(
        "buildium", lambda: create_orchestrator(Settings(), loop_mode=loop_mode)
    )
    universe = set(BUILDIUM_NAMES)
    for agent in agents_of(buildium_run):
        universe |= await _tool_names(agent)

    token = run_pms.bind_run_pms("rentvine")
    try:
        rentvine_run = create_orchestrator(Settings(), loop_mode=loop_mode)
        callable_here: set[str] = set()
        for agent in agents_of(rentvine_run):
            callable_here |= await _tool_names(agent)
    finally:
        run_pms.reset_run_pms(token)

    for agent in agents_of(rentvine_run):
        named = {n for n in universe if re.search(rf"\b{re.escape(n)}\b", agent.instructions)}
        assert named <= callable_here, (agent.name, sorted(named - callable_here))
    # The check can fail: the Buildium prompt names tools a Rentvine run lacks.
    specialist = create_buildium_agent(Settings())
    assert any(re.search(rf"\b{n}\b", specialist.instructions) for n in BUILDIUM_NAMES)


def _rentvine_specialist_prompt() -> str:
    from helixis.agents.orchestrator import harden_fast_routed

    def build() -> Any:
        agent = harden_fast_routed(create_buildium_agent(Settings()))
        run_pms.reword_for_rentvine([agent])
        return agent

    return str(_with_pms("rentvine", build).instructions)


def test_every_section_swap_matches_the_buildium_prompt() -> None:
    # A swap whose heading moved would silently stop replacing anything.
    buildium = create_buildium_agent(Settings()).instructions
    rentvine = _rentvine_specialist_prompt()
    for heading, replacement in run_pms.RENTVINE_SECTIONS:
        assert heading in buildium, heading
        assert replacement.strip() in rentvine, heading


def test_the_rentvine_prompt_has_no_broken_structure() -> None:
    import re

    lines = _rentvine_specialist_prompt().split("\n")
    nonblank = [ln for ln in lines if ln.strip()]
    for here, nxt in zip(nonblank, nonblank[1:], strict=False):
        if here.startswith("## ") and nxt.startswith("## "):
            raise AssertionError(f"empty section: {here!r}")
    # Numbered lists count up from 1 with no gap.
    numbers = [int(m.group(1)) for ln in lines if (m := re.match(r"^(\d+)\. ", ln))]
    for prev, cur in zip(numbers, numbers[1:], strict=False):
        assert cur in (prev + 1, 1), numbers
    text = "\n".join(lines)
    for dead_end in ("Settings → Connectors", "restart", "Developer Tools", "the the "):
        assert dead_end not in text, dead_end


def test_a_dropped_block_goes_whole_with_its_sub_items() -> None:
    text = (
        "Rules:\n"
        "1. Keep this one.\n"
        "2. Drop this one, which wraps\n"
        "   onto a second line naming buildium_x_tool here.\n"
        "   - and a sub-item of the dropped one\n"
        "3. Keep this too.\n"
        "\n"
        "Lease tools:\n"
        "- ``buildium_x_tool`` — gone\n"
        "\n"
        "## Next\n"
        "Body."
    )
    out = run_pms._drop_blocks_naming(text, frozenset({"buildium_x_tool"}))
    assert out == "Rules:\n1. Keep this one.\n2. Keep this too.\n\n## Next\nBody."


def test_a_buildium_run_keeps_every_section() -> None:
    from helixis.agents.orchestrator import harden_fast_routed

    def build() -> Any:
        agent = harden_fast_routed(create_buildium_agent(Settings()))
        run_pms.reword_for_rentvine([agent])
        return agent

    plain = harden_fast_routed(create_buildium_agent(Settings())).instructions
    assert _with_pms("buildium", build).instructions == plain


# ── a call to a tool the run does not hold ─────────────────────────────────


def test_an_unknown_tool_never_sends_the_user_to_reconnect_anything() -> None:
    from helixis.agents.orchestrator import UNKNOWN_TOOL_MESSAGE

    lowered = UNKNOWN_TOOL_MESSAGE.lower()
    for wrong in ("calendar", "email", "reconnect", "connectors", "toolkit"):
        assert wrong not in lowered, wrong
    assert "couldn't complete" in lowered and "try asking again" in lowered


def test_the_tool_not_found_branch_publishes_that_message() -> None:
    from helixis.agents import orchestrator

    tree = _run_agent_streaming_tree()
    branches = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Constant) and c.value == "not found in agent"
            for c in ast.walk(n.test)
        )
    ]
    assert len(branches) == 1
    names = {n.id for s in branches[0].body for n in ast.walk(s) if isinstance(n, ast.Name)}
    assert "UNKNOWN_TOOL_MESSAGE" in names
    assert "calendar/email" not in inspect.getsource(orchestrator.run_agent_streaming)
