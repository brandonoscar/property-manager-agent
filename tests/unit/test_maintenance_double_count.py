"""Every work order is also a task — count it once.

Buildium backs each work order with a to-do item and we mirror both. Both the
local rollup and the live tool counted the two lists and added them, so every
work order in the portfolio was counted twice.

LIVE-VERIFIED 2026-08-28, before any code changed:

    93 of 93 work orders carry a Task.Id
    93 of 93 have a matching helixis_tasks row
    Blue Ridge: 60 open work orders + 90 open tasks   = 150 rendered
                60 open work orders + 30 open tasks   =  90 real

That number is on every delinquency and expiring-lease row, and the rollup's
own footer tells the model *"you have what you need; do NOT look maintenance up
per lease"* — so nothing downstream could catch it.

⚠ WHAT THIS FILE CAN AND CANNOT EXECUTE, stated plainly rather than implied.
The live tool's rule is Python and is executed here for real, including the one
input that distinguishes the two implementations: a task that backs an open
work order. The mirror's rule is SQL, and this container has the Postgres
CLIENT only (``initdb`` fails, no server), so it cannot be run here. Its
evidence is (a) the live query above, executed against the real database before
the fix and after, and (b) ``test_the_mirror_excludes_backing_tasks`` below,
which re-runs that verification when a DB url is present — gotcha 23's rule
that a credential-gated test must be executed once against the live target
before merge is satisfied by (a).

⚠ The two rules must stay IDENTICAL. Gotcha 50 already records two disagreeing
spellings of "open" in this codebase; a third would make the same question
answer differently depending on which tool the router happened to reach.
"""

from __future__ import annotations

import os
import re
import textwrap
from types import SimpleNamespace

import pytest

from helixis.agents import buildium_tools, mirror_tools

# ── the four states, as data ─────────────────────────────────────────────
#
# The exclusion is scoped to tasks behind an OPEN work order, not to every
# backing task, and these four rows are why. A work order's status LATCHES at
# Completed and cannot be reopened through the API (gotcha 20), so a task can
# legitimately be open behind a closed work order. Dropping it would turn a
# visible over-count into a confident false zero — strictly worse.
STATES = [
    ("wo open, task open", "New", "New", 1),
    ("wo open, task closed", "New", "Completed", 1),
    ("wo closed, task open", "Completed", "New", 1),
    ("wo closed, task closed", "Completed", "Completed", 0),
]


def _wo(wo_id: int, status: str, task_id: int, unit_id: int = 500) -> SimpleNamespace:
    return SimpleNamespace(
        id=wo_id,
        status=status,
        title=f"work order {wo_id}",
        property_id=10,
        unit_id=unit_id,
        task=SimpleNamespace(id=task_id, unit_id=unit_id, priority=None, due_date=None),
    )


def _task(task_id: int, status: str, unit_id: int = 500) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        title=f"task {task_id}",
        task_status=status,
        status=None,
        property_id=10,
        unit_id=unit_id,
        property=SimpleNamespace(id=10),
    )


# ── the live tool: executed for real ─────────────────────────────────────


@pytest.mark.parametrize(("label", "wo_status", "task_status", "expected"), STATES)
def test_the_live_tool_counts_each_item_once(
    label: str, wo_status: str, task_status: str, expected: int
) -> None:
    """⚠ THE TEST THAT COULD SEE THE BUG. A test handed two numbers cannot —
    it would have to already know they overlap, which is the thing that was
    wrong. This one hands the tool a work order and THE TASK THAT BACKS IT and
    counts what comes out.

    The tool filters open work orders upstream (the API call passes a status
    filter), so a closed work order is modelled by not being in that list.
    """
    open_statuses = set(buildium_tools._OPEN_WORK_ORDER_STATUSES)
    work_orders = [_wo(900, wo_status, task_id=77)] if wo_status in open_statuses else []
    all_tasks = [_task(77, task_status)]

    backing = {
        tid
        for w in work_orders
        if (tid := getattr(getattr(w, "task", None), "id", None)) is not None
    }
    tasks = [
        t
        for t in all_tasks
        if buildium_tools._task_status(t) in buildium_tools._OPEN_TASK_STATUSES
        and getattr(t, "id", None) not in backing
    ]
    index = buildium_tools._maintenance_index(work_orders, tasks)
    counted = sum(len(v) for v in index.values())
    assert counted == expected, f"{label}: expected {expected}, indexed {counted} — {index}"


def test_a_standalone_task_is_never_dropped() -> None:
    """The other direction. 86 of Blue Ridge's 179 tasks back no work order at
    all; excluding those would be a fresh under-count and would hide real work.
    """
    work_orders = [_wo(900, "New", task_id=77)]
    all_tasks = [_task(77, "New"), _task(88, "New", unit_id=501)]
    backing = {w.task.id for w in work_orders}
    tasks = [t for t in all_tasks if t.id not in backing]
    index = buildium_tools._maintenance_index(work_orders, tasks)
    assert sum(len(v) for v in index.values()) == 2, index


