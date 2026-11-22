"""CORS allowlist must accept the production Vercel host AND any
preview-deployment URL of the form
``your-app-<hash>-your-team.vercel.app``.

This is the bug we hit in production: the page rendered an empty
connectors list because the browser blocked the cross-origin response.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        env="test",
        auth={"jwt_secret": "test-secret"},
        memory={"enabled": False},
        api={
            "host": "127.0.0.1",
            "port": 8000,
            "cors_origins": ["https://your-app.vercel.app"],
            "cors_origin_regex": (
                r"^https://your-app"
                r"(-[a-z0-9]+-your-team)?"
                r"\.vercel\.app$"
            ),
        },
    )
    return TestClient(create_app(settings))


def _allow_origin_for(client: TestClient, origin: str) -> str | None:
    """Send a CORS preflight and return the Access-Control-Allow-Origin echo."""
    resp = client.options(
        "/api/v1/connectors/",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    return resp.headers.get("access-control-allow-origin")


def test_cors_allows_production_vercel(client: TestClient) -> None:
    assert _allow_origin_for(client, "https://your-app.vercel.app") == "https://your-app.vercel.app"


def test_cors_allows_vercel_preview_deployment(client: TestClient) -> None:
    preview = "https://your-app-aas5jfc18-your-team.vercel.app"
    assert _allow_origin_for(client, preview) == preview


def test_cors_blocks_unrelated_origin(client: TestClient) -> None:
    assert _allow_origin_for(client, "https://evil.example.com") is None


def test_cors_contract_origins_survive_config_drift(client: TestClient) -> None:
    """#477 regression: the fixture's configured origins DON'T include the
    onboarding wizard (simulating a stale env var overriding the YAML in
    prod) — the ADR-0003 contract origins must be code-pinned so no config
    source can silently drop a client."""
    wizard = "https://your-onboarding.vercel.app"
    assert _allow_origin_for(client, wizard) == wizard
