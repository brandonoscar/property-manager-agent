"""Rent Manager records -> mirror rows, from the synthetic fixture.

Three kinds of check, each a defect class this product already shipped:

* **Unknown is never zero** (rule 14): every value Rent Manager did not send,
  or sent unreadably, is ``None``, never 0, "" or False.
* **Only the writer's columns** (``pms.mirror``): it ignores any other key in
  silence, so a misspelled column would drop data without a sound. Read from
  the writer itself (rule 15).
* **One id sequence per table**: each table's ids come from one resource's
  primary key, or two records collide on ``(company_id, buildium_id)``.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from helixis.pms import mirror
from helixis.pms.mirror import TableRead
from helixis.rentmanager import mapper, sync
from helixis.rentmanager.resources import RESOURCES
from helixis.rentmanager.sync import Read, build_mirror_rows
from tests.unit._rentmanager_fakes import TODAY, portfolio


def _reads(data: dict[str, Any] | None = None, **over: Read) -> dict[str, Read]:
    data = data if data is not None else portfolio()
    reads = {name: Read(rows=data[name], complete=True) for name in RESOURCES}
    reads.update(over)
    return reads


def _built(data: dict[str, Any] | None = None, **over: Read) -> dict[str, TableRead]:
    return build_mirror_rows(_reads(data, **over), today=TODAY, read_seconds=2.5)


def _by_id(read: TableRead, key: str = "buildium_id") -> dict[Any, dict[str, Any]]:
    return {row[key]: row for row in read.rows}


@pytest.fixture
def built() -> dict[str, TableRead]:
    return _built()


# ── only the writer's columns ─────────────────────────────────────────


def _link_columns() -> set[str]:
    """The columns the shared link writer INSERTs: its one upsert statement,
    ``pms.mirror._LINK_UPSERT``, which the full and the delta paths both run
    (``test_the_link_columns_are_read_from_the_writer`` holds that)."""
    found = re.search(r"INSERT INTO \S+\s*\(([^)]*)\)", mirror._LINK_UPSERT)
    assert found, "the link writer's INSERT moved; re-point this test"
    return {c.strip() for c in found.group(1).split(",")} - {"company_id", "pms"}


def _runs_the_upsert(function: Any) -> bool:
    """``function`` names ``_LINK_UPSERT`` in its body (an AST walk, rule 17)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return any(isinstance(n, ast.Name) and n.id == "_LINK_UPSERT" for n in ast.walk(tree))


def _allowed(table: str) -> set[str]:
    if table in mirror.KEYED_COLUMNS:
        return {"buildium_id", "raw", *mirror.KEYED_COLUMNS[table]}
    if table == "helixis_lease_balances":
        return {"raw", *mirror.BALANCE_COLUMNS}
    if table == mirror.LINK_TABLE:
        return _link_columns()
    raise AssertionError(f"{table} is not a table this connector should write")


def test_every_mapped_row_carries_only_the_writers_columns(built: dict[str, TableRead]) -> None:
    assert set(built) == set(sync.SYNC_STATE_STAGE)
    checked = 0
    for table, read in built.items():
        assert read.error is None, (table, read.error)
        assert read.rows, f"{table}: the fixture produced no rows, so nothing was checked"
        for row in read.rows:
            extra = set(row) - _allowed(table)
            assert not extra, f"{table} writes columns the writer ignores: {sorted(extra)}"
            checked += 1
    assert checked > 25


def test_the_link_columns_are_read_from_the_writer() -> None:
    assert _link_columns() == {
        "lease_buildium_id",
        "tenant_buildium_id",
        "status",
        "move_in_date",
    }
    # Both ways a pass writes links run that statement, so its columns are
    # the writer's whole set.
    assert _runs_the_upsert(mirror._write_links)
    assert _runs_the_upsert(mirror._write_links_delta)


# ── one id sequence per table ─────────────────────────────────────────


