"""Property intelligence clients — free, keyless-or-free-key geo-data facts.

Five sources, all keyed off a property's stored coordinates (populated by
``geo/backfill.py`` onto ``helixis_properties.geog``):

* ``flood_zone``      — FEMA NFHL flood zone at the point (keyless).
* ``wildfire_hazard`` — USFS Wildfire Hazard Potential class 1-5, from the
                        federal GeoPlatform copy of the 2023 classified
                        raster (keyless).
* ``radon_zone``      — EPA county radon zone 1-3 (county FIPS via the Census
                        geocoder, zone from a bundled public-domain table).
* ``superfund_sites`` — EPA NPL (Superfund) sites near the point, from EPA's
                        NPL feature layer on ArcGIS Online (keyless).
* ``fmr_for_zip``     — HUD Fair Market Rents for a ZIP (free key —
                        ``HELIXIS_INTEL__HUD_API_KEY``; dark without it).

Same iron discipline as :mod:`helixis.geo.geocode`: never raise — catch
``Exception``, log, return ``None`` (or ``None`` vs ``[]`` for the site list,
where ``[]`` means "checked, clean" and ``None`` means "couldn't check").
These are enhancements, never load-bearing; a failed lookup just leaves that
source's row absent until the next backfill pass.

None of these endpoints have an SLA (same class as the census geocoder —
gotcha 18); results are cached in ``helixis_property_intel``, so a steady
state makes zero calls for the static sources.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from helixis.settings import Settings

logger = logging.getLogger(__name__)

# ``helixis_property_intel.source`` values (one row per source per property).
SOURCE_FLOOD = "flood"
SOURCE_WILDFIRE = "wildfire"
SOURCE_RADON = "radon"
SOURCE_SUPERFUND = "superfund"
SOURCE_FMR = "fmr"

# FEMA National Flood Hazard Layer — layer 28 is the flood-hazard-zone polygon
# layer of the public NFHL MapServer.
_FEMA_NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"

# USFS Wildfire Hazard Potential (classified, 2023) — ArcGIS ImageServer
# ``identify`` returns the class raster value at a point. This is the federal
# GeoPlatform copy of the USFS raster. The USFS's own service
# (apps.fs.usda.gov/fsgisx01/.../RMRS_WildfireHazardPotential_Classified_2023)
# answered 403 "Access is denied." to every call from the production worker
# (Render logs, 2026-10-03; gotcha 193). Chosen from the public catalog
# entry: no session container can reach the host, so the first enrichment
# pass after that deploy made the first live call. "wildfire_hazard failed"
# in the worker log means it did not answer.
_WHP_IDENTIFY_URL = (
    "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/"
    "USFS_EDW_RMRS_WildfireHazardPotentialClassified/ImageServer/identify"
)

# Sent by the wildfire and Superfund lookups, which name the caller. A
# government WAF can refuse a bare library user agent (``python-httpx/…``).
_USER_AGENT = "Occupella/1.0 (+https://occupella.com)"

# Census geographies lookup (point -> geography GEOIDs). Same keyless service
# the geocoder uses; benchmark pinned like geo/geocode.py. Shared (public name)
# with geo/jurisdiction.py, which resolves the full state/county/place stack
# from the same endpoint.
CENSUS_GEO_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

# EPA's "Superfund National Priorities List (NPL) Sites with Status
# Information" feature layer, in EPA's own ArcGIS Online organisation: one
# point per NPL site (proposed, final or deleted) with its status. It replaces
# the Envirofacts table SEMS_ACTIVE_SITES, which answered 404 to every call
# from the production worker (Render logs, 2026-10-03; gotcha 193). Same
# provenance as the wildfire URL above: chosen from the public catalog entry,
# first called live by the worker. Queried with a bounding box (an envelope
# filter every ArcGIS layer supports); the true distance is computed here.
_EPA_NPL_QUERY_URL = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "Superfund_National_Priorities_List_(NPL)_Sites_with_Status_Information/"
    "FeatureServer/0/query"
)

# HUD Fair Market Rents (free key from huduser.gov registration).
_HUD_FMR_URL = "https://www.huduser.gov/hudapi/public/fmr/data/{zip}"

_KM_PER_DEG_LAT = 110.574


@dataclass(frozen=True)
class IntelRecord:
    """One source's answer for one point — the shape upserted into
    ``helixis_property_intel`` (``code``/``label`` headline + full ``detail``)."""

    source: str
    code: str | None
    label: str
    detail: dict[str, Any] = field(default_factory=dict)


# ── FEMA flood zone ───────────────────────────────────────────────────

# FEMA zone designations. A*/V* are the SFHA (Special Flood Hazard Area,
# 1% annual chance); X is minimal-to-moderate depending on subtype; D is
# unstudied. Zone letters beyond these (A1-A30 / V1-V30) share their
# family's meaning via the prefix fallback below.
_FLOOD_ZONE_LABELS = {
    "A": "A — high risk (1% annual chance), no base flood elevation determined",
    "AE": "AE — high risk (1% annual chance)",
    "AH": "AH — high risk, shallow ponding (1-3 ft)",
    "AO": "AO — high risk, shallow sheet flow (1-3 ft)",
    "AR": "AR — high risk, flood control system being restored",
    "A99": "A99 — high risk, federal levee under construction",
    "V": "V — very high risk, coastal wave hazard",
    "VE": "VE — very high risk, coastal wave hazard",
    "X": "X — minimal to moderate risk (outside the 1% floodplain)",
    "B": "B — moderate risk (0.2% annual chance)",
    "C": "C — minimal risk",
    "D": "D — flood risk undetermined (unstudied area)",
}


def _flood_zone_label(zone: str, subtype: str | None) -> str:
    zone = (zone or "").strip().upper()
    if zone == "X" and subtype:
        sub = subtype.strip().upper()
        if "0.2" in sub:
            return "X (shaded) — moderate risk (0.2% annual chance)"
        if "MINIMAL" in sub:
            return "X — minimal risk"
    if zone in _FLOOD_ZONE_LABELS:
        return _FLOOD_ZONE_LABELS[zone]
    # A1-A30 / V1-V30 numbered zones inherit their family's meaning.
    if zone.startswith("V"):
        return f"{zone} — very high risk, coastal wave hazard"
    if zone.startswith("A"):
        return f"{zone} — high risk (1% annual chance)"
    return zone or "unknown"


async def flood_zone(lat: float, lng: float, *, settings: Settings) -> IntelRecord | None:
    """FEMA NFHL flood zone at the point, or ``None`` on any failure.

    A successful query with NO intersecting polygon still returns a record
    ("not in a mapped flood zone") so the backfill can persist the answer and
    stop re-probing; only an actual failure returns ``None``.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(
                _FEMA_NFHL_URL,
                params={
                    "geometry": f"{lng},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "FLD_ZONE,ZONE_SUBTY",
                    "returnGeometry": "false",
                    "f": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        # ArcGIS reports errors as 200 + an ``error`` object — treat as failure.
        if "error" in data:
            logger.warning("flood_zone: NFHL error for (%s,%s): %s", lat, lng, data["error"])
            return None
        features = data.get("features") or []
        if not features:
            return IntelRecord(
                source=SOURCE_FLOOD,
                code=None,
                label="Not in a mapped FEMA flood zone",
                detail={"mapped": False},
            )
        attrs = features[0].get("attributes") or {}
        zone = (attrs.get("FLD_ZONE") or "").strip().upper()
        subtype = attrs.get("ZONE_SUBTY") or None
        return IntelRecord(
            source=SOURCE_FLOOD,
            code=zone or None,
            label=_flood_zone_label(zone, subtype),
            detail={
                "mapped": True,
                "fld_zone": zone,
                "zone_subtype": subtype,
                "sfha": zone.startswith(("A", "V")),
            },
        )
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("flood_zone failed for (%s,%s)", lat, lng, exc_info=True)
        return None


# ── USFS wildfire hazard potential ────────────────────────────────────

_WHP_LABELS = {
    1: "very low",
    2: "low",
    3: "moderate",
    4: "high",
    5: "very high",
    6: "non-burnable",
    7: "water",
}


async def wildfire_hazard(lat: float, lng: float, *, settings: Settings) -> IntelRecord | None:
    """USFS Wildfire Hazard Potential class (1-5) at the point, or ``None``.

    ArcGIS ImageServer ``identify`` on the CLASSIFIED raster (the continuous
    one answers an index, not a class). It also carries 6 (non-burnable) and
    7 (water), which are kept honestly labeled rather than forced onto the
    1-5 scale; any other value is a failure, never a class.
    """
    try:
        import httpx

        geometry = json.dumps(
            {"x": lng, "y": lat, "spatialReference": {"wkid": 4326}}, separators=(",", ":")
        )
        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(
                _WHP_IDENTIFY_URL,
                params={
                    "geometry": geometry,
                    "geometryType": "esriGeometryPoint",
                    "returnGeometry": "false",
                    "returnCatalogItems": "false",
                    "f": "json",
                },
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            logger.warning("wildfire_hazard: WHP error for (%s,%s): %s", lat, lng, data["error"])
            return None
        raw = str(data.get("value", "")).strip()
        # "NoData" (outside the raster, e.g. offshore) is a real answer.
        if not raw or raw.lower() == "nodata":
            return IntelRecord(
                source=SOURCE_WILDFIRE,
                code=None,
                label="No wildfire hazard data at this location",
                detail={"mapped": False},
            )
        klass = int(float(raw))
        label = _WHP_LABELS.get(klass)
        if label is None:
            logger.warning("wildfire_hazard: unexpected WHP class %r at (%s,%s)", raw, lat, lng)
            return None
        return IntelRecord(
            source=SOURCE_WILDFIRE,
            code=str(klass),
            label=f"Wildfire hazard potential: {label}",
            detail={"mapped": True, "whp_class": klass, "whp_label": label},
        )
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("wildfire_hazard failed for (%s,%s)", lat, lng, exc_info=True)
        return None


# ── EPA county radon zone ─────────────────────────────────────────────

_RADON_CSV = Path(__file__).resolve().parent / "data" / "radon_zones.csv"

_RADON_ZONE_LABELS = {
    1: "Zone 1 — high radon potential (predicted average over 4 pCi/L; EPA action level)",
    2: "Zone 2 — moderate radon potential (predicted average 2-4 pCi/L)",
    3: "Zone 3 — low radon potential (predicted average under 2 pCi/L)",
}


@lru_cache(maxsize=1)
def _radon_zone_table() -> dict[str, int]:
    """The bundled EPA county radon zone table (``county_fips -> zone``).

    Public-domain EPA "Map of Radon Zones" county assignments. Coverage is
    ~3,100 counties; a county absent from the table (e.g. Puerto Rico, the
    2022 Connecticut planning regions) simply yields ``None`` from
    :func:`radon_zone` — never a guess.
    """
    table: dict[str, int] = {}
    try:
        with open(_RADON_CSV, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                fips = (row.get("county_fips") or "").strip()
                zone = (row.get("zone") or "").strip()
                if len(fips) == 5 and zone in ("1", "2", "3"):
                    table[fips] = int(zone)
    except Exception:  # noqa: BLE001 — missing/corrupt data file → empty table
        logger.warning("radon zone table failed to load from %s", _RADON_CSV, exc_info=True)
    return table


async def _county_fips(lat: float, lng: float, *, settings: Settings) -> str | None:
    """County GEOID (5-digit FIPS) for the point via the Census geographies
    endpoint (keyless, US-only — same service family as the geocoder)."""
    import httpx

    async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
        resp = await client.get(
            CENSUS_GEO_URL,
            params={
                "x": lng,
                "y": lat,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "format": "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    counties = ((data.get("result") or {}).get("geographies") or {}).get("Counties") or []
    if not counties:
        return None
    geoid = str(counties[0].get("GEOID") or "").strip()
    return geoid or None


async def radon_zone(lat: float, lng: float, *, settings: Settings) -> IntelRecord | None:
    """EPA radon zone (county-level, 1=high / 2=moderate / 3=low) for the
    point, or ``None`` when the county can't be resolved or isn't in the
    bundled table. Never raises."""
    try:
        fips = await _county_fips(lat, lng, settings=settings)
        if not fips:
            return None
        zone = _radon_zone_table().get(fips)
        if zone is None:
            logger.info("radon_zone: county %s not in the bundled EPA table", fips)
            return None
        return IntelRecord(
            source=SOURCE_RADON,
            code=str(zone),
            label=f"EPA radon {_RADON_ZONE_LABELS[zone]}",
            detail={"county_fips": fips, "zone": zone},
        )
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("radon_zone failed for (%s,%s)", lat, lng, exc_info=True)
        return None


# ── EPA Superfund (NPL) sites nearby ──────────────────────────────────


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _attr(attrs: dict[str, Any], name: str) -> Any:
    """An ArcGIS attribute by field name in any casing (``Site_Name`` or
    ``SITE_NAME``); a layer republished from another tool can change it."""
    if name in attrs:
        return attrs[name]
    wanted = name.lower()
    for key, value in attrs.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value
    return None


def _site_point(feature: dict[str, Any], attrs: dict[str, Any]) -> tuple[float, float] | None:
    """``(lat, lng)`` of one NPL feature: its geometry (asked for in WGS84),
    else its ``Latitude``/``Longitude`` fields. A pair outside the degree
    ranges is not a point (a server that ignored ``outSR`` answers in metres),
    and neither is NaN."""
    geometry = feature.get("geometry")
    candidates: list[tuple[Any, Any]] = []
    if isinstance(geometry, dict):
        candidates.append((geometry.get("y"), geometry.get("x")))
    candidates.append((_attr(attrs, "Latitude"), _attr(attrs, "Longitude")))
    for raw_lat, raw_lng in candidates:
        try:
            s_lat, s_lng = float(raw_lat), float(raw_lng)
        except (TypeError, ValueError):
            continue
        if -90.0 <= s_lat <= 90.0 and -180.0 <= s_lng <= 180.0:
            return s_lat, s_lng
    return None


async def superfund_sites(
    lat: float,
    lng: float,
    *,
    settings: Settings,
    radius_km: float = 2.0,
) -> list[dict[str, Any]] | None:
    """NPL (National Priorities List — Superfund) sites within ``radius_km``
    of the point, nearest first: ``[{name, npl_status, distance_km}]``.

    ``[]`` means checked-and-clean; ``None`` means the check itself failed
    (so the caller must NOT record a clean answer). A body without a
    ``features`` list (ArcGIS reports errors as HTTP 200 + an ``error``
    object) is a failure, and so is a site in the box that cannot be placed:
    dropping it could turn the one nearby site into "none". The layer holds
    NPL sites only; a status reading "Not on the NPL" is still dropped, and a
    missing status is kept and said, never hidden.
    """
    try:
        import httpx

        dlat = radius_km / _KM_PER_DEG_LAT
        dlng = radius_km / (_KM_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat))))
        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(
                _EPA_NPL_QUERY_URL,
                params={
                    "where": "1=1",
                    # Envelope "xmin,ymin,xmax,ymax": x is LONGITUDE.
                    "geometry": (
                        f"{lng - dlng:.6f},{lat - dlat:.6f},{lng + dlng:.6f},{lat + dlat:.6f}"
                    ),
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": 4326,
                    "f": "json",
                },
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            logger.warning(
                "superfund_sites: no NPL features for (%s,%s): %s",
                lat,
                lng,
                data.get("error") if isinstance(data, dict) else type(data).__name__,
            )
            return None
        sites: list[dict[str, Any]] = []
        for feature in features:
            attrs = feature.get("attributes") if isinstance(feature, dict) else None
            if not isinstance(attrs, dict):
                logger.warning("superfund_sites: unreadable NPL feature near (%s,%s)", lat, lng)
                return None
            status = str(_attr(attrs, "Status") or "").strip()
            if "not on the npl" in status.lower():
                continue
            point = _site_point(feature, attrs)
            if point is None:
                logger.warning("superfund_sites: an NPL site near (%s,%s) has no point", lat, lng)
                return None
            dist = _haversine_km(lat, lng, *point)
            if dist > radius_km:
                continue  # bounding-box corner outside the true radius
            sites.append(
                {
                    "name": str(_attr(attrs, "Site_Name") or "").strip() or "Unnamed site",
                    "npl_status": status or "NPL status unknown",
                    "distance_km": round(dist, 2),
                }
            )
        sites.sort(key=lambda s: s["distance_km"])
        return sites
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("superfund_sites failed for (%s,%s)", lat, lng, exc_info=True)
        return None


# ── HUD Fair Market Rents ─────────────────────────────────────────────

_FMR_BEDROOM_KEYS = (
    ("Efficiency", "efficiency"),
    ("One-Bedroom", "one_bedroom"),
    ("Two-Bedroom", "two_bedroom"),
    ("Three-Bedroom", "three_bedroom"),
    ("Four-Bedroom", "four_bedroom"),
)


def _fmr_basic_entry(basicdata: Any, zip_code: str) -> dict[str, Any] | None:
    """``basicdata`` is a dict for one-FMR areas and a list of per-ZIP
    entries in Small Area FMR metros — pick the entry for our ZIP (falling
    back to the first)."""
    if isinstance(basicdata, dict):
        return basicdata
    if isinstance(basicdata, list) and basicdata:
        for entry in basicdata:
            if isinstance(entry, dict) and str(entry.get("zip_code") or "") == zip_code:
                return entry
        first = basicdata[0]
        return first if isinstance(first, dict) else None
    return None


async def fmr_for_zip(zip_code: str, *, settings: Settings) -> IntelRecord | None:
    """HUD Fair Market Rents for a ZIP, or ``None`` when the (free) HUD API
    key is unconfigured, the ZIP is blank/unknown, or the call fails. Never
    raises. Key: ``HELIXIS_INTEL__HUD_API_KEY`` (register at huduser.gov)."""
    token = settings.intel.hud_api_key
    zip_code = (zip_code or "").strip()[:5]
    if not token or not zip_code:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(
                _HUD_FMR_URL.format(zip=zip_code),
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        entry = _fmr_basic_entry(payload.get("basicdata"), zip_code)
        if entry is None:
            return None
        rents: dict[str, int] = {}
        for hud_key, ours in _FMR_BEDROOM_KEYS:
            try:
                rents[ours] = int(entry[hud_key])
            except (KeyError, TypeError, ValueError):
                continue
        if not rents:
            return None
        year = entry.get("year") or payload.get("year")
        two = rents.get("two_bedroom")
        label = (
            f"HUD FMR {year}: ${two:,}/mo two-bedroom"
            if two is not None and year
            else f"HUD Fair Market Rents ({year})"
            if year
            else "HUD Fair Market Rents"
        )
        return IntelRecord(
            source=SOURCE_FMR,
            code=str(two) if two is not None else None,
            label=label,
            detail={
                "zip_code": zip_code,
                "year": year,
                "area_name": payload.get("area_name"),
                "smallarea_status": payload.get("smallarea_status"),
                **rents,
            },
        )
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("fmr_for_zip failed for zip=%s", zip_code, exc_info=True)
        return None


__all__ = [
    "SOURCE_FLOOD",
    "SOURCE_FMR",
    "SOURCE_RADON",
    "SOURCE_SUPERFUND",
    "SOURCE_WILDFIRE",
    "IntelRecord",
    "flood_zone",
    "fmr_for_zip",
    "radon_zone",
    "superfund_sites",
    "wildfire_hazard",
]
