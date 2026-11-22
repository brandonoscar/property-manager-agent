"""Apply-orchestration for Tier-2 proposals (Inc.4b).

The load-bearing guarantee: a guard-rejected action NEVER reaches ``dispatch``
(the thing that writes). Plus per-action isolation and honest reporting. The
dispatch is a fake here — this is the pure orchestration, no real tools.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.codeexec.action_guard import MAX_PROPOSED_ACTIONS
from helixis.codeexec.apply import apply_proposed_actions


def _recording_dispatch(seen: list[str], *, fail: set[str] | None = None):
    fail = fail or set()

    async def _dispatch(action: str, args: dict) -> Any:
        seen.append(action)
        if action in fail:
            raise RuntimeError(f"{action} declined at its gate")
        return {"applied": action, "args": args}

    return _dispatch


@pytest.mark.asyncio
async def test_allowed_action_is_dispatched() -> None:
    seen: list[str] = []
    report = await apply_proposed_actions(
        [{"action": "create_followup_task", "args": {"title": "x"}}],
        _recording_dispatch(seen),
    )
    assert seen == ["create_followup_task"]
    assert report.applied[0].ok is True
    assert report.applied[0].outcome == {"applied": "create_followup_task", "args": {"title": "x"}}


@pytest.mark.asyncio
async def test_rejected_action_is_never_dispatched() -> None:
    seen: list[str] = []
    report = await apply_proposed_actions(
        [
            {"action": "buildium_record_payment", "args": {"amount": 500}},  # money — refused
            {"action": "buildium_close_work_order", "args": {}},  # irreversible — refused
            {"action": "add_internal_note", "args": {"note": "ok"}},  # allowed
        ],
        _recording_dispatch(seen),
    )
    # ONLY the allowed action reached the writer.
    assert seen == ["add_internal_note"]
    assert {r.action for r in report.rejected} == {
        "buildium_record_payment",
        "buildium_close_work_order",
    }


@pytest.mark.asyncio
async def test_per_action_failure_is_isolated() -> None:
    seen: list[str] = []
    report = await apply_proposed_actions(
        [
            {"action": "add_internal_note", "args": {}},
            {"action": "create_followup_task", "args": {}},  # this one "declines"
            {"action": "send_task_to_teammate", "args": {}},
        ],
        _recording_dispatch(seen, fail={"create_followup_task"}),
    )
    # All three were attempted (isolation — the failure didn't abort the rest).
    assert seen == ["add_internal_note", "create_followup_task", "send_task_to_teammate"]
    by_action = {a.action: a for a in report.applied}
    assert by_action["add_internal_note"].ok is True
    assert by_action["create_followup_task"].ok is False
    assert "declined" in (by_action["create_followup_task"].error or "")
    assert by_action["send_task_to_teammate"].ok is True


@pytest.mark.asyncio
async def test_bulk_overflow_is_not_dispatched() -> None:
    seen: list[str] = []
    actions = [{"action": "add_internal_note", "args": {}}] * (MAX_PROPOSED_ACTIONS + 3)
    report = await apply_proposed_actions(actions, _recording_dispatch(seen))
    assert len(seen) == MAX_PROPOSED_ACTIONS  # only up to the cap reached dispatch
    assert report.truncated is True
    assert len(report.rejected) == 3


@pytest.mark.asyncio
async def test_report_serialises() -> None:
    seen: list[str] = []
    report = await apply_proposed_actions(
        [
            {"action": "add_internal_note", "args": {}},
            {"action": "buildium_post_charge", "args": {}},  # refused
        ],
        _recording_dispatch(seen),
    )
    d = report.to_dict()
    assert d["applied"][0]["action"] == "add_internal_note"
    assert d["applied"][0]["ok"] is True
    assert d["rejected"][0]["action"] == "buildium_post_charge"
    assert "money" in d["rejected"][0]["reason"]
