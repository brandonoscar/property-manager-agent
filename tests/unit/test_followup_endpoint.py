"""Tests for ``POST /buildium/events/{id}/actions/{i}/followup`` and
for the Phase A.2.5 chain-aware ``/execute`` body.

We reuse the fake-pool pattern from ``test_inbox_endpoints.py`` so
the tests stand on their own without hitting Supabase or Buildium.
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
    """Write execution now takes manager+ (approval-pipeline PR); satisfy the
    gate — the floor itself is covered in test_agent_ownership."""
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


_ACTION_SURFACE_V2 = {
    "schema_version": 2,
    "summary": "Tenant Josh Gil in Unit 1 reported a kitchen sink leak.",
    "actions": [
        {
            "tool_id": "send_tenant_message",
            "label": "Message Josh",
            "intent": "Send message to Josh confirming plumber dispatch",
            "parameters": {
                "tenant_id": 82336,
                "subject": "Plumber dispatched",
                "body": "Hi Josh — a plumber is on the way.",
            },
            "first_followup": {
                "prompt": "What tone should the message take?",
                "options": [
                    {
                        "label": "Reassuring & detailed",
                        "parameter_patch": {
                            "body": "Hi Josh, sorry about the leak. Plumber ETA 2hrs."
                        },
                        "needs_followup": False,
                        "followup_hint": None,
                    },
                    {
                        "label": "Quick confirmation",
                        "parameter_patch": {"body": "Plumber on the way."},
                        "needs_followup": False,
                        "followup_hint": None,
                    },
                ],
                "allow_freeform": True,
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
    state: dict[str, dict[str, Any]] = {
        "rows": {
            EVENT_ID: {
                "id": EVENT_ID,
                "event_name": "Task.Created",
                "event_time": datetime(2026, 5, 14, 23, 39, 58, tzinfo=UTC),
                "status": "done",
                "last_error": None,
                "account_id": 652932,
                "company_id": COMPANY_ID,
                "action_surface": _ACTION_SURFACE_V2,
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
        rows = [r for r in state["rows"].values() if r["company_id"] == args[0]]
        rows.sort(key=lambda r: r["event_time"], reverse=True)
        return rows[: args[1] if len(args) > 1 else 50]

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(sql.split())
        if "FROM buildium_events" in normalized:
            # PR #94 — event_id arrives as ``uuid.UUID`` now (path-param
            # type-constraint), so stringify before lookup against the
            # string-keyed state dict.
            row = state["rows"].get(str(args[0]))
            if row is None or row["company_id"] != args[1]:
                return None
            return row
        if "FROM public.company_buildium_credentials" in normalized:
            return state["creds"].get(args[0])
        raise AssertionError(f"unexpected fetchrow SQL: {normalized[:120]}")

    async def execute(sql: str, *args: Any) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE buildium_events SET executed_action_index"):
            row = state["rows"].get(str(args[0]))  # PR #94 — UUID-aware
            if row is not None:
                row["executed_action_index"] = args[1]
                row["executed_by_user_id"] = args[2]
                row["executed_at"] = datetime.now(UTC)
                row["execution_result"] = args[3]
        elif normalized.startswith("INSERT INTO helixis_activity_log"):
            state["activity_log_rows"].append(args)

    pool.fetch = AsyncMock(side_effect=fetch)
    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.execute = AsyncMock(side_effect=execute)
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


# ── POST /followup ───────────────────────────────────────────────


def test_followup_happy_path_returns_next_question(client):
    """Planner returns a FollowupQuestion; endpoint surfaces it as
    JSON the frontend can render."""
    from helixis.agent.planner import FollowupQuestion

    fake_planner = MagicMock()
    fake_planner.followup = AsyncMock(
        return_value=FollowupQuestion.model_validate(
            {
                "prompt": "Should we also schedule the inspection?",
                "options": [
                    {"label": "Yes", "parameter_patch": {}, "needs_followup": False},
                    {"label": "No", "parameter_patch": {}, "needs_followup": False},
                ],
                "allow_freeform": True,
            }
        )
    )
    with patch(
        "helixis.api.routes.buildium.ActionPlanner",
        return_value=fake_planner,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/followup",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"previous_picks": [], "freeform_hint": None},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "done" not in body
    assert body["prompt"].startswith("Should we also")
    assert len(body["options"]) == 2
    fake_planner.followup.assert_awaited_once()


def test_followup_terminal_returns_done_signal(client):
    """Planner returns a DoneSignal — endpoint surfaces it with the
    merged_parameters the frontend uses to populate the confirmation
    step."""
    from helixis.agent.planner import DoneSignal

    fake_planner = MagicMock()
    fake_planner.followup = AsyncMock(
        return_value=DoneSignal.model_validate(
            {
                "done": True,
                "merged_parameters": {
                    "tenant_id": 82336,
                    "subject": "Plumber on the way",
                    "body": "Hi Josh, sorry about the leak.",
                },
            }
        )
    )
    with patch(
        "helixis.api.routes.buildium.ActionPlanner",
        return_value=fake_planner,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/followup",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={
                "previous_picks": [
                    {
                        "prompt": "What tone?",
                        "label": "Reassuring",
                        "parameter_patch": {"body": "Hi Josh, sorry…"},
                    }
                ],
                "freeform_hint": None,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["done"] is True
    assert body["merged_parameters"]["tenant_id"] == 82336


def test_followup_502_when_planner_raises(client):
    from helixis.agent.planner import PlannerError

    fake_planner = MagicMock()
    fake_planner.followup = AsyncMock(side_effect=PlannerError("LLM exploded"))
    with patch(
        "helixis.api.routes.buildium.ActionPlanner",
        return_value=fake_planner,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/followup",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"previous_picks": [], "freeform_hint": None},
        )

    assert resp.status_code == 502
    body = resp.json()
    detail = body["detail"]
    assert detail["tool_id"] == "send_tenant_message"
    assert "Couldn't generate" in detail["error"]


def test_followup_404_for_unknown_event(client):
    resp = client.post(
        "/api/v1/buildium/events/00000000-0000-0000-0000-000000000000/actions/0/followup",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"previous_picks": [], "freeform_hint": None},
    )
    assert resp.status_code == 404


def test_followup_409_when_already_executed(client, fake_pool):
    fake_pool._state["rows"][EVENT_ID]["executed_action_index"] = 0
    resp = client.post(
        f"/api/v1/buildium/events/{EVENT_ID}/actions/0/followup",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"previous_picks": [], "freeform_hint": None},
    )
    assert resp.status_code == 409


def test_followup_400_for_action_index_out_of_bounds(client):
    resp = client.post(
        f"/api/v1/buildium/events/{EVENT_ID}/actions/99/followup",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"previous_picks": [], "freeform_hint": None},
    )
    assert resp.status_code == 400


# ── execute with chain ────────────────────────────────────────────


def test_execute_with_chain_merges_patches_in_order(client, fake_pool):
    """A chain pick's parameter_patch is deep-merged into the action's
    base parameters BEFORE parameter_overrides — overrides win last."""
    fake_handler = MagicMock()
    fake_handler.execute = AsyncMock(return_value={"buildium_id": 1, "summary": "Sent message"})

    with patch(
        "helixis.api.routes.buildium.get_handler",
        return_value=fake_handler,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={
                "parameter_overrides": {"subject": "Final subject (override)"},
                "freeform_note": None,
                "chain": [
                    {
                        "prompt": "What tone should the message take?",
                        "label": "Reassuring",
                        "parameter_patch": {"body": "Hi Josh, sorry about the leak."},
                    }
                ],
            },
        )

    assert resp.status_code == 200
    # Handler saw the merged params: base ← chain patch ← override.
    merged = fake_handler.execute.await_args.args[1]
    assert merged["tenant_id"] == 82336  # from base
    assert merged["body"] == "Hi Josh, sorry about the leak."  # from chain
    assert merged["subject"] == "Final subject (override)"  # from overrides


def test_execute_persists_chain_in_execution_result(client, fake_pool):
    fake_handler = MagicMock()
    fake_handler.execute = AsyncMock(return_value={"buildium_id": 1, "summary": "ok"})
    with patch(
        "helixis.api.routes.buildium.get_handler",
        return_value=fake_handler,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={
                "parameter_overrides": None,
                "freeform_note": None,
                "chain": [
                    {
                        "prompt": "What tone?",
                        "label": "Short",
                        "parameter_patch": {"body": "Plumber on the way."},
                    }
                ],
            },
        )

    assert resp.status_code == 200
    import json as _json

    row = fake_pool._state["rows"][EVENT_ID]
    persisted = _json.loads(row["execution_result"])
    assert persisted["chain"] == [
        {
            "prompt": "What tone?",
            "label": "Short",
            "parameter_patch": {"body": "Plumber on the way."},
        }
    ]
    # Activity log description references the click-chain step count.
    args = fake_pool._state["activity_log_rows"][0]
    description = args[5]  # ActivityEntry description is positional arg 5
    assert "via click-chain (1 steps)" in description


def test_execute_without_chain_keeps_legacy_description(client, fake_pool):
    """Backwards-compat: when chain is absent the description stays
    the bare intent string — Phase A.2 behaviour preserved."""
    fake_handler = MagicMock()
    fake_handler.execute = AsyncMock(return_value={"buildium_id": 1, "summary": "ok"})
    with patch(
        "helixis.api.routes.buildium.get_handler",
        return_value=fake_handler,
    ):
        resp = client.post(
            f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
            headers={"Authorization": f"Bearer {_make_token()}"},
            json={"parameter_overrides": None, "freeform_note": None},
        )

    assert resp.status_code == 200
    args = fake_pool._state["activity_log_rows"][0]
    description = args[5]
    assert "via click-chain" not in description
    assert description.startswith("Send message")
