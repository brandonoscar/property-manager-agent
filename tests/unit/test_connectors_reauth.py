"""The connectors page distinguishes a LAPSED OAuth grant from never-connected.

Before this, ``GET /connectors`` only set ``isConnected`` for connections in an
active status; a connection that existed but expired/dropped (the #183 orphan
class, or a 7-day testing-mode revoke) rendered identically to one that was
never set up — so the card showed "Connect", as if new, and the user had no
signal their integration had gone quiet. The route now emits ``needsReauth`` so
the frontend can show a "Reconnect" affordance instead.

The active-vs-lapsed classification itself is covered by
``test_composio_connection_degradation`` (``degraded_toolkits`` /
``connection_health``); this pins the wiring at the page route.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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


class _FakeComposio:
    """Returns a canned connection list: gmail lapsed, calendar active."""

    available = True
    _client = SimpleNamespace(connected_accounts=SimpleNamespace())

    def list_connections(self, _identity: str) -> list[dict[str, Any]]:
        return [
            {"toolkit": "gmail", "status": "expired"},  # lapsed → Reconnect
            {"toolkit": "googlecalendar", "status": "active"},  # healthy
            # google_drive absent entirely → never-connected, NOT reauth
        ]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _rows(settings: Settings) -> dict[str, dict[str, Any]]:
    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            c.app.state.composio_client = _FakeComposio()
            resp = c.get(
                "/api/v1/connectors/",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    return {row["id"]: row for row in resp.json()}


def test_lapsed_grant_flags_needs_reauth(settings: Settings) -> None:
    rows = _rows(settings)
    gmail = rows["gmail"]
    assert gmail["isConnected"] is False
    assert gmail["needsReauth"] is True


def test_active_grant_is_connected_not_reauth(settings: Settings) -> None:
    rows = _rows(settings)
    cal = rows["google_calendar"]
    assert cal["isConnected"] is True
    assert cal["needsReauth"] is False


def test_never_connected_is_not_reauth(settings: Settings) -> None:
    rows = _rows(settings)
    drive = rows["google_drive"]
    assert drive["isConnected"] is False
    assert drive["needsReauth"] is False
