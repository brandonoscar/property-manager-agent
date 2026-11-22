"""Rentvine leases are complete, priced, linked to tenants, and pruned only on proof.

2026-10-01, Step 5 of the Rentvine connector. Measured on demoaccount41 first:

* ``GET /leases`` (the old source) returns a 25-row slice and carries NO
  ``rentAmount`` key on any row, although the spec declares one.
* ``GET /leases/export`` pages to the end and — although the spec declares no
  response headers for it — sends ``pagination-total-items`` / ``-total-pages``.
* ``GET /leases/{leaseID}`` (``getLease``) returns ``rentAmount`` filled in.
* The export's ``lease.tenants`` carries each tenant's ``contactID`` and
  ``isActive`` on every lease.

Every fixture here is SYNTHETIC, built from the spec's schemas (terms rule 18):
no id, name or value below came from a live account.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import mapper, sync, transport
from helixis.settings import Settings

from .test_rentvine_sync import (
    PROPERTY,
    _client,
    _fake_pool,
    export_leases,
    lease_route,
    wire,
)

SPEC_PATH = "docs/rentvine/vendor/docs.rentvine.com/openapi.json"


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real waits, and no account hold leaking between tests (a 429 below
    holds the fake account; the hold table is process-wide)."""
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))
    monkeypatch.setattr(
        rvclient, "RATE_LIMIT_BACKOFF_S", (0.0,) * len(rvclient.RATE_LIMIT_BACKOFF_S)
    )
    monkeypatch.setattr(rvclient, "_holds", {})


def _app() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


def _handler(leases: list[dict[str, Any]], *, headers: bool = True, rent: Any = 1800, **over: Any):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if "lease" in over and request.url.path.rsplit("/", 1)[-1].isdigit():
            return over["lease"](request)
        if "/properties?" in str(request.url):
            return httpx.Response(
                200,
                json=wire("GET /properties", PROPERTY),
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        answered = lease_route(request, leases, headers=headers, rent=rent)
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler, calls


def _pool(stored: dict[int, str] | None = None, read_on: dict[int, str] | None = None) -> Any:
    """The sync test's driver-faithful pool, with stored rent markers (and the
    date each was read, when given) and a DELETE that reports how many rows it
    removed."""
    pool = _fake_pool()
    pool._conn.fetch.return_value = [
        {"buildium_id": lease_id, "mark": mark, "read_on": (read_on or {}).get(lease_id)}
        for lease_id, mark in (stored or {}).items()
    ]
    enforce = pool._conn.execute.side_effect

    async def execute(sql: str, *args: Any) -> str:
        if sql.lstrip().startswith("DELETE"):
            return "DELETE 1"
        return enforce(sql, *args)

    pool._conn.execute.side_effect = execute
    return pool


#: The tables the LEASE prune deletes from. Bills and balances prune in their
#: own stages (``sync._sync_bills``, ``sync._replace_balances``).
LEASE_PRUNE_TABLES = frozenset({"helixis_leases", "helixis_lease_tenants"})


def _lease_deletes(pool: Any) -> list[tuple[str, tuple[Any, ...]]]:
    return _statements(pool, "DELETE FROM helixis_leases") + _statements(
        pool, "DELETE FROM helixis_lease_tenants"
    )


def _statements(pool: Any, needle: str) -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (c.args[0], c.args[1:]) for c in pool._conn.execute.await_args_list if needle in c.args[0]
    ]


def _lease_upserts(pool: Any) -> dict[int, tuple[str, dict[str, Any]]]:
    """lease id -> (SQL, column -> value) for every helixis_leases upsert."""
    out = {}
    for sql, args in _statements(pool, "INSERT INTO helixis_leases"):
        columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
        row = dict(zip(columns, args, strict=False))
        out[int(row["buildium_id"])] = (sql, row)
    return out


async def _sync(pool: Any, handler: Any) -> sync.SyncResult:
    return await sync.sync_company(pool, "co", settings=_app(), client=_client(handler))


