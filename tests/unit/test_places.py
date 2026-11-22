"""Tests for the Places + static-map client — mocked HTTP, no live Maps API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.geo.places import (
    STATIC_MAP_MAX_MARKERS,
    Place,
    places_nearby,
    static_map_proxy_url,
    static_map_url,
)
from helixis.settings import Settings


def _settings(**maps) -> Settings:
    s = Settings(env="test", memory={"enabled": False})
    defaults = {"provider": "google", "api_key": "test-key"}
    defaults.update(maps)
    for k, v in defaults.items():
        setattr(s.maps, k, v)
    return s


def _fake_httpx(monkeypatch, *, json_body=None, raises=None) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body or {})
    resp.raise_for_status = MagicMock()
    get = AsyncMock(return_value=resp)
    if raises is not None:
        get.side_effect = raises
    client = MagicMock()
    client.get = get
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    return get


_NEARBY_OK = {
    "status": "OK",
    "results": [
        {
            "name": "Birch Lane Plumbing & Heating",
            "vicinity": "482 Industrial Way, Queens",
            "geometry": {"location": {"lat": 40.72, "lng": -73.9}},
            "rating": 4.6,
        },
        {  # missing geometry -> skipped, not crashed
            "name": "No Coords Co",
            "vicinity": "somewhere",
        },
    ],
}


@pytest.mark.asyncio
async def test_places_nearby_google_maps_and_skips_coordless(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_NEARBY_OK)
    out = await places_nearby(
        settings=_settings(), lat=40.71, lng=-74.0, keyword="plumber", radius_meters=5000
    )
    assert out == [
        Place(
            name="Birch Lane Plumbing & Heating",
            address="482 Industrial Way, Queens",
            lat=40.72,
            lng=-73.9,
            rating=4.6,
        )
    ]
    params = get.await_args.kwargs["params"]
    assert params["location"] == "40.71,-74.0"
    assert params["keyword"] == "plumber"
    assert params["radius"] == 5000


@pytest.mark.asyncio
async def test_places_nearby_blank_key_returns_empty_without_calling(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_NEARBY_OK)
    assert await places_nearby(settings=_settings(api_key=""), lat=0, lng=0, keyword="x") == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_places_nearby_mapbox_unsupported_returns_empty(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_NEARBY_OK)
    out = await places_nearby(
        settings=_settings(provider="mapbox"), lat=1, lng=2, keyword="plumber"
    )
    assert out == []
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_places_nearby_swallows_provider_error(monkeypatch):
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await places_nearby(settings=_settings(), lat=1, lng=2, keyword="plumber") == []


@pytest.mark.asyncio
async def test_places_nearby_zero_results_is_empty_not_error(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"status": "ZERO_RESULTS", "results": []})
    assert await places_nearby(settings=_settings(), lat=1, lng=2, keyword="unicorn vet") == []


def test_static_map_url_google_builds_markers_and_key():
    url = static_map_url(settings=_settings(), markers=[(40.71, -74.0), (40.72, -73.9)])
    assert url is not None
    assert url.startswith("https://maps.googleapis.com/maps/api/staticmap?")
    assert "markers=color:0x8676c6|40.71,-74.0|40.72,-73.9" in url
    assert "key=test-key" in url


def test_static_map_url_mapbox_format():
    url = static_map_url(settings=_settings(provider="mapbox"), markers=[(40.71, -74.0)])
    assert url is not None
    # Mapbox wants pin-s+HEX(lng,lat) and the token as a query param.
    assert "pin-s+8676c6(-74.0,40.71)" in url
    assert "access_token=test-key" in url


def test_static_map_url_none_without_key_or_markers():
    assert static_map_url(settings=_settings(api_key=""), markers=[(1, 2)]) is None
    assert static_map_url(settings=_settings(), markers=[]) is None


# ── static_map_proxy_url (maps #539) — the KEY-FREE path for inline chat ──


def test_static_map_proxy_url_is_key_free_and_relative():
    url = static_map_proxy_url([(40.71, -74.0), (40.72, -73.9)])
    assert url is not None
    # Points at OUR proxy, carries NO provider key, and is same-origin relative.
    assert url.startswith("/api/v1/maps/static?")
    assert "key" not in url
    assert "googleapis" not in url
    assert "40.710000,-74.000000" in url
    assert "40.720000,-73.900000" in url


def test_static_map_proxy_url_caps_markers():
    many = [(1.0 + i / 100, 2.0) for i in range(30)]
    url = static_map_proxy_url(many)
    assert url is not None
    # Only STATIC_MAP_MAX_MARKERS points survive (pipe-joined => N-1 separators).
    assert url.count("%7C") + url.count("|") == STATIC_MAP_MAX_MARKERS - 1


def test_static_map_proxy_url_bad_size_falls_back():
    url = static_map_proxy_url([(1.0, 2.0)], size="99999x99999")
    assert url is not None
    assert "size=600x400" in url


def test_static_map_proxy_url_none_without_markers():
    assert static_map_proxy_url([]) is None
