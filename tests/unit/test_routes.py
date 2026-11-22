"""Integration tests for API routes using FastAPI TestClient."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"


def _make_token(
    sub: str = "user-123",
    email: str = "test@example.com",
    role: str = "authenticated",
    company_ids: list[str] | None = None,
) -> str:
    payload = {
        "sub": sub,
        "email": email,
        "role": role,
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "iss": TEST_ISSUER,
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
    )


@pytest.fixture
def client(test_settings):
    """Create a TestClient with mocked lifespan to avoid real service init."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    # Patch get_settings so auth module uses our test settings (with jwt_secret)
    with patch("helixis.api.auth.get_settings", return_value=test_settings):
        app = create_app(test_settings)

        # Manually set app.state attributes that lifespan would set
        app.state.memory_manager = MagicMock(available=False)
        app.state.composio_client = MagicMock(available=False)

        # Override lifespan to be a no-op
        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app.router.lifespan_context = noop_lifespan

        with TestClient(app) as c:
            yield c


# --- Health routes (public, no auth) ---


def test_health_endpoint(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_endpoint(client):
    resp = client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert "checks" in body
    assert body["status"] in ("ready", "degraded")


# --- Agent routes (require auth) ---


def test_agent_run_no_auth(client):
    """POST /agent/run without Authorization header returns 401."""
    resp = client.post(
        "/api/v1/agent/run",
        json={"task": "test task", "company_id": "test-co"},
    )
    assert resp.status_code == 401


def test_agent_run_invalid_token(client):
    """POST /agent/run with invalid token returns 401."""
    resp = client.post(
        "/api/v1/agent/run",
        json={"task": "test task", "company_id": "test-co"},
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code == 401


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_agent_run_no_company_access(mock_resolve, client):
    """POST /agent/run with valid token but wrong company returns 403."""
    mock_resolve.return_value = ["other-company"]
    token = _make_token()

    resp = client.post(
        "/api/v1/agent/run",
        json={"task": "test task", "company_id": "test-co"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_agent_confirm_no_auth(client):
    """POST /agent/confirm without auth returns 401."""
    resp = client.post(
        "/api/v1/agent/confirm",
        json={"confirm_id": "abc", "approved": True},
    )
    assert resp.status_code == 401


def test_agent_cancel_no_auth(client):
    """POST /agent/cancel without auth returns 401."""
    resp = client.post("/api/v1/agent/cancel/some-session")
    assert resp.status_code == 401


# --- Connector routes (require auth) ---


def test_connectors_no_auth(client):
    """GET /connectors without auth returns 401."""
    resp = client.get("/api/v1/connectors/")
    assert resp.status_code == 401


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connectors_with_auth(mock_resolve, client):
    """GET /connectors with valid auth returns a flat connector list."""
    mock_resolve.return_value = ["test-co"]
    token = _make_token()

    resp = client.get(
        "/api/v1/connectors/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Frontend expects a bare array, not {"connectors": [...]}.
    assert isinstance(body, list)
    assert len(body) > 0
    first = body[0]
    expected_keys = {"id", "name", "category", "description", "authMethod", "isConnected"}
    assert expected_keys.issubset(first.keys())
    # Nothing is pre-connected on the backend until OAuth lands. (Buildium
    # would reflect a saved-credentials row, but there's no DB pool in this
    # test so its lookup fails closed to disconnected — see the next test.)
    assert all(item["isConnected"] is False for item in body)


@patch("helixis.buildium.credentials.fetch_buildium_row", new_callable=AsyncMock)
@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connectors_buildium_row_alone_is_not_connected(mock_resolve, mock_fetch, client):
    """⚠ This test used to assert the OPPOSITE, and it was pinning a bug.

    A saved credentials row was reported as ``isConnected`` — so the card read
    Connected for keys that were revoked, expired, mistyped, or undecryptable
    after a data-key rotation. Storing a secret is not the same as it working
    (the same confusion as gotcha 67's configured-vs-enabled and gotcha 70's
    2xx-vs-written).

    A row with no successful test and no mirror sync is now the RECONNECT
    state, which is the honest one.
    """
    mock_resolve.return_value = ["test-co"]
    mock_fetch.return_value = {"company_id": "test-co", "client_id_last4": "1234"}
    token = _make_token()

    resp = client.get("/api/v1/connectors/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    buildium = next(item for item in body if item["id"] == "buildium")
    assert buildium["isConnected"] is False
    assert buildium["needsReauth"] is True
    assert all(item["isConnected"] is False for item in body if item["id"] != "buildium")


@patch("helixis.buildium.credentials.fetch_buildium_row", new_callable=AsyncMock)
@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connectors_buildium_connected_when_the_keys_are_proven(mock_resolve, mock_fetch, client):
    """The other direction. A successful credential test is proof the keys
    work, so the card says Connected — without this the fix above would just
    be a different wrong answer."""
    from datetime import UTC, datetime

    mock_resolve.return_value = ["test-co"]
    mock_fetch.return_value = {
        "company_id": "test-co",
        "client_id_last4": "1234",
        "last_tested_at": datetime.now(UTC),
    }
    token = _make_token()

    resp = client.get("/api/v1/connectors/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    buildium = next(item for item in resp.json() if item["id"] == "buildium")
    assert buildium["isConnected"] is True
    assert buildium["needsReauth"] is False


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connector_connect_400s_for_non_composio_connector(mock_resolve, client):
    """PR #131 — the generic ``/connect`` endpoint is now Composio-only.
    Buildium has its own dedicated ``/buildium/credentials`` flow
    (handled by the BuildiumApiDetail panel) and shouldn't hit this
    endpoint. Returns 400 with an instructive message so the frontend
    surfaces a real error rather than a silent "connected" stub."""
    mock_resolve.return_value = ["test-co"]
    token = _make_token()

    resp = client.post(
        "/api/v1/connectors/buildium/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "Composio" in body["detail"]


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connector_connect_503s_when_composio_not_configured(mock_resolve, client):
    """For Composio-managed connectors (Google Calendar / Drive / Gmail
    / Slack) — if Composio isn't configured on this deployment, return
    503 with a clear message. The frontend toast surfaces this verbatim
    so the operator knows what to set."""
    mock_resolve.return_value = ["test-co"]
    token = _make_token()

    resp = client.post(
        "/api/v1/connectors/google_drive/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    # In the test app Composio isn't initialized → 503.
    assert resp.status_code == 503
    body = resp.json()
    assert "HELIXIS_COMPOSIO__API_KEY" in body["detail"]


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connector_connect_unknown_id_404s(mock_resolve, client):
    mock_resolve.return_value = ["test-co"]
    token = _make_token()

    resp = client.post(
        "/api/v1/connectors/does_not_exist/connect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@patch("helixis.api.auth._resolve_company_ids", new_callable=AsyncMock)
def test_connector_disconnect_returns_disconnected_for_non_composio(
    mock_resolve,
    client,
):
    """PR #131 — disconnect on a non-Composio connector returns the
    placeholder shape (isConnected: false) so the frontend's existing
    poll-after-disconnect path still works. Real disconnect for raw
    connectors happens through their dedicated endpoints."""
    mock_resolve.return_value = ["test-co"]
    token = _make_token()

    resp = client.post(
        "/api/v1/connectors/buildium/disconnect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "buildium"
    assert body["isConnected"] is False
