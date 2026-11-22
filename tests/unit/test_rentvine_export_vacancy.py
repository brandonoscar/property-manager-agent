"""A unit the units export filled has its occupancy, from the export's own flag.

The per-property route sends 20 units, so the export completes the rest, and
its units were all written with occupancy UNKNOWN: measured 2026-10-03, 100 of
the sandbox's 157 units read "occupancy unknown" while every one of them carried
``isVacant: "1"`` and had no Active lease (gotcha 187). Unknown still never
reads as vacant (rule 14): a row without the flag stays ``None``.

The sync tests drive the real ``sync.sync_company`` over an HTTP mock. All rows
are synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.rentvine import mapper, sync

from .test_rentvine_truncated_reads import (
    PID,
    _app,
    _client,
    _fake_pool,
    _fill_handler,
    _written_units,
)

#: Where ``is_occupied`` sits in a recorded upsert's args: the SQL, then
#: ``sync._upsert``'s buildium_id, account_id, company_id and pms, then the columns.
_OCCUPIED_AT = 5 + sync.TABLE_COLUMNS["helixis_units"].index("is_occupied")


@pytest.mark.parametrize(
    ("flag", "occupied"),
    [("1", False), ("0", True), (1, False), (0, True), (True, False), (False, True)],
)
def test_the_flag_is_read_both_ways(flag: Any, occupied: bool) -> None:
    assert mapper.export_unit_occupied({"isVacant": flag}) is occupied


@pytest.mark.parametrize("row", [{}, {"isVacant": None}, {"isVacant": ""}, {"isVacant": "yes"}])
def test_no_flag_is_unknown_never_vacant(row: dict[str, Any]) -> None:
    assert mapper.export_unit_occupied(row) is None


def test_the_export_mapper_uses_the_flag_not_the_lease_id() -> None:
    """The export row carries ``leaseID`` too, null on all 100 measured rows.
    Read through ``map_unit`` it would say vacant for every unit, flag or not."""
    row = {"unitID": 5100, "name": "U1", "leaseID": None, "property": {"propertyID": PID}}
    assert mapper.map_unit_export({**row, "isVacant": "0"})["is_occupied"] is True
    assert mapper.map_unit_export(row)["is_occupied"] is None


def _export_rows(unit_ids: range, flag: Any) -> list[dict[str, Any]]:
    unit: dict[str, Any] = {"name": "U", "rent": "1000.00"}
    if flag is not None:
        unit["isVacant"] = flag
    return [
        {"property": {"propertyID": PID, "name": "Seed"}, "unit": {**unit, "unitID": i}}
        for i in unit_ids
    ]


@pytest.mark.asyncio
async def test_units_past_the_twentieth_are_written_with_the_flag() -> None:
    export = _export_rows(range(5020, 5070), "1") + _export_rows(range(5070, 5120), "0")
    handler, _calls = _fill_handler(export)
    pool = _fake_pool()
    result = await sync.sync_company(pool, "c", settings=_app(), client=_client(handler))
    assert result.rows_by_table["helixis_units"] == 120
    occupied = {args[1]: args[_OCCUPIED_AT] for args in _written_units(pool)}
    assert {occupied[i] for i in range(5020, 5070)} == {False}
    assert {occupied[i] for i in range(5070, 5120)} == {True}
    # The 20 the per-property route sent keep its leaseID reading.
    assert {occupied[i] for i in range(5000, 5020)} == {True}


@pytest.mark.asyncio
async def test_an_export_unit_without_the_flag_is_still_unknown() -> None:
    handler, _calls = _fill_handler(_export_rows(range(5020, 5120), None))
    pool = _fake_pool()
    await sync.sync_company(pool, "c", settings=_app(), client=_client(handler))
    occupied = {args[1]: args[_OCCUPIED_AT] for args in _written_units(pool)}
    assert {occupied[i] for i in range(5020, 5120)} == {None}
