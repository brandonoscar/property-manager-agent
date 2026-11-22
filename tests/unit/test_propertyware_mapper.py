"""The pure mapper: Propertyware records to mirror rows.

Every quirk in the design's "built correctly" table that slice 1 implements has
a case here, read from the synthetic fixtures, and every place the rule says
"unknown is None, never 0" is asserted as exactly ``None``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from helixis.propertyware import mapper
from helixis.propertyware.mapper import UnitIndex
from tests.unit._propertyware_fake import TODAY, fixture_rows

#: The official spec, as Brandon downloaded it (design W1).
SPEC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "propertyware"
    / "vendor"
    / "app.propertyware.com"
    / "openapi.json"
)


def _by_id(path: str) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in fixture_rows(path)}


BUILDINGS = _by_id("/buildings")
UNITS = _by_id("/units")
LEASES = _by_id("/leases")
WORK_ORDERS = _by_id("/workorders")
BILLS = _by_id("/bills")
COMPLETE_UNITS = UnitIndex.from_rows(fixture_rows("/units"), complete=True)


# ── buildings ─────────────────────────────────────────────────────────────


def test_a_building_maps_its_address_and_keeps_its_label() -> None:
    row = mapper.map_building(BUILDINGS[990100003])
    assert row["buildium_id"] == 990100003
    assert row["name"] == "Synthetic Cedar Duplex"
    # A read-side LABEL, kept as written (api-notes §1).
    assert row["rental_sub_type"] == "Du/Fourplex"
    assert (row["address_line1"], row["city"], row["state"], row["postal_code"]) == (
        "22 Sample Road",
        "Exampleton",
        "TX",
        "00003",
    )
    assert row["is_active"] is False


def test_a_building_with_no_address_is_unknown_not_blank() -> None:
    row = mapper.map_building({"id": 1, "address": None})
    assert row["address_line1"] is None and row["city"] is None and row["is_active"] is None


def test_building_and_unit_raw_is_the_record_as_sent() -> None:
    """The target deposit has no mirror column and no reader yet (rule 12), so
    nothing is computed from it: it stays in ``raw`` exactly as sent."""
    building = BUILDINGS[990100002]
    assert mapper.map_building(building)["raw"] == building
    assert mapper.map_building(building)["raw"]["targetDeposit"] == "$2,100.00"
    unit = UNITS[990200005]
    assert mapper.map_unit(unit)["raw"] == unit
    assert mapper.map_unit(unit)["raw"]["targetDepositAmount"] == 900.0


# ── units ─────────────────────────────────────────────────────────────────


def test_market_rent_is_the_target_rent_only_when_it_is_monthly() -> None:
    assert mapper.map_unit(UNITS[990200002])["market_rent"] == Decimal("1475.55")
    # Per square foot is not a monthly rent; no units at all is unknown too.
    assert mapper.map_unit(UNITS[990200003])["market_rent"] is None
    assert mapper.map_unit(UNITS[990200005])["market_rent"] is None


@pytest.mark.parametrize(
    ("unit_id", "occupied"),
    [
        (990200001, True),
        (990200002, False),
        (990200003, None),
        (990100002, True),
        (990200005, None),
    ],
)
def test_occupancy_is_occupied_vacant_or_unknown_never_vacant_by_default(
    unit_id: int, occupied: bool | None
) -> None:
    assert mapper.map_unit(UNITS[unit_id])["is_occupied"] is occupied


def test_a_unit_without_a_name_falls_back_to_its_abbreviation() -> None:
    row = mapper.map_unit(UNITS[990100002])
    assert row["unit_number"] == "C-A" and row["property_id"] == 990100003


# ── contacts, vendors, GL accounts ────────────────────────────────────────


def test_tenants_map_and_another_type_is_left_out() -> None:
    rows = {r["id"]: mapper.map_tenant(r) for r in fixture_rows("/contacts")}
    assert rows[990400004] is None, "an OWNER contact must never land in the tenants table"
    jordan, riley, casey = rows[990400001], rows[990400002], rows[990400003]
    assert jordan is not None and riley is not None and casey is not None
    assert jordan["phone"] == "555-0101", "a mobile first: it can take a text"
    assert riley["phone"] == "555-0102" and riley["email"] == "riley.sample@example.com"
    # No type at all: it came back for a tenant read, so it is kept.
    assert casey["first_name"] == "Casey" and casey["phone"] == "555-0103"
    assert "birthDate" not in casey["raw"]


def test_a_tenant_with_nothing_on_file_is_unknown_not_blank() -> None:
    row = mapper.map_tenant({"id": 7, "type": "tenant"})
    assert row is not None
    assert (row["first_name"], row["last_name"], row["email"], row["phone"]) == (None,) * 4


def test_a_vendor_has_no_persons_name_and_an_unknown_company_flag() -> None:
    plumber = mapper.map_vendor(fixture_rows("/vendors")[0])
    handy = mapper.map_vendor(fixture_rows("/vendors")[1])
    assert plumber["name"] == "Synthetic Plumbing Co" and plumber["category_name"] == "Plumber"
    assert plumber["is_company"] is None and plumber["first_name"] is None
    assert plumber["category_id"] is None
    assert "taxId" not in plumber["raw"]
    assert handy["is_active"] is False and handy["phone"] == "555-0121"
    assert handy["company_name"] is None and handy["email"] is None


def test_a_gl_account_keeps_the_deposit_flag_and_never_the_bank_numbers() -> None:
    accounts = {r["id"]: mapper.map_gl_account(r) for r in fixture_rows("/accounting/glaccounts")}
    cash = accounts[990900001]
    assert "bankAccountNumber" not in cash["raw"] and "bankRoutingNumber" not in cash["raw"]
    assert cash["is_bank_account"] is None, "unknown, never 'not a bank account'"
    assert (cash["type"], cash["sub_type"]) == ("Asset", "Asset")
    assert cash["account_number"] == "1000"
    assert accounts[990900002]["raw"]["securityDepositAccount"] is True
    assert accounts[990900003]["parent_gl_account_id"] == 990900004
    assert accounts[990900003]["is_active"] is False


#: The spec's own account types (SaveAccountDTO.accountType), by name and by
#: number, read straight from the vendored spec so this table cannot drift.
def _spec_account_types() -> dict[str, str]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    text = spec["components"]["schemas"]["SaveAccountDTO"]["properties"]["accountType"]
    found = dict(re.findall(r"(\d+):([A-Za-z -]+?)(?=,|\.|$)", text["description"]))
    assert len(found) == 11, found
    return {number: name.strip() for number, name in found.items()}


#: Which of Buildium's five each of the spec's types belongs to.
_SPEC_TYPE_IS = {
    "Bank": "Asset",
    "Credit Card": "Liability",
    "Current Asset": "Asset",
    "Non-Current Asset": "Asset",
    "Current Liability": "Liability",
    "Non-Current Liability": "Liability",
    "Equity": "Equity",
    "Income": "Income",
    "Expense": "Expense",
    "Non-Operating Income": "Income",
    "Non-Operating Expense": "Expense",
}


def test_every_account_type_the_spec_names_has_a_gl_type_by_name_and_by_number() -> None:
    """The spec names 11 account types; the mapper knew Buildium's five
    words only, so a Current Asset or a Bank account had no type at all."""
    types = _spec_account_types()
    assert set(types.values()) == set(_SPEC_TYPE_IS)
    for number, name in types.items():
        assert mapper.gl_type(name) == _SPEC_TYPE_IS[name], name
        assert mapper.gl_type(name.upper()) == _SPEC_TYPE_IS[name], name
        assert mapper.gl_type(number) == _SPEC_TYPE_IS[name], number


@pytest.mark.parametrize(
    ("word", "gl_type"),
    [
        ("Asset", "Asset"),
        ("LIABILITY", "Liability"),
        ("equity", "Equity"),
        (" Income ", "Income"),
        ("Expense", "Expense"),
        ("Current Asset", "Asset"),
        ("non-operating expense", "Expense"),
        # Re-pointed (rule 19): "Bank" was pinned unknown, before the spec's
        # type list was read; it is the spec's bank account type, an asset.
        ("Bank", "Asset"),
        # Not a type the spec names: kept as Propertyware's word, never forced.
        ("Other Income", None),
        ("Expenses", None),
        ("Cost of Goods Sold", None),
        ("3", None),
        (None, None),
    ],
)
def test_the_gl_type_is_buildiums_word_only_for_a_type_propertyware_names(
    word: str | None, gl_type: str | None
) -> None:
    row = mapper.map_gl_account({"id": 990900099, "accountType": word})
    assert row["type"] == gl_type
    assert row["sub_type"] == (word.strip() if word else None), "Propertyware's own word"
    assert gl_type is None or gl_type in mapper.GL_TYPES


@pytest.mark.parametrize(
    ("word", "is_bank"),
    [("Bank", True), (" bank ", True), ("1", True), ("Current Asset", None), (None, None)],
)
def test_only_the_bank_type_says_an_account_is_a_bank_account(
    word: str | None, is_bank: bool | None
) -> None:
    row = mapper.map_gl_account({"id": 990900099, "accountType": word})
    assert row["is_bank_account"] is is_bank, "never False: no type says 'not a bank'"


def test_withheld_keys_are_removed_at_any_depth() -> None:
    nested = {"a": {"taxId": "1", "b": [{"birthDate": "x", "keep": 1}]}, "bankRoutingNumber": "0"}
    assert mapper.withheld(nested) == {"a": {"b": [{"keep": 1}]}}


# ── the lease → unit rule ─────────────────────────────────────────────────


def test_rule_1_a_unit_in_the_leases_building_is_that_unit() -> None:
    row, outcome = mapper.map_lease(LEASES[990600001], COMPLETE_UNITS)
    assert (outcome, row["unit_id"]) == (mapper.UNIT, 990200001)


def test_rule_2_a_single_family_lease_names_its_building_and_gets_no_unit() -> None:
    """The colliding-ids case: unit 990100002 EXISTS, in another building, and
    the Birch House lease names its own building id 990100002."""
    assert COMPLETE_UNITS.building_of[990100002] == 990100003
    row, outcome = mapper.map_lease(LEASES[990600002], COMPLETE_UNITS)
    assert outcome == mapper.SINGLE_FAMILY
    assert "unit_id" in row and row["unit_id"] is None


def test_rule_3_a_unit_in_another_building_is_never_guessed() -> None:
    row, outcome = mapper.map_lease(LEASES[990600003], COMPLETE_UNITS)
    assert (outcome, row["unit_id"]) == (mapper.NOT_FOUND, None)


def test_a_lease_naming_no_unit_has_none() -> None:
    row, outcome = mapper.map_lease(LEASES[990600006], COMPLETE_UNITS)
    assert (outcome, row["unit_id"]) == (mapper.NO_UNIT, None)


def test_without_a_units_read_the_stored_unit_is_left_as_it_was() -> None:
    row, outcome = mapper.map_lease(LEASES[990600001], None)
    assert outcome == mapper.UNKNOWN and "unit_id" not in row


def test_a_partial_units_read_confirms_a_match_and_never_a_miss() -> None:
    partial = UnitIndex.from_rows(fixture_rows("/units")[:1], complete=False)
    found, outcome = mapper.map_lease(LEASES[990600001], partial)
    assert (outcome, found["unit_id"]) == (mapper.UNIT, 990200001)
    missing, outcome = mapper.map_lease(LEASES[990600005], partial)
    assert outcome == mapper.UNKNOWN and "unit_id" not in missing
    # Even the single-family shape waits for a complete read.
    sfr, outcome = mapper.map_lease(LEASES[990600002], partial)
    assert outcome == mapper.UNKNOWN and "unit_id" not in sfr


# ── leases and their tenants ──────────────────────────────────────────────


def test_a_lease_maps_its_term_rent_and_status() -> None:
    """⚠ Re-pointed 2026-10-05 (rule 19): this asserted the customer's word
    ("Active - Notice Given") reached the status column. Every lease reader
    filters ``status = 'Active'``, so that held the defect in place: the lease
    read as not active. The active flag now decides; the word stays in raw."""
    row, _ = mapper.map_lease(LEASES[990600002], COMPLETE_UNITS)
    assert row["status"] == "Active"
    assert row["raw"]["status"] == "Active - Notice Given", "the customer's word is kept"
    assert row["lease_from_date"] == date(2024, 10, 1)
    assert row["lease_to_date"] == date(2026, 9, 30), "the term end, not the move-out"
    assert row["rent"] == Decimal("2100.0")
    assert row["property_id"] == 990100002


@pytest.mark.parametrize(
    ("record", "status"),
    [
        # active: the readers' word, whatever the customer calls it
        ({"active": True, "status": "Current"}, "Active"),
        ({"active": True, "status": "Active"}, "Active"),
        ({"active": True}, "Active"),
        ({"active": "true", "status": "Current"}, "Active"),
        # not active: the customer's own word
        ({"active": False, "status": "Terminated"}, "Terminated"),
        ({"active": False, "status": " Past "}, "Past"),
        ({"active": False}, None),
        # not active, but the word says Active: it contradicts itself
        ({"active": False, "status": "Active"}, None),
        ({"active": False, "status": "ACTIVE"}, None),
        # no flag, or not a yes/no: unknown, never inactive
        ({"status": "Active"}, None),
        ({"status": "Terminated"}, None),
        ({"active": None, "status": "Current"}, None),
        ({"active": "maybe", "status": "Current"}, None),
        ({"active": 2, "status": "Active"}, None),
    ],
)
def test_the_active_flag_decides_the_lease_status(
    record: dict[str, Any], status: str | None
) -> None:
    row, _ = mapper.map_lease({"id": 990600099, **record}, COMPLETE_UNITS)
    assert row["status"] == status
    assert row["raw"].get("status") == record.get("status"), "the customer's word stays in raw"


def test_the_fixture_leases_read_in_the_readers_word() -> None:
    statuses = {
        lease_id: mapper.map_lease(lease, COMPLETE_UNITS)[0]["status"]
        for lease_id, lease in LEASES.items()
    }
    assert statuses == {
        990600001: "Active",  # the customer says Current
        990600002: "Active",  # the customer says Active - Notice Given
        990600003: "Active",
        990600004: "Inactive",
        990600005: "Pending",
        990600006: "Active",
        990600007: None,  # not active, and its word says Active
        990600008: None,  # no active flag
    }


def test_deposit_held_is_unknown_and_a_missing_rent_is_unknown() -> None:
    row, _ = mapper.map_lease(LEASES[990600003], COMPLETE_UNITS)
    assert "security_deposit" in row and row["security_deposit"] is None
    assert "rent" in row and row["rent"] is None


def test_each_contact_on_a_lease_is_a_link_and_the_primary_flag_stays_in_raw() -> None:
    links = mapper.lease_tenant_links(LEASES[990600001])
    assert [(x["lease_buildium_id"], x["tenant_buildium_id"]) for x in links] == [
        (990600001, 990400001),
        (990600001, 990400002),
    ]
    assert {x["status"] for x in links} == {mapper.LEASE_TENANT_STATUS}
    assert all(x["move_in_date"] is None for x in links), "the lease's date is not each tenant's"
    row, _ = mapper.map_lease(LEASES[990600001], COMPLETE_UNITS)
    flags = {c["id"]: (c["primary"], c["role"]) for c in row["raw"]["contacts"]}
    assert flags == {990400001: (True, "Tenant"), 990400002: (False, "Co-Tenant")}


def test_a_contact_without_an_id_still_yields_a_row_for_the_writer_to_count() -> None:
    contacts = [{"id": 9}, {"id": 9}, {"id": None, "firstName": "X"}]
    links = mapper.lease_tenant_links({"id": 5, "active": True, "contacts": contacts})
    assert [x["tenant_buildium_id"] for x in links] == [9, None]


@pytest.mark.parametrize(
    "lease_id",
    [
        990600004,  # past (Inactive)
        990600005,  # pending, not active yet
        990600007,  # not active, its word says Active
        990600008,  # no active flag: unknown
    ],
)
def test_only_an_active_lease_links_its_tenants(lease_id: int) -> None:
    """No reader filters on a link's status: every one reads a link as "lives
    there now". A past or pending lease's contacts must not be linked."""
    # A lease that lists nobody proves nothing, so 990600004 is given someone.
    lease = {**LEASES[lease_id]}
    lease["contacts"] = lease.get("contacts") or [{"id": 990400001}]
    assert mapper.lease_tenant_links(lease) == []
    active = {**lease, "active": True}
    assert mapper.lease_tenant_links(active), "the same lease, active, links its tenants"