def test_every_filled_table_names_one_source_and_lease_transactions_none() -> None:
    assert set(sync.TABLE_SOURCES) == set(sync.SYNC_STATE_STAGE)
    assert set(sync.TABLE_SOURCES.values()) <= set(RESOURCES)
    # Three sequences in one table (RM-12 open): left unfilled, so it declines.
    assert "helixis_lease_transactions" not in sync.SYNC_STATE_STAGE
    assert "helixis_bill_lines" not in sync.SYNC_STATE_STAGE


def test_each_keyed_tables_ids_are_its_one_resources_primary_key(
    built: dict[str, TableRead],
) -> None:
    for table, source in sync.TABLE_SOURCES.items():
        if table not in mirror.KEYED_COLUMNS:
            continue
        key = RESOURCES[source].key
        for row in built[table].rows:
            assert row["buildium_id"] == row["raw"][key], (table, key)


def test_balances_and_links_are_keyed_by_the_lease(built: dict[str, TableRead]) -> None:
    lease_ids = set(_by_id(built["helixis_leases"]))
    assert {r["lease_id"] for r in built["helixis_lease_balances"].rows} <= lease_ids
    assert {r["lease_buildium_id"] for r in built["helixis_lease_tenants"].rows} <= lease_ids
    tenant_ids = set(_by_id(built["helixis_tenants"]))
    assert {r["tenant_buildium_id"] for r in built["helixis_lease_tenants"].rows} <= tenant_ids


def test_the_one_sequence_check_can_fail(built: dict[str, TableRead]) -> None:
    """Mapping a lease's id from its tenant would put two sequences in one
    table; the check above must see it."""
    row = dict(built["helixis_leases"].rows[0])
    row["buildium_id"] = row["raw"]["TenantID"]
    assert row["buildium_id"] != row["raw"][RESOURCES["leases"].key]


# ── properties, units ─────────────────────────────────────────────────


