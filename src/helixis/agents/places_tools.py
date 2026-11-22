"""Lane B agent tool — find an external vendor/service near one of the
company's properties.

Distinct from the Lane A ``properties_near`` mirror tool (which finds the
company's OWN properties by proximity, free, over PostGIS): this anchors on a
property, then hits the Maps provider's Places API to surface nearby
BUSINESSES ("a plumber near Garden Row", "hardware stores near 88 Birch
Lane"). It costs per call, so it only fires on an explicit find-a-vendor ask.

Honest degradations, never a fake answer: no Maps key ⇒ "unconfigured"; the
property isn't on file / is ambiguous ⇒ ask; the property has no coordinates
and its address can't be geocoded ⇒ say so.
"""

from __future__ import annotations

import logging

from agents import function_tool

from helixis.agents.visual_tools import queue_map
from helixis.db.engine import get_pool
from helixis.db.repositories import geo as geo_repo
from helixis.geo.geocode import geocode
from helixis.geo.places import places_nearby, places_supported
from helixis.memory.context import current_memory_context
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

_MILE_M = 1609.34
_NO_COMPANY = "I couldn't determine which company this session belongs to."
# ⚠ NO `_MAPS_UNCONFIGURED` constant here, and its absence is deliberate.
# There was one, and it said "set HELIXIS_MAPS__API_KEY" — advice that cannot
# help on the keyless census default, because census has no POI data at any
# price. `places_supported`'s own branch below says the true thing instead.
# A convenient constant carrying the wrong message is what gets re-wired into
# a new early return (gotcha 74's deleted `greeting_or_default`).


def _company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


async def _find_places_near_property_impl(
    keyword: str, property_query: str, radius_miles: float
) -> str:
    company_id = _company_id()
    if not company_id:
        return _NO_COMPANY
    keyword = (keyword or "").strip()
    if not keyword:
        return "What kind of place should I look for (e.g. 'plumber', 'hardware store')?"
    if not (property_query or "").strip():
        return "Which property should I search around? Give me its name or address."
    settings = get_settings()
    # ⚠ NO raw-key bail here. There WAS one, and it SHADOWED the honest branch
    # 45 lines below: on the keyless census default it answered "set
    # HELIXIS_MAPS__API_KEY" — an instruction that cannot help, because census
    # has no POI data at any price — where `places_supported` says the true
    # thing, that this deployment cannot search and that is NOT evidence there
    # is nothing nearby. Two gates for one question, and the worse one ran
    # first. `places_supported` is the only place that decides (gotcha 107).
    pool = get_pool()
    try:
        matches = await geo_repo.resolve_property(pool, company_id=company_id, query=property_query)
    except Exception:  # noqa: BLE001 — missing 0053 migration / no geog column
        logger.warning("resolve_property failed", exc_info=True)
        matches = []
    if not matches:
        return (
            f"I don't have a property matching '{property_query}' on file. "
            "Use its exact name or address."
        )
    if len(matches) > 1:
        names = ", ".join(m.name or f"#{m.buildium_id}" for m in matches)
        return f"Multiple properties match '{property_query}': {names}. Which one?"

    prop = matches[0]
    lat, lng = prop.lat, prop.lng
    # Not geocoded yet? Fall back to geocoding its address on the fly.
    if lat is None or lng is None:
        point = await geocode(prop.address, settings=settings) if prop.address else None
        if point is None:
            return (
                f"I don't have coordinates for {prop.name or 'that property'} yet and "
                "couldn't geocode its address. Occupella's geocoder covers US "
                "addresses only, so an address outside the US cannot be placed — "
                "that is a limit of our lookup, NOT evidence that the property or "
                "the address is wrong. Do not tell the user to wait for a sync; a "
                "re-sync cannot change this."
            )
        lat, lng = point.lat, point.lng

    radius_miles = max(0.1, min(float(radius_miles or 5.0), 30.0))
    places = await places_nearby(
        settings=settings,
        lat=lat,
        lng=lng,
        keyword=keyword,
        radius_meters=int(radius_miles * _MILE_M),
        limit=10,
    )
    where = prop.name or prop.address or "the property"
    if not places:
        # ⚠ Two different facts, and only one of them is about the world.
        if not places_supported(settings):
            return (
                f"I can't search for nearby {keyword} — this deployment's map "
                "provider has no place search, so I have no data either way. "
                f"That is NOT the same as there being no {keyword} near {where}; "
                "do not report it as one."
            )
        return f"No {keyword} found within {radius_miles:g} miles of {where}."
    lines = [f"{keyword.title()} near {where} (within {radius_miles:g} miles):"]
    for p in places:
        rating = f" — ★{p.rating:g}" if p.rating is not None else ""
        addr = f" ({p.address})" if p.address else ""
        lines.append(f"- {p.name}{addr}{rating}")
    # Render a static map inline: the property + each found place.
    queue_map(
        [(lat, lng)] + [(p.lat, p.lng) for p in places],
        caption=f"{keyword.title()} near {where}",
    )
    lines.append("These are public search results, not vetted vendors — confirm before hiring.")
    return "\n".join(lines)


@function_tool
async def find_places_near_property(
    keyword: str, property_query: str, radius_miles: float = 5.0
) -> str:
    """Find external businesses/services near one of the company's properties
    using live Maps data — e.g. "a plumber near Garden Row", "hardware stores
    near 88 Birch Lane".

    ``keyword`` is what to look for ("plumber", "locksmith", "hardware store").
    ``property_query`` names the property to search around (name or address).
    ``radius_miles`` defaults to 5.

    This is a LIVE external lookup (costs per call), so use it only when the
    user explicitly wants to find a vendor/place near a property — NOT for
    "which of my properties are near X" (that's ``properties_near``) and NOT
    for the company's own Buildium vendor list (that's ``contact_lookup`` /
    the live buildium vendor tools).
    """
    return await _find_places_near_property_impl(keyword, property_query, radius_miles)


places_tools = [find_places_near_property]

__all__ = ["find_places_near_property", "places_tools"]
