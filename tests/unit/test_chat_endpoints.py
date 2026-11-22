"""GET /api/v1/chat/sessions + /sessions/{id}/messages (PR #71).

Both endpoints lean on ``helixis.agents.chat_history`` so the route-
layer tests focus on auth + company scoping + payload shape. Library-
level filtering tests live in ``test_chat_history.py``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
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


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


@pytest.fixture
def fake_sessions() -> list[dict[str, Any]]:
    return [
        {
            "session_id": "s1",
            "preview": "what leases are about to end",
            "created_at": "2026-05-17T00:00:00+00:00",
        },
        {
            "session_id": "s2",
            "preview": "how many work orders are open",
            "created_at": "2026-05-16T23:00:00+00:00",
        },
    ]


@pytest.fixture
def fake_messages() -> list[dict[str, Any]]:
    return [
        {
            "id": "msg-1",
            "role": "user",
            "content": "what leases are about to end",
            "tool_name": None,
            "tool_result": None,
            "created_at": "2026-05-17T00:00:00+00:00",
            "user_id": USER_ID,
        },
        {
            "id": "msg-2",
            "role": "assistant",
            "content": "Over what timeframe?",
            "tool_name": None,
            "tool_result": None,
            "created_at": "2026-05-17T00:00:01+00:00",
            "user_id": None,
        },
    ]


@pytest.fixture
def client(
    test_settings: Settings,
    fake_sessions: list[dict[str, Any]],
    fake_messages: list[dict[str, Any]],
):
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    list_mock = AsyncMock(return_value=fake_sessions)
    messages_mock = AsyncMock(return_value=fake_messages)

    with (
        patch(
            "helixis.api.routes.chat.list_sessions_for_company",
            new=list_mock,
        ),
        patch(
            "helixis.api.routes.chat.load_session_messages_for_company",
            new=messages_mock,
        ),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            c.list_mock = list_mock  # type: ignore[attr-defined]
            c.messages_mock = messages_mock  # type: ignore[attr-defined]
            yield c


def test_sessions_requires_authentication(test_settings: Settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as c:
        resp = c.get("/api/v1/chat/sessions")
    assert resp.status_code in (401, 403)


def test_sessions_returns_rows(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/chat/sessions",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["rows"][0]["session_id"] == "s1"
    assert body["rows"][0]["preview"] == "what leases are about to end"


def test_sessions_scopes_by_company(client: TestClient) -> None:
    """The route must pass the requester's company_id, not the user_id,
    into the persistence helper. Pin the kwarg so a future refactor
    can't accidentally widen the scope."""
    client.get(
        "/api/v1/chat/sessions",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    list_mock = client.list_mock  # type: ignore[attr-defined]
    list_mock.assert_awaited_once()
    args, kwargs = list_mock.await_args
    assert args[0] == COMPANY_ID


def test_session_messages_returns_full_thread(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/chat/sessions/s1/messages",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["rows"][0]["role"] == "user"
    assert body["rows"][1]["role"] == "assistant"


def test_session_messages_passes_company_scope_to_loader(client: TestClient) -> None:
    client.get(
        "/api/v1/chat/sessions/some-session/messages",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    messages_mock = client.messages_mock  # type: ignore[attr-defined]
    messages_mock.assert_awaited_once()
    args, _kwargs = messages_mock.await_args
    assert args[0] == "some-session"
    assert args[1] == COMPANY_ID


def test_no_company_membership_is_403(test_settings: Settings) -> None:
    async def fake_resolve_empty(_user_id: str) -> list[str]:
        return []

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve_empty),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/chat/sessions",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 403
