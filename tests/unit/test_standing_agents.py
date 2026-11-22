"""Per-person standing agents v1 — due-selection + delivery mapping.

Pure cores and mocked seams only (no live LLM): the due-predicate's
calendar semantics, the claim's atomicity contract, the Inbox delivery
mapping onto the team-task pipeline, and the cycle's per-task failure
containment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.webhooks import standing_agents as sa

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"


def _task(**over: object) -> sa.StandingTask:
    base: dict = dict(
        id=1,
        company_id=COMPANY,
        user_id=USER,
        title="Morning digest",
        prompt="Summarize anything needing my attention today.",
        hour_utc=12,
        last_run_at=None,
    )
    base.update(over)
    return sa.StandingTask(**base)


def _at(hour: int, day: int = 20) -> datetime:
    return datetime(2026, 7, day, hour, 30, tzinfo=UTC)


# ── is_due: pure calendar semantics ──────────────────────────────────


def test_not_due_before_its_hour() -> None:
    assert sa.is_due(_task(hour_utc=12), _at(hour=11)) is False


def test_due_at_or_after_its_hour_when_never_run() -> None:
    assert sa.is_due(_task(hour_utc=12), _at(hour=12)) is True
    assert sa.is_due(_task(hour_utc=12), _at(hour=23)) is True


def test_not_due_again_same_day() -> None:
    ran_today = _task(last_run_at=_at(hour=12))
    assert sa.is_due(ran_today, _at(hour=18)) is False


def test_due_again_next_day_after_hour() -> None:
    ran_yesterday = _task(last_run_at=_at(hour=12, day=19))
    assert sa.is_due(ran_yesterday, _at(hour=12, day=20)) is True
    assert sa.is_due(ran_yesterday, _at(hour=11, day=20)) is False  # hour not reached


def test_flag_gate_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELIXIS_STANDING_AGENTS_ENABLED", raising=False)
    assert sa.standing_agents_enabled() is False
    monkeypatch.setenv("HELIXIS_STANDING_AGENTS_ENABLED", "true")
    assert sa.standing_agents_enabled() is True


# ── delivery mapping: rides the team-task pipeline ───────────────────


def test_deliver_result_maps_onto_team_task_pipeline() -> None:
    enqueue = AsyncMock(return_value=True)
    member = MagicMock()
    member.user_id = USER
    with (
        patch.object(sa, "enqueue_team_task", enqueue),
        patch.object(sa, "list_members", AsyncMock(return_value=[member])),
        patch.object(sa, "member_label", lambda _m: "Brandon Oscar"),
    ):
        ok = asyncio.run(sa.deliver_result(MagicMock(), _task(), "Here's your digest."))
    assert ok is True
    kwargs = enqueue.await_args.kwargs
    assert kwargs["company_id"] == COMPANY
    assert kwargs["to_user_id"] == USER
    assert kwargs["to_name"] == "Brandon Oscar"
    assert kwargs["from_user_id"] == "standing-agent:1"
    assert "Morning digest" in kwargs["from_name"]
    assert kwargs["title"] == "Morning digest"
    assert kwargs["details"] == "Here's your digest."


def test_deliver_result_tolerates_roster_failure() -> None:
    # Display-name lookup is cosmetic — a members failure must not stop
    # delivery.
    enqueue = AsyncMock(return_value=True)
    with (
        patch.object(sa, "enqueue_team_task", enqueue),
        patch.object(sa, "list_members", AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        ok = asyncio.run(sa.deliver_result(MagicMock(), _task(), "text"))
    assert ok is True
    assert enqueue.await_args.kwargs["to_name"].startswith("user ")


# ── cycle: claim-before-run + per-task containment ───────────────────


def test_cycle_skips_unclaimed_and_contains_failures() -> None:
    t1, t2, t3 = _task(id=1), _task(id=2), _task(id=3)
    run = AsyncMock(side_effect=[RuntimeError("boom"), True])
    with (
        patch.object(sa, "fetch_due_tasks", AsyncMock(return_value=[t1, t2, t3])),
        # t1 loses the claim race; t2 claims but its run explodes; t3 delivers.
        patch.object(sa, "claim_task", AsyncMock(side_effect=[False, True, True])),
        patch.object(sa, "run_standing_task", run),
    ):
        delivered = asyncio.run(sa.run_standing_cycle(MagicMock(), MagicMock(), now=_at(hour=13)))
    assert delivered == 1  # only t3 — t1 skipped, t2's failure contained
    assert run.await_count == 2  # t1 never ran (claim lost)