def test_a_move_out_is_the_actual_date_else_the_scheduled_one() -> None:
    assert mapper.lease_move_out(LEASES[990600002]) == date(2026, 9, 20)
    assert mapper.lease_move_out(LEASES[990600001]) == date(2026, 10, 31)
    assert mapper.lease_move_out(LEASES[990600003]) is None
    both = {"moveOutDate": "2026-10-20", "scheduleMoveOutDate": "2026-10-31"}
    assert mapper.lease_move_out(both) == date(2026, 10, 20)


# ── owners from portfolios ────────────────────────────────────────────────


def _owners(buildings_complete: bool = True) -> dict[int, dict[str, Any]]:
    by_portfolio = (
        mapper.portfolio_buildings(fixture_rows("/buildings")) if buildings_complete else None
    )
    rows = mapper.owners_from_portfolios(fixture_rows("/portfolios"), by_portfolio)
    return {r["buildium_id"]: r for r in rows}


def test_an_owner_on_two_portfolios_is_one_row_owning_both_portfolios_buildings() -> None:
    owners = _owners()
    assert set(owners) == {990450001, 990450002}
    assert owners[990450002]["property_ids"] == [990100001, 990100002, 990100003]
    assert owners[990450001]["property_ids"] == [990100001, 990100003]


def test_a_company_name_only_when_no_persons_name_is_given() -> None:
    owners = _owners()
    assert owners[990450001]["company_name"] == "Synthetic Holdings LLC"
    assert owners[990450002]["company_name"] is None
    assert owners[990450002]["phone"] == "555-0111"
    assert owners[990450001]["phone"] == "555-0110"


