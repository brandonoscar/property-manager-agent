"""Rentvine owner-to-property links (``sync._link_owner_properties``).

Rentvine has no owner→property list. A property names its ``portfolioID`` and
a portfolio lists its owner ``contacts``; one paged ``GET /portfolios`` read
joins the two. Links are rewritten only from complete reads, and preserved
otherwise, because "no owner on record" from a missed page is the fabricated
absence rule 14 forbids.

All rows here are synthetic (ids 990 000 xxx). The live row shape of
``/portfolios`` is NOT VERIFIED; the runbook check "Owner-to-property links"
settles it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import mapper, sync, transport
from helixis.rentvine._row_envelopes import ROW_ENVELOPE
from helixis.settings import RentvineSettings, Settings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

CO = "6a456191-0000-4000-8000-000000000001"
MAPLE, BIRCH, ELM = 990000101, 990000102, 990000103
P_MAPLE, P_BIRCH = 990000951, 990000952
HOLDINGS, SMITH = 990000401, 990000402


def _pool(stored_owners: list[int]) -> Any:
    conn = MagicMock()

    def _enforce(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        return "UPDATE 1"

    conn.execute = AsyncMock(side_effect=_enforce)
    owners = [{"buildium_id": o} for o in stored_owners]

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        # Only the owner join's read gets the stored owners; the lease pass's
        # own reads (rent markers) see an empty mirror.
        return owners if sql.startswith("SELECT buildium_id FROM helixis_owners") else []

    conn.fetch = AsyncMock(side_effect=_fetch)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


def _links(pool: Any) -> dict[int, list[int]]:
    """owner id → the property_ids each UPDATE wrote."""
    out: dict[int, list[int]] = {}
    for call in pool._conn.execute.await_args_list:
        sql = call.args[0]
        if sql.startswith("UPDATE helixis_owners SET property_ids"):
            assert "company_id = $1::uuid" in sql and "pms = 'rentvine'" in sql
            assert call.args[1] == CO
            out[call.args[2]] = call.args[3]
    return out


class _Client:
    def __init__(self, rows: list[dict[str, Any]] | Exception, *, documented: bool = True):
        self.rows = rows
        self.documented = documented
        self.paths: list[str] = []

    async def iter_all(self, path: str) -> rvclient.Collection:
        self.paths.append(path)
        if isinstance(self.rows, Exception):
            raise self.rows
        return rvclient.Collection(rows=self.rows, stop_was_documented=self.documented)


PORTFOLIOS = [
    {"portfolioID": P_MAPLE, "contacts": [{"contactID": HOLDINGS, "percentOwned": "60"}]},
    {
        "portfolioID": P_BIRCH,
        "contacts": [
            {"contactID": HOLDINGS, "percentOwned": "50"},
            {"contactID": SMITH, "percentOwned": "50"},
        ],
    },
]


def _result(*, complete: bool = True) -> sync.SyncResult:
    r = sync.SyncResult(company_id=CO)
    if complete:
        r.confirmed_complete = {"helixis_properties", "helixis_owners"}
    return r


def _link(pool: Any, client: Any, portfolio_of: dict[int, int | None], result: Any) -> None:
    asyncio.run(sync._link_owner_properties(pool, client, CO, portfolio_of, result))


# ── the join ──────────────────────────────────────────────────────────────────


def test_a_complete_join_links_every_owner_to_every_property_of_their_portfolios():
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    _link(pool, _Client(PORTFOLIOS), {MAPLE: P_MAPLE, BIRCH: P_BIRCH}, result)
    assert _links(pool) == {HOLDINGS: [MAPLE, BIRCH], SMITH: [BIRCH]}
    assert result.owner_links == 2
    assert result.owners_unlinked_properties == 0
    assert result.ok
    # Only this company's RENTVINE owners are rewritten: a workspace that once
    # synced Buildium keeps those rows' links (rule 11).
    select = pool._conn.fetch.await_args
    assert "company_id = $1::uuid" in select.args[0]
    assert "pms = 'rentvine'" in select.args[0]
    assert select.args[1] == CO


def test_an_owner_on_no_portfolio_is_written_empty_because_both_lists_were_complete():
    stray = 990000499
    pool = _pool([HOLDINGS, SMITH, stray])
    _link(pool, _Client(PORTFOLIOS), {MAPLE: P_MAPLE, BIRCH: P_BIRCH}, _result())
    assert _links(pool)[stray] == []


def test_a_property_with_no_usable_portfolio_is_counted_not_failed_and_gets_no_owner():
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    _link(pool, _Client(PORTFOLIOS), {MAPLE: P_MAPLE, ELM: None}, result)
    assert _links(pool) == {HOLDINGS: [MAPLE], SMITH: []}
    assert result.owners_unlinked_properties == 1
    assert result.ok


def test_a_minority_owner_is_still_an_owner():
    pool = _pool([SMITH])
    rows = [{"portfolioID": P_BIRCH, "contacts": [{"contactID": SMITH, "percentOwned": "1"}]}]
    _link(pool, _Client(rows), {BIRCH: P_BIRCH}, _result())
    assert _links(pool) == {SMITH: [BIRCH]}


# ── nothing is written from an incomplete read ────────────────────────────────


def _assert_untouched(pool: Any, result: sync.SyncResult) -> None:
    assert _links(pool) == {}
    assert sync.OWNER_LINKS in result.failed
    assert result.owner_links is None


def test_a_portfolio_list_that_was_not_confirmed_complete_writes_nothing():
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    _link(pool, _Client(PORTFOLIOS, documented=False), {MAPLE: P_MAPLE}, result)
    _assert_untouched(pool, result)


def test_a_property_naming_a_portfolio_the_complete_list_lacks_writes_nothing():
    """A row-shape contradiction (a wrong envelope parses as no portfolios), not
    an unowned property: writing would tell every owner they own nothing."""
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    _link(pool, _Client(PORTFOLIOS), {MAPLE: P_MAPLE, ELM: 990000959}, result)
    _assert_untouched(pool, result)
    assert "row shape" in result.failed[sync.OWNER_LINKS]


def test_a_wrong_envelope_writes_nothing():
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    wrapped = [{"portfolio": row} for row in PORTFOLIOS]  # never unwrapped
    _link(pool, _Client(wrapped), {MAPLE: P_MAPLE, BIRCH: P_BIRCH}, result)
    _assert_untouched(pool, result)


def test_portfolios_that_name_no_owner_contact_at_all_write_nothing():
    """A renamed ``contacts`` key parses as "every portfolio has no owner"."""
    pool = _pool([HOLDINGS, SMITH])
    result = _result()
    rows = [{"portfolioID": P_MAPLE, "owners": [{"contactID": HOLDINGS}]}]
    _link(pool, _Client(rows), {MAPLE: P_MAPLE}, result)
    _assert_untouched(pool, result)
    assert "row shape" in result.failed[sync.OWNER_LINKS]


def test_a_portfolio_with_no_owners_beside_one_with_owners_is_fine():
    pool = _pool([HOLDINGS])
    rows = [*PORTFOLIOS[:1], {"portfolioID": P_BIRCH, "contacts": []}]
    result = _result()
    _link(pool, _Client(rows), {MAPLE: P_MAPLE, BIRCH: P_BIRCH}, result)
    assert _links(pool) == {HOLDINGS: [MAPLE]}
    assert result.ok


def test_a_failed_portfolio_read_writes_nothing():
    pool = _pool([HOLDINGS])
    result = _result()
    err = rvclient.RentvineAPIError(403, transport.Failure.PERMISSION, False, "forbidden")
    _link(pool, _Client(err), {MAPLE: P_MAPLE}, result)
    _assert_untouched(pool, result)
    assert "portfolios" in result.failed[sync.OWNER_LINKS]


def test_an_incomplete_property_list_writes_nothing_and_skips_the_read():
    pool = _pool([HOLDINGS])
    result = _result(complete=False)
    client = _Client(PORTFOLIOS)
    _link(pool, client, {MAPLE: P_MAPLE}, result)
    _assert_untouched(pool, result)
    assert client.paths == []


@pytest.mark.parametrize("table", ["helixis_properties", "helixis_owners"])
def test_a_failed_properties_or_owners_stage_writes_nothing(table):
    pool = _pool([HOLDINGS])
    result = _result()
    result.failed[table] = "boom"
    client = _Client(PORTFOLIOS)
    _link(pool, client, {MAPLE: P_MAPLE}, result)
    assert _links(pool) == {}
    assert sync.OWNER_LINKS in result.failed
    assert client.paths == []


def test_the_links_key_does_not_block_the_owners_freshness_stamp():
    """``OWNER_LINKS`` is not a mirror table: the owners synced fine."""
    assert sync.OWNER_LINKS not in sync.SYNC_STATE_STAGE
    assert sync.OWNER_LINKS not in {t for t, _ in sync.SOURCES}


# ── the mapper ────────────────────────────────────────────────────────────────


def test_an_unusable_contact_id_is_dropped_not_coerced():
    row = {
        "portfolioID": str(P_MAPLE),
        "contacts": [{"contactID": "abc"}, {"contactID": None}, "junk", {"contactID": SMITH}],
    }
    assert mapper.portfolio_owner_contact_ids(row) == (P_MAPLE, {SMITH})


def test_a_portfolio_without_contacts_has_no_owners():
    assert mapper.portfolio_owner_contact_ids({"portfolioID": P_MAPLE}) == (P_MAPLE, set())


def test_the_property_portfolio_is_read_from_the_row():
    assert mapper.property_portfolio_id({"portfolioID": str(P_MAPLE)}) == P_MAPLE
    assert mapper.property_portfolio_id({"portfolioID": None}) is None
    assert mapper.property_portfolio_id({}) is None


# ── the upsert does not wipe links ────────────────────────────────────────────


def _upsert_sql(table: str, row: dict[str, Any]) -> str:
    pool = _pool([])
    asyncio.run(sync._write_rows(pool, table, [row], CO))
    calls = [c.args[0] for c in pool._conn.execute.await_args_list]
    return next(s for s in calls if s.startswith(f"INSERT INTO {table}"))


def test_the_owner_upsert_writes_property_ids_on_insert_and_keeps_them_on_update():
    sql = _upsert_sql("helixis_owners", {"contactID": HOLDINGS, "name": "Synthetic Holdings LLC"})
    insert, _, update = sql.partition("DO UPDATE SET")
    assert "property_ids" in insert
    assert "property_ids" not in update
    assert "raw = EXCLUDED.raw" in update


def test_other_tables_still_update_every_column():
    sql = _upsert_sql("helixis_vendors", {"contactID": 990000501, "name": "Synthetic Plumbing"})
    _, _, update = sql.partition("DO UPDATE SET")
    assert "name = EXCLUDED.name" in update


def test_a_single_record_refresh_does_not_wipe_an_owners_links():
    pool = _pool([])

    class _One:
        async def get_object(self, path: str, **_: Any) -> dict[str, Any]:
            return {"contactID": HOLDINGS, "name": "Synthetic Holdings LLC"}

    landed = asyncio.run(
        sync.refresh_one(
            pool,
            _One(),  # type: ignore[arg-type]
            table="helixis_owners",
            path="/owners/{contactID}",
            path_params={"contactID": HOLDINGS},
            id_field="contactID",
            company_id=CO,
        )
    )
    assert landed
    calls = [c.args[0] for c in pool._conn.execute.await_args_list]
    sql = next(s for s in calls if "helixis_owners" in s)
    assert "property_ids" not in sql.partition("DO UPDATE SET")[2]


# ── wired into the sync ───────────────────────────────────────────────────────


def _wire(operation: str, *records: dict[str, Any]) -> list[dict[str, Any]]:
    key = ROW_ENVELOPE[operation]
    return [r if key is None else {key: r} for r in records]


def test_the_sync_reads_portfolios_once_and_writes_the_join(monkeypatch):
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    props = [
        {"propertyID": MAPLE, "name": "Synthetic Maple Court", "portfolioID": P_MAPLE},
        {"propertyID": BIRCH, "name": "Synthetic Birch House", "portfolioID": P_BIRCH},
    ]
    one_page = {"pagination-total-pages": "1"}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith("/properties"):
            return httpx.Response(200, json=_wire("GET /properties", *props), headers=one_page)
        if path.endswith("/portfolios"):
            return httpx.Response(200, json=_wire("GET /portfolios", *PORTFOLIOS), headers=one_page)
        return httpx.Response(200, json=[], headers=one_page)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
    client = rvclient.RentvineClient(
        RentvineSettings(enabled=True, account_code="acme", api_key="k", api_secret="s"),
        client=http,
    )
    pool = _pool([HOLDINGS, SMITH])
    result = asyncio.run(sync.sync_company(pool, CO, settings=app, client=client))
    assert sync.OWNER_LINKS not in result.failed, result.failed
    assert sum(p.endswith("/portfolios") for p in seen) == 1
    assert _links(pool) == {HOLDINGS: [MAPLE, BIRCH], SMITH: [BIRCH]}
    assert result.owner_links == 2
