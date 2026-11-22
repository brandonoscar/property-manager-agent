"""A unit whose occupancy is unknown is never counted as vacant (rule 14).

Found 2026-10-02 by ``rentvine_ai_e2e.py --sandbox`` (property-summary): a
120-unit Rentvine property read "Fully Vacant, 0 of 120 occupied" when 100 of
its units carry ``is_occupied IS NULL`` (completed from the units export, which
has no lease link; gotcha 169). Both chat tools and both web screens computed
``unit_count - occupied_count``. The same day, read-only on helixis-test: 99
Buildium units, 0 with NULL, but 4 flagged OCCUPIED with no Active lease in the
mirror; those had been counted vacant too and are now unknown. Every other
Buildium number is unchanged.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.db.repositories import cockpit
from helixis.db.repositories.cockpit import PropertyDetail, PropertySummary

from .test_rentvine_blind_surfaces import CO, _cockpit_conn, _Conn, _on, _pool, _tools

REPO = Path(__file__).resolve().parents[2]


def _summary(units: int, occupied: int, unknown: int, name: str = "Seed Court") -> PropertySummary:
    return PropertySummary(
        buildium_id=15,
        name=name,
        address_line1="1 Seed Way",
        city="Estero",
        state="FL",
        unit_count=units,
        occupied_count=occupied,
        occupancy_unknown_count=unknown,
        open_work_orders=0,
        delinquent_leases=0,
        delinquent_total=0.0,
    )


# ── the repository: the SQL's consumer ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_summary_carries_the_unknown_count_from_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, "rentvine")
    conn = _cockpit_conn()
    conn.answers = [
        (n, [{**rows[0], "unit_count": 120, "occupied_count": 0, "occupancy_unknown_count": 100}])
        if n == "AS unit_count"
        else (n, rows)
        for n, rows in conn.answers
    ]
    (s,) = await cockpit.list_property_summaries(_pool(conn), CO)
    assert s.occupancy_unknown_count == 100
    assert s.vacant_count == 20
    # The query computes it: no Active lease AND the flag never stated.
    sql = next(q for q in conn.sql if "AS unit_count" in q)
    unknown_cte = sql[sql.index("AS unknown_count") - 200 : sql.index(") q ON")]
    # NULL (never stated) AND TRUE (the PMS says occupied, our leases disagree);
    # only FALSE with no Active lease is vacant (measured 2026-10-02).
    assert "is_occupied IS NOT FALSE" in unknown_cte
    assert "NOT EXISTS" in unknown_cte and "status = 'Active'" in unknown_cte


@pytest.mark.parametrize(
    ("units", "occupied", "unknown", "vacant"),
    [
        (120, 0, 100, 20),  # the sandbox property
        (4, 3, 0, 1),  # Buildium: the old subtraction, unchanged
        (6, 6, 0, 0),
        (2, 3, 0, 0),  # a lease on a unit the mirror lacks never goes negative
    ],
)
def test_vacant_never_includes_unknown(
    units: int, occupied: int, unknown: int, vacant: int
) -> None:
    s = _summary(units, occupied, unknown)
    assert s.vacant_count == vacant
    if unknown == 0:
        assert s.vacant_count == max(0, units - occupied)  # Buildium's numbers do not move


# ── the chat tools: both sites and the totals line ──────────────────────────


async def _overview(*summaries: PropertySummary) -> str:
    with (
        _tools(_Conn([])),
        patch.object(cockpit, "list_property_summaries", AsyncMock(return_value=list(summaries))),
    ):
        return await mirror_tools._portfolio_overview_impl()


async def _snapshot(s: PropertySummary) -> str:
    detail = PropertyDetail(summary=s, rent_collected_mtd=None, tenants=None, pms_name="Rentvine")
    with (
        _tools(_Conn([])),
        patch.object(cockpit, "list_property_summaries", AsyncMock(return_value=[s])),
        patch.object(cockpit, "get_property_detail", AsyncMock(return_value=detail)),
        patch.object(cockpit, "list_units_for_property", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_owner_of_property", AsyncMock(return_value="")),
    ):
        return await mirror_tools._property_snapshot_impl(s.name or "")


@pytest.mark.asyncio
async def test_the_snapshot_names_unknown_and_never_says_full_or_all_vacant() -> None:
    line = next(
        x for x in (await _snapshot(_summary(120, 0, 100))).splitlines() if "Occupancy" in x
    )
    assert line == "- Occupancy: 0/120 (20 vacant, 100 occupancy unknown)"
    only_unknown = next(
        x for x in (await _snapshot(_summary(5, 5 - 5, 5))).splitlines() if "Occupancy" in x
    )
    assert "(full)" not in only_unknown and "vacant" not in only_unknown
    assert "5 occupancy unknown" in only_unknown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("units", "occupied", "wanted"),
    [(6, 6, "(full)"), (4, 3, "(1 vacant)")],
)
async def test_the_snapshot_for_buildium_reads_as_before(
    units: int, occupied: int, wanted: str
) -> None:
    out = await _snapshot(_summary(units, occupied, 0))
    assert f"- Occupancy: {occupied}/{units} {wanted}" in out


@pytest.mark.asyncio
async def test_the_overview_lines_and_totals_carry_unknown_separately() -> None:
    out = await _overview(_summary(120, 0, 100), _summary(4, 3, 0, name="Oak Row"))
    assert "- Seed Court: 0/120 occupied, 20 vacant, 100 occupancy unknown" in out
    assert "- Oak Row: 3/4 occupied, 1 vacant" in out
    assert "3/124 units occupied, 100 occupancy unknown," in out
    assert "120 vacant" not in out


@pytest.mark.asyncio
async def test_the_overview_totals_for_buildium_read_as_before() -> None:
    out = await _overview(_summary(4, 3, 0, name="Oak Row"))
    assert "3/4 units occupied, 0 open work orders" in out
    assert "occupancy unknown" not in out


# ── both sides of the API (rule 15) ─────────────────────────────────────────


def _ts_interface_fields(path: Path, name: str) -> dict[str, str]:
    """``field -> type`` of a TS interface, comments stripped first so a
    comment that quotes a field cannot satisfy the check (rule 17)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    body = re.search(rf"export interface {name} \{{(.*?)\n\}}", text, re.DOTALL)
    assert body, f"interface {name} not found in {path}"
    return dict(re.findall(r"^\s*(\w+)\??:\s*([^;]+);", body.group(1), re.MULTILINE))