def test_without_a_complete_buildings_read_property_ids_are_left_as_they_were() -> None:
    assert all("property_ids" not in row for row in _owners(buildings_complete=False).values())


def test_an_owner_on_a_portfolio_with_no_id_keeps_their_stored_properties() -> None:
    portfolios = [{"id": None, "owners": [{"id": 3}]}, {"id": 10, "owners": [{"id": 4}]}]
    rows = {r["buildium_id"]: r for r in mapper.owners_from_portfolios(portfolios, {10: {7}})}
    assert "property_ids" not in rows[3]
    assert rows[4]["property_ids"] == [7]


# ── work orders ───────────────────────────────────────────────────────────


def test_a_work_order_keeps_free_text_and_open_enums_as_written() -> None:
    row, outcome = mapper.map_work_order(WORK_ORDERS[990700003], COMPLETE_UNITS)
    assert row["status"] == "Waiting on Parts"
    assert row["priority"] == "URGENT", "an enum value outside the spec is kept, never a crash"
    assert row["pms_updated_at"] is None
    assert (outcome, row["unit_id"]) == (mapper.NO_UNIT, None)
    assert row["task_id"] is None and row["category_id"] is None
    assert row["assigned_to_user_id"] is None


def test_a_work_orders_last_modified_time_is_an_aware_instant() -> None:
    row, outcome = mapper.map_work_order(WORK_ORDERS[990700001], COMPLETE_UNITS)
    assert row["pms_updated_at"] == datetime(2026, 9, 30, 14, 2, 11, tzinfo=UTC)
    assert (outcome, row["unit_id"]) == (mapper.UNIT, 990200003)
    assert row["title"] == "Synthetic leak under the kitchen sink"
    single, outcome = mapper.map_work_order(WORK_ORDERS[990700002], COMPLETE_UNITS)
    assert outcome == mapper.SINGLE_FAMILY and single["unit_id"] is None
    assert single["pms_updated_at"] == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


