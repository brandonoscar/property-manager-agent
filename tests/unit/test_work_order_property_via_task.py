"""A work order's property is resolvable, and the LIVE tools were dropping it.

`upsert_work_order` has resolved a work order's property through its backing
task since #14, in SQL. The two live agent tools never got the same fix, and
`buildium/linkage.py`'s own docstring stated the rule the whole time — a
comment cannot fail (gotcha 48), so it went on being right and unused.

What it cost, live 2026-08-29 on Blue Ridge:

* `_maintenance_index` keyed on `WorkOrder.property_id`, which production
  leaves NULL on all 60 rows, so the portfolio rollup built to remove the
  per-record N+1 filed every open work order under "unassigned".
* `_only_for_unit` filters on `unit_id`, also null on all 60, so it correctly
  fails open (gotcha 55: a blind field must not manufacture an absence) and
  returned the WHOLE ACCOUNT for a one-unit property.

The agent then told the operator, twice and at full confidence, that 500
Woodlane St had 60 open work orders and speculated they were duplicate test
data. The mirror says **1**. Two write-harness cases died on the wall clock
over it without ever reaching their gate.

⚠ This reads BOTH sides (gotcha 28): the resolver AND the two call sites. A
test of the resolver alone would have passed against the broken build — the
function is new; the defect was that nothing called it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from helixis.agents.buildium_tools import _maintenance_index
from helixis.buildium.linkage import work_order_property_ids

_TOOLS = Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "buildium_tools.py"


def _wo(wo_id: int, *, task_id: int | None = None, property_id: int | None = None):
    task = SimpleNamespace(id=task_id) if task_id is not None else None
    return SimpleNamespace(id=wo_id, task=task, property_id=property_id, unit_id=None, title="x")


def _task(task_id: int, property_id: int | None):
    prop = SimpleNamespace(id=property_id) if property_id is not None else None
    return SimpleNamespace(id=task_id, property=prop)


# ── the resolver ──────────────────────────────────────────────────────


def test_the_production_shape_resolves() -> None:
    """Every field the live payload actually has: no property on the work
    order, a task id, and the property nested on the task."""
    resolved = work_order_property_ids([_wo(9001, task_id=41)], [_task(41, 37951)])
    assert resolved == {9001: 37951}


def test_a_work_order_with_no_task_is_absent_not_guessed() -> None:
    """gotcha 55's rule at the row level. A key that is missing lets the caller
    say 'unassigned' honestly; a guessed property is a fabricated fact, which
    is the class of claim this whole module exists to remove."""
    assert work_order_property_ids([_wo(9002)], [_task(41, 37951)]) == {}


def test_a_task_outside_the_fetched_set_is_absent() -> None:
    """The caller passes tasks it already has. A work order whose task was not
    in that set must not silently borrow another task's property."""
    assert work_order_property_ids([_wo(9003, task_id=99)], [_task(41, 37951)]) == {}


def test_an_own_property_id_still_wins() -> None:
    """Other accounts' payloads DO carry it, and the mirror's COALESCE prefers
    it. Ignoring it here would make this a regression for a complete payload
    while fixing the incomplete one."""
    resolved = work_order_property_ids([_wo(9004, task_id=41, property_id=555)], [_task(41, 37951)])
    assert resolved == {9004: 555}


def test_a_task_with_no_property_resolves_nothing() -> None:
    assert work_order_property_ids([_wo(9005, task_id=41)], [_task(41, None)]) == {}


# ── call site 1: the portfolio rollup ─────────────────────────────────


def test_the_rollup_groups_by_the_resolved_property() -> None:
    """The defect, reproduced at the size it actually occurred: work orders
    with null property_id must NOT all land under one 'unassigned' key."""
    work_orders = [_wo(9001, task_id=41), _wo(9002, task_id=42)]
    tasks = [_task(41, 37951), _task(42, 40100)]
    index = _maintenance_index(work_orders, [], None, work_order_property_ids(work_orders, tasks))
    assert sorted(k[0] for k in index) == [37951, 40100], (
        "both work orders landed on the same key -- the task join is not being "
        "used, which is the 'all 60 unassigned' bug"
    )


def test_the_rollup_without_a_map_is_unchanged() -> None:
    """The parameter is optional on purpose: an account whose payload carries
    property_id must behave exactly as before."""
    index = _maintenance_index([_wo(9001, property_id=777)], [])
    assert list(index) == [(777, None)]


# ── blind vs merely-unchanged ─────────────────────────────────────────


def test_a_working_filter_that_keeps_everything_is_not_blind() -> None:
    """The bug I shipped in the first cut of this fix, caught by an existing
    mock. A unit filter that keeps every row because every row really does
    belong to that unit is WORKING; one that keeps every row because the field
    is null everywhere is BLIND. Inferring blindness from an unchanged count
    fires the property fallback on the healthy case — widening a correct
    single-unit answer to the whole building, which is exactly the over-claim
    gotcha 50 says is invisible in a Blue Ridge fixture."""
    from helixis.agents.buildium_tools import _field_is_blind

    all_this_unit = [SimpleNamespace(unit_id=99), SimpleNamespace(unit_id=99)]
    assert not _field_is_blind(all_this_unit, "unit_id")

    production_shape = [SimpleNamespace(unit_id=None), SimpleNamespace(unit_id=None)]
    assert _field_is_blind(production_shape, "unit_id")

    # One populated row is enough to filter (gotcha 55) -- the bar is not "most".
    assert not _field_is_blind(
        [SimpleNamespace(unit_id=None), SimpleNamespace(unit_id=7)], "unit_id"
    )


def test_the_fail_open_condition_is_the_shared_helper() -> None:
    """`_only_matching`'s fail-open and the per-unit tool's fallback must key on
    the SAME condition or they drift into disagreeing about what "blind" means
    — the two-spellings-of-open problem gotcha 50 records."""
    import ast

    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    users = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_field_is_blind"
            for n in ast.walk(node)
        )
    }
    assert {"_only_matching", "_list_work_orders_for_unit_impl"} <= users, (
        f"only {sorted(users)} use _field_is_blind -- one of them has an "
        "open-coded copy of the condition and they can now disagree"
    )


# ── call site 2: the per-unit list (AST -- 'wired to nothing') ─────────


def _calls_in(func_name: str) -> set[str]:
    """Every function called inside one top-level async def."""
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return {
                n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
            }
    raise AssertionError(f"{func_name} not found -- the scan is reading the wrong file")


def test_both_live_call_sites_use_the_resolver() -> None:
    """A behavioural test cannot see 'wired to nothing' (gotcha 62), and this
    fix is ONLY a wiring change -- the resolver would pass every test above
    while neither tool called it, which is exactly the state that shipped."""
    for func in ("_list_work_orders_for_unit_impl", "_open_maintenance_by_unit_impl"):
        assert "work_order_property_ids" in _calls_in(func), (
            f"{func} does not call work_order_property_ids -- it is back to "
            "reporting the whole account for one unit, or the whole book as "
            "property-less"
        )


def test_the_ast_scan_can_actually_see_a_call() -> None:
    """Self-check: a scan that finds nothing anywhere passes vacuously."""
    assert "_only_for_unit" in _calls_in("_list_work_orders_for_unit_impl")
    assert "definitely_not_a_real_function" not in _calls_in("_list_work_orders_for_unit_impl")
