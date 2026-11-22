"""Cross-tenant ownership gates on /agent/confirm and /agent/cancel (audit).

Both endpoints authenticated the caller but never checked the caller owns the
confirm/session — so any tenant could approve another tenant's gated WRITE, or
cancel another tenant's live run. These pin the new ownership checks (route
level) plus the two repository readers they depend on (deterministic, mocked
pool — no DB).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from helixis.db.repositories import orchestrator_runs
from helixis.db.repositories.pending_confirmations import ConfirmationRow, PgConfirmationStore
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
OTHER_COMPANY = "ffffffff-ffff-ffff-ffff-ffffffffffff"
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


# ── repository readers (deterministic, mocked pool) ──────────────


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool_returning(row: object) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool


@pytest.mark.asyncio
async def test_store_get_returns_row_company() -> None:
    row = {
        "confirm_id": "c1",
        "company_id": COMPANY_ID,
        "session_id": "s1",
        "user_id": USER_ID,
        "action": "buildium_create_to_do_task",
        "status": "pending",
        "created_at": None,
        "resolved_at": None,
    }
    store = PgConfirmationStore(_pool_returning(row))
    got = await store.get(confirm_id="c1")
    assert got is not None and got.company_id == COMPANY_ID


@pytest.mark.asyncio
async def test_store_get_returns_none_when_absent() -> None:
    store = PgConfirmationStore(_pool_returning(None))
    assert await store.get(confirm_id="missing") is None


@pytest.mark.asyncio
async def test_get_company_for_session_reads_company() -> None:
    pool = _pool_returning({"company_id": COMPANY_ID})
    assert await orchestrator_runs.get_company_for_session(pool, "s1") == COMPANY_ID


@pytest.mark.asyncio
async def test_get_company_for_session_none_when_absent() -> None:
    assert await orchestrator_runs.get_company_for_session(_pool_returning(None), "s1") is None


@pytest.mark.asyncio
async def test_get_company_for_session_swallows_db_error() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("pool down"))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    # Best-effort: never raise (would break cancel), return None.
    assert await orchestrator_runs.get_company_for_session(pool, "s1") is None


# ── route-level ownership gates ──────────────────────────────────


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


@pytest.fixture
def client(test_settings: Settings):
    from helixis.api.rate_limit import agent_action_limiter

    agent_action_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            yield c


def _confirm_store(company_id: str) -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(
        return_value=ConfirmationRow(
            confirm_id="c1",
            company_id=company_id,
            session_id="s1",
            user_id=USER_ID,
            action="buildium_create_to_do_task",
            status="pending",
            created_at=None,
            resolved_at=None,
        )
    )
    return store


def test_confirm_rejects_cross_tenant(client: TestClient) -> None:
    resolve = AsyncMock(return_value="resolved")
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=_confirm_store(OTHER_COMPANY),
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": True},
        )
    assert resp.status_code == 404
    resolve.assert_not_awaited()  # the write-consent never resolved


def test_confirm_allows_same_tenant(client: TestClient) -> None:
    resolve = AsyncMock(return_value="resolved")
    role_gate = AsyncMock(return_value="manager")
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=_confirm_store(COMPANY_ID),
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
        # Approval now takes manager+ (approval-pipeline PR); satisfy the gate.
        patch("helixis.api.routes.company.require_role", new=role_gate),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": True},
        )
    assert resp.status_code == 200
    resolve.assert_awaited_once()
    role_gate.assert_awaited_once()


def test_confirm_approval_requires_manager_role(client: TestClient) -> None:
    """A member's APPROVE is refused (403) and never resolves the confirm —
    approval drives a side-effecting write, so it takes manager+."""
    resolve = AsyncMock(return_value="resolved")
    role_gate = AsyncMock(side_effect=HTTPException(status_code=403, detail="manager+ only"))
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=_confirm_store(COMPANY_ID),
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
        patch("helixis.api.routes.company.require_role", new=role_gate),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": True},
        )
    assert resp.status_code == 403
    resolve.assert_not_awaited()


def test_confirm_decline_needs_no_role_floor(client: TestClient) -> None:
    """A DECLINE stays open to any member — blocking a member from stopping
    an action would be the wrong fail direction."""
    resolve = AsyncMock(return_value="resolved")
    role_gate = AsyncMock(side_effect=AssertionError("role gate must not run on decline"))
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=_confirm_store(COMPANY_ID),
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
        patch("helixis.api.routes.company.require_role", new=role_gate),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": False, "reason": "wrong vendor"},
        )
    assert resp.status_code == 200
    resolve.assert_awaited_once()
    # The decline reason reaches resolve_confirmation for the durable record.
    assert resolve.await_args.kwargs.get("reason") == "wrong vendor"


def test_confirm_approval_fails_closed_when_no_durable_store(client: TestClient) -> None:
    """#70: store off (no DB) → we can verify NEITHER ownership NOR the manager
    role floor. An APPROVAL drives a side-effecting write, so fail CLOSED (503)
    rather than resolve it unchecked (the old behavior let a member drive a
    write with no gate during a DB outage)."""
    resolve = AsyncMock(return_value="resolved")
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=None,
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": True},
        )
    assert resp.status_code == 503
    resolve.assert_not_awaited()


def test_confirm_decline_still_allowed_when_no_durable_store(client: TestClient) -> None:
    """#70: a DECLINE has no side effect, and blocking a member from STOPPING
    an action would be the wrong fail direction — so it still proceeds even
    when the durable store is unavailable."""
    resolve = AsyncMock(return_value="resolved")
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=None,
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": False, "reason": "not now"},
        )
    assert resp.status_code == 200
    resolve.assert_awaited_once()


def test_cancel_rejects_cross_tenant(client: TestClient) -> None:
    publish = AsyncMock()
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=OTHER_COMPANY),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
        patch("helixis.streaming.bus.EventBus.publish", new=publish),
        patch("helixis.streaming.bus.EventBus.close_session", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/agent/cancel/victim-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404
    publish.assert_not_awaited()  # the victim's run was not touched


def test_cancel_allows_same_tenant(client: TestClient) -> None:
    publish = AsyncMock()
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=COMPANY_ID),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
        patch("helixis.streaming.bus.EventBus.publish", new=publish),
        patch("helixis.streaming.bus.EventBus.close_session", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/agent/cancel/my-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    publish.assert_awaited_once()


def _confirm_store_missing() -> MagicMock:
    """A store that is available but has no row for the confirm_id."""
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    return store


def test_confirm_denies_when_row_missing_fail_closed(client: TestClient) -> None:
    # LED-014: store available but no durable row → the write can't be attributed
    # to the caller, so DENY instead of falling through to resolve it.
    resolve = AsyncMock(return_value="resolved")
    with (
        patch(
            "helixis.db.repositories.pending_confirmations.store_from_pool",
            return_value=_confirm_store_missing(),
        ),
        patch("helixis.api.routes.agent.resolve_confirmation", new=resolve),
    ):
        resp = client.post(
            "/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"confirm_id": "c1", "approved": True},
        )
    assert resp.status_code == 404
    resolve.assert_not_awaited()  # fail-closed: the write never resolved


def test_cancel_denies_when_no_run_record_fail_closed(client: TestClient) -> None:
    # LED-014: pool available but no run record (or a swallowed DB error → None)
    # → DENY instead of falling through and cancelling.
    publish = AsyncMock()
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=None),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
        patch("helixis.streaming.bus.EventBus.publish", new=publish),
        patch("helixis.streaming.bus.EventBus.close_session", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/agent/cancel/some-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404
    publish.assert_not_awaited()  # fail-closed: the run was not cancelled


# ── GET /agent/watch — live-watch ownership gates (multiplayer PR) ────
#
# Same LED-014 fail-closed pattern as cancel: a foreign company, a missing
# run record, or a swallowed DB error (all → None / mismatch) deny with 404
# before any stream is attached.


def test_watch_rejects_cross_tenant(client: TestClient) -> None:
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=OTHER_COMPANY),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
    ):
        resp = client.get(
            "/api/v1/agent/watch/victim-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404


def test_watch_denies_when_no_run_record_fail_closed(client: TestClient) -> None:
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=None),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
    ):
        resp = client.get(
            "/api/v1/agent/watch/some-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404


def test_watch_404s_when_no_live_session(client: TestClient) -> None:
    # Owned session, but nothing live on the bus — the client falls back to
    # the persisted-transcript replay it already performed.
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=COMPANY_ID),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=False),
    ):
        resp = client.get(
            "/api/v1/agent/watch/idle-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404


def test_watch_streams_and_announces_presence(client: TestClient) -> None:
    # ``after`` is the resume cursor the route now forwards; the double
    # accepts it so the stub matches the real signature.
    async def fake_sse(_bus: object, _session_id: str, after: int | None = None):
        yield 'data: {"type": "narration", "data": {"content": "hi"}}\n\n'
        yield "data: [DONE]\n\n"

    publish = AsyncMock()
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=COMPANY_ID),
        ),
        patch("helixis.streaming.bus.EventBus.has_session", return_value=True),
        patch("helixis.streaming.bus.EventBus.publish", new=publish),
        patch("helixis.api.routes.agent.sse_event_generator", new=fake_sse),
    ):
        resp = client.get(
            "/api/v1/agent/watch/live-session",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 200
    assert "narration" in resp.text
    # watcher_update published on join AND on leave.
    assert publish.await_count == 2
    join_event = publish.await_args_list[0].args[1]
    assert join_event.type == "watcher_update"
    assert join_event.data["count"] == 1
    leave_event = publish.await_args_list[1].args[1]
    assert leave_event.data["count"] == 0


# ── POST /agent/run — session lock (multiplayer PR) ──────────────────


def test_run_409s_when_a_run_is_already_active(client: TestClient) -> None:
    with patch("helixis.api.routes.agent.run_registry.claim", return_value=False):
        resp = client.post(
            "/api/v1/agent/run",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"task": "do the thing", "company_id": COMPANY_ID, "session_id": "busy"},
        )
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]


# --- /agent/run: the third route, and the one the original audit missed ---
#
# ⚠ The audit that added the gates above scoped itself to confirm+cancel — this
# file's own docstring says so — and `/agent/run` accepts the SAME client-supplied
# session id. Posting a victim's id reached `bus.create_session` (a no-op for an
# existing session) and the response subscribed to it, so `subscribe_seq` replayed
# that company's retained log back to the caller.
#
# ⚠ And the ids are guessable: the web client mints `t_${Date.now()}_${n}`.


async def _no_events(*_a: object, **_kw: object):
    """An SSE body that ends immediately.

    ⚠ Without this the guard's failure mode is a HANG, not a red test: strip
    the ownership gate and the request succeeds, TestClient blocks reading a
    live stream, and the run neither passes nor fails. A mutation whose verdict
    is "it hung" is the same unreadable signal as gotcha 64's un-run mutant.
    """
    return
    yield  # pragma: no cover — makes this an async generator


def test_run_rejects_another_companys_session(client: TestClient) -> None:
    streaming = AsyncMock()
    with (
        patch("helixis.api.routes.agent.sse_event_generator", new=_no_events),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=OTHER_COMPANY),
        ),
        patch("helixis.api.routes.agent.run_agent_streaming", new=streaming),
    ):
        resp = client.post(
            "/api/v1/agent/run",
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "task": "read it back to me",
                "company_id": COMPANY_ID,
                "session_id": "t_1787702329204_0",
            },
        )
    assert resp.status_code == 404
    # The run must not start: reaching the orchestrator at all would publish
    # into the victim's session even if the response were discarded.
    streaming.assert_not_awaited()


def test_run_allows_a_session_nobody_owns_yet(client: TestClient) -> None:
    # ⚠ The branch that makes this route different from cancel/watch. A brand
    # new conversation has no run record, so `owner is None` — copying their
    # `owner is None or ...` condition would 404 every first message.
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=None),
        ),
        patch("helixis.streaming.run_registry.claim", return_value=False),
    ):
        resp = client.post(
            "/api/v1/agent/run",
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "task": "hello",
                "company_id": COMPANY_ID,
                "session_id": "t_brand_new_0",
            },
        )
    # 409 from the run-registry claim we deliberately refused — it got PAST the
    # ownership gate, which is what this asserts. A 404 would mean the gate
    # wrongly rejected a new session.
    assert resp.status_code == 409


def test_run_allows_the_owning_company(client: TestClient) -> None:
    with (
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=COMPANY_ID),
        ),
        patch("helixis.streaming.run_registry.claim", return_value=False),
    ):
        resp = client.post(
            "/api/v1/agent/run",
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "task": "continue",
                "company_id": COMPANY_ID,
                "session_id": "t_mine_0",
            },
        )
    assert resp.status_code == 409  # past the gate; see above


# --- the same gate on the Buildium event thread stream -------------------
#
# ⚠ `GET /buildium/events/{id}/thread/stream` company-scopes the EVENT
# (`WHERE id = $1 AND company_id = $2`) and then subscribes to a session id
# taken from a QUERY PARAMETER. The bus is keyed by session id alone, so
# holding one event of your own was enough to read another company's turn —
# and `_replay_thread_into_bus` publishes your content INTO their session,
# so it leaked both ways.


def _event_row_pool() -> MagicMock:
    """A pool whose fetchrow returns a real-looking buildium_events row, so the
    route reaches the session gate instead of 404ing on the event."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "event_name": "Task.Created",
            "action_surface": None,
            "enrichment": None,
            "executed_action_index": None,
        }
    )
    return pool