# ── bills and their lines ─────────────────────────────────────────────────


def test_a_vendor_credit_is_not_a_bill() -> None:
    assert mapper.is_vendor_credit(BILLS[990800003])
    assert mapper.map_bill(BILLS[990800003]) is None


@pytest.mark.parametrize(
    ("bill_id", "paid"), [(990800001, False), (990800002, True), (990800004, None)]
)
def test_a_bill_is_paid_only_when_every_split_is(bill_id: int, paid: bool | None) -> None:
    row = mapper.map_bill(BILLS[bill_id])
    assert row is not None and row["is_paid"] is paid


def test_a_bill_carries_its_own_total_and_reference() -> None:
    row = mapper.map_bill(BILLS[990800001])
    assert row is not None
    assert row["total_amount"] == Decimal("325.0") and row["reference_number"] == "INV-1001"
    assert row["vendor_id"] == 990500001 and row["work_order_id"] == 990700001
    assert row["bill_date"] == date(2026, 9, 20) and row["due_date"] == date(2026, 10, 15)


@pytest.mark.parametrize(
    ("splits", "paid"),
    [
        ([], None),
        ([{"amount": 10.0, "amountPaid": 10.0}], True),
        ([{"amount": 10.0, "amountPaid": 4.0}], False),
        ([{"amount": 10.0}, {"paid": False}], False),
        ([{"amount": 10.0}, {"paid": True}], None),
    ],
)
def test_bill_paid_rules(splits: list[dict[str, Any]], paid: bool | None) -> None:
    assert mapper.bill_paid(splits) is paid


