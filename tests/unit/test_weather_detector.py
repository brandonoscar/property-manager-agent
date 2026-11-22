"""Tests for the weather-alert detector (detectors/weather_rules.py).

Mocks the NWS fetch — zero live calls. Pins: registered-but-dark, the
severity + event filters, cross-property dedup by alert id (one Finding per
alert, all affected properties in evidence), the per-run point cache, and
the best-effort per-point degrade."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.detectors import weather_rules as w
from helixis.detectors.registry import enabled_state_rule_detectors, state_rule_detectors

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


@pytest.fixture(autouse=True)
def _fresh_cache():
    w.clear_alert_cache()
    yield
    w.clear_alert_cache()


def _conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _prop(bid: int, name: str, lat: float, lng: float) -> dict[str, Any]:
    return {"buildium_id": bid, "name": name, "lat": lat, "lng": lng}


def _alert(
    aid: str = "urn:oid:2.49.0.1.840.0.abc",
    event: str = "Hard Freeze Warning",
    severity: str = "Severe",
    **extra: Any,
) -> dict[str, Any]:
    return {"id": aid, "event": event, "severity": severity, **extra}


# ── registered but dark by default ───────────────────────────────────


def test_registered_but_dark() -> None:
    ids = {d.detector_id for d in state_rule_detectors()}
    assert "weather_alert" in ids
    assert "weather_alert" not in {d.detector_id for d in enabled_state_rule_detectors()}
    # Promotion via the env allowlist opts it in (the invoice_no_work_order precedent).
    opted = {d.detector_id for d in enabled_state_rule_detectors(frozenset({"weather_alert"}))}
    assert "weather_alert" in opted


# ── relevance filter ─────────────────────────────────────────────────


def test_relevance_filter_severity_and_event() -> None:
    assert w.is_relevant_alert(_alert(severity="Severe", event="Hard Freeze Warning"))
    assert w.is_relevant_alert(_alert(severity="Extreme", event="Tornado Warning"))
    assert w.is_relevant_alert(_alert(severity="Severe", event="Flood Warning"))
    assert w.is_relevant_alert(_alert(severity="Severe", event="High Wind Warning"))
    assert w.is_relevant_alert(_alert(severity="Severe", event="Winter Storm Warning"))
    assert w.is_relevant_alert(_alert(severity="Extreme", event="Hurricane Warning"))
    # Wrong severity — even for an allow-listed event.
    assert not w.is_relevant_alert(_alert(severity="Moderate", event="Flood Warning"))
    assert not w.is_relevant_alert(_alert(severity="Minor", event="Tornado Warning"))
    # Allow-listed severity, off-list event.
    assert not w.is_relevant_alert(_alert(severity="Severe", event="Heat Advisory"))
    assert not w.is_relevant_alert(_alert(severity="Severe", event="Dense Fog Advisory"))


# ── dedup: one alert over many properties = ONE finding ──────────────


@pytest.mark.asyncio
async def test_one_alert_many_properties_one_finding() -> None:
    props = [_prop(i, f"Prop {i}", 44.9 + i * 0.05, -93.2) for i in range(9)]
    alert = _alert(headline="Hard Freeze Warning until noon", ends="2026-11-20T12:00:00-06:00")
    with patch.object(w, "_fetch_alerts", AsyncMock(return_value=[alert])):
        findings = await w.WeatherAlertDetector().scan(_conn(props), COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "weather_alert"
    assert f.fingerprint == f"alert:{alert['id']}"
    assert f.title == "Hard Freeze Warning affects 9 properties"
    assert f.severity == "warning"  # Severe -> warning; Extreme -> critical
    assert f.evidence["property_ids"] == list(range(9))
    assert f.evidence["property_count"] == 9
    # Multi-property finding is not pinned to one entity.
    assert f.property_id is None and f.entity_id is None


@pytest.mark.asyncio
async def test_extreme_alert_is_critical_and_single_property_is_linked() -> None:
    props = [_prop(7, "Garden Row", 29.95, -90.07)]
    alert = _alert(aid="urn:x", event="Hurricane Warning", severity="Extreme")
    with patch.object(w, "_fetch_alerts", AsyncMock(return_value=[alert])):
        findings = await w.WeatherAlertDetector().scan(_conn(props), COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"
    assert f.title == "Hurricane Warning affects 1 property"
    assert f.property_id == 7 and f.entity_type == "property" and f.entity_id == 7


@pytest.mark.asyncio
async def test_irrelevant_alerts_emit_nothing() -> None:
    props = [_prop(1, "A", 44.9, -93.2)]
    alerts = [
        _alert(aid="a1", severity="Moderate", event="Flood Warning"),
        _alert(aid="a2", severity="Severe", event="Heat Advisory"),
    ]
    with patch.object(w, "_fetch_alerts", AsyncMock(return_value=alerts)):
        findings = await w.WeatherAlertDetector().scan(_conn(props), COMPANY_ID)
    assert findings == []


@pytest.mark.asyncio
async def test_no_geocoded_properties_no_fetch() -> None:
    with patch.object(w, "_fetch_alerts", AsyncMock()) as fetch:
        findings = await w.WeatherAlertDetector().scan(_conn([]), COMPANY_ID)
    assert findings == []
    fetch.assert_not_awaited()


# ── per-run cache: co-located properties share one NWS lookup ────────


@pytest.mark.asyncio
async def test_same_rounded_point_queries_once() -> None:
    # Three units of one complex — identical coordinates to 2 decimals.
    props = [
        _prop(1, "A", 44.9501, -93.2001),
        _prop(2, "B", 44.9502, -93.2002),
        _prop(3, "C", 44.9503, -93.1996),
    ]
    fetch = AsyncMock(return_value=[_alert()])
    with patch.object(w, "_fetch_alerts", fetch):
        findings = await w.WeatherAlertDetector().scan(_conn(props), COMPANY_ID)
    assert fetch.await_count == 1
    assert len(findings) == 1
    assert findings[0].evidence["property_count"] == 3


@pytest.mark.asyncio
async def test_cache_carries_across_scans_within_ttl() -> None:
    props = [_prop(1, "A", 44.95, -93.20)]
    fetch = AsyncMock(return_value=[_alert()])
    det = w.WeatherAlertDetector()
    with patch.object(w, "_fetch_alerts", fetch):
        await det.scan(_conn(props), COMPANY_ID)
        await det.scan(_conn(props), "another-company")  # same point, other company
    assert fetch.await_count == 1  # second scan served from the run cache


@pytest.mark.asyncio
async def test_failed_point_degrades_to_no_alerts() -> None:
    """_fetch_alerts itself swallows errors; a point with a dead NWS answer
    contributes nothing and the scan still succeeds for other points."""
    props = [_prop(1, "A", 44.95, -93.20), _prop(2, "B", 29.95, -90.07)]
    fetch = AsyncMock(side_effect=[[], [_alert(aid="only")]])
    with patch.object(w, "_fetch_alerts", fetch):
        findings = await w.WeatherAlertDetector().scan(_conn(props), COMPANY_ID)
    assert len(findings) == 1
    assert findings[0].evidence["property_ids"] == [2]


@pytest.mark.asyncio
async def test_fetch_alerts_swallows_http_errors(monkeypatch) -> None:
    import httpx

    get = AsyncMock(side_effect=RuntimeError("boom"))
    client = MagicMock()
    client.get = get
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    assert await w._fetch_alerts(44.95, -93.20, 6.0) == []


@pytest.mark.asyncio
async def test_fetch_alerts_sends_required_user_agent(monkeypatch) -> None:
    import httpx

    resp = MagicMock()
    resp.json = MagicMock(
        return_value={"features": [{"properties": _alert(aid="urn:a"), "id": "urn:a"}]}
    )
    resp.raise_for_status = MagicMock()
    get = AsyncMock(return_value=resp)
    client = MagicMock()
    client.get = get
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    alerts = await w._fetch_alerts(44.95, -93.2, 6.0)
    assert alerts and alerts[0]["id"] == "urn:a"
    # NWS requires an identifying User-Agent; the point rides the query.
    assert get.await_args.kwargs["headers"]["User-Agent"].startswith("Occupella")
    assert get.await_args.kwargs["params"]["point"] == "44.95,-93.2"
