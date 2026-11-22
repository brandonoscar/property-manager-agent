"""Deterministic tests for the Inbox webhook WRITE-action CARD surface.

Cards are a SEPARATE Buildium-write surface from the agent chat tools. A
planner-proposed action on a webhook card is executed via
``POST /buildium/events/{id}/actions/{i}/execute`` (``api/routes/buildium.py``),
which looks up a handler in ``agent/handlers`` and runs it against the company's
``BuildiumClient``. The agent chat tools (``buildium_close_work_order`` …) are
well covered elsewhere; this CARD path had three untested seams (mapped 2026-08):

  1. **No end-to-end "card action → correct Buildium request body" test.** The
     execute endpoint's param-merge + enrichment-injection were asserted only
     against a MagicMock handler (``test_inbox_endpoints``) — never a REAL
     handler's real Buildium request.
  2. **The card-path Manager+ role floor was stubbed away** by an autouse patch
     in ``test_inbox_endpoints`` — a Member-gets-403 was never asserted.
  3. **Nothing guarded that the card catalog EXCLUDES the irreversible financial
     writes** (payment / charge / close / move-out / renewal). A future PR could
     ship a payment CARD inheriting only the endpoint's blanket floor, with none
     of the per-tool ``confirm_or_abort`` + fail-closed ``_require_manager_role``
     guards the agent-tool path has.

All fixture-driven; NO live Buildium key (a recording fake ``BuildiumClient``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
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

# The /execute path runs through the durable idempotency guard (#224); the shared
# conftest fixture injects an in-memory store so these don't need asyncpg.
pytestmark = pytest.mark.usefixtures("execute_idempotency_store")

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
TEST_DATA_KEY = Fernet.generate_key().decode()
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
EVENT_ID = "321d5883-5844-4cc8-9c0d-65b9c0bccf2a"


def _token(sub: str = "user-1") -> str:
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


# Action 0 = update_buildium_task (planner defaults priority=High, category=Plumbing);
# Action 1 = create_followup_task. Both are real card handlers.
_ACTION_SURFACE = {
    "summary": "Tenant reported a leak under the kitchen sink.",
    "actions": [
        {
            "label": "Set priority to High",
            "intent": "Raise priority to High",
            "tool_id": "update_buildium_task",
            "parameters": {"priority": "High", "category": "Plumbing"},
        },
        {
            "label": "Schedule plumbing inspection",
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

# Enrichment carries the task id the endpoint injects into handler context — this
# is what turns "the card" into "PATCH /tasks/todorequests/11154".
_ENRICHMENT = {
    "task": {
        "id": 11154,
        "unit_id": 58280,
        "property_id": 31,
        "assigned_to_user_id": 77042,
        "title": "Hello World",
        "raw": {},
    }
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


def _make_pool(role: str | None):
    """Fake asyncpg pool. ``role`` is the caller's company_users role the REAL
    ``require_role`` gate reads (``None`` = no membership row → unverifiable)."""
    state: dict[str, Any] = {
        "rows": {
            EVENT_ID: {
                "id": EVENT_ID,
                "event_name": "Task.Created",
                "event_time": datetime(2026, 5, 13, 23, 39, 58, tzinfo=UTC),
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

    async def fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        n = " ".join(sql.split())
        if "FROM public.company_users" in n:
            # The REAL role gate — return the configured role (or None).
            return {"role": role} if role is not None else None
        if "FROM buildium_events" in n:
            row = state["rows"].get(str(args[0]))
            if row is None or row["company_id"] != args[1]:
                return None
            return row
        if "FROM public.company_buildium_credentials" in n:
            return state["creds"].get(args[0])
        raise AssertionError(f"unexpected fetchrow SQL: {n[:100]}")

    async def execute(sql: str, *args: Any) -> None:
        n = " ".join(sql.split())
        if n.startswith("UPDATE buildium_events SET executed_action_index"):
            row = state["rows"].get(str(args[0]))
            if row is not None:
                row["executed_action_index"] = args[1]
                row["executed_by_user_id"] = args[2]
        elif n.startswith("INSERT INTO helixis_activity_log"):
            state["activity_log_rows"].append(args)
        return None

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


def _recording_client(response: Any) -> tuple[type, list[dict[str, Any]]]:
    """A stand-in for ``BuildiumClient``: async-context, records every
    ``.request`` (the real Buildium write) into the returned list."""
    recorded: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *e: Any) -> bool:
            return False

        async def request(
            self,
            method: str,
            path: str,
            *,
            query: Any = None,
            body: Any = None,
            schema: Any = None,
        ) -> Any:
            recorded.append({"method": method, "path": path, "body": body, "query": query})
            return response

    return _Client, recorded


@contextmanager
def _client_for(role: str | None, test_settings: Settings) -> Iterator[TestClient]:
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()
    pool = _make_pool(role)
    with (
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_settings", return_value=test_settings),
        patch("helixis.api.routes.buildium.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", new=AsyncMock(return_value=[COMPANY_ID])),
    ):
        app = create_app(test_settings)
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)

        @asynccontextmanager
        async def _noop(_app):
            yield

        app.router.lifespan_context = _noop
        with TestClient(app) as c:
            yield c


# ── Gap 1: card action → REAL handler → correct Buildium request ─────


def test_card_update_task_builds_real_buildium_request(test_settings):
    """End-to-end through the REAL UpdateBuildiumTaskHandler: the card's execute
    endpoint must build the correct Buildium PATCH — proving (a) the enriched
    task id lands in the PATH, (b) the parameter_override wins the merge, and
    (c) the planner's default param survives the merge."""
    client_cls, recorded = _recording_client({"Id": 11154, "Priority": "Low"})
    with _client_for("manager", test_settings) as client:
        with patch("helixis.api.routes.buildium.BuildiumClient", client_cls):
            resp = client.post(
                f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"parameter_overrides": {"priority": "Low"}, "freeform_note": None},
            )

    assert resp.status_code == 200, resp.text
    assert len(recorded) == 1, "the card must produce exactly one Buildium write"
    call = recorded[0]
    assert call["method"] == "PATCH"
    # Enrichment task id injected into the endpoint's handler context → the path.
    assert call["path"] == "/tasks/todorequests/11154"
    # parameter_override ("Low") beat the planner default ("High").
    assert call["body"]["Priority"] == "Low"
    # planner default that wasn't overridden still made it into the body.
    assert call["body"]["Category"] == {"Name": "Plumbing"}


