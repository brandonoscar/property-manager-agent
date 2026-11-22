"""Real dispatch adapters (Inc.4c).

Each allowed note action must route to its existing self-gating Buildium impl
with correctly-mapped args; an allow-listed-but-unwired action and bad args must
fail cleanly (so the apply-core records them, never aborts). The impls are
mocked — we're testing the mapping + the write path's shape, not Buildium.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.codeexec import dispatch as dsp
from helixis.codeexec.apply import apply_proposed_actions
from helixis.codeexec.dispatch import ActionNotWiredError, default_dispatch


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    async def _fake_add_note(*, kind: str, target_id: int, note: str, label: str | None) -> str:
        calls["note"] = {"kind": kind, "target_id": target_id, "note": note, "label": label}
        return f"noted {kind} {target_id}"

    async def _fake_vendor_note(*, vendor_id: int, note: str, vendor_label: str | None) -> str:
        calls["vendor"] = {"vendor_id": vendor_id, "note": note, "vendor_label": vendor_label}
        return "vendor noted"

    async def _fake_create_task(**kwargs: Any) -> str:
        calls["create_task"] = kwargs
        return "task created"

    async def _fake_update_status(**kwargs: Any) -> str:
        calls["update_status"] = kwargs
        return "status updated"

    monkeypatch.setattr(dsp, "_add_note_impl", _fake_add_note)
    monkeypatch.setattr(dsp, "_add_vendor_note_impl", _fake_vendor_note)
    monkeypatch.setattr(dsp, "_create_to_do_task_impl", _fake_create_task)
    monkeypatch.setattr(dsp, "_update_task_status_impl", _fake_update_status)
    return calls


@pytest.mark.asyncio
async def test_task_note_routes_to_impl(captured: dict[str, Any]) -> None:
    out = await default_dispatch(
        "buildium_add_task_note",
        {"task_id": 11154, "note": "Vendor on-site Friday", "task_label": "Plumbing"},
    )
    assert captured["note"] == {
        "kind": "task",
        "target_id": 11154,
        "note": "Vendor on-site Friday",
        "label": "Plumbing",
    }
    assert "noted task 11154" == out


@pytest.mark.asyncio
async def test_work_order_note_routes_to_impl(captured: dict[str, Any]) -> None:
    await default_dispatch(
        "buildium_add_work_order_note", {"work_order_id": 9006, "note": "Parts ordered"}
    )
    assert captured["note"]["kind"] == "work_order"
    assert captured["note"]["target_id"] == 9006
    assert captured["note"]["label"] is None  # optional label omitted


@pytest.mark.asyncio
async def test_vendor_note_routes_to_impl(captured: dict[str, Any]) -> None:
    await default_dispatch(
        "buildium_add_vendor_note",
        {"vendor_id": "482", "note": "COI expires next month", "vendor_label": "Reliable"},
    )
    # String id is coerced to int.
    assert captured["vendor"] == {
        "vendor_id": 482,
        "note": "COI expires next month",
        "vendor_label": "Reliable",
    }


@pytest.mark.asyncio
async def test_create_task_routes_to_impl(captured: dict[str, Any]) -> None:
    await default_dispatch(
        "buildium_create_to_do_task",
        {"title": "Inspect 2B", "due_date": "2026-09-01", "unit_id": "58280"},
    )
    assert captured["create_task"]["title"] == "Inspect 2B"
    assert captured["create_task"]["due_date"] == "2026-09-01"
    assert captured["create_task"]["unit_id"] == 58280  # coerced
    assert captured["create_task"]["priority"] == "Normal"  # default


@pytest.mark.asyncio
async def test_update_task_status_routes_to_impl(captured: dict[str, Any]) -> None:
    await default_dispatch(
        "buildium_update_task_status",
        {"task_id": 11154, "status": "Completed", "task_label": "Plumbing"},
    )
    assert captured["update_status"] == {
        "task_id": 11154,
        "status": "Completed",
        "task_label": "Plumbing",
    }


@pytest.mark.asyncio
async def test_unwired_but_allowlisted_action_raises_not_wired(captured: dict[str, Any]) -> None:
    # create_followup_task IS on the guard allow-list but has no adapter yet.
    with pytest.raises(ActionNotWiredError):
        await default_dispatch("create_followup_task", {"title": "x"})


@pytest.mark.asyncio
async def test_missing_required_arg_raises_valueerror(captured: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await default_dispatch("buildium_add_task_note", {"task_id": 5})  # no note


@pytest.mark.asyncio
async def test_end_to_end_through_apply_core(captured: dict[str, Any]) -> None:
    # A proposal mixing a wired note, an unwired-but-allowed action, and a
    # FORBIDDEN money-write — only the note writes; the money-write is never
    # dispatched; the unwired one fails cleanly.
    report = await apply_proposed_actions(
        [
            {"action": "buildium_add_task_note", "args": {"task_id": 1, "note": "ok"}},
            {"action": "create_followup_task", "args": {"title": "x"}},  # allowed, unwired
            {"action": "buildium_record_payment", "args": {"amount": 500}},  # refused
        ],
        default_dispatch,
    )
    by_action = {a.action: a for a in report.applied}
    assert by_action["buildium_add_task_note"].ok is True  # the note actually wrote
    assert by_action["create_followup_task"].ok is False  # unwired → clean failure
    # The money-write never reached dispatch — it's in rejected, not applied.
    assert "buildium_record_payment" not in by_action
    assert any(r.action == "buildium_record_payment" for r in report.rejected)
    assert "note" in captured  # the note impl was the ONLY write invoked
