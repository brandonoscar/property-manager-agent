"""Tests for Phase A.2 Inbox endpoints:

* ``GET  /api/v1/buildium/recent-events?include=action_surface,enrichment``
* ``GET  /api/v1/buildium/events/{event_id}``
* ``POST /api/v1/buildium/events/{event_id}/actions/{action_index}/execute``
* ``POST /api/v1/buildium/events/{event_id}/freeform``

The fake asyncpg pool from
``test_buildium_routes.py`` stays unchanged but a single new fake
``execute_pool`` here matches the row-shape these endpoints need
(action_surface + enrichment + execution columns).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

# The /execute body runs through the durable idempotency guard (#224); inject an
# in-memory store so these end-to-end tests don't need asyncpg.
pytestmark = pytest.mark.usefixtures("execute_idempotency_store")


@pytest.fixture(autouse=True)
def _manager_role_floor():
    """Write approval/execution now takes manager+ (approval-pipeline PR).
    These tests exercise the routes' own behavior, so satisfy the role gate;
    the floor itself is covered in test_agent_ownership."""
    with patch("helixis.api.routes.company.require_role", new=AsyncMock(return_value="manager")):
        yield


TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
TEST_DATA_KEY = Fernet.generate_key().decode()
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
EVENT_ID = "321d5883-5844-4cc8-9c0d-65b9c0bccf2a"


def _make_token(sub: str = "user-1") -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "test@example.com",
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
    "summary": (
        "Tenant Josh Gil in Unit 1 at 1534 Cheat Lake Road reported an "
        "active leak under the kitchen sink."
    ),
    "actions": [
        {
            "label": "Set priority to High",
            "intent": "Raise priority to High",
            "tool_id": "update_buildium_task",
            "parameters": {"priority": "High", "category": "Plumbing"},
        },
        {
            "label": "Schedule plumbing inspection in 30 days",
            "intent": "Schedule plumbing inspection in 30 days",
            "tool_id": "create_followup_task",
            "parameters": {
                "title": "Follow-up Plumbing Inspection",
                "due_date": "2026-06-13",
                "description": "Third plumbing report in 90 days.",
                "priority": "Normal",
            },
        },
    ],
    "freeform_placeholder": "e.g. 'Call preferred plumber directly'",
}

_ENRICHMENT = {
    "task": {
        "id": 11154,
        "unit_id": 58280,
        "property_id": 31,
        "assigned_to_user_id": 77042,
        "title": "Hello World",
        "raw": {},
    },
    "active_lease": {
        "id": 45104,
        "tenants": [{"id": 82336, "first_name": "Josh", "last_name": "Gil"}],
    },
}


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        data_key=TEST_DATA_KEY,
        memory={"enabled": False},
    )


@pytest.fixture
def fake_pool():
    """Pool stand-in that holds one event row + one creds row.

    Tests mutate ``state['rows'][event_id]`` to flip execution flags
    or to seed an event without an action_surface.
    """
    state: dict[str, dict[str, Any]] = {
        "rows": {
            EVENT_ID: {
                "id": EVENT_ID,
                "event_name": "Task.Created",
                "event_time": datetime(2026, 5, 13, 23, 39, 58, tzinfo=UTC),
                "status": "done",
                "last_error": None,
                "account_id": 652932,
                "company_id": COMPANY_ID,
                "action_surface": _ACTION_SURFACE,
                "enrichment": _ENRICHMENT,
                "executed_action_index": None,
                "executed_at": None,
                "executed_by_user_id": None,
                "execution_result": None,
            }
        },
        "creds": {
            COMPANY_ID: {
                "company_id": COMPANY_ID,
                "encrypted_client_id": Fernet(TEST_DATA_KEY.encode())
                .encrypt(b"client-id-plain")
                .decode(),
                "encrypted_client_secret": Fernet(TEST_DATA_KEY.encode())
                .encrypt(b"client-secret-plain")
                .decode(),
                "client_id_last4": "ain",
                "encrypted_webhook_secret": None,
                "webhook_secret_last4": None,
                "account_id": 652932,
                "base_url": "https://apisandbox.buildium.com",
                "last_tested_at": None,
            }
        },
        "activity_log_rows": [],
    }

    pool = MagicMock()

    async def fetch(_sql: str, *args: Any) -> list[dict[str, Any]]:
        # Only used by recent-events; matches by company_id.
        company_id = args[0]
        rows = [r for r in state["rows"].values() if r["company_id"] == company_id]
        # Inbox path (include=action_surface) passes a 3rd arg: the
        # array of LIKE patterns (e.g. ``Task.%``) for the allowlisted
        # event families. Emulate ``event_name LIKE ANY(...)`` with a
        # prefix match (every pattern is ``<prefix>%``).
        if len(args) > 2 and args[2]:
            prefixes = tuple(p[:-1] for p in args[2])
            rows = [r for r in rows if r["event_name"].startswith(prefixes)]
        rows.sort(key=lambda r: r["event_time"], reverse=True)
        limit = args[1] if len(args) > 1 else 50
        return rows[:limit]

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
        if "FROM buildium_events" in normalized:
            # PR #94 — handlers now declare ``event_id: UUID`` so the
            # SQL arg arrives as a ``uuid.UUID`` object, not a string.
            # ``state["rows"]`` is keyed by the canonical UUID string,
            # so coerce here to keep both legacy str-arg paths and the
            # new UUID-arg path working in the same fixture.
            event_id = str(args[0])
            row = state["rows"].get(event_id)
            if row is None or row["company_id"] != args[1]:
                return None
            return row
        if "FROM public.company_buildium_credentials" in normalized:
            return state["creds"].get(args[0])
        raise AssertionError(f"unexpected fetchrow SQL: {normalized[:100]}")

    async def execute(sql: str, *args: Any) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE buildium_events SET executed_action_index"):
            event_id = str(args[0])  # PR #94 — UUID-aware lookup
            row = state["rows"].get(event_id)
            if row is not None:
                row["executed_action_index"] = args[1]
                row["executed_by_user_id"] = args[2]
                row["executed_at"] = datetime.now(UTC)
                # args[3] is jsonb-serialized result; not parsing here.
                row["execution_result"] = args[3]
        elif normalized.startswith("INSERT INTO helixis_activity_log"):
            state["activity_log_rows"].append(args)
        else:
            # Other UPDATEs (credentials / webhook secret) — no-op for these tests.
            return None

    pool.fetch = AsyncMock(side_effect=fetch)
    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.execute = AsyncMock(side_effect=execute)

    # ``write_activity`` (and a handful of other callers) acquire a
    # connection via ``async with pool.acquire() as conn`` and run
    # ``await conn.execute(...)`` on it. Bridge that path back into the
    # same ``execute`` side-effect so activity-log inserts hit our state.
    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock(side_effect=execute)

    @asynccontextmanager
    async def acquire():
        yield fake_conn

    pool.acquire = acquire
    pool._state = state
    return pool


@pytest.fixture
def client(test_settings, fake_pool):
    # Clear the connectors_limiter so concurrent test files don't
    # starve each other.
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    with (
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_pool", return_value=fake_pool),
        patch(
            "helixis.api.auth._resolve_company_ids",
            new=AsyncMock(return_value=[COMPANY_ID]),
        ),
    ):
        app = create_app(test_settings)
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)

        @asynccontextmanager
        async def noop_lifespan(_app):
            yield

        app.router.lifespan_context = noop_lifespan

        with TestClient(app) as c:
            yield c


# ── GET /recent-events ?include= ────────────────────────────────


def test_recent_events_skinny_shape_omits_action_surface(client):
    """Default (no ?include=) preserves the pre-A.2 shape — important
    because the Webhooks page and connector detail panel call this
    endpoint without the param and shouldn't pay the jsonb payload
    cost on every poll."""
    resp = client.get(
        "/api/v1/buildium/recent-events",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "action_surface" not in body[0] or body[0]["action_surface"] is None
    assert body[0]["id"] == EVENT_ID


def test_recent_events_with_include_returns_action_surface(client):
    resp = client.get(
        "/api/v1/buildium/recent-events?include=action_surface,enrichment",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["action_surface"]["actions"][0]["tool_id"] == "update_buildium_task"
    assert body[0]["enrichment"]["task"]["id"] == 11154


def _seed_event(fake_pool, event_id: str, event_name: str) -> str:
    """Seed a bare event row (no surface) under the test company."""
    fake_pool._state["rows"][event_id] = {
        "id": event_id,
        "event_name": event_name,
        "event_time": datetime(2026, 6, 2, 9, 25, 27, tzinfo=UTC),
        "status": "done",
        "last_error": None,
        "account_id": 654170,
        "company_id": COMPANY_ID,
        "action_surface": None,
        "enrichment": None,
        "executed_action_index": None,
        "executed_at": None,
        "executed_by_user_id": None,
        "execution_result": None,
    }
    return event_id


_LEDGER_ID = "9f0e1d2c-3b4a-5968-7766-554433221100"
_PAYMENT_ID = "9f0e1d2c-3b4a-5968-7766-554433221101"
_LEASE_ID = "9f0e1d2c-3b4a-5968-7766-554433221102"


def test_inbox_hides_non_allowlisted_events(client, fake_pool):
    """Only allowlisted, actionable events reach the Inbox. Owner-ledger
    bookkeeping and rent-payment transactions must not — a Buildium
    owner-charge billing run was flooding the General lane with bare
    "assigned to the admin" cards."""
    _seed_event(fake_pool, _LEDGER_ID, "OwnershipAccountTransaction.Created")
    # ``LeaseTransaction.*`` must NOT leak through the ``Lease.%`` pattern.
    _seed_event(fake_pool, _PAYMENT_ID, "LeaseTransaction.Created")
    resp = client.get(
        "/api/v1/buildium/recent-events?include=action_surface,enrichment",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert _LEDGER_ID not in ids
    assert _PAYMENT_ID not in ids
    assert EVENT_ID in ids  # Task.Created — allowlisted


def test_inbox_shows_allowlisted_leasing_event(client, fake_pool):
    """Leasing-pipeline milestones (new leases) are allowlisted and show."""
    _seed_event(fake_pool, _LEASE_ID, "Lease.Created")
    resp = client.get(
        "/api/v1/buildium/recent-events?include=action_surface,enrichment",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert _LEASE_ID in ids


def test_skinny_shape_still_lists_all_events_for_debug(client, fake_pool):
    """The connector's webhook-delivery debug panel calls recent-events
    WITHOUT includes and must keep showing every delivery, allowlisted
    or not."""
    _seed_event(fake_pool, _LEDGER_ID, "OwnershipAccountTransaction.Created")
    resp = client.get(
        "/api/v1/buildium/recent-events",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert _LEDGER_ID in ids


# ── GET /events/{event_id} ──────────────────────────────────────


def test_get_event_returns_full_row(client):
    resp = client.get(
        f"/api/v1/buildium/events/{EVENT_ID}",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == EVENT_ID
    assert body["action_surface"]["summary"].startswith("Tenant Josh Gil")
    assert body["executed_action_index"] is None


def test_get_event_404_for_wrong_company(client, fake_pool):
    # Mutate the row's company_id so the WHERE clause excludes it.
    fake_pool._state["rows"][EVENT_ID]["company_id"] = "another-company-uuid"
    resp = client.get(
        f"/api/v1/buildium/events/{EVENT_ID}",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 404


# ── POST /events/.../execute ────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_action_runs_handler_and_marks_row_executed(client, fake_pool):
    # Patch the handler to a fake that captures its call.
    fake_handler = MagicMock()
    fake_handler.execute = AsyncMock(
        return_value={
            "buildium_id": 11154,
            "summary": "Updated task #11154 (Priority=High)",
        }
    )

    with patch(
        "helixis.api.routes.buildium.get_handler",
        return_value=fake_handler,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"parameter_overrides": {"priority": "Medium"}, "freeform_note": None},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "executed"
    assert body["result"]["buildium_id"] == 11154

    # Override merged into the call.
    fake_handler.execute.assert_awaited_once()
    merged_params = fake_handler.execute.await_args.args[1]
    assert merged_params["priority"] == "Medium"
    assert merged_params["category"] == "Plumbing"
    # Context populated from enrichment.
    ctx = fake_handler.execute.await_args.kwargs["context"]
    assert ctx["event_task_id"] == 11154
    assert ctx["event_unit_id"] == 58280
    assert ctx["assigned_to_user_id"] == 77042

    # Row marked executed.
    row = fake_pool._state["rows"][EVENT_ID]
    assert row["executed_action_index"] == 0
    assert row["executed_by_user_id"] == "user-1"

    # Activity log row written.
    assert len(fake_pool._state["activity_log_rows"]) == 1


def test_execute_409_when_already_executed(client, fake_pool):
    fake_pool._state["rows"][EVENT_ID]["executed_action_index"] = 0
    resp = client.post(
        f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"parameter_overrides": None, "freeform_note": None},
    )
    assert resp.status_code == 409


def test_execute_400_when_action_index_out_of_bounds(client):
    resp = client.post(
        f"/api/v1/buildium/events/{EVENT_ID}/actions/99/execute",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"parameter_overrides": None, "freeform_note": None},
    )
    assert resp.status_code == 400


def test_execute_404_when_event_missing(client):
    resp = client.post(
        "/api/v1/buildium/events/00000000-0000-0000-0000-000000000000/actions/0/execute",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"parameter_overrides": None, "freeform_note": None},
    )
    assert resp.status_code == 404


def test_execute_502_on_handler_error(client):
    from helixis.agent.handlers import HandlerError

    fake_handler = MagicMock()
    fake_handler.execute = AsyncMock(
        side_effect=HandlerError("Buildium rejected", tool_id="update_buildium_task")
    )
    with patch(
        "helixis.api.routes.buildium.get_handler",
        return_value=fake_handler,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"parameter_overrides": None, "freeform_note": None},
        )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["tool_id"] == "update_buildium_task"


# ── POST /events/.../freeform ──────────────────────────────────


def test_freeform_note_writes_activity_row(client, fake_pool):
    resp = client.post(
        f"/api/v1/buildium/events/{EVENT_ID}/freeform",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"note": "Call preferred plumber directly"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "recorded"}
    assert len(fake_pool._state["activity_log_rows"]) == 1


def test_freeform_note_invokes_agent_with_inbox_session(client, fake_pool):
    """PR #130 — the freeform endpoint now invokes the orchestrator
    agent so the user gets a real AI response back in the inbox
    conversation thread.

    Pin: the activity row STILL gets written (audit trail preserved),
    AND ``run_agent_streaming`` is kicked off in a background task
    with the inbox SSE session id (``webhook:{event_id}``) so the
    streamed response flows back through the existing thread/stream
    plumbing. Context derives from the row's enrichment so the agent
    doesn't have to re-fetch task/property anchors.
    """
    with (
        patch(
            "helixis.api.routes.buildium.run_agent_streaming",
            new_callable=AsyncMock,
        ),
        patch(
            "helixis.api.routes.buildium.asyncio.create_task",
            side_effect=lambda coro: coro.close() or MagicMock(),
        ) as mock_create_task,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/freeform",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"note": "Walk me through Send to Sarah first"},
        )
    assert resp.status_code == 200
    # Activity-log audit trail preserved.
    assert len(fake_pool._state["activity_log_rows"]) == 1
    # Agent invocation was kicked off as a background task. We don't
    # assert on run_agent_streaming directly (the coro is closed
    # without execution); instead pin that the create_task wrapper
    # was called exactly once.
    assert mock_create_task.called
    assert mock_create_task.call_count == 1


def test_freeform_note_runs_agent_on_client_session(client, fake_pool):
    """Regression (#64): the agent must publish to the client's LIVE inbox
    stream session (``thread:{uuid}`` passed in the body), NOT a hardcoded
    ``webhook:{event_id}`` the browser never subscribes to — otherwise the
    reply + any confirm_gate land on a dead channel (silent no-op; gated
    writes time out at 300s). Pins the run session == the client session."""
    with (
        patch(
            "helixis.api.routes.buildium.run_agent_streaming", new_callable=AsyncMock
        ) as mock_run,
        patch(
            "helixis.api.routes.buildium.asyncio.create_task",
            side_effect=lambda coro: coro.close() or MagicMock(),
        ),
        patch("helixis.api.routes.buildium.run_registry.claim", return_value=True),
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/freeform",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"note": "reply to the tenant", "session_id": "thread:abc-123"},
        )
    assert resp.status_code == 200
    assert mock_run.called
    assert mock_run.call_args.args[0].session_id == "thread:abc-123"


def test_freeform_note_falls_back_to_event_session(client, fake_pool):
    """A legacy caller that omits session_id still runs (falls back to the
    per-event session) — back-compat for the wiring change."""
    with (
        patch(
            "helixis.api.routes.buildium.run_agent_streaming", new_callable=AsyncMock
        ) as mock_run,
        patch(
            "helixis.api.routes.buildium.asyncio.create_task",
            side_effect=lambda coro: coro.close() or MagicMock(),
        ),
        patch("helixis.api.routes.buildium.run_registry.claim", return_value=True),
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/freeform",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"note": "anything"},
        )
    assert resp.status_code == 200
    assert mock_run.call_args.args[0].session_id == f"webhook:{EVENT_ID}"


def test_freeform_note_404_for_unknown_event(client):
    resp = client.post(
        "/api/v1/buildium/events/00000000-0000-0000-0000-000000000000/freeform",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"note": "anything"},
    )
    assert resp.status_code == 404


# ── a write to a PMS this workspace is not on (gotcha 183) ───────


def _on(pms: str):
    return patch("helixis.api.routes.buildium.company_pms", new=AsyncMock(return_value=pms))


def test_execute_refuses_a_buildium_write_on_a_rentvine_workspace(client, fake_pool):
    """Before: a 400 saying the company has no Buildium credentials, which a
    Rentvine workspace can never add (one PMS per company)."""
    handler = MagicMock()
    handler.execute = AsyncMock()
    with _on("rentvine"), patch("helixis.api.routes.buildium.get_handler", return_value=handler):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"parameter_overrides": None, "freeform_note": None},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"].endswith("Make this change in Rentvine.")
    handler.execute.assert_not_awaited()
    assert fake_pool._state["rows"][EVENT_ID]["executed_action_index"] is None


@pytest.mark.parametrize("path", ["recent-events?include=action_surface", f"events/{EVENT_ID}"])
def test_both_inbox_reads_mark_the_write_in_place(client, path):
    with _on("rentvine"):
        resp = client.get(
            f"/api/v1/buildium/{path}", headers={"Authorization": f"Bearer {_make_token()}"}
        )
    assert resp.status_code == 200
    body = resp.json()
    surface = (body[0] if isinstance(body, list) else body)["action_surface"]
    # Both Buildium writes are kept, in their stored order, and marked.
    tools = [a["tool_id"] for a in surface["actions"]]
    assert tools == ["update_buildium_task", "create_followup_task"]
    assert all("Rentvine" in a["unavailable_reason"] for a in surface["actions"])


@pytest.mark.parametrize("path", ["recent-events?include=action_surface", f"events/{EVENT_ID}"])
def test_a_buildium_workspace_sees_its_actions_unmarked(client, path):
    with _on("buildium"):
        resp = client.get(
            f"/api/v1/buildium/{path}", headers={"Authorization": f"Bearer {_make_token()}"}
        )
    body = resp.json()
    surface = (body[0] if isinstance(body, list) else body)["action_surface"]
    assert "unavailable_reason" not in surface["actions"][0]
