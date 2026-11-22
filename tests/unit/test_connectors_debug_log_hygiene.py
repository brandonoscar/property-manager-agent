"""/connectors/debug/composio must not dump connection metadata at WARNING.

It previously logged the full probe payload (connection ids, auth_config_ids,
statuses) at WARNING on every call — log noise + metadata in the log stream.
The response still carries the full payload; the log should be a quiet debug
summary. This pins that.

⚠ This docstring used to call the response "auth'd + self-scoped", echoing the
route's own claim. It was NOT self-scoped: the handler probes the caller's user
identity AND `company_ids[0]`, and the signup trigger makes every
auto-provisioned company id equal to a user id, so authentication alone let any
member read the company identity's connection metadata. It is admin-only now,
and the second test below pins that — a false reassurance repeated in a test
file is how the next reader stops looking.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
USER_ID = "00000000-0000-0000-0000-000000000001"
_LOGGER = "helixis.api.routes.connectors"


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


class _FakeConnectedAccounts:
    def list(self, user_ids):  # noqa: ANN001, ANN201 — test double
        return SimpleNamespace(items=[])


class _FakeComposio:
    """Minimal stand-in: available, returns no connections."""

    available = True
    _client = SimpleNamespace(connected_accounts=_FakeConnectedAccounts())

    def list_connections(self, _identity):  # noqa: ANN001, ANN201
        return []


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def test_debug_composio_does_not_warn_dump(settings: Settings, caplog) -> None:
    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
        # Admin-only since the role floor landed; this test is about LOGGING,
        # so it satisfies the floor rather than asserting on it.
        patch("helixis.api.routes.company.require_role", new=AsyncMock(return_value="admin")),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            c.app.state.composio_client = _FakeComposio()
            with caplog.at_level(logging.DEBUG, logger=_LOGGER):
                resp = c.get(
                    "/api/v1/connectors/debug/composio",
                    headers={"Authorization": f"Bearer {_token()}"},
                )

    assert resp.status_code == 200
    route_logs = [r for r in caplog.records if r.name == _LOGGER]
    # No WARNING-level record (the old full-dump line was WARNING).
    assert not [r for r in route_logs if r.levelno >= logging.WARNING], (
        "debug/composio should not log at WARNING"
    )
    # The quiet debug summary fired and carries no raw connection payload.
    summaries = [r for r in route_logs if "composio debug probed" in r.getMessage()]
    assert summaries, "expected a debug-level probe summary"
    assert "auth_config" not in summaries[0].getMessage()


def test_debug_composio_is_admin_only(settings: Settings) -> None:
    """⚠ The route probes `company_ids[0]` as a Composio identity, and the
    signup trigger makes every auto-provisioned company id equal to a user id
    — so before this floor, authentication alone let any MEMBER dump the
    company identity's connection metadata (status, auth_config_id, toolkit,
    timestamps). The route is mounted unconditionally in production with no env
    gate, so "diagnostic" never limited who could reach it.

    Fails CLOSED: `require_role` raising is the refusal, not a fall-through.
    """
    from fastapi import HTTPException

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="admins only"))
    probed = _FakeComposio()
    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
        patch("helixis.api.routes.company.require_role", new=denied),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            c.app.state.composio_client = probed
            resp = c.get(
                "/api/v1/connectors/debug/composio",
                headers={"Authorization": f"Bearer {_token()}"},
            )

    assert resp.status_code == 403
    denied.assert_awaited_once()
