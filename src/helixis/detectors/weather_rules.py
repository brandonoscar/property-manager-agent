"""Weather-alert detector — active NWS alerts over the geocoded portfolio.

Queries the National Weather Service ``alerts/active`` endpoint (keyless,
public domain; NWS REQUIRES an identifying ``User-Agent``) for each distinct
geocoded property point, keeps only serious alerts (severity Severe/Extreme
AND an event class a property manager acts on: freeze / wind / winter /
hurricane / tornado / flood), and emits ONE Finding per distinct alert with
every affected property listed in its evidence — "Hard Freeze Warning
affects 9 properties", not nine findings.

Ships dark (``default_enabled = False``) like the other mirror-state
detectors; promote via ``HELIXIS_DETECTORS__ENABLED_IDS=weather_alert``
(the ``invoice_no_work_order`` precedent in render.yaml).

Dedup/caching:
* per scan, alerts are deduped by NWS alert id — N properties under one
  county-wide alert produce one Finding whose fingerprint is the alert id;
* query points are rounded (~1 km) so units of one complex share a lookup;
* a short module-level TTL cache carries responses across companies within
  one detector cycle, so a sweep doesn't re-ask NWS for the same point.

An expired alert simply stops being emitted and the framework's stale-resolve
sweep closes its Finding. NWS calls are best-effort per point (a failed
lookup skips that point, never the scan) — same degrade discipline as the
rest of the geo stack.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.registry import register
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

_NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
# NWS rejects anonymous clients — an identifying UA is required by their
# API terms of use.
_NWS_USER_AGENT = "Occupella (github.com/brandonoscar/property-manager-agent)"

# Only alerts a PM acts on. Substring match against the event name.
EVENT_ALLOWLIST = ("freeze", "wind", "winter", "hurricane", "tornado", "flood")
# NWS severity values worth surfacing (of Extreme/Severe/Moderate/Minor/Unknown).
SEVERITY_ALLOWLIST = frozenset({"Severe", "Extreme"})

# NWS severity -> Finding severity. Extreme alerts (habitability-level) ride
# the urgent path of the attention governor; Severe rides the capped digest.
_FINDING_SEVERITY = {"Extreme": "critical", "Severe": "warning"}

# Round query points to ~1 km so units of one complex share a lookup without
# meaningfully moving the point out of its alert polygon.
_POINT_DECIMALS = 2

# Module-level point->alerts cache. One detector cycle sweeps every company
# sequentially, so a few minutes of TTL means shared points (or re-runs in
# quick succession) don't re-hit NWS; alerts change on the order of hours.
_CACHE_TTL_SECONDS = 600.0
_alert_cache: dict[tuple[float, float], tuple[float, list[dict[str, Any]]]] = {}

_GEOCODED_PROPERTIES_SQL = """
SELECT buildium_id, name,
       ST_Y(geog::geometry) AS lat,
       ST_X(geog::geometry) AS lng
  FROM helixis_properties
 WHERE company_id = $1::uuid
   AND geog IS NOT NULL