def test_the_api_model_and_the_ts_type_both_carry_the_unknown_count() -> None:
    from helixis.api.routes.properties import CockpitSummary

    assert CockpitSummary.model_fields["occupancy_unknown_count"].annotation is int
    ts = _ts_interface_fields(REPO / "frontend/src/services/properties.ts", "CockpitSummary")
    assert ts.get("occupancy_unknown_count") == "number"
    # The route copies it (a model field nobody fills is rule 12's defect).
    from helixis.api.routes import properties as routes

    s = _summary(120, 0, 100)
    assert routes._summary_out(s).occupancy_unknown_count == 100


def test_the_frontend_helper_subtracts_unknown_like_the_backend() -> None:
    """Both sides of the vacancy rule: the TS helper's arithmetic is the
    dataclass's (``vacant_count``). Read with comments stripped."""
    text = (REPO / "frontend/src/lib/occupancy.ts").read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    body = text[text.index("export function vacantCount") :].split("\n}", 1)[0]
    assert "s.unit_count - s.occupied_count - (s.occupancy_unknown_count ?? 0)" in body
    assert "Math.max(0," in body


# ── the e2e harness's "$0 collected" (re-pointed, not deleted) ──────────────


def _fresh_e2e(sees: Any) -> Any:
    from helixis.db import pms_coverage

    path = REPO / "scripts" / "rentvine_ai_e2e.py"
    spec = importlib.util.spec_from_file_location("rentvine_ai_e2e_occ", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_ai_e2e_occ"] = module
    with patch.object(pms_coverage.Coverage, "sees", sees):
        spec.loader.exec_module(module)
    return module


def test_zero_collected_is_a_false_all_clear_only_while_payments_are_not_synced() -> None:
    synced = _fresh_e2e(lambda _self, _table: True)
    assert "$0 collected" not in synced._FALSE_ALL_CLEAR
    blind = _fresh_e2e(lambda _self, table: table != "helixis_lease_transactions")
    assert "$0 collected" in blind._FALSE_ALL_CLEAR
    # The rest of the list does not depend on it.
    assert "$0.00 outstanding" in synced._FALSE_ALL_CLEAR
    assert "$0.00 outstanding" in blind._FALSE_ALL_CLEAR
