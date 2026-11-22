"""#7 wo-overdue / wo-history — the date-aware work-order reads.

Covers the pure cores (no API): the WorkOrder/Task schema now parse the
Buildium date fields (sandbox probe 2026-07-13), the open-WO list orders
most-overdue-first with each item's due date, and the maintenance-history
tool ranks units by tasks CREATED in the window.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from helixis.agents.buildium_tools import (
    _format_open_work_orders,
    _format_unit_task_history,
    _rank_units_by_task_activity,
)
from helixis.buildium.schemas import Task, WorkOrder

# ── schema: the new date fields parse from the real Buildium aliases ──


def test_workorder_parses_due_date_top_level_and_nested() -> None:
    # Top-level DueDate present.
    top = WorkOrder.model_validate(
        {"Id": 1, "Title": "Leak", "DueDate": "2025-04-07", "Status": "New"}
    )
    assert top.due_date == date(2025, 4, 7)
    assert top.effective_due_date == date(2025, 4, 7)

    # Top-level null, but the nested Task carries it → effective falls back.
    nested = WorkOrder.model_validate(
        {"Id": 2, "Title": "Mouse", "Task": {"Id": 16583, "UnitId": 30662, "DueDate": "2025-05-01"}}
    )
    assert nested.due_date is None
    assert nested.effective_due_date == date(2025, 5, 1)
    assert nested.effective_unit_id == 30662


def test_task_parses_created_and_unit_from_tasks_endpoint() -> None:
    t = Task.model_validate(
        {
            "Id": 16583,
            "TaskStatus": "InProgress",
            "UnitId": 30662,
            "DueDate": "2025-04-07",
            "CreatedDateTime": "2026-04-29T02:35:02Z",
            "LastUpdatedDateTime": "2026-04-30T02:35:02Z",
        }
    )
    assert t.unit_id == 30662
    assert t.task_status == "InProgress"
    assert t.due_date == date(2025, 4, 7)
    assert t.created_date_time == datetime(2026, 4, 29, 2, 35, 2, tzinfo=UTC)
    assert t.last_updated_date_time == datetime(2026, 4, 30, 2, 35, 2, tzinfo=UTC)


# ── wo-overdue: ordering + overdue math ──


def _wo(id_: int, due: str | None, title: str = "WO") -> WorkOrder:
    payload: dict = {"Id": id_, "Title": title, "Status": "New"}
    if due is not None:
        payload["DueDate"] = due
    return WorkOrder.model_validate(payload)


def test_open_work_orders_order_most_overdue_first() -> None:
    today = date(2025, 5, 10)
    wos = [
        _wo(1, "2025-06-01", "future"),  # upcoming
        _wo(2, "2025-04-01", "very overdue"),  # 39d overdue
        _wo(3, None, "no due"),  # no due date → tail
        _wo(4, "2025-05-05", "slightly overdue"),  # 5d overdue
    ]
    out = _format_open_work_orders(wos, today)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    # Most-overdue first, then the other overdue, then upcoming, then no-due.
    assert "very overdue" in lines[0]
    assert "slightly overdue" in lines[1]
    assert "future" in lines[2]
    assert "no due" in lines[3]
    # Header counts the overdue items.
    assert "2 overdue" in out
    # Overdue days are surfaced.
    assert "overdue 39d" in lines[0]
    assert "in 22d" in lines[2]


def test_open_work_orders_due_today_and_empty() -> None:
    today = date(2025, 5, 10)
    out = _format_open_work_orders([_wo(9, "2025-05-10")], today)
    assert "due today" in out
    assert "overdue" not in out.split("\n", 1)[0]  # header has no overdue count
    assert _format_open_work_orders([], today) == "No open work orders in Buildium right now."


# ── wo-history: windowed grouping + ranking ──


def _task(id_: int, unit_id: int | None, created: datetime | None) -> Task:
    payload: dict = {"Id": id_}
    if unit_id is not None:
        payload["UnitId"] = unit_id
    if created is not None:
        payload["CreatedDateTime"] = created.isoformat()
    return Task.model_validate(payload)


def test_rank_units_windows_and_orders() -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    tasks = [
        _task(1, 100, now - timedelta(days=5)),
        _task(2, 100, now - timedelta(days=10)),
        _task(3, 100, now - timedelta(days=200)),  # outside window → dropped
        _task(4, 200, now - timedelta(days=3)),
        _task(5, None, now - timedelta(days=1)),  # no unit → skipped
        _task(6, 300, None),  # no created date → skipped
    ]
    ranked = _rank_units_by_task_activity(tasks, cutoff)
    # Unit 100 has 2 in-window items, unit 200 has 1; unit 300 excluded.
    assert ranked == [(100, 2), (200, 1)]


def test_rank_units_naive_datetime_treated_as_utc() -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    cutoff = now - timedelta(days=30)
    # A naive created timestamp inside the window must still count.
    naive = (now - timedelta(days=2)).replace(tzinfo=None)
    ranked = _rank_units_by_task_activity([_task(1, 42, naive)], cutoff)
    assert ranked == [(42, 1)]


def test_format_unit_history_empty_and_ranked() -> None:
    assert "No maintenance activity" in _format_unit_task_history([], 90)
    out = _format_unit_task_history([(100, 4), (200, 1)], 90)
    assert "1. Unit 100 — 4 maintenance item(s)" in out
    assert "2. Unit 200 — 1 maintenance item(s)" in out
    assert "last 90 days" in out