"""


def _cache_get(point: tuple[float, float]) -> list[dict[str, Any]] | None:
    hit = _alert_cache.get(point)
    if hit is None:
        return None
    stamped, alerts = hit
    if time.monotonic() - stamped >= _CACHE_TTL_SECONDS:
        _alert_cache.pop(point, None)
        return None
    return alerts


def _cache_put(point: tuple[float, float], alerts: list[dict[str, Any]]) -> None:
    _alert_cache[point] = (time.monotonic(), alerts)


def clear_alert_cache() -> None:
    """Test isolation only."""
    _alert_cache.clear()


def is_relevant_alert(props: dict[str, Any]) -> bool:
    """Severity Severe/Extreme AND an allow-listed event class."""
    if str(props.get("severity") or "") not in SEVERITY_ALLOWLIST:
        return False
    event = str(props.get("event") or "").lower()
    return any(token in event for token in EVENT_ALLOWLIST)


async def _fetch_alerts(lat: float, lng: float, timeout: float) -> list[dict[str, Any]]:
    """Active alert properties covering the point. Best-effort: any failure
    logs and returns [] (that point contributes nothing this scan)."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                _NWS_ALERTS_URL,
                params={"point": f"{lat},{lng}"},
                headers={"User-Agent": _NWS_USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[dict[str, Any]] = []
        for feature in data.get("features") or []:
            props = (feature or {}).get("properties") or {}
            alert_id = props.get("id") or (feature or {}).get("id")
            if not alert_id:
                continue
            props = dict(props)
            props["id"] = str(alert_id)
            out.append(props)
        return out
    except Exception:  # noqa: BLE001 — best-effort; skip this point, keep scanning
        logger.warning("weather detector: NWS lookup failed for (%s,%s)", lat, lng, exc_info=True)
        return []


class WeatherAlertDetector(Detector):
    """Active severe-weather alerts affecting geocoded properties."""

    detector_id = "weather_alert"
    detector_class = STATE_RULE
    default_enabled = False
    rule_keys = ("weather_alert",)
    reads_tables = frozenset({"helixis_properties"})

    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        settings = get_settings()
        timeout = settings.maps.request_timeout_seconds
        rows = await conn.fetch(_GEOCODED_PROPERTIES_SQL, company_id)
        if not rows:
            return []

        # Group properties by rounded point so one complex = one NWS call.
        by_point: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for r in rows:
            point = (
                round(float(r["lat"]), _POINT_DECIMALS),
                round(float(r["lng"]), _POINT_DECIMALS),
            )
            by_point.setdefault(point, []).append(
                {"buildium_id": r["buildium_id"], "name": r["name"]}
            )

        # alert id -> (alert props, affected properties). The cross-property
        # dedup: one county-wide alert covering nine properties is ONE entry.
        alerts: dict[str, dict[str, Any]] = {}
        affected: dict[str, list[dict[str, Any]]] = {}
        for point, props_at_point in by_point.items():
            cached = _cache_get(point)
            if cached is None:
                cached = await _fetch_alerts(point[0], point[1], timeout)
                _cache_put(point, cached)
            for alert in cached:
                if not is_relevant_alert(alert):
                    continue
                aid = alert["id"]
                alerts.setdefault(aid, alert)
                affected.setdefault(aid, []).extend(props_at_point)

        findings: list[Finding] = []
        for aid, alert in alerts.items():
            hits = affected.get(aid, [])
            # A property can sit at two rounded points' boundary only via
            # duplicates in by_point values — dedupe by buildium_id anyway.
            seen: dict[int, dict[str, Any]] = {p["buildium_id"]: p for p in hits}
            props = list(seen.values())
            n = len(props)
            event = str(alert.get("event") or "Weather alert")
            nws_severity = str(alert.get("severity") or "")
            names = [p["name"] or f"#{p['buildium_id']}" for p in props]
            shown = ", ".join(names[:5]) + (f" … and {n - 5} more" if n > 5 else "")
            findings.append(
                Finding(
                    rule_key="weather_alert",
                    fingerprint=f"alert:{aid}",
                    severity=_FINDING_SEVERITY.get(nws_severity, "warning"),
                    title=f"{event} affects {n} propert{'ies' if n != 1 else 'y'}",
                    detail=(
                        f"{alert.get('headline') or event} ({nws_severity}). "
                        f"Affected: {shown}."
                        + (f" Ends {alert['ends']}." if alert.get("ends") else "")
                    ),
                    amount=None,
                    property_id=props[0]["buildium_id"] if n == 1 else None,
                    entity_type="property" if n == 1 else None,
                    entity_id=props[0]["buildium_id"] if n == 1 else None,
                    evidence={
                        "alert_id": aid,
                        "event": event,
                        "nws_severity": nws_severity,
                        "headline": alert.get("headline"),
                        "area_desc": alert.get("areaDesc"),
                        "onset": alert.get("onset"),
                        "ends": alert.get("ends"),
                        "property_ids": sorted(seen.keys()),
                        "property_count": n,
                    },
                )
            )
        return findings


register(WeatherAlertDetector())

__all__ = [
    "EVENT_ALLOWLIST",
    "SEVERITY_ALLOWLIST",
    "WeatherAlertDetector",
    "clear_alert_cache",
    "is_relevant_alert",
]
