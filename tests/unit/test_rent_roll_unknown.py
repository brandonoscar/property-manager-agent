"""An unknown rent roll is never "$0" (CLAUDE.md rule 14).

Found 2026-10-01 reading the cockpit for a Rentvine workspace: the rent roll was
``COALESCE(SUM(rent), 0)`` over Active leases, so a property whose leases all
carried no rent (every Rentvine lease, until the lease sync reads ``rentAmount``)
showed "Rent roll $0" on the property tile and "Rent roll: $0/mo scheduled" in
chat. A rent roll nobody knows is not a rent roll of zero.

Both sides are read (rule 15): the repository's value, the chat line and the
API model's type; the frontend tile has its own test in
``frontend/src/components/PropertyCockpit.notSynced.test.tsx``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.db.repositories import cockpit
from helixis.db.repositories.cockpit import PropertyDetail

from .test_rentvine_blind_surfaces import (
    CO,
    _blind_summary,
    _cockpit_conn,
    _Conn,
    _on,
    _pool,
    _tools,
)


def _conn_with_rent(row: dict[str, Any]) -> _Conn:
    conn = _cockpit_conn()
    conn.answers = [(n, [row] if n == "SUM(rent)" else rows) for n, rows in conn.answers]
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("pms", ["rentvine", "buildium"])
async def test_active_leases_with_no_known_rent_read_as_unknown(
    monkeypatch: pytest.MonkeyPatch, pms: str
) -> None:
    _on(monkeypatch, pms)
    conn = _conn_with_rent({"scheduled": None, "active": 2, "priced": 0})
    detail = await cockpit.get_property_detail(_pool(conn), CO, 7)
    assert detail is not None
    assert detail.rent_scheduled is None
    assert detail.rent_unknown_leases == 2


@pytest.mark.asyncio
async def test_no_active_lease_is_a_true_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "rentvine")
    conn = _conn_with_rent({"scheduled": None, "active": 0, "priced": 0})
    detail = await cockpit.get_property_detail(_pool(conn), CO, 7)
    assert detail is not None
    assert detail.rent_scheduled == 0.0
    assert detail.rent_unknown_leases == 0


@pytest.mark.asyncio
async def test_a_partly_known_rent_roll_says_how_many_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, "rentvine")
    conn = _conn_with_rent({"scheduled": 1500, "active": 3, "priced": 2})
    detail = await cockpit.get_property_detail(_pool(conn), CO, 7)
    assert detail is not None
    assert detail.rent_scheduled == 1500.0
    assert detail.rent_unknown_leases == 1


def test_the_rent_sum_is_not_coalesced_to_zero_in_sql() -> None:
    import inspect

    src = inspect.getsource(cockpit.get_property_detail)
    assert "COALESCE(SUM(rent), 0)" not in src


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduled", "unknown", "wanted", "never"),
    [
        (None, 2, "rent not known", "$0"),
        (1500.0, 1, "rent not known for 1 lease", None),
    ],
)
async def test_the_chat_rent_roll_line_says_unknown(
    scheduled: float | None, unknown: int, wanted: str, never: str | None
) -> None:
    detail = PropertyDetail(
        summary=_blind_summary(),
        rent_scheduled=scheduled,
        rent_unknown_leases=unknown,
        rent_collected_mtd=None,
        tenants=None,
        delinquency_list=None,
        pms_name="Rentvine",
    )
    with (
        _tools(_Conn([])),
        patch.object(
            cockpit, "list_property_summaries", AsyncMock(return_value=[_blind_summary()])
        ),
        patch.object(cockpit, "get_property_detail", AsyncMock(return_value=detail)),
        patch.object(cockpit, "list_units_for_property", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_owner_of_property", AsyncMock(return_value="")),
    ):
        out = await mirror_tools._property_snapshot_impl("Garden")
    rent_line = next(line for line in out.splitlines() if "Rent roll" in line)
    assert wanted in rent_line.lower(), rent_line
    if never:
        assert never not in rent_line, rent_line


def test_the_api_model_lets_the_rent_roll_be_unknown() -> None:
    import types
    import typing

    from helixis.api.routes.properties import CockpitDetailResponse

    fields = CockpitDetailResponse.model_fields
    annotation = fields["rent_scheduled"].annotation
    assert type(None) in typing.get_args(annotation) or annotation is types.NoneType
    assert fields["rent_unknown_leases"].annotation is int
