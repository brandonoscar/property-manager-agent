"""Places + static maps (Lane B — external, not in Buildium).

Answers "find a [vendor/place] near this property" and renders a static map
image for chat. Distinct from Lane A (``db.repositories.geo`` — spatial queries
over our OWN properties, free): this hits the Maps provider and costs per call,
so it only fires on an explicit request.

When no Maps key is set, ``places_nearby`` returns ``[]`` and ``static_map_url``
returns ``None`` so callers degrade to ``maps_unavailable`` — same discipline as
the geocoder. ``places_nearby`` never raises.

Provider: Google has the strong POI/business data the find-a-vendor use case
needs, so places is Google-first; Mapbox POI is thinner and not wired yet
(``places_nearby`` returns ``[]`` under ``provider="mapbox"``). Static maps work
on both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from helixis.settings import Settings

logger = logging.getLogger(__name__)

_GOOGLE_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_GOOGLE_STATICMAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
_MAPBOX_STATIC_TMPL = (
    "https://api.mapbox.com/styles/v1/mapbox/dark-v11/static/{markers}/auto/{size}"
)
# Brand iris (DESIGN.md --iris) for markers so the map reads as ours.
_MARKER_HEX = "8676c6"


@dataclass(frozen=True)
class Place:
    name: str
    address: str | None
    lat: float
    lng: float
    rating: float | None = None


def places_supported(settings: Settings) -> bool:
    """Can this deployment search nearby places AT ALL?

    ⚠ Exists so a caller can tell "searched, found nothing" from "cannot
    search". ``places_nearby`` returns ``[]`` for both — correct for a
    never-raises helper, and wrong the moment somebody renders it as prose.
    ``agents/places_tools`` rendered the empty list as "No plumber found within
    5 miles", which on the KEYLESS DEFAULT provider (census, which has no POI
    data at all) is a confident false statement about the world on every single
    call. Same class as gotchas 26/42/55.
    """
    cfg = settings.maps
    return bool(cfg.api_key) and cfg.provider not in ("census", "mapbox")


async def places_nearby(
    *,
    settings: Settings,
    lat: float,
    lng: float,
    keyword: str,
    radius_meters: int | None = None,
    limit: int = 20,
) -> list[Place]:
    """External places near ``(lat, lng)`` matching ``keyword`` (e.g.
    "plumber", "hardware store"), nearest-relevance first. ``[]`` when the Maps
    key is unset or the provider has no places support. Never raises."""
    cfg = settings.maps
    if not cfg.api_key or not keyword.strip():
        return []
    if cfg.provider == "mapbox":
        # Mapbox POI search is thinner and not wired yet — Google is the
        # places provider. Static maps still work on mapbox (see below).
        logger.info("places_nearby: mapbox provider has no places support yet")
        return []
    if cfg.provider == "census":
        # Census has no POI data at all; places stays a graceful [] on the
        # keyless default (there is no permissive OSS Places equivalent).
        return []
    radius = radius_meters or cfg.default_radius_meters
    try:
        import httpx

        async with httpx.AsyncClient(timeout=cfg.request_timeout_seconds) as client:
            resp = await client.get(
                _GOOGLE_NEARBY_URL,
                params={
                    "location": f"{lat},{lng}",
                    "radius": radius,
                    "keyword": keyword,
                    "key": cfg.api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("places_nearby failed for keyword=%r", keyword, exc_info=True)
        return []

    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        logger.warning("places_nearby non-OK status: %s", data.get("status"))
        return []
    out: list[Place] = []
    for r in (data.get("results") or [])[:limit]:
        loc = (r.get("geometry") or {}).get("location") or {}
        if "lat" not in loc or "lng" not in loc:
            continue
        out.append(
            Place(
                name=r.get("name") or "(unnamed)",
                address=r.get("vicinity"),
                lat=float(loc["lat"]),
                lng=float(loc["lng"]),
                rating=float(r["rating"]) if r.get("rating") is not None else None,
            )
        )
    return out


def static_map_url(
    *,
    settings: Settings,
    markers: list[tuple[float, float]],
    size: str = "600x400",
) -> str | None:
    """Build a static-map image URL with a pin per ``(lat, lng)`` in
    ``markers`` — returned inline in chat so "show it on a map" needs no live
    JS map. ``None`` when the Maps key is unset or there are no markers."""
    cfg = settings.maps
    if not cfg.api_key or not markers:
        return None
    if cfg.provider == "census":
        # Keyless default: there is no provider URL — the /maps/static route
        # renders the PNG itself (geo/static_render.py). Returning None here
        # routes it there even if a stale key is still set in the env.
        return None
    if cfg.provider == "mapbox":
        # Mapbox static wants "pin-s+HEX(lng,lat)" segments, comma-joined.
        pins = ",".join(f"pin-s+{_MARKER_HEX}({lng},{lat})" for lat, lng in markers)
        return _MAPBOX_STATIC_TMPL.format(markers=pins, size=size) + f"?access_token={cfg.api_key}"
    # Google: one markers param, pipe-joined "lat,lng".
    pts = "|".join(f"{lat},{lng}" for lat, lng in markers)
    return (
        f"{_GOOGLE_STATICMAP_URL}?size={size}&markers=color:0x{_MARKER_HEX}|{pts}&key={cfg.api_key}"
    )


# Sizes the static-map proxy is allowed to serve — an allowlist so the public
# proxy endpoint can't be coerced into arbitrary (billable) dimensions.
STATIC_MAP_SIZES = frozenset({"600x400", "600x300", "300x200"})
# Hard cap on markers per map — bounds both the URL and the provider bill.
STATIC_MAP_MAX_MARKERS = 12


def static_map_proxy_url(
    markers: list[tuple[float, float]],
    *,
    size: str = "600x400",
) -> str | None:
    """Key-free path to our OWN static-map proxy (``/api/v1/maps/static``) for
    inline chat rendering. The proxy rebuilds the provider URL with the server
    key, so the key never reaches the browser (unlike :func:`static_map_url`).
    Returns ``None`` when there are no markers; caps markers defensively."""
    if not markers:
        return None
    if size not in STATIC_MAP_SIZES:
        size = "600x400"
    pts = "|".join(f"{lat:.6f},{lng:.6f}" for lat, lng in markers[:STATIC_MAP_MAX_MARKERS])
    from urllib.parse import quote

    return f"/api/v1/maps/static?size={size}&markers={quote(pts, safe='|,')}"


__all__ = [
    "Place",
    "STATIC_MAP_MAX_MARKERS",
    "STATIC_MAP_SIZES",
    "places_nearby",
    "static_map_proxy_url",
    "static_map_url",
]