def test_bill_lines_attribute_by_building_and_resolve_units_by_the_lease_rule() -> None:
    lines = mapper.bill_lines(BILLS[990800001], COMPLETE_UNITS)
    assert [(x["line_index"], x["amount"], x["unit_id"]) for x in lines] == [
        (0, Decimal("300.0"), 990200003),
        (1, Decimal("25.0"), None),
    ]
    # The unit id 990100002 is ALSO a building id; here it is a real unit in
    # the split's own building, so it resolves.
    lock = mapper.bill_lines(BILLS[990800004], COMPLETE_UNITS)
    assert (lock[0]["property_id"], lock[0]["unit_id"]) == (990100003, 990100002)
    assert mapper.bill_lines(BILLS[990800001], None)[0]["unit_id"] is None


def test_a_portfolio_wide_split_is_on_the_portfolio_ledger_and_no_property() -> None:
    """So the spend report states it on its own instead of leaving it out of
    the property totals in silence (ADR 0010 Decision (b))."""
    (line,) = mapper.bill_lines(BILLS[990800002], COMPLETE_UNITS)
    assert line["property_id"] is None
    assert (line["ledger_type"], line["ledger_id"]) == (mapper.PORTFOLIO_LEDGER, 990300001)
    # A line on a building is property money: the ledger columns stay NULL,
    # as Buildium's do. So does a line that names neither.
    on_building = mapper.bill_lines(BILLS[990800001], COMPLETE_UNITS)
    assert {(x["ledger_type"], x["ledger_id"]) for x in on_building} == {(None, None)}
    nowhere = mapper.bill_lines({"id": 1, "billSplits": [{"amount": 5.0}]}, COMPLETE_UNITS)
    assert (nowhere[0]["ledger_type"], nowhere[0]["property_id"]) == (None, None)


