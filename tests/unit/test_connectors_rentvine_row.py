"""Settings → Connectors lists Rentvine, by the same rules as Buildium.

The Rentvine credential routes (``api/routes/rentvine.py``) had no screen in
the web app: a workspace could connect only in the onboarding wizard, and had
nowhere to replace a revoked key, while chat told people to do exactly that
"on the Connectors page (Settings → Connectors)" (``rentvine_tools.py``).

These drive the real ``list_connectors`` with the credential reads, the sync
stamps and the connector flag stubbed. All ids are synthetic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import connectors as route

COMPANY = "00000000-0000-4000-8000-0000000000aa"
_RECENT = datetime.now(UTC) - timedelta(hours=2)
_OLD = datetime.now(UTC) - timedelta(days=30)


async def _rows(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    rentvine: dict[str, Any] | None = None,
    buildium: dict[str, Any] | None = None,
    freshness: dict[str, datetime] | None = None,
    rentvine_raises: bool = False,
) -> dict[str, dict[str, Any]]:
    monkeypatch.setattr(route.connectors_limiter, "check_by_user", AsyncMock(return_value=None))
    monkeypatch.setattr(
        route, "get_settings", lambda: SimpleNamespace(rentvine=SimpleNamespace(enabled=enabled))
    )
    monkeypatch.setattr(
        "helixis.buildium.credentials.fetch_buildium_row", AsyncMock(return_value=buildium)
    )
    rentvine_read = AsyncMock(
        side_effect=RuntimeError("pool down") if rentvine_raises else None,
        return_value=rentvine,
    )
    monkeypatch.setattr("helixis.rentvine.credentials.fetch_rentvine_row", rentvine_read)
    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        "helixis.db.repositories.mirror_sync_state.get_freshness",
        AsyncMock(return_value=freshness or {}),
    )
    request = MagicMock()
    request.app.state.composio_client = None
    user = MagicMock()
    user.company_ids = [COMPANY]
    rows = await route.list_connectors(request, user=user)  # type: ignore[arg-type]
    return {r["id"]: r for r in rows}


@pytest.mark.asyncio
async def test_a_workspace_with_no_pms_sees_both_cards_when_rentvine_is_on(monkeypatch) -> None:
    rows = await _rows(monkeypatch, enabled=True)
    assert {"buildium", "rentvine"} <= set(rows)
    rentvine = rows["rentvine"]
    assert rentvine["name"] == "Rentvine"
    assert rentvine["authMethod"] == "api_key"
    assert rentvine["category"] == "property_management"
    assert (rentvine["isConnected"], rentvine["needsReauth"]) == (False, False)
    assert "lastSyncAt" not in rentvine


@pytest.mark.asyncio
async def test_no_rentvine_card_while_the_connector_is_off(monkeypatch) -> None:
    """Saving keys would 503 (`_require_connector_enabled`): a card whose
    connect cannot work is the coming-soon row the founder removed."""
    rows = await _rows(monkeypatch, enabled=False)
    assert "rentvine" not in rows
    assert "buildium" in rows


@pytest.mark.asyncio
async def test_stored_rentvine_keys_stay_visible_with_the_connector_off(monkeypatch) -> None:
    """Gotcha 120: the reads and the disconnect work while dark, and a
    company holding this row cannot connect Buildium until it is deleted."""
    rows = await _rows(
        monkeypatch, enabled=False, rentvine={"company_id": COMPANY, "last_tested_at": None}
    )
    assert "rentvine" in rows


@pytest.mark.asyncio
async def test_a_tested_rentvine_key_is_connected_with_its_real_last_sync(monkeypatch) -> None:
    rows = await _rows(
        monkeypatch,
        enabled=True,
        rentvine={"company_id": COMPANY, "last_tested_at": _RECENT},
        freshness={"helixis_properties": _OLD, "helixis_leases": _RECENT},
    )
    rentvine = rows["rentvine"]
    assert (rentvine["isConnected"], rentvine["needsReauth"]) == (True, False)
    assert rentvine["lastSyncAt"] == int(_RECENT.timestamp() * 1000)


@pytest.mark.asyncio
async def test_a_passed_test_alone_proves_the_rentvine_key(monkeypatch) -> None:
    """The wizard's test kicks the first sync, which can take minutes: the
    key is proven before any stamp exists, and no sync is claimed."""
    rows = await _rows(
        monkeypatch, enabled=True, rentvine={"company_id": COMPANY, "last_tested_at": _RECENT}
    )
    rentvine = rows["rentvine"]
    assert (rentvine["isConnected"], rentvine["needsReauth"]) == (True, False)
    assert "lastSyncAt" not in rentvine


@pytest.mark.asyncio
async def test_a_recent_sync_alone_proves_the_rentvine_key(monkeypatch) -> None:
    rows = await _rows(
        monkeypatch,
        enabled=True,
        rentvine={"company_id": COMPANY, "last_tested_at": None},
        freshness={"helixis_properties": _RECENT},
    )
    assert rows["rentvine"]["isConnected"] is True


@pytest.mark.asyncio
async def test_saved_but_unproven_rentvine_keys_read_reconnect(monkeypatch) -> None:
    """A key change NULLs `last_tested_at` (the PUT), and a key Rentvine
    revoked stops the syncs: either way nothing proves it works."""
    rows = await _rows(
        monkeypatch,
        enabled=True,
        rentvine={"company_id": COMPANY, "last_tested_at": _OLD},
        freshness={"helixis_properties": _OLD},
    )
    rentvine = rows["rentvine"]
    assert (rentvine["isConnected"], rentvine["needsReauth"]) == (False, True)
    # Still the real stamp, not a page-load placeholder.
    assert rentvine["lastSyncAt"] == int(_OLD.timestamp() * 1000)


@pytest.mark.asyncio
async def test_one_pms_per_workspace_hides_the_other_card(monkeypatch) -> None:
    """Migration 0100's trigger refuses the second PMS, so its card could
    only offer a connect that fails."""
    on_rentvine = await _rows(
        monkeypatch, enabled=True, rentvine={"company_id": COMPANY, "last_tested_at": _RECENT}
    )
    assert "buildium" not in on_rentvine and "rentvine" in on_rentvine

    on_buildium = await _rows(
        monkeypatch, enabled=True, buildium={"company_id": COMPANY, "last_tested_at": _RECENT}
    )
    assert "rentvine" not in on_buildium and "buildium" in on_buildium


@pytest.mark.asyncio
async def test_a_failed_rentvine_read_never_fails_the_page(monkeypatch) -> None:
    rows = await _rows(monkeypatch, enabled=True, rentvine_raises=True)
    assert rows["rentvine"]["isConnected"] is False
    assert "gmail" in rows


@pytest.mark.asyncio
async def test_the_generic_connect_route_refuses_rentvine(monkeypatch) -> None:
    """Rentvine has its own routes; the Composio connect must not pretend."""
    monkeypatch.setattr(route.connectors_limiter, "check_by_user", AsyncMock(return_value=None))
    with pytest.raises(route.HTTPException) as caught:
        await route.connect_connector(  # type: ignore[call-arg]
            "rentvine", MagicMock(), body=route.ConnectInitRequest(), user=MagicMock()
        )
    assert caught.value.status_code == 400


# ── the web app routes the row the server lists (rule 15) ────────────────────

_REPO = Path(__file__).resolve().parents[2]


def test_every_pms_row_the_server_lists_opens_its_own_panel() -> None:
    """A PMS row the TypeScript does not know falls through to the
    coming-soon placeholder: the row would show, and nothing behind it would
    work. Python names the rows; the web app must declare and route each."""
    pms_ids = {
        spec["id"]
        for spec in route.SUPPORTED_CONNECTORS
        if spec["category"] == "property_management"
    }
    # Re-pointed 2026-10-05: Rent Manager's and Propertyware's rows arrived with
    # their panels; 2026-10-06, DoorLoop's. The loop below still requires a
    # panel per row.
    assert pms_ids == {"buildium", "rentvine", "rentmanager", "propertyware", "doorloop"}

    service = (_REPO / "frontend/src/services/connectors.ts").read_text(encoding="utf-8")
    declared = re.search(
        r"export const PMS_CONNECTOR_IDS: readonly string\[\] = \[([^\]]*)\]", service
    )
    assert declared, "connectors.ts no longer declares PMS_CONNECTOR_IDS"
    assert set(re.findall(r"'(\w+)'", declared.group(1))) == pms_ids
    overrides = service.split("const CAPABILITY_OVERRIDES", 1)[1].split("\n};", 1)[0]
    for pms_id in pms_ids:
        assert re.search(rf"\n  {pms_id}: \{{\n    capabilities: \[[^\]]*'api'", overrides), (
            f"CAPABILITY_OVERRIDES has no API row for {pms_id}"
        )

    page = (_REPO / "frontend/src/components/SettingsPage.tsx").read_text(encoding="utf-8")
    for pms_id in pms_ids:
        assert re.search(rf'open\.id === "{pms_id}" && \(\s*<\w+ApiDetail\b', page), (
            f"Settings → Connectors does not open a panel for {pms_id}"
        )