# ── completeness and the prune ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_complete_export_prunes_the_stale_lease_and_its_links() -> None:
    leases = export_leases(3)
    handler, _ = _handler(leases)
    pool = _pool(stored={1: "x", 2: "x", 3: "x", 99: "x"})  # 99: Rentvine no longer has it
    result = await _sync(pool, handler)

    assert "helixis_leases" not in result.failed, result.failed
    assert {"helixis_leases", "helixis_lease_tenants"} <= result.confirmed_complete
    (lease_delete,) = _statements(pool, "DELETE FROM helixis_leases")
    assert lease_delete[1][1] == [1, 2, 3]  # kept: exactly the export's ids
    (link_delete,) = _statements(pool, "DELETE FROM helixis_lease_tenants")
    kept_pairs = list(zip(link_delete[1][1], link_delete[1][2], strict=True))
    assert kept_pairs == [(1, 50_001), (2, 50_002), (3, 50_003)]
    # Bills and balances prune on their own stages; this is the leases'.
    leases_pruned = {t: n for t, n in result.pruned.items() if t in LEASE_PRUNE_TABLES}
    assert leases_pruned == {"helixis_leases": 1, "helixis_lease_tenants": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "leases", "headers", "reason"),
    [
        ("no headers", export_leases(3), False, "no pagination headers"),
        ("duplicate ids", [*export_leases(2), export_leases(1)[0]], True, "distinct lease ids"),
    ],
)
async def test_an_unprovable_export_prunes_nothing_and_is_stale(
    case: str, leases: list[dict[str, Any]], headers: bool, reason: str
) -> None:
    handler, _ = _handler(leases, headers=headers)
    pool = _pool(stored={99: "x"})
    result = await _sync(pool, handler)

    assert reason in result.failed["helixis_leases"], case
    assert "helixis_lease_tenants" in result.failed
    assert "helixis_leases" not in result.confirmed_complete
    assert not set(result.pruned) & LEASE_PRUNE_TABLES
    assert not _lease_deletes(pool)
    # The rows that arrived are still written: stale is not empty.
    assert result.rows_by_table["helixis_leases"] == len(leases)


@pytest.mark.asyncio
async def test_a_failed_export_writes_and_prunes_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leases/export"):
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _pool(stored={1: "x"})
    result = await _sync(pool, handler)
    assert "helixis_leases" in result.failed and "helixis_lease_tenants" in result.failed
    assert not _lease_deletes(pool)
    assert not _statements(pool, "INSERT INTO helixis_leases")


# ── rent ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_lease_the_old_slice_missed_gets_its_rent_from_get_lease() -> None:
    """The export carries no rentAmount (its schema has none); getLease does."""
    leases = export_leases(1)
    assert "rentAmount" not in leases[0]
    handler, calls = _handler(leases, rent=1525.5)
    pool = _pool()
    await _sync(pool, handler)

    sql, row = _lease_upserts(pool)[1]
    assert row["rent"] == Decimal("1525.5")
    assert "rent = EXCLUDED.rent" in sql
    assert any(
        c.startswith("GET /api/manager/leases/1?") and "includes=tenants" in c for c in calls
    )


@pytest.mark.asyncio
async def test_a_capped_pass_keeps_stored_rent_prunes_nothing_and_finishes_next_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "LEASE_RENT_READS_PER_SYNC", 2)
    leases = export_leases(3)
    handler, calls = _handler(leases)
    pool = _pool(stored={99: "x"})
    result = await _sync(pool, handler)

    assert sum("/leases/" in c and "export" not in c for c in calls) == 2
    assert "rent read for 2 of 3" in result.failed["helixis_leases"]
    assert not set(result.pruned) & LEASE_PRUNE_TABLES
    assert not _lease_deletes(pool)
    upserts = _lease_upserts(pool)
    read, unread = upserts[1], upserts[3]
    assert "rent = EXCLUDED.rent" in read[0]
    # The unread lease's stored rent is not touched...
    assert "rent = EXCLUDED.rent" not in unread[0] and "rent" not in unread[1]
    # ...and it carries no marker, so the next pass reads it first.
    assert sync.RENT_READ_MARKER not in json.loads(unread[1]["raw"])


@pytest.mark.asyncio
async def test_a_429_stops_the_rent_reads_and_leaves_the_pass_incomplete() -> None:
    leases = export_leases(3)
    reads: list[str] = []

    def throttled(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.path)
        return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "600"})

    handler, _ = _handler(leases, lease=throttled)
    pool = _pool()
    result = await _sync(pool, handler)

    assert len(reads) == 1, "a 429 must stop the loop, not be pressed through"
    assert "stopped after 0" in result.failed["helixis_leases"]
    assert not _lease_deletes(pool)


