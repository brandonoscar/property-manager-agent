"""Self-rendered static maps — the keyless replacement for provider PNGs.

Renders marker maps with py-staticmaps (MIT) over OpenStreetMap raster tiles,
so the ``/api/v1/maps/static`` proxy can serve chat maps with NO provider key
and NO per-image bill. Attribution ("© OpenStreetMap contributors") is drawn
onto the image by the library — keep it: it is the one obligation the ODbL
tile data carries.

Design constraints:
- **Sync render, thread offload.** py-staticmaps fetches tiles with blocking
  ``requests``; callers run :func:`render_static_map` via
  ``asyncio.to_thread`` so the event loop never blocks on tile I/O.
- **Small in-process LRU.** The same markers+size always renders the same
  image; caching spares the OSM tile servers (whose usage policy asks for
  restraint) and makes repeat chat renders instant. Bounded so memory stays
  trivial (~256 PNGs × ~50 KB).
- **Never raises to the route.** Tile fetch failures return ``None``; the
  route degrades to 502 the same way the provider-proxy path always has.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_MARKER_RGB = (0x86, 0x76, 0xC6)  # --iris, the brand accent


def _parse_size(size: str) -> tuple[int, int]:
    w, h = size.split("x", 1)
    return int(w), int(h)


@lru_cache(maxsize=256)
def render_static_map(markers: tuple[tuple[float, float], ...], size: str) -> bytes | None:
    """Render a PNG with one pin per ``(lat, lng)``; ``None`` on any failure.

    ``markers`` is a tuple (hashable) — callers convert from list. The route
    has already validated ranges, cap, and the size allowlist; this function
    trusts its inputs.
    """
    try:
        import staticmaps

        width, height = _parse_size(size)
        ctx = staticmaps.Context()
        ctx.set_tile_provider(staticmaps.tile_provider_OSM)
        color = staticmaps.Color(*_MARKER_RGB)
        for lat, lng in markers:
            ctx.add_object(
                staticmaps.Marker(
                    staticmaps.create_latlng(lat, lng),
                    color=color,
                    size=12,
                )
            )
        image = ctx.render_pillow(width, height)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — tile outage / render hiccup; degrade
        logger.warning("static map render failed (%d markers)", len(markers), exc_info=True)
        return None


__all__ = ["render_static_map"]
