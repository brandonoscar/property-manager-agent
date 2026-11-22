"""GET /api/v1/buildium/events/{event_id}/thread/stream (PR #70).

The endpoint replays a row's persisted ``action_surface`` as a sequence
of conversational events on a bus session, then opens an SSE stream
backed by that session. Tests assert:

1. Auth required.
2. 404 for an event the user's company doesn't own.
3. 404 for ``buildium_events`` table missing (defensive degrade).
4. Happy path: the SSE stream contains the expected replay event
   types (``assistant_message_start`` → token → end, then one
   ``suggestion_chip`` per action).
5. With ``executed_action_index`` set, the replay includes a closing
   ``assistant_message_end`` confirmation turn.

We don't test the live publish/subscribe path (writes pushing live
events to a subscribed client) — that's covered by the integration
shape in the InboxPage E2E once the frontend wiring lands.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
EVENT_ID = "321d5883-5844-4cc8-9c0d-65b9c0bccf2a"
USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
    return jwt.encode(
        {
            "sub": USER_ID,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


_ACTION_SURFACE = {
    "schema_version": 2,
    "summary": "Tenant Mei Chen reported a kitchen sink leak at unit 3B.",
    "actions": [
        {
            "tool_id": "update_buildium_task",
            "label": "Set priority to High",
            "intent": "Raise priority to High",
            "parameters": {"priority": "High"},
        },
        {
            "tool_id": "add_internal_note",
            "label": "Note the recurrence",
            "intent": "Note the recurring complaint",
            "parameters": {"note_body": "Third plumbing report in 90 days."},
        },
    ],
    "freeform_placeholder": "e.g. 'send vendor with parts'",
}


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _build_pool(
    *,
    row_present: bool = True,
    company_owns_row: bool = True,
    table_missing: bool = False,
    executed_action_index: int | None = None,
) -> Any:
    pool = MagicMock()

    async def fetchrow(_sql: str, *_args: Any) -> dict[str, Any] | None:
        if table_missing:
            raise asyncpg.exceptions.UndefinedTableError("relation does not exist")
        if not row_present:
            return None
        return (
            {
                "id": EVENT_ID,
                "event_name": "Task.Created",
                "action_surface": _ACTION_SURFACE,
                "enrichment": None,
                "executed_action_index": executed_action_index,
                # company_id check is enforced by the WHERE clause in the
                # endpoint, so when company_owns_row=False we just return
                # None to mimic "not found".
                **({"company_id": COMPANY_ID} if company_owns_row else {}),
            }
            if company_owns_row
            else None
        )

    pool.fetchrow = AsyncMock(side_effect=fetchrow)

    @asynccontextmanager
    async def _acquire():
        yield MagicMock()

    pool.acquire = _acquire
    return pool


def _make_client(test_settings: Settings, pool: Any) -> TestClient:
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    patches = [
        patch("helixis.api.routes.buildium.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ]
    for p in patches:
        p.start()
    app = create_app(test_settings)
    client = TestClient(app)

    # Tear-down patches when the client is closed.
    original_close = client.close

    def close_with_patches() -> None:
        for p in reversed(patches):
            p.stop()
        original_close()

    client.close = close_with_patches  # type: ignore[method-assign]
    return client


def _stream_until_done(text: str) -> list[dict[str, Any]]:
    """Parse an SSE response body into a list of event dicts.

    The fake bus emits no [DONE] marker on its own — tests close the
    stream when they've consumed enough. We just pluck JSON lines.
    """
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            break
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def test_thread_stream_requires_auth(test_settings: Settings) -> None:
    pool = _build_pool()
    client = _make_client(test_settings, pool)
    try:
        resp = client.get(f"/api/v1/buildium/events/{EVENT_ID}/thread/stream")
        assert resp.status_code in (401, 403)
    finally:
        client.close()


def test_thread_stream_404_for_other_companys_event(test_settings: Settings) -> None:
    pool = _build_pool(company_owns_row=False)
    client = _make_client(test_settings, pool)
    try:
        resp = client.get(
            f"/api/v1/buildium/events/{EVENT_ID}/thread/stream",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert resp.status_code == 404
    finally:
        client.close()


def test_thread_stream_404_when_table_missing(test_settings: Settings) -> None:
    pool = _build_pool(table_missing=True)
    client = _make_client(test_settings, pool)
    try:
        resp = client.get(
            f"/api/v1/buildium/events/{EVENT_ID}/thread/stream",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert resp.status_code == 404
    finally:
        client.close()


async def _drain_bus(bus: Any, session_id: str, expected: int) -> list[Any]:
    """Pull up to ``expected`` events from the bus session for
    assertions. Bounded so the test can't hang if the production code
    publishes fewer events than expected — the test fails with a clear
    "got 4, wanted 5" rather than timing out the suite.
    """
    drained: list[Any] = []
    async for event in bus.subscribe(session_id):
        drained.append(event)
        if len(drained) >= expected:
            break
    return drained


@pytest.mark.asyncio
async def test_replay_emits_chips_without_summary_turn() -> None:
    """Bypass the HTTP layer — call the replay function directly and
    assert against the bus's queued events.

    PR #127 — the replay USED to emit ``action_surface.summary`` as
    the opening assistant turn here. The inbox detail pane now shows
    that summary inside the EventHeader card (SummarySection), so
    re-emitting it as a chat bubble produced an "orphan paragraph"
    below the workflow card when the dedupe didn't catch it (e.g.
    the SSE turn carried the deterministic floor text, then the
    planner overwrote action_surface.summary with its narrative,
    and the streamed turn outlived the row update). The chips are
    still emitted; only the redundant summary turn is gone.
    """
    from helixis.api.routes.buildium import _replay_thread_into_bus
    from helixis.streaming.bus import EventBus

    bus = EventBus()
    session_id = "test-session-replay"
    bus.create_session(session_id)

    await _replay_thread_into_bus(
        bus,
        session_id,
        event_name="Task.Created",
        action_surface=_ACTION_SURFACE,
        enrichment=None,
        executed_action_index=None,
    )

    drained = await _drain_bus(bus, session_id, expected=2)
    types = [e.type for e in drained]
    # 2 chips, no leading summary turn.
    assert types == ["suggestion_chip", "suggestion_chip"]
    chip_labels = [e.data["label"] for e in drained]
    assert chip_labels == ["Set priority to High", "Note the recurrence"]
    # action_index is the position in the actions array — the executor
    # endpoints rely on this 1:1 mapping.
    assert [e.data["action_index"] for e in drained] == [0, 1]


@pytest.mark.asyncio
async def test_replay_with_executed_action_index_appends_closing_turn() -> None:
    """PR #127 — same as above (no opening summary turn), but the
    closing "Done — …" turn for an executed action still emits so
    the user knows the action completed on a fresh subscribe."""
    from helixis.api.routes.buildium import _replay_thread_into_bus
    from helixis.streaming.bus import EventBus

    bus = EventBus()
    session_id = "test-session-executed"
    bus.create_session(session_id)

    await _replay_thread_into_bus(
        bus,
        session_id,
        event_name="Task.Created",
        action_surface=_ACTION_SURFACE,
        enrichment=None,
        executed_action_index=0,
    )

    drained = await _drain_bus(bus, session_id, expected=5)
    types = [e.type for e in drained]
    # No opening summary turn; 2 chips; closing "Done — …" turn.
    assert types == [
        "suggestion_chip",
        "suggestion_chip",
        "assistant_message_start",
        "assistant_message_token",
        "assistant_message_end",
    ]
    closing_text = drained[3].data["content"]
    assert closing_text.startswith("Done — ")
    # The closing turn references the executed action's intent.
    assert _ACTION_SURFACE["actions"][0]["intent"] in closing_text


@pytest.mark.asyncio
async def test_replay_without_action_surface_emits_narration_placeholder() -> None:
    """When the row hasn't been enriched yet, the replay emits a single
    narration line instead of leaving the thread empty. The frontend
    renders it as italic text so the user knows we're still working."""
    from helixis.api.routes.buildium import _replay_thread_into_bus
    from helixis.streaming.bus import EventBus

    bus = EventBus()
    session_id = "test-session-pending"
    bus.create_session(session_id)

    await _replay_thread_into_bus(
        bus,
        session_id,
        event_name="Task.Created",
        action_surface=None,
        enrichment=None,
        executed_action_index=None,
    )
    drained = await _drain_bus(bus, session_id, expected=1)
    assert drained[0].type == "narration"
    assert "Task.Created" in drained[0].data["content"]
