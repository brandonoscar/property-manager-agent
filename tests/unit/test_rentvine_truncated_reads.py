"""A short unpaged read is raised, written as partial, and never counted.

MEASURED 2026-09-24 on demoaccount41 (the founder's machine, after the seed
script's full run): ``GET /leases`` and ``GET /properties/{propertyID}/units``
declare no ``page`` in the spec, yet the server pages them and says so.

* ``GET /leases``: 25 rows, ``pagination-total-items: 28``,
  ``pagination-total-pages: 2``, ``pagination-page-size: 25``.
* ``GET /properties/15/units`` on a 120-unit property: 20 rows.

Before this, the sync and Phase 8's "independent" API count both read one
response and nothing looked at the headers. So they agreed with each other:
Phase 8 scored units RECONCILED with 100 missing, and called the mirror's 3
real leases "extra".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import sync
from helixis.settings import Settings

from .test_rentvine_complete_leases import LEASE_PRUNE_TABLES
from .test_rentvine_sync import (
    LEASE,
    PROPERTY,
    UNIT,
    _client,
    _fake_pool,
    export_leases,
    export_row,
    lease_route,
    wire,
)

#: The unpaged route the client's truncation guard was measured on. The sync no
#: longer reads it (leases come from the export, 2026-10-01), but it is still
#: the documented operation whose short slice these client tests describe.
LEASES = "/leases"

#: The headers /leases sent on demoaccount41, verbatim in shape.
LEASES_SHORT = {
    "pagination-current-page": "1",
    "pagination-page-size": "25",
    "pagination-total-items": "28",
    "pagination-total-pages": "2",
    "pagination-next-page": "2",
}


def _p8() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "rentvine_phase8.py"
    spec = importlib.util.spec_from_file_location("rentvine_phase8_trunc", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_phase8_trunc"] = module
    spec.loader.exec_module(module)
    return module


def _leases(n: int) -> list[dict[str, Any]]:
    return [{**LEASE, "leaseID": i + 1} for i in range(n)]


# ── the client ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_short_unpaged_read_raises_with_the_rows_that_arrived() -> None:
    rows = wire("GET /leases", *_leases(25))

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows, headers=LEASES_SHORT)

    with pytest.raises(rvclient.RentvineTruncatedError) as exc:
        await _client(handler).get_rows(LEASES)
    assert len(exc.value.rows) == 25
    assert exc.value.server_total == 28
    assert "25 rows" in exc.value.detail and "28" in exc.value.detail
    # A subclass, so every existing `except RentvineAPIError` still catches it.
    assert isinstance(exc.value, rvclient.RentvineAPIError)


@pytest.mark.asyncio
async def test_more_than_one_page_raises_even_without_a_total() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=wire("GET /leases", *_leases(3)), headers={"pagination-total-pages": "2"}
        )

    with pytest.raises(rvclient.RentvineTruncatedError) as exc:
        await _client(handler).get_rows(LEASES)
    assert exc.value.server_total is None


@pytest.mark.asyncio
async def test_a_total_above_the_rows_raises_without_a_page_count() -> None:
    """The units response's headers were not fully captured, so each header
    must be enough on its own."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=wire("GET /leases", *_leases(20)),
            headers={"pagination-total-items": "120", "pagination-page-size": "20"},
        )

    with pytest.raises(rvclient.RentvineTruncatedError) as exc:
        await _client(handler).get_rows(LEASES)
    assert exc.value.server_total == 120


@pytest.mark.parametrize(
    "headers",
    [
        {},  # what the spec promises: no headers at all
        {"pagination-total-items": "3", "pagination-total-pages": "1"},
        {"pagination-total-items": "junk"},  # unreadable is not "more"
    ],
)
@pytest.mark.asyncio
async def test_a_complete_or_unlabelled_read_is_returned(headers: dict[str, str]) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=wire("GET /leases", *_leases(3)), headers=headers)

    rows = await _client(handler).get_rows(LEASES)
    assert len(rows) == 3


# ── the sync ────────────────────────────────────────────────────────────────


def _app() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


