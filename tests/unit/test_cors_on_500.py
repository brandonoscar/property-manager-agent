"""An unhandled 500 must still carry CORS headers (#63).

The masking bug: when a route raised an unhandled exception, it propagated
UP past ``CORSMiddleware`` as an exception (not a response), so CORS never
stamped ``Access-Control-Allow-Origin`` onto the 500. The browser then
blocked the response and the frontend showed only "failed to fetch",
hiding the real error. It bit ``/auth/bootstrap`` and ``/buildium/test``
live within one week.

We force a genuine unhandled exception via ``GET /debug/sentry`` (raises
``SentryTestError`` when the token matches) and assert the 500 comes back
WITH CORS headers and a structured, correlatable body.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings

ORIGIN = "https://your-app.vercel.app"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # /debug/sentry only raises when the request token matches this env var.
    monkeypatch.setenv("SENTRY_TEST_TOKEN", "unit-test-token")
    settings = Settings(
        env="test",
        auth={"jwt_secret": "test-secret"},
        memory={"enabled": False},
        api={
            "host": "127.0.0.1",
            "port": 8000,
            "cors_origins": [ORIGIN],
        },
    )
    return TestClient(create_app(settings))


def _boom(client: TestClient):
    return client.get(
        "/api/v1/debug/sentry",
        params={"token": "unit-test-token"},
        headers={"Origin": ORIGIN},
    )


def test_unhandled_500_carries_cors_headers(client: TestClient) -> None:
    resp = _boom(client)
    assert resp.status_code == 500
    # The whole point: the browser must see this header or it reports
    # "failed to fetch" and the real error is invisible.
    assert resp.headers.get("access-control-allow-origin") == ORIGIN


def test_unhandled_500_returns_structured_correlatable_body(client: TestClient) -> None:
    resp = _boom(client)
    body = resp.json()
    assert body["error"] == "internal_server_error"
    # A request id the user can quote to support, echoed on the header too.
    assert body.get("request_id")
    assert resp.headers.get("x-request-id")
    # The raw exception text must not leak to the client.
    assert "SentryTestError" not in resp.text


def test_normal_response_still_has_cors_headers(client: TestClient) -> None:
    """Reordering CORS to outermost must not regress the happy path."""
    resp = client.get("/api/v1/health/", headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ORIGIN


def test_cors_preflight_still_short_circuits(client: TestClient) -> None:
    resp = client.options(
        "/api/v1/connectors/",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == ORIGIN
