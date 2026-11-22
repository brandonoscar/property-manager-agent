"""Tests for the property-intel clients (geo/intel.py) — mocked HTTP, zero
live calls. Pins each source's parse of the documented response shape, the
no-match answers, and the never-raise degrade contract."""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.geo import intel
from helixis.settings import Settings


def _settings(hud_api_key: str = "") -> Settings:
    s = Settings(env="test", memory={"enabled": False})
    s.intel.hud_api_key = hud_api_key
    return s


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, *, json_body=None, raises=None) -> MagicMock:
    """Replace httpx.AsyncClient with a fake whose ``get`` returns a response
    exposing ``json()`` + ``raise_for_status()`` (or raises). Same pattern as
    test_geocode.py."""
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body if json_body is not None else {})
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


# ── FEMA flood zone ───────────────────────────────────────────────────

_FEMA_OK = {
    "features": [{"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": None}}],
}


@pytest.mark.asyncio
async def test_flood_zone_parses_sfha(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_FEMA_OK)
    rec = await intel.flood_zone(29.95, -90.07, settings=_settings())
    assert rec is not None
    assert rec.source == "flood"
    assert rec.code == "AE"
    assert "high risk" in rec.label
    assert rec.detail["sfha"] is True
    params = get.await_args.kwargs["params"]
    # ArcGIS point geometry is "lng,lat" — the transposition trap.
    assert params["geometry"] == "-90.07,29.95"
    assert params["outFields"] == "FLD_ZONE,ZONE_SUBTY"


@pytest.mark.asyncio
async def test_flood_zone_x_minimal(monkeypatch):
    _fake_httpx(
        monkeypatch,
        json_body={
            "features": [
                {"attributes": {"FLD_ZONE": "X", "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD"}}
            ]
        },
    )
    rec = await intel.flood_zone(30.0, -97.0, settings=_settings())
    assert rec is not None
    assert rec.code == "X"
    assert "minimal" in rec.label.lower()
    assert rec.detail["sfha"] is False


@pytest.mark.asyncio
async def test_flood_zone_no_feature_is_a_persistable_answer(monkeypatch):
    """No intersecting polygon is still a record (the backfill's work-list
    sentinel relies on flood always writing after a successful probe)."""
    _fake_httpx(monkeypatch, json_body={"features": []})
    rec = await intel.flood_zone(30.0, -97.0, settings=_settings())
    assert rec is not None
    assert rec.code is None
    assert "not in a mapped" in rec.label.lower()
    assert rec.detail == {"mapped": False}


@pytest.mark.asyncio
async def test_flood_zone_arcgis_error_object_returns_none(monkeypatch):
    # ArcGIS reports failure as HTTP 200 + an ``error`` object.
    _fake_httpx(monkeypatch, json_body={"error": {"code": 500, "message": "boom"}})
    assert await intel.flood_zone(30.0, -97.0, settings=_settings()) is None


@pytest.mark.asyncio
async def test_flood_zone_swallows_network_error(monkeypatch):
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await intel.flood_zone(30.0, -97.0, settings=_settings()) is None


# ── USFS wildfire hazard potential ────────────────────────────────────


@pytest.mark.asyncio
async def test_wildfire_hazard_parses_class(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body={"objectId": 0, "name": "Pixel", "value": "4"})
    rec = await intel.wildfire_hazard(39.75, -105.05, settings=_settings())
    assert rec is not None
    assert rec.source == "wildfire"
    assert rec.code == "4"
    assert "high" in rec.label
    assert rec.detail == {"mapped": True, "whp_class": 4, "whp_label": "high"}
    # ArcGIS identify takes a JSON point geometry with x=lng / y=lat.
    geometry = get.await_args.kwargs["params"]["geometry"]
    assert '"x":-105.05' in geometry and '"y":39.75' in geometry


@pytest.mark.asyncio
async def test_wildfire_hazard_asks_the_classified_raster_not_the_refusing_host(monkeypatch):
    """apps.fs.usda.gov answered 403 to every production call (gotcha 193).
    The continuous raster answers an index, which would parse as no class."""
    get = _fake_httpx(monkeypatch, json_body={"value": "2"})
    await intel.wildfire_hazard(39.75, -105.05, settings=_settings())
    url = get.await_args.args[0]
    assert url.startswith("https://imagery.geoplatform.gov/") and url.endswith("/identify")
    assert "WildfireHazardPotentialClassified/ImageServer" in url
    assert get.await_args.kwargs["headers"]["User-Agent"].startswith("Occupella/")
    assert get.await_args.kwargs["follow_redirects"] is True