def _handler(*, leases_short: bool, units_short: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        if url.endswith(f"/properties/{PROPERTY['propertyID']}/units"):
            units = [{**UNIT, "unitID": 5000 + i} for i in range(20)]
            head = {"pagination-total-items": "120", "pagination-total-pages": "6"}
            return httpx.Response(
                200,
                json=wire("GET /properties/{propertyID}/units", *units),
                headers=head if units_short else {},
            )
        if leases_short and request.url.path.endswith("/leases/export"):
            # The measured slice shape, on the export: 25 rows while the
            # server says 28 (page 2 comes back empty).
            page = int(request.url.params.get("page", "1"))
            rows = [export_row(r) for r in export_leases(25)] if page == 1 else []
            return httpx.Response(200, json=rows, headers=LEASES_SHORT)
        answered = lease_route(request, export_leases(25))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler


@pytest.mark.asyncio
async def test_a_short_lease_export_is_refused_whole_and_reported_stale() -> None:
    """Re-pointed 2026-10-01 from the unpaged ``/leases`` slice to the export.

    The export pages, so a short one is the server contradicting itself (25
    rows, ``total-items`` 28): ``iter_all`` refuses it rather than return a
    short collection as complete. Nothing is written over the stored leases,
    nothing is pruned, and the table is STALE, never stamped fresh."""
    handler = _handler(leases_short=True, units_short=False)
    pool = _fake_pool()
    result = await sync.sync_company(pool, "c", settings=_app(), client=_client(handler))
    assert "28" in result.failed["helixis_leases"]
    assert "helixis_lease_tenants" in result.failed
    assert not result.ok
    assert "helixis_leases" not in result.confirmed_complete
    assert "helixis_leases" not in result.rows_by_table
    assert not set(result.pruned) & LEASE_PRUNE_TABLES
    sql = [c.args[0] for c in pool._conn.execute.await_args_list]
    # Re-pointed 2026-10-01: bills and balances prune in their own stages; no LEASE prune.
    assert not any(
        "DELETE FROM helixis_leases" in q or "DELETE FROM helixis_lease_tenants" in q for q in sql
    )


@pytest.mark.asyncio
async def test_the_sync_writes_a_short_units_read_and_names_the_property() -> None:
    handler = _handler(leases_short=False, units_short=True)
    result = await sync.sync_company(_fake_pool(), "c", settings=_app(), client=_client(handler))
    assert result.rows_by_table["helixis_units"] == 20
    failed = result.failed["helixis_units"]
    assert str(PROPERTY["propertyID"]) in failed and "120" in failed
    assert "helixis_leases" not in result.failed  # a complete read stays complete


@pytest.mark.asyncio
async def test_a_complete_sync_is_still_ok() -> None:
    """The control: without the short headers nothing is flagged."""
    handler = _handler(leases_short=False, units_short=False)
    result = await sync.sync_company(_fake_pool(), "c", settings=_app(), client=_client(handler))
    assert result.ok, result.failed


# ── Phase 8's independent count ─────────────────────────────────────────────


class _ShortClient:
    """Answers the harness's two read shapes; the unpaged ones come back short."""

    def __init__(self, *, leases_short: bool, units_short: bool) -> None:
        self.leases_short = leases_short
        self.units_short = units_short

    async def iter_all(self, template: str, **_kw: Any) -> Any:
        if template == sync.LEASES_EXPORT_PATH:
            if self.leases_short:
                raise rvclient.RentvineAPIError(
                    0,
                    rvclient.transport.Failure.UNDOCUMENTED,
                    True,
                    f"{template}: paged to the end with 25 rows but pagination-total-items said 28",
                )
            rows = [{"leaseID": i, "tenants": []} for i in range(25)]
        else:
            rows = [{"propertyID": 15}] if template == "/properties" else []
        return type("C", (), {"rows": rows, "stop_was_documented": True})()

    async def get_rows(self, template: str, **_kw: Any) -> Any:
        rows = [{"unitID": i} for i in range(20)]
        if self.units_short:
            raise rvclient.RentvineTruncatedError(template, rows, 120)
        return rows


@pytest.mark.asyncio
async def test_phase8_counts_a_short_lease_read_as_unread_not_25() -> None:
    """25 read as a number made the mirror's 3 real leases look "extra"."""
    p8 = _p8()
    counts, prov = await p8._api_counts(_ShortClient(leases_short=True, units_short=False))
    assert counts["helixis_leases"] is None
    assert counts["helixis_lease_tenants"] is None
    assert prov["helixis_leases"]["read"] is False
    assert "28" in prov["helixis_leases"]["error"]
    assert counts["helixis_units"] == 20  # the complete family still counts


@pytest.mark.asyncio
async def test_phase8_never_scores_short_units_as_a_count() -> None:
    """THE FALSE PASS: 20 of 120 on both sides read as RECONCILED. The count is
    the server's own total, never the rows that arrived."""
    p8 = _p8()
    counts, prov = await p8._api_counts(_ShortClient(leases_short=False, units_short=True))
    assert counts["helixis_units"] == 120
    assert counts["helixis_units"] != 20
    (short,) = prov["helixis_units"]["truncated"]
    assert short == {**short, "property": 15, "sent": 20, "server_total": 120}
    notes = p8._truncation_notes(prov)
    assert notes == ["helixis_units: property 15 sent 20 of 120"]


@pytest.mark.asyncio
async def test_phase8_units_with_no_server_total_are_unread() -> None:
    class _NoTotal(_ShortClient):
        async def get_rows(self, template: str, **_kw: Any) -> Any:
            if template == sync.UNITS_PATH:
                raise rvclient.RentvineTruncatedError(template, [{"unitID": 1}], None)
            return await super().get_rows(template, **_kw)

    p8 = _p8()
    counts, _prov = await p8._api_counts(_NoTotal(leases_short=False, units_short=False))
    assert counts["helixis_units"] is None


# ── units the per-property read missed, from the documented export ─────────
# Founder decision 2026-09-24 (design/rentvine-terms-compliance.md §5a).

PID = PROPERTY["propertyID"]


def _export_rows(unit_ids: range, pid: int = PID) -> list[dict[str, Any]]:
    """Rows as the export sends them: the unit with its property BESIDE it (probe K)."""
    return [
        {
            "property": {"propertyID": pid, "name": "Seed"},
            "unit": {"unitID": i, "name": f"U{i}", "rent": "1000.00"},
        }
        for i in unit_ids
    ]


def _fill_handler(export: list[dict[str, Any]] | None, *, export_status: int = 200):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/properties/units/export" in url:
            if export_status != 200:
                return httpx.Response(export_status, json={"error": "boom"})
            page = int(request.url.params["page"])
            size = int(request.url.params["pageSize"])
            return httpx.Response(200, json=(export or [])[(page - 1) * size : page * size])
        if "/properties?" in url:
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        if url.endswith(f"/properties/{PID}/units"):
            units = [{**UNIT, "unitID": 5000 + i, "leaseID": 900 + i} for i in range(20)]
            return httpx.Response(
                200,
                json=wire("GET /properties/{propertyID}/units", *units),
                headers={"pagination-total-items": "120", "pagination-total-pages": "6"},
            )
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler, calls


def _written_units(pool: Any) -> list[tuple[Any, ...]]:
    """The unit rows UPSERTED. Re-pointed from "any statement naming the table"
    once a complete properties read began pruning the units of a property it no
    longer lists: that DELETE writes no unit and must not count as one."""
    conn = pool.acquire.return_value.__aenter__.return_value
    return [
        c.args
        for c in conn.execute.await_args_list
        if str(c.args[0]).startswith("INSERT INTO helixis_units")
    ]


@pytest.mark.asyncio
async def test_a_short_property_is_completed_from_the_export() -> None:
    """20 of 120 from the per-property route; the export supplies the other
    100, and a unit of ANOTHER property in the export is left alone."""
    export = _export_rows(range(5000, 5120)) + _export_rows(range(7000, 7001), pid=999)
    handler, calls = _fill_handler(export)
    pool = _fake_pool()
    result = await sync.sync_company(pool, "c", settings=_app(), client=_client(handler))
    assert result.rows_by_table["helixis_units"] == 120
    assert "helixis_units" not in result.failed, result.failed
    assert any("/properties/units/export" in u and "pageSize=25" in u for u in calls)
    assert len(_written_units(pool)) == 120  # not 121: property 999 is not ours to fill


@pytest.mark.asyncio
async def test_a_complete_sync_never_reads_the_export() -> None:
    inner = _handler(leases_short=False, units_short=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return inner(request)

    await sync.sync_company(_fake_pool(), "c", settings=_app(), client=_client(handler))
    assert calls  # the sync did run
    assert not any("/properties/units/export" in u for u in calls)


@pytest.mark.asyncio
async def test_an_export_that_is_still_short_keeps_the_property_incomplete() -> None:
    """The export sends no pagination headers, so its end is never the
    evidence: the property's OWN total (120) is."""
    handler, _ = _fill_handler(_export_rows(range(5000, 5070)))  # 50 new, not 100
    result = await sync.sync_company(_fake_pool(), "c", settings=_app(), client=_client(handler))
    assert result.rows_by_table["helixis_units"] == 70
    assert "70 of 120" in result.failed["helixis_units"]


@pytest.mark.asyncio
async def test_a_failed_export_keeps_the_property_incomplete() -> None:
    handler, _ = _fill_handler(None, export_status=403)
    result = await sync.sync_company(_fake_pool(), "c", settings=_app(), client=_client(handler))
    assert result.rows_by_table["helixis_units"] == 20
    assert str(PID) in result.failed["helixis_units"]


def test_an_export_unit_has_its_property_and_unknown_occupancy() -> None:
    from helixis.rentvine import mapper

    (row,) = _export_rows(range(5100, 5101))
    record = {**row["unit"], "property": row["property"]}
    mapped = mapper.map_unit_export(record)
    assert mapped["property_id"] == PID
    assert mapped["buildium_id"] == 5100
    # The export has no leaseID: occupancy is UNKNOWN, never "vacant".
    assert mapped["is_occupied"] is None


@pytest.mark.asyncio
async def test_the_client_refuses_a_companion_the_spec_does_not_declare() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with pytest.raises(rvclient.RentvineAPIError, match="companion"):
        await _client(handler).iter_all("/properties/units/export", companion="lease")


@pytest.mark.asyncio
async def test_an_export_row_without_its_property_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"unit": {"unitID": 1}}])

    with pytest.raises(rvclient.RentvineAPIError, match="property"):
        await _client(handler).iter_all(
            "/properties/units/export", page_size=25, companion="property"
        )
