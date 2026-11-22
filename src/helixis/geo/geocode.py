"""Geocoding — address string -> (lat, lng).

Deterministic + cached by design: each Buildium property address is geocoded
ONCE (on sync / backfill) and the point stored on ``helixis_properties.geog``.
The LLM never emits coordinates — it calls tools that read the stored points.

``provider`` is pluggable: ``census`` (default — the US Census Bureau
geocoder: public-domain, KEYLESS, US-only, which is the market), ``google``,
or ``mapbox``. The keyed providers return ``None`` when no API key is
configured; census needs none, so geocoding works on a fresh deploy with
zero configuration (the 2026-08-17 Google-Maps-exit — gotcha 18's release
gate — is retired by this default).

Census has no SLA, so the degrade discipline stays: any failure returns
``None`` and the caller falls back to ``maps_unavailable``. Never raises —
geocoding is best-effort infrastructure, never load-bearing enough to fail a
request over (same discipline as memory / redaction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from helixis.settings import Settings

logger = logging.getLogger(__name__)

_GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_MAPBOX_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{q}.json"
_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
# Pinned benchmark: "Public_AR_Current" tracks the latest public address
# ranges. The API 400s on an unknown benchmark rather than defaulting.
_CENSUS_BENCHMARK = "Public_AR_Current"


@dataclass(frozen=True)
class GeoPoint:
    """A resolved location. ``lat``/``lng`` are WGS84 degrees."""

    lat: float
    lng: float
    formatted_address: str | None = None


def geocoding_supported(settings: Settings) -> bool:
    """Can this deployment turn an address into a point AT ALL?

    ⚠ THE COUNTERPART TO ``places.places_supported``, and its absence is why
    three callers were checking the raw key instead. Since 2026-08-17 the
    DEFAULT provider is census, which is KEYLESS (gotcha 18 retired the Maps
    release gate on exactly that basis) — so ``not settings.maps.api_key`` is
    not "we cannot geocode", it is "we cannot geocode ON THE TWO PROVIDERS
    THAT CHARGE". Every caller that read the key directly refused to work on
    the configuration this product ships by default.

    The rule lives here and ``geocode`` itself consults it, so the predicate
    and the behaviour cannot drift into disagreeing (gotcha 28).
    """
    cfg = settings.maps
    return cfg.provider not in ("google", "mapbox") or bool(cfg.api_key)


async def geocode(address: str, *, settings: Settings) -> GeoPoint | None:
    """Resolve ``address`` to a :class:`GeoPoint`, or ``None`` if the provider
    cannot geocode, the address is blank, or it returns no match. Never
    raises."""
    cfg = settings.maps
    address = (address or "").strip()
    if not address:
        return None
    if not geocoding_supported(settings):
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=cfg.request_timeout_seconds) as client:
            if cfg.provider == "mapbox":
                return await _geocode_mapbox(client, address, cfg.api_key)
            if cfg.provider == "google":
                return await _geocode_google(client, address, cfg.api_key)
            return await _geocode_census(client, address)
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("geocode failed for address=%r", address, exc_info=True)
        return None


async def _geocode_google(client: Any, address: str, api_key: str) -> GeoPoint | None:
    resp = await client.get(_GOOGLE_URL, params={"address": address, "key": api_key})
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    # Google signals "no match" with status=ZERO_RESULTS and an empty list.
    if data.get("status") != "OK" or not results:
        return None
    top = results[0]
    loc = top["geometry"]["location"]
    return GeoPoint(
        lat=float(loc["lat"]),
        lng=float(loc["lng"]),
        formatted_address=top.get("formatted_address"),
    )


async def _geocode_mapbox(client: Any, address: str, api_key: str) -> GeoPoint | None:
    url = _MAPBOX_URL.format(q=quote(address))
    resp = await client.get(url, params={"access_token": api_key, "limit": 1})
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") or []
    if not features:
        return None
    # Mapbox returns center as [lng, lat].
    lng, lat = features[0]["center"]
    return GeoPoint(
        lat=float(lat),
        lng=float(lng),
        formatted_address=features[0].get("place_name"),
    )


async def _geocode_census(client: Any, address: str) -> GeoPoint | None:
    """US Census Bureau geocoder — public domain, keyless, US addresses only.

    Response shape: ``result.addressMatches[]`` where ``coordinates.x`` is
    LONGITUDE and ``coordinates.y`` is LATITUDE (x/y, not lat/lng — the
    classic transposition trap). No match = empty ``addressMatches``.
    """
    resp = await client.get(
        _CENSUS_URL,
        params={
            "address": address,
            "benchmark": _CENSUS_BENCHMARK,
            "format": "json",
        },
    )
    resp.raise_for_status()
    matches = (resp.json().get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    top = matches[0]
    coords = top["coordinates"]
    return GeoPoint(
        lat=float(coords["y"]),
        lng=float(coords["x"]),
        formatted_address=top.get("matchedAddress"),
    )


__all__ = ["GeoPoint", "geocode", "geocoding_supported"]
