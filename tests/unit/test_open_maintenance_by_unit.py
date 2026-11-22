"""Portfolio-wide open maintenance in one call — the N+1 killer.

Battery v3, Q5: "before recommending a late notice on anyone, check whether
they have open maintenance at their unit." Twenty delinquent tenants means
twenty per-unit lookups, which does not fit a 14-call budget — so the run
ALWAYS died on the 240s wall clock, having found the right twenty tenants and
run out of time before writing a word. That is arithmetic, not slowness.

The other thing pinned here is coverage. Q11 answered "probably not
duplicates" after reading only work orders and then generalising to
"maintenance requests" — two Buildium entities behind one English word. A tool
named for the concept has to span both or it manufactures confident wrong
answers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import Task


def _wo(wo_id: int, title: str, *, property_id=37948, unit_id=5501, status="New"):
    return SimpleNamespace(
        id=wo_id, title=title, status=status, property_id=property_id, unit_id=unit_id
    )


def _task(task_id: int, title: str, *, property_id=37948, status="New", priority="Low"):
    return Task.model_validate(
        {
            "Id": task_id,
            "Title": title,
            "TaskStatus": status,
            "Priority": priority,
            "Property": {"Id": property_id},
        }
    )


def _patched(work_orders=(), tasks=(), properties=()):
    api = AsyncMock()
    api.work_orders.list = AsyncMock(return_value=list(work_orders))
    api.tasks.list = AsyncMock(return_value=list(tasks))
    api.rentals.list_properties = AsyncMock(return_value=list(properties))
    return patch.object(bt, "_open_api", AsyncMock(return_value=(AsyncMock(), api, None))), api


_PROPS = [
    SimpleNamespace(id=37948, name="600 Dexter Ave", address=None),
    SimpleNamespace(id=41000, name="Roofline Apartments", address=None),
]


@pytest.mark.asyncio
async def test_one_call_covers_the_whole_portfolio() -> None:
    """The point of the tool: N units, a fixed number of round-trips."""
    ctx, api = _patched(
        work_orders=[_wo(1, "Leak", property_id=37948), _wo(2, "Roof", property_id=41000)],
        tasks=[_task(10, "Filter change", property_id=41000)],
        properties=_PROPS,
    )
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()

    assert api.work_orders.list.await_count == 1
    assert api.tasks.list.await_count == 1
    assert "600 Dexter Ave" in out and "Roofline Apartments" in out


@pytest.mark.asyncio
async def test_it_covers_tasks_and_work_orders_both() -> None:
    """Q11's failure: 'maintenance requests' spans two entities, and reading
    one then generalising to the other is how a confident wrong answer is
    made."""
    ctx, _ = _patched(
        work_orders=[_wo(1, "Water heater replacement")],
        tasks=[_task(39431, "URGENT: water heater leaking into the closet")],
        properties=_PROPS,
    )
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()

    assert "work order: Water heater replacement" in out
    assert "task: URGENT: water heater leaking into the closet" in out


@pytest.mark.asyncio
async def test_completed_tasks_are_not_open_maintenance() -> None:
    ctx, _ = _patched(
        tasks=[_task(1, "Old repair", status="Completed"), _task(2, "Live repair")],
        properties=_PROPS,
    )
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()
    assert "Live repair" in out
    assert "Old repair" not in out


@pytest.mark.asyncio
async def test_an_empty_portfolio_says_what_it_checked() -> None:
    """The absence claim this tool is allowed to make — and it has to state
    the scope that backs it, or it reads as a guess."""
    ctx, _ = _patched(properties=_PROPS)
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()
    assert "No open maintenance anywhere" in out
    assert "checked all" in out


@pytest.mark.asyncio
async def test_it_tells_the_agent_not_to_re_query_per_unit() -> None:
    """Without this the model treats the result as a starting point and
    hydrates each unit anyway — the exact behaviour that made #850 fail."""
    ctx, _ = _patched(work_orders=[_wo(1, "Leak")], properties=_PROPS)
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()
    assert "do NOT re-query per unit" in out


@pytest.mark.asyncio
async def test_busiest_location_comes_first() -> None:
    ctx, _ = _patched(
        work_orders=[_wo(i, f"Issue {i}", property_id=41000, unit_id=None) for i in range(3)]
        + [_wo(99, "Single", property_id=37948, unit_id=None)],
        properties=_PROPS,
    )
    with ctx:
        out = await bt._open_maintenance_by_unit_impl()
    assert out.index("Roofline Apartments") < out.index("600 Dexter Ave")


def test_the_cap_steers_to_this_tool() -> None:
    """A cap is only useful if the refusal names where to go instead — and
    that name has to be a tool that actually exists."""
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.call_budget import PER_RECORD_CAPS

    registered = {t.name for t in buildium_tools()}
    assert "buildium_open_maintenance_by_unit" in registered
    for tool in ("buildium_get_work_order", "buildium_list_work_orders_for_unit"):
        assert "buildium_open_maintenance_by_unit" in PER_RECORD_CAPS[tool].steer
