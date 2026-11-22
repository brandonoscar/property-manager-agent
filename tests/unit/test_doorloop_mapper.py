"""DoorLoop records -> mirror rows (``helixis.doorloop.mapper``).

Pinned:
  - a DoorLoop id (24-hex ObjectId) becomes one fixed 53-bit mirror id, the
    web app reads exactly, and a collision is found rather than written;
  - every row carries only the shared writer's columns (both sides, rule 15);
  - unknown is None, and a value this pass cannot determine is left out;
  - the lease and link words are the readers' words;
  - the ledger is signed by its effect on the balance;
  - the aging buckets are stated only when they add up to DoorLoop's balance;
  - ``raw`` never carries a date of birth, tax or payment details.
Every record is synthetic, shaped as the spec's schemas.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from helixis.db.tenant_links import CURRENT_LINK_STATUS, MOVED_OUT_LINK_STATUS
from helixis.doorloop import mapper as m
from helixis.pms import mirror

PROP = "64b0c0ffee0000000000a001"
UNIT_A = "64b0c0ffee0000000000b001"
UNIT_B = "64b0c0ffee0000000000b002"
LEASE = "64b0c0ffee0000000000c001"
TENANT = "64b0c0ffee0000000000d001"
OWNER = "64b0c0ffee0000000000e001"
TODAY = date(2026, 10, 6)


def _cols(table: str) -> set[str]:
    return {"buildium_id", "raw", *mirror.KEYED_COLUMNS[table]}


# ── ids ─────────────────────────────────────────────────────────────────


def test_an_object_id_becomes_one_fixed_mirror_id_in_javascripts_range() -> None:
    first = m.mirror_id(PROP)
    assert first is not None and 0 < first <= m.MAX_MIRROR_ID == 2**53 - 1
    assert m.mirror_id(PROP.upper()) == first == m.mirror_id(f"  {PROP} ")
    assert m.mirror_id(UNIT_A) != first
    # Fixed forever: re-keying would orphan every DoorLoop row. This value is
    # the function's, computed once; a change to the function fails here.
    assert m.mirror_id("507f191e810c19729de860ea") == 6368547447916770


@pytest.mark.parametrize(
    "value", [None, "", 12345, "507f191e810c19729de860e", "zz7f191e810c19729de860ea", True]
)
def test_anything_but_an_object_id_has_no_mirror_id(value: Any) -> None:
    assert m.mirror_id(value) is None


def test_a_collision_in_one_read_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": PROP}, {"id": UNIT_A}, {"id": PROP.upper()}]
    assert m.collisions(rows) == [], "the same id twice is not a collision"
    monkeypatch.setattr(m, "mirror_id", lambda value: 7 if isinstance(value, str) else None)
    assert m.collisions(rows) == sorted({PROP, UNIT_A})


# ── entities ────────────────────────────────────────────────────────────


def test_a_property_fills_the_properties_columns() -> None:
    row = m.map_property(
        {
            "id": PROP,
            "name": "Redbud Court",
            "type": "RESIDENTIAL_MULTI_FAMILY",
            "address": {"street1": "1 Synthetic Way", "city": "Testville", "zip": "00001"},
            "active": True,
            "owners": [{"owner": OWNER, "ownershipPercentage": 100}],
        }
    )
    assert set(row) <= _cols("helixis_properties")
    assert row["buildium_id"] == m.mirror_id(PROP)
    assert row["rental_sub_type"] == "RESIDENTIAL_MULTI_FAMILY"
    assert row["address_line1"] == "1 Synthetic Way" and row["postal_code"] == "00001"
    assert row["state"] is None, "no state field documented: unknown, never blank"
    assert row["is_active"] is True


def test_a_unit_is_occupied_only_by_an_active_lease_and_unknown_from_a_partial_read() -> None:
    leases = [
        {"id": LEASE, "status": "ACTIVE", "units": [UNIT_A]},
        {"id": "64b0c0ffee0000000000c002", "status": "INACTIVE", "units": [UNIT_B]},
    ]
    unit_b = {"id": UNIT_B, "property": PROP, "name": "4B", "marketRent": 1450.55}
    complete = m.Occupancy.from_leases(leases, complete=True)
    assert m.map_unit({"id": UNIT_A, "property": PROP}, complete)["is_occupied"] is True
    row = m.map_unit(unit_b, complete)
    assert set(row) <= _cols("helixis_units")
    assert row["is_occupied"] is False and row["market_rent"] == Decimal("1450.55")
    partial = m.Occupancy.from_leases(leases, complete=False)
    assert "is_occupied" not in m.map_unit(unit_b, partial), "left out: the mirror keeps it"
    assert "is_occupied" not in m.map_unit(unit_b, None)


def test_a_prospect_never_lands_in_the_tenants_table() -> None:
    tenant = {
        "id": TENANT,
        "firstName": "Jane",
        "lastName": "Doe",
        "type": "LEASE_TENANT",
        "emails": [
            {"type": "Work", "address": "w@example.test"},
            {"type": "Primary", "address": "p@example.test"},
        ],
        "phones": [{"type": "Fax", "number": "555-0100"}, {"type": "Home", "number": "555-0101"}],
        "dateOfBirth": "1990-01-01",
    }
    row = m.map_tenant(tenant)
    assert row is not None and set(row) <= _cols("helixis_tenants")
    assert row["email"] == "p@example.test", "the Primary email wins"
    assert row["phone"] == "555-0101", "a fax is never the phone"
    assert "dateOfBirth" not in json.dumps(row["raw"])
    assert m.map_tenant({**tenant, "type": "PROSPECT_TENANT"}) is None


def test_an_owners_properties_come_only_from_a_complete_properties_read() -> None:
    owned = m.owners_by_id([{"id": PROP, "owners": [{"owner": OWNER, "ownershipPercentage": 50}]}])
    owner = {"id": OWNER, "companyName": "Synthetic Holdings LLC", "federalTaxInfo": {"taxId": "x"}}
    row = m.map_owner(owner, owned)
    assert set(row) <= _cols("helixis_owners")
    assert row["property_ids"] == [m.mirror_id(PROP)]
    assert row["company_name"] == "Synthetic Holdings LLC"
    assert "federalTaxInfo" not in row["raw"]
    assert "property_ids" not in m.map_owner(owner, None), "left out after a partial read"
    assert m.map_owner({"id": "64b0c0ffee0000000000e002"}, owned)["property_ids"] == []


def test_a_vendor_states_what_doorloop_does_not_say_as_unknown() -> None:
    row = m.map_vendor({"id": OWNER, "name": "Fix-It Co", "active": False, "balance": 10})
    assert set(row) <= _cols("helixis_vendors")
    assert row["is_company"] is None and row["category_name"] is None
    assert row["is_active"] is False


def test_a_lease_takes_the_readers_word_and_one_unit() -> None:
    lease = {
        "id": LEASE,
        "property": PROP,
        "units": [UNIT_A],
        "status": "ACTIVE",
        "start": "2026-01-01",
        "end": "2026-12-31",
        "totalRecurringRent": 1200,
        "totalDepositsHeld": 1200.5,
    }
    row = m.map_lease(lease)
    assert set(row) <= _cols("helixis_leases")
    assert row["status"] == m.ACTIVE_LEASE_STATUS == "Active"
    assert row["unit_id"] == m.mirror_id(UNIT_A)
    assert row["lease_from_date"] == date(2026, 1, 1) and row["rent"] == Decimal("1200")
    assert row["security_deposit"] == Decimal("1200.5")
    assert m.map_lease({**lease, "status": "INACTIVE"})["status"] == "INACTIVE"
    assert m.map_lease({**lease, "units": [UNIT_A, UNIT_B]})["unit_id"] is None
    assert m.map_lease({**lease, "totalRecurringRent": None})["rent"] is None


def test_a_lease_tenant_link_speaks_the_readers_three_words() -> None:
    link = {"lease": LEASE, "tenant": {"id": TENANT}, "status": "PAST", "moveInAt": "2025-01-01"}
    row = m.lease_tenant_link(link)
    assert row == {
        "lease_buildium_id": m.mirror_id(LEASE),
        "tenant_buildium_id": m.mirror_id(TENANT),
        "status": MOVED_OUT_LINK_STATUS,
        "move_in_date": date(2025, 1, 1),
    }
    assert m.lease_tenant_link({**link, "status": "CURRENT"})["status"] == CURRENT_LINK_STATUS  # type: ignore[index]
    assert m.lease_tenant_link({**link, "status": "SOMETHING"})["status"] is None  # type: ignore[index]
    assert m.lease_tenant_link({**link, "lease": None}) is None
    no_tenant = m.lease_tenant_link({**link, "tenant": None})
    assert no_tenant is not None and no_tenant["tenant_buildium_id"] is None, "counted, not dropped"


def test_only_a_work_order_task_is_a_work_order() -> None:
    task = {
        "id": "64b0c0ffee0000000000f001",
        "type": "WORK_ORDER",
        "subject": "Leaking faucet",
        "status": "IN_PROGRESS",
        "priority": "HIGH",
        "property": PROP,
        "unit": UNIT_A,
        "updatedAt": 1_759_700_000_000,
        "workOrder": {"assignedToVendor": OWNER},
    }
    row = m.map_work_order(task)
    assert row is not None and set(row) <= _cols("helixis_work_orders")
    # Re-pointed 2026-10-07 from DoorLoop's own IN_PROGRESS: every reader of
    # "open" speaks Buildium's words (buildium.maintenance_status), and a
    # COMPLETED kept as written read as an open work order.
    assert row["title"] == "Leaking faucet" and row["status"] == "InProgress"
    assert row["priority"] == "High"
    assert row["pms_updated_at"] == datetime.fromtimestamp(1_759_700_000, tz=UTC)
    assert row["unit_id"] == m.mirror_id(UNIT_A)
    for other in ("INTERNAL_TASK", "TENANT_REQUEST", "OWNER_REQUEST", None):
        assert m.map_work_order({**task, "type": other}) is None


# ── the ledger and balances ───────────────────────────────────────────────


def test_the_ledger_is_signed_by_its_effect_on_the_balance() -> None:
    charge = {
        "id": "64b0c0ffee0000000000aa01",
        "lease": LEASE,
        "date": "2026-10-01",
        "lines": [{"amount": 1000, "balance": 1000}, {"amount": 50.25, "balance": 0}],
    }
    payment = {
        "id": "64b0c0ffee0000000000aa02",
        "lease": LEASE,
        "date": "2026-10-03",
        "amountReceived": 500,
    }
    signed = {
        kind: m.ledger_row(kind, obj)["total_amount"]  # type: ignore[index]
        for kind, obj in (
            ("charges", charge),
            ("credits", charge),
            ("payments", payment),
            ("reversed_payments", payment),
        )
    }
    assert signed == {
        "charges": Decimal("1050.25"),
        "credits": Decimal("-1050.25"),
        "payments": Decimal("-500"),
        "reversed_payments": Decimal("500"),
    }
    row = m.ledger_row("payments", payment)
    assert row is not None and row["transaction_type"] == "Payment"
    assert row["transaction_date"] == datetime(2026, 10, 3, tzinfo=UTC)
    assert set(row) <= {"buildium_id", "raw", *mirror.PER_PARENT["helixis_lease_transactions"][1]}
    assert m.ledger_row("charges", {**charge, "lines": [{"amount": "x"}]})["total_amount"] is None  # type: ignore[index]
    assert m.ledger_row("charges", {**charge, "lease": None}) is None


def test_the_aging_is_stated_only_when_it_adds_up_to_doorloops_balance() -> None:
    charges = m.open_charges_by_lease(
        [
            {"lease": LEASE, "date": "2026-09-30", "lines": [{"balance": 100}]},
            {"lease": LEASE, "date": "2026-07-01", "lines": [{"balance": 50}, {"balance": 0}]},
        ]
    )[m.mirror_id(LEASE)]  # type: ignore[index]
    lease = {"id": LEASE, "property": PROP, "units": [UNIT_A], "totalBalancedue": 150}
    row = m.lease_balance(lease, charges, TODAY)
    assert set(row) == {"lease_id", "raw", *mirror.BALANCE_COLUMNS}
    assert row["balance_0_30"] == Decimal("100") and row["balance_over_90"] == Decimal("50")
    off = m.lease_balance({**lease, "totalBalancedue": 175}, charges, TODAY)
    assert off["total_balance"] == Decimal("175")
    assert all(off[name] is None for name, _ in m.BUCKETS), "never a bucket that does not add up"
    assert all(m.lease_balance(lease, None, TODAY)[n] is None for n, _ in m.BUCKETS)
    assert m.lease_balance({**lease, "totalBalancedue": 0}, charges, TODAY) is None
    assert m.lease_balance({**lease, "totalBalancedue": -20}, charges, TODAY) is None
    assert m.lease_balance({**lease, "totalBalancedue": None}, charges, TODAY) is m.BALANCE_UNKNOWN


def test_an_unreadable_open_amount_makes_the_aging_unknown() -> None:
    charges = m.open_charges_by_lease([{"lease": LEASE, "date": "2026-10-01", "lines": [{}]}])
    assert charges[m.mirror_id(LEASE)] == [(date(2026, 10, 1), None)]  # type: ignore[index]
    assert m.lease_aging(charges[m.mirror_id(LEASE)], Decimal(0), TODAY) is None  # type: ignore[index]


def test_raw_never_carries_what_no_reader_needs() -> None:
    record = {
        "id": TENANT,
        "dateOfBirth": "1990-01-01",
        "federalTaxInfo": {"taxId": "12-3456789"},
        "emergencyContacts": [{"name": "x"}],
        "dependants": [{"name": "y"}],
        "nested": [{"ePayInfo": {"bank": "z"}}],
    }
    text = json.dumps(m.withheld(record))
    for key in m.RAW_WITHHELD:
        assert key not in text, key


# ── tasks, applicants, the chart of accounts, bills, turnovers ───────────

BILL = "64b0c0ffee0000000000f101"
ACCOUNT = "64b0c0ffee0000000000f201"
VENDOR = "64b0c0ffee0000000000f301"
ORDER = "64b0c0ffee0000000000f001"


def _task(**fields: Any) -> dict[str, Any]:
    return {
        "id": "64b0c0ffee0000000000f002",
        "type": "INTERNAL_TASK",
        "subject": "Renew the permit",
        "status": "NOT_STARTED",
        "priority": "LOW",
        "dueDate": "2026-10-20",
        "property": PROP,
        **fields,
    }


def test_a_task_takes_the_readers_words_and_a_work_order_is_not_a_task() -> None:
    row = m.map_task(_task())
    assert row is not None and set(row) <= _cols("helixis_tasks")
    assert (row["task_type"], row["status"], row["priority"]) == ("Todo", "New", "Low")
    assert row["due_date"] == date(2026, 10, 20) and row["property_id"] == m.mirror_id(PROP)
    assert row["assigned_to_user_id"] is None and row["category_name"] is None
    for kind, word in (
        ("TENANT_REQUEST", "ResidentRequest"),
        ("OWNER_REQUEST", "RentalOwnerRequest"),
    ):
        assert m.map_task(_task(type=kind))["task_type"] == word  # type: ignore[index]
    assert m.map_task(_task(type="WORK_ORDER")) is None
    assert m.map_task(_task(type="SOMETHING_NEW")) is None
    assert m.map_task(_task(status="SOMETHING_NEW"))["status"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    ("status", "word"),
    [
        ("NOT_STARTED", "New"),
        ("RECEIVED", "New"),
        ("IN_PROGRESS", "InProgress"),
        ("COMPLETED", "Completed"),
        ("ARCHIVED", "Closed"),
    ],
)
def test_every_task_status_lands_on_a_readers_word(status: str, word: str) -> None:
    assert m.map_task(_task(status=status))["status"] == word  # type: ignore[index]
    order = m.map_work_order(_task(type="WORK_ORDER", status=status))
    assert order is not None and order["status"] == word


def _prospect(status: str | None, interests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "64b0c0ffee0000000000f401",
        "type": "PROSPECT_TENANT",
        "firstName": "Pat",
        "lastName": "Moe",
        "emails": [{"type": "Primary", "address": "pat@example.test"}],
        "prospectInfo": {"status": status, "interests": interests or []},
    }


def test_only_a_prospect_who_reached_the_application_is_an_applicant() -> None:
    row = m.map_applicant(_prospect("APPLICATION_SUBMITTED", [{"property": PROP, "unit": UNIT_A}]))
    assert row is not None and set(row) <= _cols("helixis_applicants")
    assert row["status"] == "New" and row["email"] == "pat@example.test"
    assert (row["property_id"], row["unit_id"]) == (m.mirror_id(PROP), m.mirror_id(UNIT_A))
    for lead in (
        "NEW",
        "CONTACT_ATTEMPTED",
        "CONTACT_MADE",
        "SHOWING_SCHEDULED",
        "SHOWING_COMPLETED",
    ):
        assert m.map_applicant(_prospect(lead)) is None, lead
    assert m.map_applicant(_prospect("CLOSED_LOST")) is None, (
        "lost: never said whether they applied"
    )
    assert m.map_applicant(_prospect(None)) is None
    assert m.map_applicant({**_prospect("APPLICATION_SUBMITTED"), "type": "LEASE_TENANT"}) is None


def test_an_applicant_interested_in_several_places_names_none() -> None:
    row = m.map_applicant(_prospect("LEASE_OFFER_MADE", [{"property": PROP}, {"property": UNIT_B}]))
    assert row is not None and row["status"] == "Approved"
    assert (row["property_id"], row["unit_id"]) == (None, None)


@pytest.mark.parametrize(
    ("word", "kind", "bank"),
    [
        ("ASSET_BANK", "Asset", True),
        ("ASSET_ACCOUNTS_RECEIVABLE", "Asset", False),
        ("LIABILITY_CREDIT_CARD", "Liability", False),
        ("EQUITY_EQUITY", "Equity", False),
        ("REVENUE_OTHER_INCOME", "Income", False),
        ("EXPENSE_COGS", "Expense", False),
        ("SOMETHING_NEW", None, None),
        (None, None, None),
    ],
)
def test_an_account_takes_buildiums_type_and_keeps_doorloops(
    word: str | None, kind: str | None, bank: bool | None
) -> None:
    row = m.map_gl_account({"id": ACCOUNT, "name": "Repairs", "type": word, "active": True})
    assert set(row) <= _cols("helixis_gl_accounts")
    assert (row["type"], row["sub_type"], row["is_bank_account"]) == (kind, word, bank)
    assert row["account_number"] is None and row["parent_gl_account_id"] is None


def _bill(**fields: Any) -> dict[str, Any]:
    return {
        "id": BILL,
        "vendor": VENDOR,
        "date": "2026-09-28",
        "dueDate": "2026-10-28",
        "reference": "INV-1",
        "totalAmount": 300,
        "totalBalance": 300,
        "lines": [
            {
                "account": ACCOUNT,
                "amount": 280,
                "memo": "Part",
                "linkedToResourceType": "property",
                "linkedToResourceId": PROP,
            },
            {"account": ACCOUNT, "amount": 20},
        ],
        **fields,
    }


def test_a_bill_is_paid_only_when_nothing_is_left_on_it() -> None:
    assert m.map_bill(_bill(), None)["is_paid"] is False
    assert m.map_bill(_bill(totalBalance=0), None)["is_paid"] is True
    assert m.map_bill(_bill(totalBalance=None), None)["is_paid"] is None
    assert m.map_bill(_bill(totalBalance=-5), None)["is_paid"] is None
    row = m.map_bill(_bill(), None)
    assert set(row) <= _cols("helixis_bills")
    assert row["vendor_id"] == m.mirror_id(VENDOR) and row["total_amount"] == Decimal("300")
    assert row["bill_date"] == date(2026, 9, 28) and row["reference_number"] == "INV-1"


def test_a_bills_work_order_comes_only_from_a_complete_tasks_read() -> None:
    orders = m.bill_work_orders(
        [
            {"id": ORDER, "type": "WORK_ORDER", "workOrder": {"vendorBill": BILL, "billed": True}},
            {
                "id": "64b0c0ffee0000000000f003",
                "type": "INTERNAL_TASK",
                "workOrder": {"vendorBill": BILL},
            },
        ]
    )
    assert orders == {m.mirror_id(BILL): m.mirror_id(ORDER)}
    assert m.map_bill(_bill(), orders)["work_order_id"] == m.mirror_id(ORDER)
    assert m.map_bill(_bill(), {})["work_order_id"] is None, "a complete read: no work order"
    assert "work_order_id" not in m.map_bill(_bill(), None), "a partial read: left as it was"


def test_a_line_on_no_property_is_the_companys_own_spend() -> None:
    on_property, company = m.bill_lines(_bill())
    assert set(on_property) <= set(mirror.PER_PARENT["helixis_bill_lines"][1])
    assert on_property["property_id"] == m.mirror_id(PROP) and on_property["ledger_type"] is None
    assert on_property["gl_account_id"] == m.mirror_id(ACCOUNT)
    assert on_property["memo"] == "Part" and on_property["line_index"] == 0
    assert company["property_id"] is None and company["ledger_type"] == m.MANAGER_LEDGER
    assert m.MANAGER_LEDGER == 1, "migration 0104's 1 Manager"
    unknown = m.bill_lines(_bill(lines=[{"amount": 5, "linkedToResourceType": "unit"}]))[0]
    assert unknown["property_id"] is None and unknown["ledger_type"] is None, "unknown, not company"


def test_a_lease_turns_over_when_every_tenant_has_a_move_out() -> None:
    other = "64b0c0ffee0000000000c002"
    links = [
        {"lease": LEASE, "moveOutAt": "2026-03-01"},
        {"lease": LEASE, "moveOutAt": "2026-10-31"},
        {"lease": other, "moveOutAt": "2026-10-15"},
        {"lease": other},
    ]
    assert m.lease_move_outs(links) == {m.mirror_id(LEASE): date(2026, 10, 31)}
    assert m.lease_move_outs([{"lease": LEASE, "moveOutAt": "not a date"}]) == {}