def test_event_stream_rejects_another_companys_session(client: TestClient) -> None:
    replay = AsyncMock()
    with (
        patch("helixis.api.routes.buildium.get_pool", return_value=_event_row_pool()),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=OTHER_COMPANY),
        ),
        patch("helixis.api.routes.buildium._replay_thread_into_bus", new=replay),
        patch("helixis.api.routes.buildium.sse_event_generator", new=_no_events),
    ):
        resp = client.get(
            "/api/v1/buildium/events/11111111-1111-1111-1111-111111111111/thread/stream",
            params={"session_id": "t_1787702329204_0"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 404
    # The write direction matters as much as the read: replaying would have
    # published THIS caller's action surface into the other company's stream.
    replay.assert_not_awaited()


def test_event_stream_allows_a_session_nobody_owns_yet(client: TestClient) -> None:
    # The default `webhook:{event_id}` form, and any thread not yet run, has no
    # run record. 404ing on `owner is None` would break every first open.
    replay = AsyncMock()
    with (
        patch("helixis.api.routes.buildium.get_pool", return_value=_event_row_pool()),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
        patch(
            "helixis.db.repositories.orchestrator_runs.get_company_for_session",
            new=AsyncMock(return_value=None),
        ),
        patch("helixis.api.routes.buildium._replay_thread_into_bus", new=replay),
        patch("helixis.api.routes.buildium.sse_event_generator", new=_no_events),
    ):
        resp = client.get(
            "/api/v1/buildium/events/11111111-1111-1111-1111-111111111111/thread/stream",
            params={"session_id": "t_brand_new_0"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 200
    replay.assert_awaited_once()