def test_the_shipped_tool_body_applies_the_exclusion() -> None:
    """⚠ The parametrised tests above re-implement the filter to exercise the
    four states in isolation, which means they would still pass if the shipped
    function stopped applying it. This reads the real source and asserts the
    exclusion is in it, keyed on the same attribute path.

    Asserted on the parsed AST, never a substring — ``ruff format`` reflows a
    wrapped call and a substring guard then matches nothing (gotcha 40).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(buildium_tools._open_maintenance_by_unit_impl))

    # ⚠ Assert the set is USED as a filter, not merely defined. The first
    # version of this guard checked the name appeared anywhere — and deleting
    # the filter line left the set comprehension that builds it untouched, so
    # the guard stayed green against the exact bug it exists for. Verified by
    # mutation, which is the only reason it was caught.
    filtered = any(
        isinstance(n, ast.Compare)
        and any(isinstance(op, ast.NotIn) for op in n.ops)
        and any(
            isinstance(c, ast.Name) and c.id == "backed_by_open_work_order" for c in n.comparators
        )
        for n in ast.walk(tree)
    )
    assert filtered, (
        "the live tool no longer FILTERS on backed_by_open_work_order — every "
        "work order is being counted twice again, once as itself and once as "
        "the task Buildium backs it with"
    )
    # ⚠ And the path it keys on. The tool reaches the backing id through
    # ``getattr(getattr(w, "task", None), "id", None)`` — defensively, because
    # a work order without a nested Task must not raise — so "task" and "id"
    # are STRING CONSTANTS here, not ast.Attribute nodes. Asserting on
    # attributes passed against the old code and failed against the new one,
    # which is the wrong way round for a guard.
    getattr_keys = {
        n.args[1].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "getattr"
        and len(n.args) > 1
        and isinstance(n.args[1], ast.Constant)
        and isinstance(n.args[1].value, str)
    }
    assert {"task", "id"} <= getattr_keys, (
        f"the exclusion no longer reads the work order's backing task id: {getattr_keys}"
    )


# ── the mirror: SQL, so structure here and execution below ───────────────
#
# ⚠ The "one spelling of open" rule lives in
# test_mirror_tools.py::test_both_rollups_count_open_the_same_way, NOT here.
# It was briefly asserted in both files with different expected counts, which
# is the exact drift it exists to prevent.


def test_the_mirror_sql_excludes_tasks_behind_an_open_work_order() -> None:
    """The mirror's rule is SQL and cannot execute in this container. What can
    be checked here is that the exclusion exists, is CORRELATED to the task row
    rather than a bare subquery, and is scoped to open work orders — the three
    properties that make it the same rule as the live tool's.
    """
    sql = " ".join(mirror_tools._OPEN_MAINTENANCE_SELECT.split())
    assert "NOT EXISTS" in sql, "the backing-task exclusion is gone — work orders count twice"
    # Correlated on the task's own id, both ways round.
    assert "wb.task_id = tk.buildium_id" in sql
    assert "wb.company_id = tk.company_id" in sql, "gotcha 47: the join must stay company-scoped"
    # ⚠ Scoped to OPEN work orders. Without this clause a task left open behind
    # a latched-Completed work order vanishes — a false zero, worse than the
    # over-count it replaced.
    exclusion = sql.split("NOT EXISTS", 1)[1]
    assert "wb.status NOT IN" in exclusion, (
        "the exclusion is not scoped to OPEN work orders, so a task open behind "
        "a closed work order would be dropped entirely"
    )


@pytest.mark.skipif(
    not os.getenv("HELIXIS_SUPABASE__DB_URL"),
    reason="needs the live mirror; the SQL cannot run without a Postgres server",
)
@pytest.mark.asyncio
async def test_the_mirror_excludes_backing_tasks_against_the_live_database() -> None:
    """Re-runs, against the real mirror, the verification that produced the
    numbers in this module's docstring.

    ⚠ EXECUTED ONCE BEFORE MERGE (2026-08-28) — gotcha 23's rule. A
    credential-gated test that has never run on its target can be born broken
    and stay red forever, invisibly. The pre-fix numbers it returned were
    60 / 90 / 30.
    """
    import asyncpg

    closed = list(mirror_tools.CLOSED_MAINTENANCE_STATUSES)
    conn = await asyncpg.connect(os.environ["HELIXIS_SUPABASE__DB_URL"])
    try:
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM helixis_tasks t
                WHERE NOT (t.status = ANY($1::text[]))) AS open_tasks_naive,
              (SELECT COUNT(*) FROM helixis_tasks t
                WHERE NOT (t.status = ANY($1::text[]))
                  AND NOT EXISTS (
                        SELECT 1 FROM helixis_work_orders wb
                         WHERE wb.company_id = t.company_id
                           AND wb.task_id = t.buildium_id
                           AND NOT (wb.status = ANY($1::text[])))) AS open_tasks_deduped,
              (SELECT COUNT(*) FROM helixis_work_orders w
                WHERE w.task_id IS NOT NULL) AS work_orders_with_a_task
            """,
            closed,
        )
    finally:
        await conn.close()

    # The property that matters: the mirror really does hold a task row per
    # work order, so the naive count really is inflated.
    assert row["work_orders_with_a_task"] > 0, (
        "no work order carries a task_id — either the mirror is empty or "
        "migration 0076 has not been applied to this database"
    )
    assert row["open_tasks_deduped"] < row["open_tasks_naive"], (
        "deduplication removed nothing, which means either every backing task "
        "is already closed or the join is wrong"
    )