@pytest.mark.asyncio
async def test_wildfire_hazard_nodata(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"value": "NoData"})
    rec = await intel.wildfire_hazard(21.3, -157.8, settings=_settings())
    assert rec is not None
    assert rec.code is None
    assert rec.detail == {"mapped": False}


@pytest.mark.asyncio
async def test_wildfire_hazard_error_and_junk_return_none(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"error": {"code": 400}})
    assert await intel.wildfire_hazard(39.0, -105.0, settings=_settings()) is None
    _fake_httpx(monkeypatch, json_body={"value": "purple"})
    assert await intel.wildfire_hazard(39.0, -105.0, settings=_settings()) is None
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await intel.wildfire_hazard(39.0, -105.0, settings=_settings()) is None


# ── EPA county radon zone ─────────────────────────────────────────────

_CENSUS_COUNTY_OK = {
    "result": {
        "geographies": {
            "Counties": [{"GEOID": "08031", "NAME": "Denver County"}],
        }
    }
}


@pytest.mark.asyncio
async def test_radon_zone_resolves_county_then_bundled_table(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_CENSUS_COUNTY_OK)
    rec = await intel.radon_zone(39.74, -104.99, settings=_settings())
    assert rec is not None
    assert rec.source == "radon"
    # Denver County is Zone 1 in the bundled EPA table.
    assert rec.code == "1"
    assert "high radon potential" in rec.label
    assert rec.detail == {"county_fips": "08031", "zone": 1}
    params = get.await_args.kwargs["params"]
    # Census takes x=LONGITUDE / y=LATITUDE.
    assert params["x"] == -104.99 and params["y"] == 39.74


@pytest.mark.asyncio
async def test_radon_zone_unknown_county_returns_none(monkeypatch):
    _fake_httpx(
        monkeypatch,
        json_body={"result": {"geographies": {"Counties": [{"GEOID": "99999"}]}}},
    )
    assert await intel.radon_zone(18.4, -66.1, settings=_settings()) is None


@pytest.mark.asyncio
async def test_radon_zone_no_county_match_returns_none(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"result": {"geographies": {"Counties": []}}})
    assert await intel.radon_zone(0.0, 0.0, settings=_settings()) is None


@pytest.mark.asyncio
async def test_radon_zone_swallows_error(monkeypatch):
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await intel.radon_zone(39.0, -104.0, settings=_settings()) is None


def test_radon_table_loads_and_covers_most_counties():
    table = intel._radon_zone_table()
    assert len(table) > 3000  # ~3,100 counties bundled
    assert set(table.values()) == {1, 2, 3}
    assert table["19153"] == 1  # Polk County IA — all of Iowa is Zone 1


# ── EPA Superfund (NPL) sites ─────────────────────────────────────────


