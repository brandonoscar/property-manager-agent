"""Tests for the static-map image proxy (``GET /api/v1/maps/static``).

The proxy is what makes inline chat maps safe: it takes only marker coords,
rebuilds the provider URL WITH the server key server-side, and streams the PNG —
so the Google key never reaches the browser. These tests cover the contract:
valid markers stream the image, malformed/over-range/over-cap input fails
closed with 400, and the keyless path (census default / no key) self-renders
via geo/static_render.py instead of going dark.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"


def _settings(*, api_key: str = "test-maps-key") -> Settings:
    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
    )
    s.maps.provider = "google"
    s.maps.api_key = api_key
    return s


@pytest.fixture
def client_factory(request):
    """Build a TestClient with a given Settings + a fake httpx that returns a
    canned PNG (so no real Google call). Returns (client, fake_get). Patches are
    started manually and torn down via a finalizer so they stay active for the
    duration of the test's request (a ``with`` block would tear down on return)."""

    def _make(settings: Settings):
        # Reset the per-IP limiter so cross-test traffic can't 429 us.
        from helixis.api.rate_limit import maps_static_limiter

        maps_static_limiter.counter._log.clear()

        resp = MagicMock()
        resp.content = b"\x89PNG\r\n\x1a\nFAKEBYTES"
        resp.headers = {"content-type": "image/png"}
        resp.raise_for_status = MagicMock()
        fake_get = AsyncMock(return_value=resp)
        fake_client = MagicMock()
        fake_client.get = fake_get
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=fake_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        patchers = [
            patch("helixis.api.routes.maps.get_settings", return_value=settings),
            patch.object(httpx, "AsyncClient", MagicMock(return_value=cm)),
        ]
        for p in patchers:
            p.start()
            request.addfinalizer(p.stop)

        app = create_app(settings)

        @asynccontextmanager
        async def noop_lifespan(app):
            yield

        app.router.lifespan_context = noop_lifespan
        client = TestClient(app)
        client.__enter__()
        request.addfinalizer(lambda: client.__exit__(None, None, None))
        return client, fake_get

    return _make


def test_static_map_streams_png_and_keeps_key_server_side(client_factory):
    client, fake_get = client_factory(_settings())
    resp = client.get("/api/v1/maps/static?markers=42.36,-71.06|42.35,-71.05")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG")
    # The KEY is used server-side (in the outbound Google URL) but never
    # returned to the browser.
    outbound = fake_get.await_args.args[0]
    assert "key=test-maps-key" in outbound
    assert "test-maps-key" not in resp.text
    assert "cache-control" in {k.lower() for k in resp.headers}


def test_static_map_rejects_malformed_markers(client_factory):
    client, _ = client_factory(_settings())
    assert client.get("/api/v1/maps/static?markers=not-a-coord").status_code == 400
    assert client.get("/api/v1/maps/static?markers=42.3").status_code == 400  # missing lng
    assert client.get("/api/v1/maps/static?markers=").status_code == 400


def test_static_map_rejects_out_of_range(client_factory):
    client, _ = client_factory(_settings())
    # lat 99 is out of [-90, 90]
    assert client.get("/api/v1/maps/static?markers=99.0,10.0").status_code == 400


def test_static_map_caps_marker_count(client_factory):
    client, _ = client_factory(_settings())
    many = "|".join(f"{40 + i / 100},-71" for i in range(20))
    assert client.get(f"/api/v1/maps/static?markers={many}").status_code == 400


def test_static_map_self_renders_when_keyless(client_factory):
    """No provider key (or the census default) → the route renders the PNG
    ITSELF via geo/static_render.py instead of 503ing — the keyless path that
    retired the Maps release gate (2026-08-17). Render is mocked: real tile
    fetches are network and never belong in unit tests."""
    with patch(
        "helixis.geo.static_render.render_static_map",
        return_value=b"\x89PNG\r\n\x1a\nSELFRENDERED",
    ) as fake_render:
        client, fake_get = client_factory(_settings(api_key=""))
        resp = client.get("/api/v1/maps/static?markers=42.36,-71.06")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")
        fake_get.assert_not_awaited()  # no provider call on the keyless path
        fake_render.assert_called_once()
        (markers, size) = fake_render.call_args.args
        assert markers == ((42.36, -71.06),)
        assert size == "600x400"


def test_static_map_self_render_failure_is_502(client_factory):
    """Tile outage / render failure degrades exactly like a provider outage."""
    with patch("helixis.geo.static_render.render_static_map", return_value=None):
        client, _ = client_factory(_settings(api_key=""))
        resp = client.get("/api/v1/maps/static?markers=42.36,-71.06")
        assert resp.status_code == 502


def test_static_map_census_provider_self_renders_even_with_stale_key(client_factory):
    """provider=census ignores a leftover api_key — never builds a Google URL."""
    with patch(
        "helixis.geo.static_render.render_static_map",
        return_value=b"\x89PNG\r\n\x1a\nSELFRENDERED",
    ):
        settings = _settings(api_key="stale-dead-key")
        settings.maps.provider = "census"
        client, fake_get = client_factory(settings)
        resp = client.get("/api/v1/maps/static?markers=42.36,-71.06")
        assert resp.status_code == 200
        fake_get.assert_not_awaited()
        assert "stale-dead-key" not in resp.text