# ── rent balances and aging ───────────────────────────────────────────────


def _balance(lease_id: int, charges: list[dict[str, Any]] | None = None) -> Any:
    all_charges = fixture_rows("/leases/charges")
    by_lease, by_id = mapper.charges_by_lease(all_charges)
    mine = by_lease.get(lease_id, []) if charges is None else charges
    return mapper.lease_balance(LEASES[lease_id], mine, by_id, COMPLETE_UNITS, TODAY)


def test_aging_buckets_by_post_date_when_the_open_charges_add_up() -> None:
    row = _balance(990600001)
    assert row["total_balance"] == Decimal("1600.0")
    assert row["balance_0_30"] == Decimal("1450.0")
    assert row["balance_31_60"] == Decimal("150.0")
    assert row["balance_61_90"] == Decimal("0") and row["balance_over_90"] == Decimal("0")
    assert (row["property_id"], row["unit_id"]) == (990100001, 990200001)


def test_a_hidden_recharge_is_dropped_and_a_later_one_counted() -> None:
    row = _balance(990600003)
    assert row["total_balance"] == Decimal("275.25")
    assert row["balance_31_60"] == Decimal("275.25")
    assert row["balance_0_30"] == Decimal("0")


def test_an_old_debt_is_over_90() -> None:
    assert _balance(990600004)["balance_over_90"] == Decimal("480.0")


def test_buckets_that_do_not_add_up_are_unknown_and_the_total_stands() -> None:
    row = _balance(990600005)
    assert row["total_balance"] == Decimal("300.0")
    assert [row[name] for name, _ in mapper.BUCKETS] == [None, None, None, None]


def test_a_lease_that_owes_nothing_or_is_in_credit_has_no_row() -> None:
    assert _balance(990600002) is None
    assert _balance(990600006) is None


def test_a_missing_or_unreadable_balance_is_unknown_never_zero() -> None:
    by_id: dict[int, Any] = {}
    for bad in (None, "n/a", float("nan")):
        lease = {**LEASES[990600001], "arBalance": bad}
        assert (
            mapper.lease_balance(lease, [], by_id, COMPLETE_UNITS, TODAY) is mapper.BALANCE_UNKNOWN
        )
    no_balance = {k: v for k, v in LEASES[990600001].items() if k != "arBalance"}
    assert (
        mapper.lease_balance(no_balance, [], by_id, COMPLETE_UNITS, TODAY) is mapper.BALANCE_UNKNOWN
    )


