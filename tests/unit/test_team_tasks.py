"""Teammate task requests (multiplayer PR B).

Pins the deterministic core (payload/dedup identity, EnrichedEvent-shape
enrichment, floor surface), the pipeline wiring (inbox allowlist +
enrichment registry), the roster resolution (never guesses between
matches), and the tool's confirm-gate discipline (the enqueue is
unreachable without an explicit approval).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agents.team_task_tools import (
    member_label,
    resolve_teammate,
    send_task_to_teammate,
)
from helixis.db.repositories.members import Member
from helixis.webhooks.event_schemas import dedup_key
from helixis.webhooks.inbox_filter import surfaces_in_inbox
from helixis.webhooks.team_tasks import (
    TEAM_TASK_EVENT_NAME,
    build_team_task_action_surface,
    build_team_task_enrichment,
    build_team_task_payload,
    enqueue_team_task,
)

COMPANY = "11111111-1111-1111-1111-111111111111"


def _member(user_id: str, name: str | None, email: str | None, role: str = "member") -> Member:
    return Member(
        user_id=user_id, email=email, display_name=name, role=role, is_primary_admin=False
    )


ROSTER = [
    _member("u-brandon", "Brandon Oscar", "brandon@example.com", role="admin"),
    _member("u-alex", "Alex Rivera", "alex@example.com"),
    _member("u-alexa", "Alexa Chen", "alexa@example.com"),
    _member("u-noname", None, "jordan@example.com"),
]


# ── deterministic core ───────────────────────────────────────────────


def _payload(request_id: str) -> dict:
    return build_team_task_payload(
        request_id=request_id,
        from_user_id="u-brandon",
        from_name="Brandon Oscar",
        to_user_id="u-alex",
        to_name="Alex Rivera",
        title="Review the COI",
        created_at="2026-07-24T12:00:00+00:00",
    )


def test_two_tasks_in_the_same_second_never_collide_on_dedup() -> None:
    # The email-ingest trap: account+event+second alone collide. The request
    # UUID rides MessageId (the documented non-Buildium string key), so two
    # same-second tasks get distinct dedup keys.
    a, b = dedup_key(_payload("req-1")), dedup_key(_payload("req-2"))
    assert a != b
    assert "MessageId=req-1" in a


def test_enrichment_validates_as_enriched_event() -> None:
    enrichment = build_team_task_enrichment(
        request_id="req-1",
        company_id=COMPANY,
        from_user_id="u-brandon",
        from_name="Brandon Oscar",
        to_user_id="u-alex",
        to_name="Alex Rivera",
        title="Review the COI",
        details="Vendor 4821's certificate expires Friday — check the renewal.",
        created_at="2026-07-24T12:00:00+00:00",
    )
    event = EnrichedEvent.model_validate(enrichment)  # the worker's reconstruction
    assert event.task.title == "Review the COI"
    assert "expires Friday" in (event.task.description or "")
    # The SENDER is the requester (the planner frames "who's asking").
    assert event.task.requested_by_first_name == "Brandon"
    assert event.task.raw["for_user_id"] == "u-alex"


def test_floor_surface_carries_who_asked_whom_for_what() -> None:
    surface = build_team_task_action_surface(
        from_name="Brandon Oscar",
        to_name="Alex Rivera",
        title="Review the COI",
        details="Full details here.",
    )
    assert surface["kind"] == "team_task"
    assert "Brandon Oscar" in surface["summary"]
    assert "Alex Rivera" in surface["summary"]
    assert surface["team_task"]["details"] == "Full details here."
    assert surface["signals"] == []  # floor: planner fills these later


# ── pipeline wiring ──────────────────────────────────────────────────


def test_task_requests_surface_in_the_inbox() -> None:
    assert surfaces_in_inbox(TEAM_TASK_EVENT_NAME)


def test_enrichment_registry_has_a_handler() -> None:
    from helixis.webhooks.handlers.enrichment_registry import get_enrichment_handler

    assert get_enrichment_handler(TEAM_TASK_EVENT_NAME) is not None


def test_enqueue_stores_enrichment_and_floor_surface() -> None:
    enq = AsyncMock(return_value=MagicMock(duplicate=False, id=42))
    store = AsyncMock()
    with (
        patch("helixis.webhooks.team_tasks.enqueue", new=enq),
        patch("helixis.webhooks.team_tasks.store_enrichment", new=store),
    ):
        sent = asyncio.run(
            enqueue_team_task(
                MagicMock(),
                company_id=COMPANY,
                from_user_id="u-brandon",
                from_name="Brandon Oscar",
                to_user_id="u-alex",
                to_name="Alex Rivera",
                title="Review the COI",
                details="details",
            )
        )
    assert sent is True
    assert enq.await_args.kwargs["event_name"] == TEAM_TASK_EVENT_NAME
    assert enq.await_args.kwargs["company_id"] == COMPANY
    store.assert_awaited_once()
    assert store.await_args.args[1] == 42  # the new row's id
    assert store.await_args.kwargs["enrichment"]["task"]["title"] == "Review the COI"


def test_enqueue_duplicate_skips_enrichment() -> None:
    enq = AsyncMock(return_value=MagicMock(duplicate=True, id=None))
    store = AsyncMock()
    with (
        patch("helixis.webhooks.team_tasks.enqueue", new=enq),
        patch("helixis.webhooks.team_tasks.store_enrichment", new=store),
    ):
        sent = asyncio.run(
            enqueue_team_task(
                MagicMock(),
                company_id=COMPANY,
                from_user_id="u",
                from_name="U",
                to_user_id="v",
                to_name="V",
                title="t",
                details="d",
            )
        )
    assert sent is False
    store.assert_not_awaited()


# ── roster resolution — never guesses ────────────────────────────────


def test_exact_email_match_wins_outright() -> None:
    # "alex@example.com" substring-matches Alexa's address too; the exact
    # match must short-circuit to exactly one result.
    matches = resolve_teammate(ROSTER, "alex@example.com")
    assert [m.user_id for m in matches] == ["u-alex"]


def test_name_substring_match_is_case_insensitive() -> None:
    assert [m.user_id for m in resolve_teammate(ROSTER, "rivera")] == ["u-alex"]
    assert [m.user_id for m in resolve_teammate(ROSTER, "JORDAN")] == ["u-noname"]


def test_ambiguous_prefix_returns_all_matches() -> None:
    assert {m.user_id for m in resolve_teammate(ROSTER, "alex")} == {"u-alex", "u-alexa"}


def test_no_match_and_empty_query() -> None:
    assert resolve_teammate(ROSTER, "nobody") == []
    assert resolve_teammate(ROSTER, "  ") == []


def test_member_label_falls_back_name_then_email() -> None:
    assert member_label(ROSTER[0]) == "Brandon Oscar"
    assert member_label(ROSTER[3]) == "jordan@example.com"


# ── the tool — confirm-gate discipline ───────────────────────────────


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.company_id = COMPANY
    ctx.user_id = "u-brandon"
    return ctx


def _invoke(**patches: object) -> str:
    args = json.dumps(
        {"teammate": "Alex Rivera", "title": "Review the COI", "details": "Vendor 4821."}
    )
    with (
        patch(
            "helixis.agents.team_task_tools.current_memory_context",
            return_value=patches.get("ctx", _ctx()),
        ),
        patch("helixis.agents.team_task_tools.get_pool", return_value=MagicMock()),
        patch(
            "helixis.agents.team_task_tools.list_members",
            new=AsyncMock(return_value=patches.get("members", ROSTER)),
        ),
        patch(
            "helixis.agents.team_task_tools.confirm_or_abort",
            new=patches.get("confirm", AsyncMock(return_value=True)),
        ),
        patch(
            "helixis.agents.team_task_tools.enqueue_team_task",
            new=patches.get("enqueue", AsyncMock(return_value=True)),
        ),
    ):
        return asyncio.run(send_task_to_teammate.on_invoke_tool(MagicMock(), args))  # type: ignore[arg-type]


def test_approved_task_enqueues_with_resolved_identities() -> None:
    enqueue = AsyncMock(return_value=True)
    reply = _invoke(enqueue=enqueue)
    assert "Alex Rivera" in reply and "Inbox" in reply
    kwargs = enqueue.await_args.kwargs
    assert kwargs["to_user_id"] == "u-alex"
    assert kwargs["from_user_id"] == "u-brandon"
    assert kwargs["from_name"] == "Brandon Oscar"  # resolved from the roster


def test_denied_confirm_sends_nothing() -> None:
    enqueue = AsyncMock(return_value=True)
    reply = _invoke(confirm=AsyncMock(return_value=False), enqueue=enqueue)
    assert "Cancelled" in reply
    enqueue.assert_not_awaited()  # the gate is structural: no approval, no enqueue


def test_ambiguous_teammate_never_reaches_the_gate() -> None:
    confirm = AsyncMock(return_value=True)
    enqueue = AsyncMock(return_value=True)
    args = json.dumps({"teammate": "alex", "title": "t", "details": "d"})
    with (
        patch("helixis.agents.team_task_tools.current_memory_context", return_value=_ctx()),
        patch("helixis.agents.team_task_tools.get_pool", return_value=MagicMock()),
        patch("helixis.agents.team_task_tools.list_members", new=AsyncMock(return_value=ROSTER)),
        patch("helixis.agents.team_task_tools.confirm_or_abort", new=confirm),
        patch("helixis.agents.team_task_tools.enqueue_team_task", new=enqueue),
    ):
        reply = asyncio.run(send_task_to_teammate.on_invoke_tool(MagicMock(), args))  # type: ignore[arg-type]
    assert "more than one" in reply
    confirm.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_unknown_teammate_lists_the_roster() -> None:
    confirm = AsyncMock(return_value=True)
    args = json.dumps({"teammate": "zed", "title": "t", "details": "d"})
    with (
        patch("helixis.agents.team_task_tools.current_memory_context", return_value=_ctx()),
        patch("helixis.agents.team_task_tools.get_pool", return_value=MagicMock()),
        patch("helixis.agents.team_task_tools.list_members", new=AsyncMock(return_value=ROSTER)),
        patch("helixis.agents.team_task_tools.confirm_or_abort", new=confirm),
    ):
        reply = asyncio.run(send_task_to_teammate.on_invoke_tool(MagicMock(), args))  # type: ignore[arg-type]
    assert "couldn't find" in reply and "Brandon Oscar" in reply
    confirm.assert_not_awaited()


def test_no_company_context_degrades_cleanly() -> None:
    args = json.dumps({"teammate": "alex", "title": "t", "details": "d"})
    with patch("helixis.agents.team_task_tools.current_memory_context", return_value=None):
        reply = asyncio.run(send_task_to_teammate.on_invoke_tool(MagicMock(), args))  # type: ignore[arg-type]
    assert "can't send tasks" in reply
