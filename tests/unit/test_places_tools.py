"""Unit tests for the Lane-B places agent tool (agents/places_tools.py).

Mocks the resolver, geocoder, and places client — no live DB, no Maps API.
Pins the honesty contract: unconfigured / no-match / ambiguous / no-coords,
the geocode fallback for ungeocoded properties, and result formatting with
the not-a-vetted-vendor caveat.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import places_tools
from helixis.db.repositories.geo import ResolvedProperty
from helixis.geo.geocode import GeoPoint
from helixis.geo.places import Place


def _ctx(company_id: str = "co-1") -> MagicMock:
    ctx = MagicMock()
    ctx.company_id = company_id
    return ctx


def _settings(api_key: str = "k", provider: str = "google") -> MagicMock:
    """⚠ Default = a deployment that CAN search places: google, keyed.

    `places_supported` is stricter than `geocoding_supported` — census
    geocodes perfectly and has no POI data at any price — so a places fixture
    has to name its provider. It used to leave `provider` a bare MagicMock,
    truthy and matching nothing, which is a state no deployment is in
    (gotchas 67/72/73/96).
    """
    settings = MagicMock()
    settings.maps.api_key = api_key
    settings.maps.provider = provider
    return settings


def _prop(lat: float | None = 30.26, lng: float | None = -97.74) -> ResolvedProperty:
    return ResolvedProperty(700, "Garden Row", "1 Garden Way, Austin, TX", lat, lng)


@pytest.mark.asyncio
async def test_a_deployment_without_place_search_says_so_honestly() -> None:
    """⚠ THIS TEST WAS PINNING THE DEFECT, and it is the whole finding here.

    It asserted the "Maps integration is unconfigured (set
    HELIXIS_MAPS__API_KEY)" wording, produced by a raw-key bail at the TOP of
    the function that SHADOWED the honest `places_supported` branch 45 lines
    below. On the keyless census default that advice cannot help — census has
    no POI data at any price — so the customer was told to buy a key that
    changes nothing, and the branch written to say the true thing was
    unreachable. An assertion holds a defect in place as firmly as it catches
    one (gotchas 68/74/93).

    Re-pointed at the property that matters: the reply must separate "I cannot
    search" from "there is nothing nearby" (gotchas 26/42/55).
    """
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings(provider="census")),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(places_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_prop()])),
        patch.object(places_tools, "places_nearby", AsyncMock(return_value=[])),
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Garden Row", 5.0)
    assert "no place search" in out
    assert "NOT the same as there being no plumber" in out
    assert "HELIXIS_MAPS__API_KEY" not in out


@pytest.mark.asyncio
async def test_missing_keyword_asks() -> None:
    with patch.object(places_tools, "current_memory_context", return_value=_ctx()):
        out = await places_tools._find_places_near_property_impl("  ", "Garden Row", 5.0)
    assert "What kind of place" in out


@pytest.mark.asyncio
async def test_no_property_match() -> None:
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(places_tools.geo_repo, "resolve_property", AsyncMock(return_value=[])),
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Nowhere", 5.0)
    assert "don't have a property matching 'Nowhere'" in out


@pytest.mark.asyncio
async def test_ambiguous_property_asks() -> None:
    matches = [_prop(), ResolvedProperty(701, "Garden Court", "2 Court St", 1.0, 2.0)]
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(places_tools.geo_repo, "resolve_property", AsyncMock(return_value=matches)),
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Garden", 5.0)
    assert "Multiple properties match" in out and "Which one?" in out


@pytest.mark.asyncio
async def test_ungeocoded_property_falls_back_to_geocode() -> None:
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(
            places_tools.geo_repo,
            "resolve_property",
            AsyncMock(return_value=[_prop(lat=None, lng=None)]),
        ),
        patch.object(
            places_tools, "geocode", AsyncMock(return_value=GeoPoint(lat=1.0, lng=2.0))
        ) as gc,
        patch.object(
            places_tools,
            "places_nearby",
            AsyncMock(return_value=[Place("Apex Plumbing", "9 Pipe St", 1.0, 2.0, 4.6)]),
        ) as pn,
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Garden Row", 5.0)
    gc.assert_awaited_once()  # geocode fallback fired
    # The places call used the geocoded coords.
    assert pn.await_args.kwargs["lat"] == 1.0 and pn.await_args.kwargs["lng"] == 2.0
    assert "Apex Plumbing" in out


@pytest.mark.asyncio
async def test_ungeocoded_and_ungeocodable_is_honest() -> None:
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(
            places_tools.geo_repo,
            "resolve_property",
            AsyncMock(return_value=[_prop(lat=None, lng=None)]),
        ),
        patch.object(places_tools, "geocode", AsyncMock(return_value=None)),
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Garden Row", 5.0)
    assert "don't have coordinates" in out


@pytest.mark.asyncio
async def test_lists_places_with_caveat() -> None:
    from helixis.agents.visual_tools import drain_pending_maps

    await drain_pending_maps()  # clear any cross-test residue first
    places = [
        Place("Apex Plumbing", "9 Pipe St", 30.3, -97.7, 4.6),
        Place("Budget Rooter", None, 30.31, -97.71, None),
    ]
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(places_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_prop()])),
        patch.object(places_tools, "places_nearby", AsyncMock(return_value=places)),
    ):
        out = await places_tools._find_places_near_property_impl("plumber", "Garden Row", 5.0)
    assert "Plumber near Garden Row (within 5 miles)" in out
    assert "Apex Plumbing (9 Pipe St) — ★4.6" in out
    assert "- Budget Rooter" in out  # no address/rating still lists
    assert "not vetted vendors" in out
    # A static map was auto-queued: the property + both found places.
    from helixis.agents.visual_tools import drain_pending_maps

    maps = await drain_pending_maps()
    assert len(maps) == 1
    assert len(maps[0]["markers"]) == 3  # (property) + 2 places
    assert maps[0]["url"].startswith("/api/v1/maps/static?")


@pytest.mark.asyncio
async def test_no_places_found() -> None:
    with (
        patch.object(places_tools, "current_memory_context", return_value=_ctx()),
        patch.object(places_tools, "get_settings", return_value=_settings()),
        patch.object(places_tools, "get_pool", return_value=MagicMock()),
        patch.object(places_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_prop()])),
        patch.object(places_tools, "places_nearby", AsyncMock(return_value=[])),
    ):
        out = await places_tools._find_places_near_property_impl("locksmith", "Garden Row", 2.0)
    assert "No locksmith found within 2 miles of Garden Row" in out
