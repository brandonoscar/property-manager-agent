"""Static-map image proxy — the key-free ``src`` for inline chat maps.

The location tools (``properties_near`` / ``find_places_near_property``) emit a
``map`` SSE event whose ``url`` points HERE, never at the raw provider URL —
because :func:`helixis.geo.places.static_map_url` embeds the Maps API key, and
an <img> in a chat bubble would leak that key to every browser. This endpoint
takes only marker coordinates, rebuilds the provider URL WITH the server key
server-side, and streams the PNG back.

Locked down because it's unauthenticated (an <img> src can't carry a JWT) and
each hit is a billable provider call: markers are strictly parsed and capped
(:data:`helixis.geo.places.STATIC_MAP_MAX_MARKERS`), ``size`` is an allowlist
(:data:`helixis.geo.places.STATIC_MAP_SIZES`), and it's IP-rate-limited. No
part of the request reaches the provider except validated numbers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from helixis.api.rate_limit import maps_static_limiter
from helixis.geo.places import (
    STATIC_MAP_MAX_MARKERS,
    STATIC_MAP_SIZES,
    static_map_url,
)
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/maps", tags=["maps"])


def _parse_markers(raw: str) -> list[tuple[float, float]]:
    """Parse ``"lat,lng|lat,lng"`` into validated points. Raises HTTP 400 on any
    malformed / out-of-range / over-cap input — fail closed, never forward junk
    to the provider."""
    points: list[tuple[float, float]] = []
    for seg in (raw or "").split("|"):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split(",")
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="Bad marker (expected 'lat,lng').")
        try:
            lat, lng = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Marker coords must be numbers.") from exc
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            raise HTTPException(status_code=400, detail="Marker coords out of range.")
        points.append((lat, lng))
    if not points:
        raise HTTPException(status_code=400, detail="No markers given.")
    if len(points) > STATIC_MAP_MAX_MARKERS:
        raise HTTPException(status_code=400, detail="Too many markers.")
    return points


@router.get("/static", summary="Proxy a provider static-map image (key stays server-side)")
async def static_map(
    request: Request,
    markers: str = Query(..., description="Pipe-joined 'lat,lng' points."),
    size: str = Query("600x400", description="Image size (allowlisted)."),
) -> Response:
    await maps_static_limiter.check_by_ip(request)

    if size not in STATIC_MAP_SIZES:
        size = "600x400"
    points = _parse_markers(markers)

    settings = get_settings()
    url = static_map_url(settings=settings, markers=points, size=size)

    if url is None:
        # Keyless path (census default, or keyed provider without a key):
        # render the PNG OURSELVES over OSM tiles — no provider, no key, no
        # bill (2026-08-17 Google-Maps-exit). Same 502 degrade on failure.
        import asyncio

        from helixis.geo.static_render import render_static_map

        png = await asyncio.to_thread(render_static_map, tuple(points), size)
        if png is None:
            raise HTTPException(status_code=502, detail="Map renderer unavailable.")
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — upstream hiccup; surface as 502, never leak the URL/key
        logger.warning("static map fetch failed (%d markers)", len(points), exc_info=True)
        raise HTTPException(status_code=502, detail="Map provider unavailable.") from None

    content_type = resp.headers.get("content-type", "image/png")
    return Response(
        content=resp.content,
        media_type=content_type,
        # Immutable for a day: the same markers always render the same tile, and
        # the browser cache spares both us and the provider a repeat call.
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


__all__ = ["router"]