def test_a_property_reads_its_address_and_none_without_one(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_properties"])
    cedar, alder = rows[880000101], rows[880000102]
    assert cedar["address_line1"] == "100 Synthetic Way", "only the first line of the street"
    assert (cedar["city"], cedar["state"], cedar["postal_code"]) == ("Testville", "OH", "43000")
    assert cedar["rental_sub_type"] == "Residential"
    assert alder["is_active"] is False, "a known no is False, not unknown"
    assert all(alder[c] is None for c in ("address_line1", "city", "state", "postal_code"))


def test_unit_occupancy_and_market_rent_are_known_or_none(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_units"])
    assert rows[880000201]["is_occupied"] is True
    assert rows[880000201]["market_rent"] == Decimal("1450.0")
    assert rows[880000202]["is_occupied"] is False, "an empty occupants list is vacant"
    assert rows[880000202]["market_rent"] == Decimal("1475.5"), "a one-item list is the record"
    assert rows[880000203]["is_occupied"] is None, "no occupants embed is unknown, never vacant"
    assert rows[880000203]["market_rent"] is None, "no market rent is unknown, never $0"


@pytest.mark.parametrize(
    ("record", "field", "expected"),
    [
        ({"UnitID": 1, "CurrentOccupants": [880000301]}, "is_occupied", None),
        ({"UnitID": 1, "CurrentOccupants": "yes"}, "is_occupied", None),
        ({"UnitID": 1, "CurrentOccupants": {"TenantID": 2}}, "is_occupied", True),
    ],
)
def test_an_embed_in_a_shape_not_understood_is_unknown(
    record: dict[str, Any], field: str, expected: Any
) -> None:
    """A list of bare ids is not "no occupants": filtering it down would turn
    an occupied unit vacant."""
    assert mapper.map_unit(record)[field] is expected


def test_ownerships_in_a_shape_not_understood_are_unknown_not_none_owned() -> None:
    row = mapper.map_owner({"OwnerID": 1, "Ownerships": [880000101, 880000102]})
    assert row["property_ids"] is None


# ── people ────────────────────────────────────────────────────────────


def test_a_tenant_reads_its_primary_contact(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_tenants"])
    avery = rows[880000301]
    assert (avery["first_name"], avery["last_name"]) == ("Avery", "Synthwood")
    assert avery["email"] == "avery.synthwood@example.com", "the primary contact, not the other"
    assert avery["phone"] == "555-0101"
    company = rows[880000304]
    assert (company["first_name"], company["last_name"]) == (None, "Synthetic Ventures LLC")
    assert company["email"] is None and company["phone"] is None


def test_an_owners_properties_are_known_empty_or_unknown(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_owners"])
    assert rows[880000401]["property_ids"] == [880000101, 880000102], "distinct and ordered"
    assert rows[880000401]["company_name"] == "Synthetic Holdings LLC"
    assert rows[880000401]["phone"] == "555-0141"
    assert rows[880000402]["property_ids"] == [], "Rent Manager listed none: owns nothing"
    assert rows[880000403]["property_ids"] is None, "no ownerships came back: unknown"
    assert rows[880000403]["first_name"] is None and rows[880000403]["email"] is None


def test_a_vendor_takes_the_primary_phone_and_guesses_nothing(
    built: dict[str, TableRead],
) -> None:
    rows = _by_id(built["helixis_vendors"])
    plumbing = rows[880000501]
    assert plumbing["phone"] == "555-0152", "the primary number, though listed second"
    assert plumbing["category_name"] == "Plumbing"
    for row in rows.values():
        assert row["is_company"] is None and row["company_name"] is None
        assert row["category_id"] is None
    assert rows[880000502]["is_active"] is False


# ── money ─────────────────────────────────────────────────────────────


def test_bills_paid_unpaid_and_reversed(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_bills"])
    assert set(rows) == {880000801, 880000802}, "a reversed bill is not owed"
    unpaid = rows[880000801]
    assert unpaid["is_paid"] is False and unpaid["total_amount"] == Decimal("325.0")
    assert (unpaid["bill_date"], unpaid["due_date"]) == (date(2026, 9, 15), date(2026, 10, 15))
    assert unpaid["vendor_id"] == 880000501 and unpaid["reference_number"] == "INV-1001"
    assert rows[880000802]["is_paid"] is True
    assert all(r["work_order_id"] is None for r in rows.values()), "unknown, never 'none'"


def test_a_bill_with_no_amount_or_payee_is_unknown_never_zero() -> None:
    row = mapper.map_bill({"ID": 1, "Amount": None, "IsFullyAllocated": None, "Vendor": None})
    assert row["total_amount"] is None and row["is_paid"] is None and row["vendor_id"] is None


def test_gl_types_keep_the_customers_word(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_gl_accounts"])
    assert (rows[880000901]["type"], rows[880000901]["sub_type"]) == ("Expense", "Expense")
    assert (rows[880000902]["type"], rows[880000902]["sub_type"]) == ("Income", "income")
    assert rows[880000903]["type"] is None, "a word outside the five is unknown there"
    assert rows[880000903]["sub_type"] == "Bank"
    assert rows[880000903]["parent_gl_account_id"] == 880000901
    assert all(r["is_bank_account"] is None for r in rows.values())


# ── work orders ───────────────────────────────────────────────────────


def test_work_order_words_are_the_customers_own(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_work_orders"])
    faucet = rows[880000701]
    assert faucet["status"] == "Waiting on Parts", "never translated into Buildium's words"
    assert faucet["priority"] == "Urgent"
    assert (faucet["property_id"], faucet["unit_id"]) == (880000101, 880000201)
    assert faucet["pms_updated_at"] == datetime(
        2026, 10, 1, 9, 30, tzinfo=timezone(timedelta(hours=-4))
    )
    assert faucet["task_id"] is None


def test_a_work_orders_links_say_one_place_or_unknown(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_work_orders"])
    light = rows[880000702]
    assert (light["property_id"], light["unit_id"]) == (880000102, None)
    assert light["pms_updated_at"] == datetime(2026, 9, 15, 14, 0, tzinfo=UTC), "zoneless is UTC"
    gutter = rows[880000703]
    assert gutter["property_id"] is None, "two linked properties are unknown, never the first"
    assert gutter["priority"] is None and gutter["pms_updated_at"] is None


def test_a_unit_link_names_the_property_when_none_is_linked() -> None:
    row = mapper.map_work_order(
        {"ServiceManagerIssueID": 1, "LinkedUnits": [{"UnitID": 5, "PropertyID": 9}]}, {}, {}
    )
    assert (row["property_id"], row["unit_id"]) == (9, 5)
    clash = mapper.map_work_order(
        {
            "ServiceManagerIssueID": 2,
            "LinkedUnits": [{"UnitID": 5, "PropertyID": 9}],
            "LinkedProperties": [{"PropertyID": 8}],
        },
        {},
        {},
    )
    assert clash["property_id"] is None, "two answers that disagree are unknown"


def test_a_status_the_lookup_does_not_name_keeps_the_word_it_had() -> None:
    """Most "open work" readers drop a work order with no status, so the
    status column is left out (the writer then keeps the old word) and the
    table is incomplete: written, not stamped, not pruned."""
    data = portfolio()
    data["work_orders"][0]["StatusID"] = 99
    read = _built(data)["helixis_work_orders"]
    assert read.error is None and read.complete is False
    rows = _by_id(read)
    assert "status" not in rows[880000701], "an unknown word must not blank the old one"
    assert rows[880000702]["status"] == "Closed", "the others are written as usual"


@pytest.mark.parametrize("lookup", ["work_order_statuses", "work_order_priorities"])
def test_a_failed_lookup_leaves_the_work_orders_as_they_were(lookup: str) -> None:
    built = _built(**{lookup: Read(error="403 (permission): no")})
    read = built["helixis_work_orders"]
    assert read.rows == [] and read.error and "lookup failed" in read.error


# ── leases ────────────────────────────────────────────────────────────


def test_rent_is_unknown_on_every_lease_never_zero(built: dict[str, TableRead]) -> None:
    rows = built["helixis_leases"].rows
    assert rows and all("rent" in r and r["rent"] is None for r in rows)


def test_a_leases_status_is_its_move_dates(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_leases"])
    assert rows[880000601]["status"] == "Active"
    assert rows[880000602]["status"] == "Future"
    assert rows[880000603]["status"] == "Past", "moved out before today"
    assert rows[880000605]["status"] == "Past"
    assert rows[880000604]["status"] == "Active"


@pytest.mark.parametrize(
    ("lease", "expected"),
    [
        ({"MoveInDate": "2026-10-05", "MoveOutDate": "2026-10-05"}, "Active"),
        ({"MoveInDate": "2026-10-06"}, "Future"),
        ({"MoveOutDate": "2026-10-04"}, "Past"),
        ({}, None),
        ({"MoveInDate": "not a date"}, None),
    ],
)
def test_lease_status_edges(lease: dict[str, Any], expected: str | None) -> None:
    assert mapper.lease_status(lease, TODAY) == expected


def test_the_term_comes_from_start_and_end_date(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_leases"])
    assert rows[880000601]["lease_from_date"] == date(2025, 11, 1)
    assert rows[880000601]["lease_to_date"] == date(2026, 10, 31)
    assert rows[880000604]["lease_from_date"] is None, "no term on file is unknown"


@pytest.mark.parametrize(
    ("lease", "expected"),
    [
        ({"UnitID": 5, "Unit": {"UnitID": 5}}, 5),
        ({"UnitID": 5}, 5),
        ({"Unit": {"UnitID": 5}}, 5),
        ({"UnitID": 5, "Unit": {"UnitID": 6}}, None),
        ({"UnitID": None, "Unit": None}, None),
    ],
)
def test_the_leases_unit_is_the_one_both_fields_agree_on(
    lease: dict[str, Any], expected: int | None
) -> None:
    assert mapper.lease_unit_id(lease) == expected


def test_only_an_active_lease_links_its_tenant(built: dict[str, TableRead]) -> None:
    links = built["helixis_lease_tenants"].rows
    assert {(r["lease_buildium_id"], r["tenant_buildium_id"]) for r in links} == {
        (880000601, 880000301),
        (880000604, 880000304),
    }
    assert all(r["status"] == "Current" for r in links)
    assert _by_id(built["helixis_lease_tenants"], "lease_buildium_id")[880000601][
        "move_in_date"
    ] == date(2025, 11, 1)


def test_a_move_out_is_the_actual_date_else_the_notices() -> None:
    assert mapper.lease_move_out(
        {"MoveOutDate": "2026-10-20", "ExpectedMoveOutDate": "2026-10-31"}
    ) == date(2026, 10, 20)
    assert mapper.lease_move_out({"ExpectedMoveOutDate": "2026-10-31"}) == date(2026, 10, 31)
    assert mapper.lease_move_out({"EndDate": "2026-12-31"}) is None, "the end is not a move-out"


# ── the tenant account's deposit and balance, on one lease ────────────


def test_the_current_lease_is_the_only_one_or_the_latest_started() -> None:
    leases = portfolio()["leases"]
    current = mapper.current_leases(leases, TODAY)
    assert current[880000303] == 880000603, "the later of two started leases"
    assert current[880000302] == 880000602, "an only lease, even one not started"
    assert current[880000301] == 880000601


@pytest.mark.parametrize(
    "moves",
    [
        [("2026-01-01", 1), ("2026-01-01", 2)],
        [("2026-01-01", 1), (None, 2)],
        [("2026-11-01", 1), ("2026-12-01", 2)],
    ],
    ids=["two on one day", "a move-in missing", "neither started"],
)
def test_a_tenant_whose_leases_cannot_be_ordered_has_no_current_lease(
    moves: list[tuple[str | None, int]],
) -> None:
    leases = [{"LeaseID": lid, "TenantID": 7, "MoveInDate": m} for m, lid in moves]
    assert mapper.current_leases(leases, TODAY) == {}


def test_a_deposit_sits_on_the_tenants_current_lease_only(built: dict[str, TableRead]) -> None:
    rows = _by_id(built["helixis_leases"])
    assert rows[880000601]["security_deposit"] == Decimal("1450.0")
    assert rows[880000603]["security_deposit"] == Decimal("900.0")
    assert rows[880000605]["security_deposit"] is None, "never copied onto an old lease"
    assert rows[880000604]["security_deposit"] == Decimal("0.0"), "a held $0 is a fact"


def test_a_deposit_rent_manager_did_not_send_is_unknown() -> None:
    data = portfolio()
    del data["tenants"][0]["SecurityDepositHeld"]
    rows = _by_id(_built(data)["helixis_leases"])
    assert rows[880000601]["security_deposit"] is None


@pytest.mark.parametrize(
    "tenants",
    [Read(error="500 (undocumented): x"), Read(rows=portfolio()["tenants"], complete=False)],
    ids=["tenants failed", "tenants incomplete"],
)
def test_deposits_that_cannot_be_placed_leave_each_lease_as_it_was(tenants: Read) -> None:
    rows = _built(tenants=tenants)["helixis_leases"].rows
    assert rows and all("security_deposit" not in r for r in rows)


def test_balances_only_for_accounts_that_owe_with_their_aging(
    built: dict[str, TableRead],
) -> None:
    read = built["helixis_lease_balances"]
    assert read.complete and read.error is None
    rows = _by_id(read, "lease_id")
    assert set(rows) == {880000601, 880000603, 880000604}, "a $0 account has no row"
    avery = rows[880000601]
    assert avery["total_balance"] == Decimal("250.0")
    assert avery["balance_31_60"] == Decimal("250.0") and avery["balance_0_30"] == 0
    casey = rows[880000603]
    assert casey["balance_over_90"] == Decimal("75.00"), "100 charged, 25 paid, 157 days old"
    assert (casey["property_id"], casey["unit_id"]) == (880000102, 880000203)


def test_aging_that_does_not_add_up_is_unknown_never_current(
    built: dict[str, TableRead],
) -> None:
    company = _by_id(built["helixis_lease_balances"], "lease_id")[880000604]
    assert company["total_balance"] == Decimal("120.0")
    for bucket in ("balance_0_30", "balance_31_60", "balance_61_90", "balance_over_90"):
        assert company[bucket] is None, bucket


@pytest.mark.parametrize(
    "receivables",
    [None, "x", [{"Amount": 250.0, "AmountAllocated": None, "TransactionDate": "2026-09-01"}]],
)
def test_aging_from_unreadable_receivables_is_unknown(receivables: Any) -> None:
    tenant = {"TenantID": 1, "Balance": 250.0, "OpenReceivables": receivables}
    assert mapper.aging(tenant, Decimal("250.0"), TODAY) is None


def test_a_balance_rent_manager_did_not_send_keeps_the_old_snapshot() -> None:
    data = portfolio()
    del data["tenants"][1]["Balance"]
    read = _built(data)["helixis_lease_balances"]
    assert read.rows == [] and read.error and "without a readable balance" in read.error


def test_a_balance_with_no_lease_to_hold_it_keeps_the_old_snapshot() -> None:
    data = portfolio()
    data["leases"] = [lease for lease in data["leases"] if lease["TenantID"] != 880000304]
    read = _built(data)["helixis_lease_balances"]
    assert read.rows == [] and read.error and "no lease" in read.error


@pytest.mark.parametrize("which", ["tenants", "leases"])
def test_a_balance_snapshot_needs_both_reads_whole(which: str) -> None:
    rows = portfolio()[which]
    incomplete = _built(**{which: Read(rows=rows, complete=False)})["helixis_lease_balances"]
    failed = _built(**{which: Read(error="403 (permission): x")})["helixis_lease_balances"]
    for read in (incomplete, failed):
        assert read.rows == [] and read.error, which


# ── stale, never empty ────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(RESOURCES))
def test_a_failed_read_is_an_error_never_an_empty_table(name: str) -> None:
    built = _built(**{name: Read(error="403 (permission): nothing visible")})
    tables = [t for t, source in sync.TABLE_SOURCES.items() if source == name]
    if name in ("work_order_statuses", "work_order_priorities"):
        tables = ["helixis_work_orders"]
    if name == "tenants":
        tables.append("helixis_lease_balances")
    assert tables, name
    for table in tables:
        read = built[table]
        assert read.error, f"{table} would read as empty after {name} failed"
        assert read.rows == [] and read.complete is False


def test_an_incomplete_read_is_rows_not_claimed_whole() -> None:
    data = portfolio()
    built = _built(data, vendors=Read(rows=data["vendors"], complete=False))
    assert built["helixis_vendors"].rows and built["helixis_vendors"].complete is False


# ── coercions: unknown is never zero ──────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "  ", "abc", True, "NaN", "Infinity", {}, []])
def test_money_that_cannot_be_read_is_none(value: Any) -> None:
    assert mapper._money(value) is None


def test_money_stays_exact() -> None:
    assert mapper._money(1450.1) == Decimal("1450.1")
    assert mapper._money("0") == Decimal("0")


@pytest.mark.parametrize("value", [None, True, False, "", "12.5", "abc", 3.0])
def test_an_id_that_is_not_a_whole_number_is_none(value: Any) -> None:
    assert mapper._int(value) is None


@pytest.mark.parametrize(("value", "expected"), [(True, True), ("false", False), (0, False)])
def test_a_flag_reads_its_word(value: Any, expected: bool) -> None:
    assert mapper._flag(value) is expected


@pytest.mark.parametrize("value", [None, "", "maybe", 2])
def test_a_flag_that_is_not_a_yes_or_no_is_none(value: Any) -> None:
    assert mapper._flag(value) is None


def test_a_date_that_does_not_parse_is_none_never_today() -> None:
    assert mapper._date("2026-13-45") is None and mapper._date("soon") is None
    assert mapper._date("2026-04-29T15:57:12.1667646-04:00") == date(2026, 4, 29)
