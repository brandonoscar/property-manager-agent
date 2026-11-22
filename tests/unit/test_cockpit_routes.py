"""Property cockpit routes (Workstream A) — auth, mapping, 404.

The SQL lives in ``db/repositories/cockpit.py`` and is exercised against the
real schema when the mirror is populated; here the repo functions are mocked
and the tests pin the route contract: auth required, summary dataclass →
response mapping, and a clean 404 when a property isn't mirrored yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import properties as properties_routes
from helixis.db.repositories.cockpit import PropertyDetail, PropertySummary


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    test_user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-1"],
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    return app


def _summary() -> PropertySummary:
    return PropertySummary(
        buildium_id=700,
        name="Maple Court",
        address_line1="12 Maple St",
        city="Boston",
        state="MA",
        unit_count=8,
        occupied_count=6,
        open_work_orders=2,
        expiring_leases_90d=1,
        delinquent_leases=1,
        delinquent_total=350.0,
    )


def test_cockpit_list_requires_auth() -> None:
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    resp = TestClient(app).get("/api/v1/properties/cockpit")
    assert resp.status_code in (401, 403)


def test_cockpit_list_maps_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "list_property_summaries",
        AsyncMock(return_value=[_summary()]),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit")
    assert resp.status_code == 200
    row = resp.json()["properties"][0]
    assert row["buildium_id"] == 700
    assert row["unit_count"] == 8
    assert row["occupied_count"] == 6
    assert row["delinquent_total"] == 350.0


def test_cockpit_detail_maps_starter_six(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    detail = PropertyDetail(summary=_summary())
    detail.rent_scheduled = 14400.0
    detail.rent_collected_mtd = 9600.0
    detail.tenants = [
        {"buildium_id": 901, "first_name": "Ada", "last_name": "L", "unit_number": "4B"}
    ]
    detail.expiring_lease_list = [
        {"buildium_id": 101, "unit_number": "4B", "lease_to_date": datetime(2026, 9, 1, tzinfo=UTC)}
    ]
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_property_detail",
        AsyncMock(return_value=detail),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit/700")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["name"] == "Maple Court"
    assert body["rent_scheduled"] == 14400.0
    assert body["rent_collected_mtd"] == 9600.0
    assert body["tenants"][0]["first_name"] == "Ada"
    assert body["expiring_lease_list"][0]["unit_number"] == "4B"


def test_cockpit_detail_404_when_not_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_property_detail",
        AsyncMock(return_value=None),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit/999")
    assert resp.status_code == 404
    # Re-pointed (rule 19): this pinned "backfill", the word the property page
    # keyed on to tell every workspace to run a Buildium backfill. The suite's
    # conftest puts this company on Buildium, whose card's button is Sync data.
    detail = resp.json()["detail"]
    assert "run Sync data on the Buildium card in Settings → Connectors" in detail
    assert "backfill" not in detail
