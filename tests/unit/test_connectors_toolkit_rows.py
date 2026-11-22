"""Settings → Connectors for the connectors built on ``helixis.pms``.

The card rule is shared: one place decides when a Rent Manager or
Propertyware card shows, by Rentvine's rules (``test_connectors_rentvine_row``):
listed while the system's switch is on and the workspace is on no PMS yet, or
while it holds that system's keys. Each connector adds its own catalog row
together with its panel (``test_every_pms_row_the_server_lists_opens_its_own_panel``
pins the two together). Rent Manager's row is the real one now; Propertyware's
is stood up where a test needs it. All ids are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import connectors as route
from helixis.pms import registry
from tests.unit.test_connectors_rentvine_row import COMPANY, _rows

RM = "rentmanager"
_RECENT = datetime.now(UTC) - timedelta(hours=2)
_OLD = datetime.now(UTC) - timedelta(days=30)

#: Rent Manager's catalog row, which arrived with its panel (re-pointed
#: 2026-10-05 from a row these tests stood up themselves).
_RM_ROW = next(spec for spec in route.SUPPORTED_CONNECTORS if spec["id"] == RM)


@pytest.fixture
def catalog() -> None:
    """The row Rent Manager's branch added with its panel, exactly once."""
    assert [spec["id"] for spec in route.SUPPORTED_CONNECTORS].count(RM) == 1
    assert _RM_ROW["category"] == "property_management" and _RM_ROW["authMethod"] == "api_key"


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setattr(registry, "enabled", lambda key: on if key == RM else key == "buildium")


def _on(monkeypatch: pytest.MonkeyPatch, pms: str | None, tested: datetime | None = None) -> None:
    monkeypatch.setattr("helixis.db.company_pms.company_pms", AsyncMock(return_value=pms))
    monkeypatch.setattr(route, "_last_tested", AsyncMock(return_value=tested))


async def test_no_card_while_the_switch_is_off_and_no_keys_are_held(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    _switch(monkeypatch, on=False)
    _on(monkeypatch, None)
    assert RM not in await _rows(monkeypatch, enabled=False)


async def test_a_workspace_on_no_pms_sees_the_card_once_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    _switch(monkeypatch, on=True)
    _on(monkeypatch, None)
    rows = await _rows(monkeypatch, enabled=False)
    assert rows[RM]["isConnected"] is False and rows[RM]["needsReauth"] is False
    assert "buildium" in rows, "a workspace on no PMS still chooses between them"
    route._last_tested.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_workspace_on_one_new_system_never_sees_the_other(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    """Rent Manager and Propertyware exclude each other like every pair."""
    pw_row = {**_RM_ROW, "id": "propertyware", "name": "Propertyware"}
    monkeypatch.setattr(route, "SUPPORTED_CONNECTORS", [*route.SUPPORTED_CONNECTORS, pw_row])
    monkeypatch.setattr(registry, "enabled", lambda key: True)
    _on(monkeypatch, RM, tested=_RECENT)
    rows = await _rows(monkeypatch, enabled=True)
    assert RM in rows and "propertyware" not in rows


async def test_a_buildium_workspace_never_sees_the_card(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    """One PMS per workspace: the card could only offer a connect the trigger refuses."""
    _switch(monkeypatch, on=True)
    _on(monkeypatch, "buildium")
    rows = await _rows(
        monkeypatch, enabled=True, buildium={"company_id": COMPANY, "last_tested_at": _RECENT}
    )
    assert RM not in rows and "buildium" in rows


async def test_stored_keys_stay_visible_with_the_switch_off_and_hide_the_others(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    """Keys must stay reachable to be replaced or disconnected (gotcha 120)."""
    _switch(monkeypatch, on=False)
    _on(monkeypatch, RM, tested=_RECENT)
    rows = await _rows(monkeypatch, enabled=True)
    assert rows[RM]["isConnected"] is True
    assert "buildium" not in rows and "rentvine" not in rows


async def test_keys_with_no_recent_proof_read_reconnect(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    """Stored is not working (gotcha 70): an old test and no sync is not Connected."""
    _switch(monkeypatch, on=True)
    _on(monkeypatch, RM, tested=_OLD)
    rows = await _rows(monkeypatch, enabled=False)
    assert rows[RM]["isConnected"] is False and rows[RM]["needsReauth"] is True


async def test_a_recent_sync_alone_proves_the_keys_and_is_the_last_sync(
    monkeypatch: pytest.MonkeyPatch, catalog: None
) -> None:
    _switch(monkeypatch, on=True)
    _on(monkeypatch, RM, tested=None)
    rows = await _rows(monkeypatch, enabled=False, freshness={"properties": _RECENT})
    assert rows[RM]["isConnected"] is True
    assert rows[RM]["lastSyncAt"] == int(_RECENT.timestamp() * 1000)


async def test_without_its_catalog_row_nothing_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The card arrives with its panel, on the connector's branch. Re-pointed
    2026-10-05: Rent Manager has its row now, so the row is taken away here."""
    without = [spec for spec in route.SUPPORTED_CONNECTORS if spec["id"] != RM]
    monkeypatch.setattr(route, "SUPPORTED_CONNECTORS", without)
    _switch(monkeypatch, on=True)
    _on(monkeypatch, None)
    assert RM not in await _rows(monkeypatch, enabled=False)


async def test_the_test_proof_is_read_from_the_registry_table_for_this_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 11: the read carries the company; the table name comes from the
    registry, never from a caller's string."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=_RECENT)
    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: pool)
    assert await route._last_tested(RM, COMPANY) == _RECENT
    sql, company = pool.fetchval.await_args.args
    assert "FROM public.company_rentmanager_credentials" in sql
    assert "company_id = $1::uuid" in sql and company == COMPANY
    assert await route._last_tested("appfolio", COMPANY) is None
    assert pool.fetchval.await_count == 1, "an unknown system must not reach SQL"


async def test_a_failed_proof_read_never_fails_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("pool down"))
    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: pool)
    assert await route._last_tested(RM, COMPANY) is None