def test_without_a_complete_charges_read_every_bucket_is_unknown() -> None:
    by_id: dict[int, Any] = {}
    row = mapper.lease_balance(LEASES[990600001], None, by_id, COMPLETE_UNITS, TODAY)
    assert row["total_balance"] == Decimal("1600.0")
    assert [row[name] for name, _ in mapper.BUCKETS] == [None, None, None, None]


@pytest.mark.parametrize(
    "charge",
    [
        {"id": 1, "date": "2026-09-01", "amountDue": None},
        {"id": 1, "date": "not a date", "amountDue": 100.0},
        {"id": 1, "date": "2026-09-01", "amountDue": 100.0, "parentTxID": 424242},
    ],
)
def test_a_charge_that_cannot_be_read_makes_the_aging_unknown(charge: dict[str, Any]) -> None:
    assert mapper.lease_aging([charge], {1: charge}, Decimal("100.0"), TODAY) is None


def test_a_parent_id_of_zero_is_no_parent() -> None:
    charge = {"id": 1, "date": "2026-09-01", "amountDue": 100.0, "parentTxID": 0}
    aging = mapper.lease_aging([charge], {1: charge}, Decimal("100.0"), TODAY)
    assert aging is not None and aging["balance_31_60"] == Decimal("100.0")


def test_aging_is_counted_in_the_calendar_it_is_given() -> None:
    charge = {"id": 1, "date": "2026-09-05", "amountDue": 100.0}
    assert mapper.lease_aging([charge], {}, Decimal(100), date(2026, 10, 5))["balance_0_30"] == 100
    assert mapper.lease_aging([charge], {}, Decimal(100), date(2026, 10, 6))["balance_31_60"] == 100


# ── coercion: unknown is None ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1450.55, Decimal("1450.55")),
        ("12.5", Decimal("12.5")),
        (True, None),
        ("1e3", None),
        (float("inf"), None),
        ("", None),
        (None, None),
        ("12 dollars", None),
    ],
)
def test_money_is_exact_or_unknown(value: Any, expected: Decimal | None) -> None:
    assert mapper._money(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("false", False),
        ("TRUE", True),
        ("0", False),
        ("yes", None),
        (None, None),
    ],
)
def test_a_flag_is_a_real_yes_or_no_or_unknown(value: Any, expected: bool | None) -> None:
    assert mapper._bool(value) is expected


def test_ids_refuse_booleans_and_text() -> None:
    assert mapper._int(True) is None and mapper._int("abc") is None and mapper._int(" 12 ") == 12


def test_open_enums_and_odd_shapes_never_crash_a_mapper() -> None:
    odd: dict[str, Any] = {
        "id": "990100009",
        "status": {"not": "text"},
        "priority": ["HIGH"],
        "propertyType": "Treehouse",
        "category": "MIXED_USE",
        "type": "SUPPLIER",
        "approvalStatus": "PENDING_REVIEW",
        "frequency": "ANUUAL",
        "address": "not an object",
        "contacts": "not a list",
        "owners": None,
        "billSplits": {"not": "a list"},
        "credit": "maybe",
    }
    assert mapper.map_building(odd)["rental_sub_type"] == "Treehouse"
    assert mapper.map_unit(odd)["is_occupied"] is None
    assert mapper.map_tenant(odd) is None  # typed SUPPLIER: not a tenant
    assert mapper.map_vendor(odd)["category_name"] == "SUPPLIER"
    assert mapper.map_gl_account(odd)["buildium_id"] == 990100009
    work_order, _ = mapper.map_work_order(odd, COMPLETE_UNITS)
    assert work_order["status"] is None and work_order["priority"] is None
    lease, _ = mapper.map_lease(odd, COMPLETE_UNITS)
    assert lease["status"] is None
    assert mapper.lease_tenant_links(odd) == []
    assert mapper.owners_from_portfolios([odd], {}) == []
    bill = mapper.map_bill(odd)
    assert bill is not None and bill["is_paid"] is None
    assert mapper.bill_lines(odd, COMPLETE_UNITS) == []
