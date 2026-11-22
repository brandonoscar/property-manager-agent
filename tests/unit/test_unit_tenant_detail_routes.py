"""Unit + tenant cockpit detail routes — auth, mapping, 404.

The SQL lives in ``db/repositories/cockpit.py``; here the repo functions are
mocked and the tests pin the route contract: auth required, dataclass →
response mapping, and a clean 404 when the entity isn't mirrored yet.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import properties as properties_routes
from helixis.db.pms_coverage import Coverage
from helixis.db.repositories.cockpit import TenantDetail, UnitDetail
from helixis.pms import registry

REPO = Path(__file__).resolve().parents[2]


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


def _unit_detail() -> UnitDetail:
    detail = UnitDetail(
        unit={
            "buildium_id": 4001,
            "unit_number": "2B",
            "market_rent": 1450.0,
            "is_occupied": True,
            "property_id": 700,
            "property_name": "Maple Court",
        }
    )
    detail.current_lease = {
        "buildium_id": 9001,
        "status": "Active",
        "lease_from_date": "2025-09-01T00:00:00+00:00",
        "lease_to_date": "2026-08-31T00:00:00+00:00",
        "rent": 1450.0,
    }
    detail.tenants = [
        {
            "buildium_id": 901,
            "first_name": "Ada",
            "last_name": "L",
            "email": "ada@example.com",
            "phone": "555-0101",
            "move_in_date": None,
        }
    ]
    detail.balance = {"total_balance": 850.0, "balance_0_30": 850.0}
    detail.open_work_orders = [{"buildium_id": 11, "title": "Leaky faucet", "status": "New"}]
    detail.recent_work_orders = [{"buildium_id": 10, "title": "Old fix", "status": "Completed"}]
    detail.open_tasks = [{"buildium_id": 31, "title": "Renewal call", "status": "New"}]
    return detail


def _tenant_detail() -> TenantDetail:
    detail = TenantDetail(
        tenant={
            "buildium_id": 901,
            "first_name": "Ada",
            "last_name": "L",
            "email": "ada@example.com",
            "phone": "555-0101",
        }
    )
    detail.leases = [
        {
            "buildium_id": 9001,
            "status": "Active",
            "rent": 1450.0,
            "unit_id": 4001,
            "unit_number": "2B",
            "property_name": "Maple Court",
            "total_balance": 850.0,
        }
    ]
    detail.recent_transactions = [
        {
            "transaction_date": "2026-07-01T00:00:00+00:00",
            "transaction_type": "Payment",
            "total_amount": 1450.0,
            "lease_id": 9001,
        }
    ]
    detail.open_work_orders = [
        {"buildium_id": 11, "title": "Leaky faucet", "status": "New", "unit_id": 4001}
    ]
    return detail


def test_unit_detail_requires_auth() -> None:
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    resp = TestClient(app).get("/api/v1/properties/cockpit/units/4001")
    assert resp.status_code in (401, 403)


def test_tenant_detail_requires_auth() -> None:
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    resp = TestClient(app).get("/api/v1/properties/cockpit/tenants/901")
    assert resp.status_code in (401, 403)


def test_unit_detail_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_unit_detail",
        AsyncMock(return_value=_unit_detail()),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit/units/4001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unit"]["unit_number"] == "2B"
    assert body["unit"]["property_name"] == "Maple Court"
    assert body["current_lease"]["status"] == "Active"
    assert body["tenants"][0]["email"] == "ada@example.com"
    assert body["balance"]["total_balance"] == 850.0
    assert body["open_work_orders"][0]["title"] == "Leaky faucet"
    assert body["recent_work_orders"][0]["status"] == "Completed"
    assert body["open_tasks"][0]["title"] == "Renewal call"


def test_unit_detail_vacant_unit_has_null_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    detail = UnitDetail(unit={"buildium_id": 4002, "unit_number": "3C", "is_occupied": False})
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_unit_detail",
        AsyncMock(return_value=detail),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit/units/4002")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_lease"] is None
    assert body["balance"] is None
    assert body["tenants"] == []


def _on(monkeypatch: pytest.MonkeyPatch, pms: str | None) -> None:
    monkeypatch.setattr(properties_routes, "coverage_for", AsyncMock(return_value=Coverage(pms)))


# Re-pointed (rule 19): these pinned "backfill" in the 404, the word the panes
# keyed on to say "Run a Buildium backfill" to every workspace, Rentvine's too.
def test_unit_detail_404_when_not_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_unit_detail",
        AsyncMock(return_value=None),
    )
    _on(monkeypatch, "rentvine")
    resp = TestClient(app).get("/api/v1/properties/cockpit/units/999")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "run Sync now on the Rentvine card" in detail
    assert "Buildium" not in detail and "backfill" not in detail


def test_tenant_detail_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_tenant_detail",
        AsyncMock(return_value=_tenant_detail()),
    )
    resp = TestClient(app).get("/api/v1/properties/cockpit/tenants/901")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant"]["first_name"] == "Ada"
    assert body["leases"][0]["property_name"] == "Maple Court"
    assert body["leases"][0]["total_balance"] == 850.0
    assert body["recent_transactions"][0]["transaction_type"] == "Payment"
    assert body["open_work_orders"][0]["unit_id"] == 4001


def test_tenant_detail_404_when_not_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit,
        "get_tenant_detail",
        AsyncMock(return_value=None),
    )
    _on(monkeypatch, "buildium")
    resp = TestClient(app).get("/api/v1/properties/cockpit/tenants/999")
    assert resp.status_code == 404
    assert "run Sync data on the Buildium card" in resp.json()["detail"]


def test_property_detail_404_names_rent_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app()
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        properties_routes.cockpit, "get_property_detail", AsyncMock(return_value=None)
    )
    _on(monkeypatch, "rentmanager")
    resp = TestClient(app).get("/api/v1/properties/cockpit/123")
    assert resp.status_code == 404
    assert "your Rent Manager data" in resp.json()["detail"]
    assert "run Sync now on the Rent Manager card" in resp.json()["detail"]


def test_a_404_with_the_system_unknown_names_none() -> None:
    detail = properties_routes.not_synced_yet("unit", Coverage(None))
    assert "Settings → Connectors" in detail
    assert not any(spec.name in detail for spec in registry.PMS.values())


#: Each system's settings panel, where the sync button the 404 names lives.
_PANELS = {
    "buildium": "BuildiumApiDetail.tsx",
    "rentvine": "RentvineApiDetail.tsx",
    "rentmanager": "RentManagerApiDetail.tsx",
    "propertyware": "PropertywareApiDetail.tsx",
    "doorloop": "DoorLoopApiDetail.tsx",
}


@pytest.mark.parametrize("pms", sorted(registry.PMS))
def test_the_button_the_404_names_is_on_that_system_s_panel(pms: str) -> None:
    """Rule 15: the sentence is Python, the button is a React label. Each
    system's panel must carry the label the 404 tells the user to click."""
    detail = properties_routes.not_synced_yet("unit", Coverage(pms))
    button = re.search(r"run (Sync \w+) on the", detail)
    panel = REPO / "frontend" / "src" / "components" / "connectors" / _PANELS[pms]
    labels = re.findall(r"'(Sync \w+)'", panel.read_text(encoding="utf-8"))
    if not labels:
        # Re-pointed 2026-10-06 (rule 19): a panel with no sync button (a
        # connector before its sync, as DoorLoop was in slice A0) must be named
        # by no 404, rather than sent to a button that is not there.
        assert button is None, (pms, detail)
        return
    assert button, detail
    assert button.group(1) in labels, (pms, button.group(1), labels)


def test_a_system_with_no_sync_yet_names_no_button_and_promises_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector built before its sync: the 404 says so, in its name, and
    sends nobody to a Sync button (rule 16).

    Re-pointed 2026-10-06 (rule 19): DoorLoop was that connector until its
    sync (slice A1); every registry system has one now, so DoorLoop stands in
    for the next connector built that way, with its sync hidden."""
    real = registry.connector_attr

    def no_sync(pms: Any, module: str, attr: str) -> Any:
        if (pms, module, attr) == ("doorloop", "sync", "sync_company"):
            return None
        return real(pms, module, attr)

    monkeypatch.setattr(properties_routes.registry, "connector_attr", no_sync)
    detail = properties_routes.not_synced_yet("unit", Coverage("doorloop"))
    assert "doesn't sync DoorLoop yet" in detail
    assert "Sync" not in detail.replace("sync", "")
    assert "team@occupella.com" in detail
