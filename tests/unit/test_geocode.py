"""Tests for the geocoding client — mocked HTTP, no live Maps API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.geo.geocode import GeoPoint, geocode
from helixis.settings import Settings


def _settings(**maps) -> Settings:
    s = Settings(env="test", memory={"enabled": False})
    defaults = {"provider": "google", "api_key": "test-key"}
    defaults.update(maps)
    for k, v in defaults.items():
        setattr(s.maps, k, v)
    return s


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, *, json_body=None, raises=None) -> MagicMock:
    """Replace httpx.AsyncClient with a fake whose ``get`` returns a response
    exposing ``json()`` + ``raise_for_status()`` (or raises)."""
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


_GOOGLE_OK = {
    "status": "OK",
    "results": [
        {
            "geometry": {"location": {"lat": 40.7128, "lng": -74.006}},
            "formatted_address": "88 Birch Lane, Queens, NY 11378, USA",
        }
    ],
}


@pytest.mark.asyncio
async def test_geocode_google_success(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_GOOGLE_OK)
    pt = await geocode("88 Birch Lane, Queens, NY", settings=_settings())
    assert pt == GeoPoint(
        lat=40.7128, lng=-74.006, formatted_address="88 Birch Lane, Queens, NY 11378, USA"
    )
    # Address + key were forwarded as params.
    assert get.await_args.kwargs["params"]["address"] == "88 Birch Lane, Queens, NY"
    assert get.await_args.kwargs["params"]["key"] == "test-key"


@pytest.mark.asyncio
async def test_geocode_blank_key_returns_none_without_calling(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_GOOGLE_OK)
    assert await geocode("anywhere", settings=_settings(api_key="")) is None
    get.assert_not_awaited()  # no HTTP call when disabled


@pytest.mark.asyncio
async def test_geocode_blank_address_returns_none(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_GOOGLE_OK)
    assert await geocode("   ", settings=_settings()) is None
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_geocode_google_zero_results_returns_none(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"status": "ZERO_RESULTS", "results": []})
    assert await geocode("nowhere", settings=_settings()) is None


@pytest.mark.asyncio
async def test_geocode_swallows_provider_error(monkeypatch):
    """A network / provider error degrades to None, never propagates."""
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await geocode("88 Birch Lane", settings=_settings()) is None


@pytest.mark.asyncio
async def test_geocode_mapbox_success(monkeypatch):
    body = {"features": [{"center": [-74.006, 40.7128], "place_name": "88 Birch Lane, Queens, NY"}]}
    _fake_httpx(monkeypatch, json_body=body)
    pt = await geocode("88 Birch Lane", settings=_settings(provider="mapbox"))
    # Mapbox returns [lng, lat] — the client must not swap them.
    assert pt is not None
    assert (round(pt.lat, 4), round(pt.lng, 4)) == (40.7128, -74.006)


_CENSUS_OK = {
    "result": {
        "addressMatches": [
            {
                # Census returns x=LONGITUDE, y=LATITUDE — the transposition
                # trap the parser must get right.
                "coordinates": {"x": -76.9327, "y": 38.8462},
                "matchedAddress": "4600 SILVER HILL RD, WASHINGTON, DC, 20233",
            }
        ]
    }
}


@pytest.mark.asyncio
async def test_geocode_census_success_and_xy_order(monkeypatch):
    """Census is the keyless default: works with NO api_key, and x/y map to
    lng/lat (not lat/lng)."""
    get = _fake_httpx(monkeypatch, json_body=_CENSUS_OK)
    pt = await geocode("4600 Silver Hill Rd", settings=_settings(provider="census", api_key=""))
    assert pt == GeoPoint(
        lat=38.8462,
        lng=-76.9327,
        formatted_address="4600 SILVER HILL RD, WASHINGTON, DC, 20233",
    )
    # The keyless call really went out (unlike keyed providers without a key).
    assert get.await_count == 1
    params = get.await_args.kwargs.get("params") or get.await_args.args[-1]
    assert params["benchmark"] == "Public_AR_Current"


@pytest.mark.asyncio
async def test_geocode_census_no_match_returns_none(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"result": {"addressMatches": []}})
    assert (
        await geocode("nowhere at all", settings=_settings(provider="census", api_key="")) is None
    )


@pytest.mark.asyncio
async def test_keyed_providers_still_require_key_census_does_not(monkeypatch):
    """The no-key dark-out applies ONLY to google/mapbox; census is keyless by
    design (the retired Maps release gate — gotcha 18)."""
    get = _fake_httpx(monkeypatch, json_body=_CENSUS_OK)
    assert await geocode("x", settings=_settings(provider="google", api_key="")) is None
    assert await geocode("x", settings=_settings(provider="mapbox", api_key="")) is None
    assert get.await_count == 0  # keyed providers never called without a key
    assert await geocode("x", settings=_settings(provider="census", api_key="")) is not None


def test_default_provider_is_census():
    """A fresh Settings() geocodes with zero configuration."""
    s = Settings(env="test", memory={"enabled": False})
    assert s.maps.provider == "census"