def test_the_docstring_numbers_are_not_silently_edited() -> None:
    """The measurement is the argument for this whole change. If someone
    rewrites it without re-measuring, the file starts asserting a fact nobody
    checked — a comment cannot fail (gotcha 48), so pin the shape."""
    doc = __doc__ or ""
    assert re.search(r"93 of 93 work orders carry a Task\.Id", doc)
    assert re.search(r"=\s*150 rendered", doc) and re.search(r"=\s*90 real", doc)


# ── the vendor name on the bulk rows ─────────────────────────────────────
#
# Eighth instance of gotchas 41/50/51/53. ``_maintenance_index`` rendered
# "work order: Leak under sink [New]" and nothing about who is handling it, so
# "who's on the leak at 4B?" reads the whole-portfolio result, finds no vendor,
# and hydrates record by record — the N+1 this tool exists to remove, promoted
# one field over (gotcha 32: removing one fan-out promotes the next).
#
# Live-verified 2026-08-28, Blue Ridge only: all 60 open work orders carry a
# VendorId and all 60 resolve to a name via helixis_vendors (3 distinct
# vendors), so the join is one extra portfolio-wide call, not a guess.


def _vwo(**over: object) -> SimpleNamespace:
    base = {"property_id": 1, "unit_id": None, "title": "Leak", "status": "New", "vendor_id": 7}
    base.update(over)
    return SimpleNamespace(**base)


def _only_line(index: dict) -> str:
    lines = [line for rows in index.values() for line in rows]
    assert len(lines) == 1, lines
    return lines[0]


def test_a_work_order_row_names_the_vendor_handling_it() -> None:
    line = _only_line(
        buildium_tools._maintenance_index([_vwo()], [], {7: "Redbud General Maintenance"})
    )
    assert "Redbud General Maintenance" in line, line


def test_an_unresolvable_vendor_prints_nothing_rather_than_its_id() -> None:
    """⚠ The other direction. "vendor 999" is an identifier wearing a name's
    place (PR #75 Bug B) — and a model reading it will say it aloud. Silence
    lets the answer say the vendor is not known, which is true."""
    line = _only_line(buildium_tools._maintenance_index([_vwo(vendor_id=999)], [], {7: "Redbud"}))
    assert "999" not in line, line
    assert "Redbud" not in line, line
    assert line.endswith("[New]"), line


def test_a_work_order_with_no_vendor_is_unchanged() -> None:
    line = _only_line(buildium_tools._maintenance_index([_vwo(vendor_id=None)], [], {7: "Redbud"}))
    assert line == "work order: Leak [New]", line


def test_tasks_never_claim_a_vendor() -> None:
    """Buildium does not assign vendors to to-do tasks. Rendering one would be
    inventing a fact — gotcha 41's confident false statement, pointed at a
    person who would then get called about it."""
    task = SimpleNamespace(
        property=SimpleNamespace(id=1), unit_id=None, title="Inspect roof", priority="High"
    )
    line = _only_line(buildium_tools._maintenance_index([], [task], {7: "Redbud"}))
    assert "Redbud" not in line, line


def test_the_bulk_tool_actually_fetches_the_vendors_it_renders() -> None:
    """⚠ Every test above exercises ``_maintenance_index`` in isolation, so all
    of them pass with the tool never fetching a vendor and always passing an
    empty map — which is the shipped state this change exists to fix. Same
    mutation that caught the owner join going uncalled (test_owner_path).

    AST rather than a substring (gotcha 40).
    """
    import ast
    import inspect

    from helixis.agents import buildium_tools

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(buildium_tools._open_maintenance_by_unit_impl))
    )
    # ``api.vendors.list()`` — an Attribute chain, so match on the attribute names.
    fetches_vendors = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "list"
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "vendors"
        for n in ast.walk(tree)
    )
    assert fetches_vendors, "the bulk tool renders vendor names it never fetches"
    passes_them_on = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_maintenance_index"
        and len(n.args) >= 3
        for n in ast.walk(tree)
    )
    assert passes_them_on, "vendors are fetched and then dropped before rendering"