def test_only_never_read_changed_or_not_read_today_leases_are_re_read() -> None:
    """Re-pointed 2026-10-02 (founder): the weekly 1/7 rotation became a daily
    re-read, because a rent change was measured NOT to move any stamp."""
    today = date(2026, 10, 2)
    rows = [
        {"leaseID": 1, "dateTimeModified": "2026-09-30 04:00:00"},  # never read
        {"leaseID": 2, "dateTimeModified": "2026-10-01 04:00:00"},  # changed since read
        {"leaseID": 3, "dateTimeModified": "2026-09-01 00:00:00"},  # unchanged, read today
        {"leaseID": 4, "dateTimeModified": "2026-09-01 00:00:00"},  # unchanged, read yesterday
    ]
    marks = {2: "2026-09-30 04:00:00", 3: "2026-09-01 00:00:00", 4: "2026-09-01 00:00:00"}
    read_on = {2: "2026-10-02", 3: "2026-10-02", 4: "2026-10-01"}
    assert sync._pick_rent_reads(rows, marks, today, read_on=read_on) == [1, 2, 4]


def test_over_the_cap_the_longest_unread_go_first() -> None:
    """A pass that cannot read everyone still reaches every lease in turn."""
    today = date(2026, 10, 2)
    rows = [{"leaseID": i, "dateTimeModified": "m"} for i in (10, 11, 12, 13)]
    marks = {i: "m" for i in (10, 11, 12, 13)}
    read_on = {10: "2026-10-01", 11: "2026-09-29", 13: "2026-09-30"}  # 12: never dated
    assert sync._pick_rent_reads(rows, marks, today, read_on=read_on) == [12, 11, 13, 10]


def test_a_closed_lease_is_read_once_then_only_when_it_changes() -> None:
    """Founder change 2026-10-03: a Closed lease leaves the DAILY tier only.
    With every lease due daily, an account past 250 reads x passes a day never
    finished a pass, so nothing was stamped or pruned. A lease of unknown
    status stays daily: unknown is never read as Closed (rule 14)."""
    today = date(2026, 10, 3)
    rows = [
        {"leaseID": 1, "primaryLeaseStatusID": 3, "dateTimeModified": "m"},  # closed, read: skip
        {"leaseID": 2, "primaryLeaseStatusID": 2, "dateTimeModified": "m"},  # active: daily
        {"leaseID": 3, "primaryLeaseStatusID": 3, "dateTimeModified": "m2"},  # closed, changed
        {"leaseID": 4, "primaryLeaseStatusID": 3, "dateTimeModified": "m"},  # closed, never read
        {"leaseID": 5, "primaryLeaseStatusID": 3, "dateTimeModified": "m"},  # closed, money moved
        {"leaseID": 6, "dateTimeModified": "m"},  # status unknown: daily
        {"leaseID": 7, "primaryLeaseStatusID": 1, "dateTimeModified": "m"},  # pending: daily
    ]
    marks = {i: "m" for i in (1, 2, 3, 5, 6, 7)}
    read_on = {i: "2026-10-02" for i in (1, 2, 3, 5, 6, 7)}
    got = sync._pick_rent_reads(rows, marks, today, touched={5}, read_on=read_on)
    assert got == [4, 3, 5, 2, 6, 7]


@pytest.mark.asyncio
async def test_raw_is_refreshed_every_pass_and_the_marker_carried_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Founder change 2026-10-01: never freeze raw. A lease not re-read keeps
    its marker (so the change check still works) inside a raw that is current."""
    leases = export_leases(1, dateTimeModified="2026-09-30 04:00:00", code="NEW")
    handler, calls = _handler(leases)
    # Read before at this same modification time, and already TODAY...
    pool = _pool(stored={1: "2026-09-30 04:00:00"}, read_on={1: "2026-10-02"})
    monkeypatch.setattr(sync, "_today", lambda: date(2026, 10, 2))
    await _sync(pool, handler)

    assert not any("/leases/1?" in c for c in calls), "an unchanged lease was re-read"
    _sql, row = _lease_upserts(pool)[1]
    raw = json.loads(row["raw"])
    assert raw["code"] == "NEW"  # current fields from this pass's export
    assert raw[sync.RENT_READ_MARKER] == "2026-09-30 04:00:00"  # carried over
    assert raw[sync.RENT_READ_ON] == "2026-10-02"  # carried over too


@pytest.mark.asyncio
async def test_a_lease_read_yesterday_is_re_read_once_and_dated_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daily re-read, end to end: due, read, stamped with today, so the
    next pass the same day leaves it alone (the 4-hour sync's six passes)."""
    leases = export_leases(1, dateTimeModified="2026-09-30 04:00:00")
    handler, calls = _handler(leases)
    pool = _pool(stored={1: "2026-09-30 04:00:00"}, read_on={1: "2026-10-01"})
    monkeypatch.setattr(sync, "_today", lambda: date(2026, 10, 2))
    await _sync(pool, handler)

    assert any("/leases/1?" in c for c in calls), "a lease due today was not re-read"
    _sql, row = _lease_upserts(pool)[1]
    assert json.loads(row["raw"])[sync.RENT_READ_ON] == "2026-10-02"