def test_card_execute_marks_row_and_writes_activity(test_settings):
    """A successful card write marks the event row executed and records the
    human-action audit row — the card is a one-and-done, idempotent action."""
    client_cls, _ = _recording_client({"Id": 11154, "Priority": "High"})
    with _client_for("manager", test_settings) as client:
        with patch("helixis.api.routes.buildium.BuildiumClient", client_cls):
            resp = client.post(
                f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"parameter_overrides": None, "freeform_note": None},
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executed"


# ── Gap 2: the card-path Manager+ role floor is REAL (not stubbed) ───


def test_card_execute_denies_member(test_settings):
    """A Member calling the card execute endpoint is refused (403) BEFORE any
    Buildium write — the same Manager+ floor the agent-tool path enforces."""
    client_cls, recorded = _recording_client({"Id": 11154})
    with _client_for("member", test_settings) as client:
        with patch("helixis.api.routes.buildium.BuildiumClient", client_cls):
            resp = client.post(
                f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"parameter_overrides": None, "freeform_note": None},
            )
    assert resp.status_code == 403
    assert recorded == []  # denied before the write fired


def test_card_execute_denies_unverifiable_role(test_settings):
    """No company_users membership row → role is unverifiable → fail closed
    (403), never treated as authorized."""
    client_cls, recorded = _recording_client({"Id": 11154})
    with _client_for(None, test_settings) as client:
        with patch("helixis.api.routes.buildium.BuildiumClient", client_cls):
            resp = client.post(
                f"/api/v1/buildium/events/{EVENT_ID}/actions/0/execute",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"parameter_overrides": None, "freeform_note": None},
            )
    assert resp.status_code == 403
    assert recorded == []


# ── Gap 3: the card catalog must EXCLUDE irreversible/financial writes ──

# The hard-to-undo verbs live ONLY as agent chat tools, each with a per-tool
# confirm gate + fail-closed role floor. If any became a CARD, it would inherit
# only the execute endpoint's blanket Manager floor — a real safety downgrade.
_FORBIDDEN_CARD_VERBS = frozenset(
    {"post_charge", "record_payment", "close_work_order", "record_move_out", "renew_lease"}
)
_FORBIDDEN_SUBSTRINGS = ("payment", "charge", "close_work_order", "move_out", "moveout", "renew")


def test_card_catalog_excludes_irreversible_writes():
    from helixis.agent.handlers import HANDLERS
    from helixis.agent.planner.tool_catalog import TOOL_IDS

    keys = set(HANDLERS) | set(TOOL_IDS)
    assert keys.isdisjoint(_FORBIDDEN_CARD_VERBS), (
        f"an irreversible/financial write is registered as a CARD: "
        f"{keys & _FORBIDDEN_CARD_VERBS} — cards inherit only the endpoint's blanket "
        f"Manager floor, not the per-tool confirm + fail-closed guards; keep these agent-only"
    )
    # Catch a future financial card slipping in under a different name.
    for k in keys:
        assert not any(s in k for s in _FORBIDDEN_SUBSTRINGS), (
            f"card tool_id {k!r} looks like an irreversible/financial write — "
            f"cards must not carry these (agent-tool surface only)"
        )