def _npl(
    name: Any, status: Any, lat: Any, lng: Any, *, geometry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One feature of EPA's NPL layer as an ArcGIS ``query`` returns it:
    attributes plus a WGS84 point (``outSR=4326``; x is LONGITUDE)."""
    return {
        "attributes": {"Site_Name": name, "Status": status, "Latitude": lat, "Longitude": lng},
        "geometry": geometry if geometry is not None else {"x": lng, "y": lat},
    }


_NPL_OK = {
    "features": [
        # Inside the bounding box but outside the true radius (corner).
        _npl("FAR CORNER SITE", "Final", 30.0135, -90.0605),
        _npl("AGRICULTURE STREET LANDFILL", "Final", 29.996, -90.06),
        # A "Not on the NPL" row must be dropped.
        _npl("SOME SCREENING SITE", "Not on the NPL", 29.995, -90.061),
        _npl("NEARER SITE", "Proposed", 29.996, -90.07),
    ]
}


@pytest.mark.asyncio
async def test_superfund_sites_filters_status_and_radius_nearest_first(monkeypatch):
    _fake_httpx(monkeypatch, json_body=_NPL_OK)
    sites = await intel.superfund_sites(29.996, -90.078, settings=_settings(), radius_km=2.0)
    assert sites is not None
    assert [s["name"] for s in sites] == ["NEARER SITE", "AGRICULTURE STREET LANDFILL"]
    assert [s["npl_status"] for s in sites] == ["Proposed", "Final"]
    assert 0 < sites[0]["distance_km"] < sites[1]["distance_km"] <= 2.0


@pytest.mark.asyncio
async def test_superfund_sites_queries_the_npl_layer_with_a_lng_lat_envelope(monkeypatch):
    """Data.epa.gov's SEMS_ACTIVE_SITES answered 404 to every production call
    (gotcha 193). The envelope is the box around the point, x = longitude."""
    get = _fake_httpx(monkeypatch, json_body={"features": []})
    await intel.superfund_sites(29.996, -90.078, settings=_settings(), radius_km=2.0)
    url = get.await_args.args[0]
    assert url.startswith("https://services.arcgis.com/") and url.endswith("/FeatureServer/0/query")
    assert "Superfund_National_Priorities_List_(NPL)" in url
    params = get.await_args.kwargs["params"]
    assert params["geometryType"] == "esriGeometryEnvelope" and params["where"] == "1=1"
    assert params["inSR"] == 4326 and params["outSR"] == 4326
    assert params["returnGeometry"] == "true" and params["outFields"] == "*"
    xmin, ymin, xmax, ymax = (float(v) for v in params["geometry"].split(","))
    assert (xmin + xmax) / 2 == pytest.approx(-90.078) and (ymin + ymax) / 2 == pytest.approx(
        29.996
    )
    assert ymax - ymin == pytest.approx(2 * 2.0 / 110.574, rel=1e-3)
    # A degree of longitude is shorter off the equator, so the box widens.
    assert xmax - xmin == pytest.approx(
        2 * 2.0 / (110.574 * math.cos(math.radians(29.996))), rel=1e-3
    )
    assert get.await_args.kwargs["headers"]["User-Agent"].startswith("Occupella/")
    assert get.await_args.kwargs["follow_redirects"] is True


@pytest.mark.asyncio
async def test_superfund_sites_reads_fields_in_any_casing(monkeypatch):
    feature = {
        "attributes": {"SITE_NAME": "UPPER CASE SITE", "STATUS": "Deleted"},
        "geometry": {"x": -90.07, "y": 29.996},
    }
    _fake_httpx(monkeypatch, json_body={"features": [feature]})
    sites = await intel.superfund_sites(29.996, -90.078, settings=_settings())
    assert sites is not None
    assert [(s["name"], s["npl_status"]) for s in sites] == [("UPPER CASE SITE", "Deleted")]


@pytest.mark.asyncio
async def test_superfund_sites_falls_back_to_the_coordinate_fields(monkeypatch):
    """No geometry, or one in metres (a server that ignored outSR), is not a
    point; the Latitude/Longitude fields are. Geometry wins when it is one."""
    no_geometry = _npl("FIELDS ONLY", "Final", 29.996, -90.07)
    del no_geometry["geometry"]
    metres = _npl("METRES", "Final", 29.996, -90.071, geometry={"x": -10026777.0, "y": 3498000.0})
    # The fields say far away; the geometry says near. Geometry is what matched.
    near = _npl("GEOMETRY WINS", "Final", 31.0, -91.0, geometry={"x": -90.072, "y": 29.996})
    _fake_httpx(monkeypatch, json_body={"features": [no_geometry, metres, near]})
    sites = await intel.superfund_sites(29.996, -90.078, settings=_settings())
    assert sites is not None
    assert [s["name"] for s in sites] == ["GEOMETRY WINS", "METRES", "FIELDS ONLY"]


@pytest.mark.asyncio
async def test_superfund_sites_a_missing_status_or_name_is_said_not_dropped(monkeypatch):
    feature = _npl(None, "", 29.996, -90.07)
    _fake_httpx(monkeypatch, json_body={"features": [feature]})
    sites = await intel.superfund_sites(29.996, -90.078, settings=_settings())
    assert sites is not None
    assert [(s["name"], s["npl_status"]) for s in sites] == [("Unnamed site", "NPL status unknown")]


@pytest.mark.asyncio
async def test_superfund_sites_clean_is_empty_list(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"features": []})
    assert await intel.superfund_sites(45.0, -122.0, settings=_settings()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # ArcGIS reports failure as HTTP 200 + an ``error`` object.
        {"error": {"code": 400, "message": "Invalid URL", "details": []}},
        {},
        {"features": "nope"},
        [],
        # A site in the box that cannot be placed or read: "none nearby"
        # could be false, so the check failed.
        {"features": [_npl("NO POINT", "Final", None, None, geometry={})]},
        {"features": [_npl("NAN", "Final", "nan", "nan", geometry={"x": "nan", "y": "nan"})]},
        {"features": [{"geometry": {"x": -90.07, "y": 29.996}}]},
        {"features": ["junk"]},
    ],
)
async def test_superfund_sites_failure_is_none_not_clean(monkeypatch, body):
    """None (couldn't check) must be distinguishable from [] (clean)."""
    _fake_httpx(monkeypatch, json_body=body)
    assert await intel.superfund_sites(29.996, -90.078, settings=_settings()) is None


@pytest.mark.asyncio
async def test_superfund_sites_network_failure_is_none(monkeypatch):
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await intel.superfund_sites(45.0, -122.0, settings=_settings()) is None


@pytest.mark.asyncio
async def test_superfund_sites_an_unplaceable_row_off_the_npl_is_just_dropped(monkeypatch):
    feature = _npl("SCREENING ONLY", "Not on the NPL", None, None, geometry={})
    _fake_httpx(monkeypatch, json_body={"features": [feature]})
    assert await intel.superfund_sites(29.996, -90.078, settings=_settings()) == []


# ── HUD Fair Market Rents ─────────────────────────────────────────────

_HUD_OK = {
    "data": {
        "county_name": "Orleans Parish",
        "metro_status": "1",
        "area_name": "New Orleans-Metairie, LA HUD Metro FMR Area",
        "smallarea_status": "1",
        "year": "2026",
        "basicdata": [
            {
                "zip_code": "70115",
                "year": "2026",
                "Efficiency": 1080,
                "One-Bedroom": 1200,
                "Two-Bedroom": 1420,
                "Three-Bedroom": 1830,
                "Four-Bedroom": 2110,
            },
            {
                "zip_code": "70118",
                "year": "2026",
                "Efficiency": 990,
                "One-Bedroom": 1100,
                "Two-Bedroom": 1300,
                "Three-Bedroom": 1680,
                "Four-Bedroom": 1940,
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_fmr_parses_smallarea_list_and_picks_our_zip(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_HUD_OK)
    rec = await intel.fmr_for_zip("70115", settings=_settings(hud_api_key="tok"))
    assert rec is not None
    assert rec.source == "fmr"
    assert rec.code == "1420"
    assert rec.detail["two_bedroom"] == 1420
    assert rec.detail["four_bedroom"] == 2110
    assert rec.detail["year"] == "2026"
    assert rec.detail["zip_code"] == "70115"
    # Bearer token forwarded; the ZIP is in the path.
    assert get.await_args.kwargs["headers"]["Authorization"] == "Bearer tok"
    assert "70115" in get.await_args.args[0]


@pytest.mark.asyncio
async def test_fmr_parses_single_area_dict(monkeypatch):
    body = {
        "data": {
            "year": "2026",
            "basicdata": {
                "year": "2026",
                "Efficiency": 700,
                "One-Bedroom": 800,
                "Two-Bedroom": 950,
                "Three-Bedroom": 1200,
                "Four-Bedroom": 1400,
            },
        }
    }
    _fake_httpx(monkeypatch, json_body=body)
    rec = await intel.fmr_for_zip("52501", settings=_settings(hud_api_key="tok"))
    assert rec is not None
    assert rec.detail["two_bedroom"] == 950


@pytest.mark.asyncio
async def test_fmr_without_key_returns_none_without_calling(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_HUD_OK)
    assert await intel.fmr_for_zip("70115", settings=_settings(hud_api_key="")) is None
    get.assert_not_awaited()  # dark without the free HUD key — no HTTP call


@pytest.mark.asyncio
async def test_fmr_blank_zip_and_errors_return_none(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_HUD_OK)
    assert await intel.fmr_for_zip("  ", settings=_settings(hud_api_key="tok")) is None
    get.assert_not_awaited()
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await intel.fmr_for_zip("70115", settings=_settings(hud_api_key="tok")) is None
    _fake_httpx(monkeypatch, json_body={"data": {"basicdata": []}})
    assert await intel.fmr_for_zip("70115", settings=_settings(hud_api_key="tok")) is None