# ── tenant links ────────────────────────────────────────────────────────────


def test_an_inactive_tenant_produces_no_link() -> None:
    lease = {
        "leaseID": 7,
        "tenants": [
            {"contactID": 1, "isActive": 1},
            {"contactID": 2, "isActive": 0},
            {"contactID": 3, "isActive": "0"},
            {"contactID": 4},  # absent is not active
            {"contactID": "5", "isActive": "1"},
        ],
    }
    links = mapper.map_lease_tenant_links(lease)
    assert [link["tenant_buildium_id"] for link in links] == [1, 5]
    assert {link["status"] for link in links} == {mapper.LEASE_TENANT_STATUS}


@pytest.mark.asyncio
async def test_links_are_written_for_rentvine_and_a_tenant_gone_inactive_is_pruned() -> None:
    lease = {
        **export_leases(1)[0],
        "tenants": [
            {"contactID": 50_001, "isActive": 1},
            {"contactID": 60_000, "isActive": 0},  # moved out
        ],
    }
    handler, _ = _handler([lease])
    pool = _pool()
    result = await _sync(pool, handler)

    inserts = _statements(pool, "INSERT INTO helixis_lease_tenants")
    assert [(args[1], args[2], args[5]) for _sql, args in inserts] == [(1, 50_001, "rentvine")]
    # The move-in date came from getLease's tenants include, as a date.
    assert inserts[0][1][4] == date(2026, 1, 2)
    (link_delete,) = _statements(pool, "DELETE FROM helixis_lease_tenants")
    kept = set(zip(link_delete[1][1], link_delete[1][2], strict=True))
    assert (1, 60_000) not in kept and (1, 50_001) in kept
    assert result.rows_by_table["helixis_lease_tenants"] == 1


def test_no_link_means_no_reminder_recipient() -> None:
    """Every reader treats every link as a current tenant (no reader filters
    ``status``), so the guarantee for an inactive tenant is that NO link is
    written. This holds the other half: the reminder producer finds its
    recipients only THROUGH ``helixis_lease_tenants``, so no link, no email."""
    import inspect

    from helixis.reminders import producers

    src = inspect.getsource(producers)
    start = src.index("SELECT DISTINCT lower(t.email)")
    recipients = src[start : src.index("AS tenant_emails", start)]
    assert "FROM helixis_lease_tenants lt" in recipients
    assert "JOIN helixis_tenants t" in recipients
    assert "lt.lease_buildium_id = l.buildium_id" in recipients


def test_move_in_dates_are_converted_never_passed_as_strings() -> None:
    tenants = [
        {"leaseTenant": {"contactID": "8", "moveInDate": "2025-03-05"}, "contact": {}},
        {"leaseTenant": {"contactID": "9", "moveInDate": None}, "contact": {}},
        "junk",
    ]
    assert mapper.lease_tenant_move_ins(tenants) == {8: date(2025, 3, 5)}


# ── the recorded spec deviation ─────────────────────────────────────────────


def test_the_export_is_recorded_as_sending_headers_the_spec_does_not_declare() -> None:
    """The record and the document must disagree, or the record is stale: if a
    future spec declares the headers, this fails and the override goes."""
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parents[2] / SPEC_PATH).read_text(encoding="utf-8"))
    declared = spec["paths"]["/leases/export"]["get"]["responses"]["200"].get("headers") or {}
    assert not any("pagination" in name for name in declared)
    assert "GET /leases/export" in transport.MEASURED_PAGINATION_HEADERS
    assert "GET /leases/export" not in transport.OPERATIONS_WITHOUT_PAGINATION_HEADERS
    assert sync.LEASES_EXPORT_PATH == "/leases/export"
