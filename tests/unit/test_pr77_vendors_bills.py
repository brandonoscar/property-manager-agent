"""PR #77 — vendor categories + search, unpaid bills tool, expanded schemas.

Three gaps from post-PR-#76 testing:

1. **Vendor schema was starved.** ``Vendor`` only had id/name/company/
   is_active. Buildium ships ``Category``, ``Address``, ``PhoneNumbers``,
   ``PrimaryEmail``, ``Website``, etc. on every row. PR #77 restores
   the full shape so the agent can answer "find me a plumber in
   Cleveland" without falling back on the user.

2. **No AP bills tool.** "How many unpaid bills at gardens row" fired
   the delinquency tool — wrong category entirely. Added
   ``buildium_list_unpaid_bills`` that filters by property name and
   days-overdue, and extended the ``Bill`` schema with ``Lines[]``.

   ⚠ This said the filter joins on ``Lines[].PropertyId``. It never could:
   Buildium sends that key on all 131 production lines and populates it on
   none of them (gotcha 53). Ownership is ``Lines[].AccountingEntity.Id`` on
   ``Rental`` lines, which is what ``BillLine.resolved_property_id`` reads.

3. **Diagnostic admin route for outstanding balances.** Brandon's
   account still returns ``[]`` from ``/leases/outstandingbalances``
   after the PR #76 fix. The diag endpoint dumps the raw response
   across three call shapes so we can decide the real fix in PR #78
   instead of guessing again.

Tests use the same lightweight client/api patch helpers the other
buildium tool tests use; no Buildium HTTP traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.agents import buildium_tools as bt
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import admin as admin_routes
from helixis.buildium.schemas import (
    Bill,
    BillLine,
    Vendor,
    VendorCategory,
)
from helixis.memory.context import memory_context

# ── Shared patch helper (mirrors test_pr76_tasks_and_errors.py) ────


def _ctx() -> Any:
    return memory_context(company_id="company-1", user_id="u1", session_id="s1")


def _client(api: Any) -> Any:
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


def _settings() -> Any:
    return MagicMock()


def _patch(client: Any, api: Any) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()

    class _Combined:
        def __enter__(self) -> Any:
            stack.enter_context(_ctx())
            stack.enter_context(
                patch.object(
                    bt,
                    "load_buildium_settings_for_company",
                    new=AsyncMock(return_value=_settings()),
                )
            )
            stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client))
            stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api))
            return None

        def __exit__(self, *exc: Any) -> None:
            stack.__exit__(*exc)

    return _Combined()


# ── Vendor fixtures ────────────────────────────────────────────────


def _vendor(
    id_: int,
    *,
    company_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    is_active: bool = True,
    category: str | None = None,
    city: str | None = None,
    state: str | None = None,
    phone_work: str | None = None,
) -> Any:
    v = MagicMock(spec=[])
    v.id = id_
    v.company_name = company_name
    v.first_name = first_name
    v.last_name = last_name
    v.name = company_name or (f"{first_name or ''} {last_name or ''}".strip() or None)
    v.is_active = is_active
    v.category = MagicMock(name=category) if category else None
    if category:
        v.category.name = category
        v.category.id = 1
    v.address = None
    if city or state:
        addr = MagicMock()
        addr.city = city
        addr.state = state
        v.address = addr
    v.phone_numbers = None
    if phone_work:
        phone = MagicMock()
        phone.number = phone_work
        phone.type = "Work"
        v.phone_numbers = [phone]
    return v


# ─────────────────────────────────────────────────────────────────────
# Schema parsing
# ─────────────────────────────────────────────────────────────────────


def test_vendor_schema_parses_full_buildium_response() -> None:
    """Feed a realistic Buildium /vendors row through ``Vendor``;
    every PR #77 field must round-trip."""
    payload = {
        "Id": 42,
        "IsCompany": True,
        "FirstName": None,
        "LastName": None,
        "CompanyName": "Acme Plumbing LLC",
        "IsActive": True,
        "Category": {"Id": 7, "Name": "Plumbing"},
        "Address": {
            "AddressLine1": "123 Pipe Way",
            "AddressLine2": "Suite 4",
            "AddressLine3": None,
            "City": "Cleveland",
            "State": "OH",
            "PostalCode": "44115",
            "Country": "US",
        },
        "PhoneNumbers": [
            {"Number": "555-0100", "Type": "Work"},
            {"Number": "555-0101", "Type": "Mobile"},
            {"Number": "555-0102", "Type": "Fax"},
        ],
        "PrimaryEmail": "ops@acme-plumbing.example",
        "AlternateEmail": "billing@acme-plumbing.example",
        "Website": "https://acme-plumbing.example",
        "TaxId": "12-3456789",
        "AccountNumber": "ACME-001",
        "Comments": "Net-30 terms",
    }
    v = Vendor.model_validate(payload)
    assert v.id == 42
    assert v.is_company is True
    assert v.company_name == "Acme Plumbing LLC"
    assert v.is_active is True
    assert v.category is not None
    assert v.category.id == 7
    assert v.category.name == "Plumbing"
    assert v.address is not None
    assert v.address.city == "Cleveland"
    assert v.address.state == "OH"
    assert v.address.address_line_1 == "123 Pipe Way"
    assert v.address.country == "US"
    assert v.phone_numbers is not None
    assert len(v.phone_numbers) == 3
    assert v.phone_numbers[0].number == "555-0100"
    assert v.phone_numbers[0].type == "Work"
    assert v.phone_numbers[1].number == "555-0101"
    assert v.phone_numbers[1].type == "Mobile"
    assert v.phone_numbers[2].number == "555-0102"
    assert v.phone_numbers[2].type == "Fax"
    assert v.primary_email == "ops@acme-plumbing.example"
    assert v.website == "https://acme-plumbing.example"
    assert v.account_number == "ACME-001"


def test_vendor_category_schema_round_trip() -> None:
    """``/vendors/categories`` returns just ``Id`` + ``Name``."""
    cat = VendorCategory.model_validate({"Id": 12, "Name": "Plumbing"})
    assert cat.id == 12
    assert cat.name == "Plumbing"


def test_bill_schema_parses_lines_for_property_filter() -> None:
    """``Lines[]`` round-trips, ``PropertyId`` included.

    ⚠ ``PropertyId`` is NOT the join key — it is the FALLBACK. Production
    populates it on zero rows (gotcha 53); ownership arrives on
    ``AccountingEntity.Id``. It is still typed and still asserted here
    because it is in the SDK contract, and if Buildium ever starts sending
    it a hard-coded switch to the other field would then be the bug. The
    scope test below is the one that exercises the key that actually works.
    """
    payload = {
        "Id": 999,
        "VendorId": 42,
        "Date": "2026-05-01T00:00:00",
        "DueDate": "2026-05-31T00:00:00",
        "TotalAmount": 540.0,
        "IsPaid": False,
        "ReferenceNumber": "INV-12",
        "Lines": [
            {"Id": 1, "PropertyId": 100, "UnitId": 200, "Amount": 540.0, "Memo": "Repair"},
        ],
    }
    b = Bill.model_validate(payload)
    assert b.id == 999
    assert b.is_paid is False
    assert b.reference_number == "INV-12"
    assert b.lines is not None
    assert len(b.lines) == 1
    assert isinstance(b.lines[0], BillLine)
    assert b.lines[0].property_id == 100
    assert b.lines[0].amount == 540.0


# ─────────────────────────────────────────────────────────────────────
# _vendor_label
# ─────────────────────────────────────────────────────────────────────


def test_vendor_label_falls_back_to_first_last_name() -> None:
    """An individual vendor (no ``company_name``, no ``name``) must
    still render with their first/last name — never "Unnamed vendor"."""
    individual = MagicMock()
    individual.company_name = None
    individual.name = None
    individual.first_name = "Jane"
    individual.last_name = "Doe"
    assert bt._vendor_label(individual) == "Jane Doe"


def test_vendor_label_returns_unnamed_when_no_name_data() -> None:
    """Defense-in-depth — if EVERY name field is empty (corrupt row),
    we never fall back to an id."""
    empty = MagicMock()
    empty.company_name = None
    empty.name = None
    empty.first_name = None
    empty.last_name = None
    assert bt._vendor_label(empty) == "Unnamed vendor"


# ─────────────────────────────────────────────────────────────────────
# buildium_list_vendor_categories
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_vendor_categories_returns_sorted_unique_names() -> None:
    cats = [
        MagicMock(name="Plumbing"),
        MagicMock(name="HVAC"),
        MagicMock(name="Plumbing"),  # duplicate
        MagicMock(name="Electrical"),
    ]
    for c, n in zip(cats, ["Plumbing", "HVAC", "Plumbing", "Electrical"], strict=False):
        c.name = n
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list_categories = AsyncMock(return_value=cats)

    with _patch(_client(api), api):
        result = await bt._list_vendor_categories_impl()

    # Sorted alphabetically, deduped.
    assert result == "Vendor categories: Electrical, HVAC, Plumbing."


@pytest.mark.asyncio
async def test_list_vendor_categories_empty() -> None:
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list_categories = AsyncMock(return_value=[])

    with _patch(_client(api), api):
        result = await bt._list_vendor_categories_impl()
    assert "No vendor categories" in result


# ─────────────────────────────────────────────────────────────────────
# buildium_search_vendors
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_vendors_by_category_is_case_insensitive() -> None:
    """``category="plumbing"`` matches Buildium's "Plumbing" (and any
    superset like "Plumbing & Pipefitting") — substring + case
    insensitive."""
    vendors = [
        _vendor(1, company_name="Acme Plumbing", category="Plumbing"),
        _vendor(2, company_name="Cold Snap HVAC", category="HVAC"),
        _vendor(3, company_name="Big Pipe", category="Plumbing & Pipefitting"),
        _vendor(4, company_name="Sparky Electric", category="Electrical"),
    ]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(category="plumbing")

    assert "Acme Plumbing" in result
    assert "Big Pipe" in result
    assert "Cold Snap HVAC" not in result
    assert "Sparky Electric" not in result


@pytest.mark.asyncio
async def test_search_vendors_by_city_filter() -> None:
    """The ``city`` filter does an exact (case-insensitive) match
    against ``Address.City``."""
    vendors = [
        _vendor(1, company_name="Cleveland Plumbing", category="Plumbing", city="Cleveland"),
        _vendor(2, company_name="Akron Plumbing", category="Plumbing", city="Akron"),
        _vendor(3, company_name="No-Address Plumbing", category="Plumbing"),
    ]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(city="cleveland")

    assert "Cleveland Plumbing" in result
    assert "Akron" not in result
    # Vendor with no address is filtered out when a city filter is set.
    assert "No-Address Plumbing" not in result


@pytest.mark.asyncio
async def test_search_vendors_no_matches_message_includes_filters() -> None:
    vendors = [_vendor(1, company_name="HVAC Co", category="HVAC")]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(category="plumbing", city="Cleveland")

    assert "No vendors found" in result
    assert "plumbing" in result
    assert "Cleveland" in result
    # No internal ids leak even on the no-matches path.
    assert "(#" not in result


@pytest.mark.asyncio
async def test_search_vendors_caps_at_15_with_overflow_note() -> None:
    """Brandon's chat doesn't want a wall of 200 plumbers. Cap at 15
    and tell the user there's more."""
    vendors = [_vendor(i, company_name=f"Plumber {i}", category="Plumbing") for i in range(1, 31)]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(category="plumbing")

    # Header carries the total.
    assert "Found 30 vendors" in result
    assert "showing first 15" in result
    # Only the first 15 vendors are listed.
    assert "Plumber 1 " in result or "Plumber 1 (" in result
    assert "Plumber 15 " in result or "Plumber 15 (" in result
    assert "Plumber 16" not in result
    assert "Plumber 30" not in result


@pytest.mark.asyncio
async def test_search_vendors_excludes_inactive_by_default() -> None:
    vendors = [
        _vendor(1, company_name="Active Plumbing", category="Plumbing", is_active=True),
        _vendor(2, company_name="Closed Down Inc", category="Plumbing", is_active=False),
    ]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(category="plumbing")

    assert "Active Plumbing" in result
    assert "Closed Down Inc" not in result


@pytest.mark.asyncio
async def test_search_vendors_surfaces_id_for_agent() -> None:
    """Option 1 (Buildium writes): vendor ids ARE surfaced in the tool output —
    in the clean ``(id: N)`` form — so the agent can pass ``vendor_id`` on a
    work-order write. The "no ids to the user" rule moves to the REPLY level
    (the agent says the name, never the id), enforced by the routing eval guards
    (``reply_must_not_contain`` "Property #" etc.), not by stripping ids from the
    tool's own model-facing output."""
    vendors = [
        _vendor(9999, company_name="Acme Plumbing", category="Plumbing", phone_work="555-1212"),
    ]
    api = MagicMock()
    api.vendors = MagicMock()
    api.vendors.list = AsyncMock(return_value=vendors)

    with _patch(_client(api), api):
        result = await bt._search_vendors_impl(category="plumbing")

    # The id is available to the agent for the write…
    assert "id: 9999" in result
    # …the human name is still present; the id is not a user-facing "Vendor #" leak.
    assert "Acme Plumbing" in result
    assert "Vendor #9999" not in result


# ─────────────────────────────────────────────────────────────────────
# buildium_list_unpaid_bills
# ─────────────────────────────────────────────────────────────────────


def _bill(
    id_: int,
    *,
    total_amount: float,
    is_paid: bool,
    due_date: datetime | None = None,
    property_ids: list[int] | None = None,
) -> Any:
    # ⚠ Built through the REAL schema, not a MagicMock. A MagicMock answers
    # every attribute with another MagicMock, so this fixture happily served
    # ``line.property_id`` — a key production sends on all 131 lines and
    # populates on none (gotcha 53) — and the test passed while the shipped
    # tool returned "No unpaid bills found at <property>" for a property with
    # real money outstanding. The mock supplied the field the code wanted,
    # which is precisely how this class of defect survives a unit suite.
    #
    # Ownership therefore rides ``AccountingEntity`` here, as it does on the
    # wire, and the schema refuses any shape Buildium would not send.
    lines: list[dict[str, Any]] = [
        {"AccountingEntity": {"Id": pid, "AccountingEntityType": "Rental"}}
        for pid in (property_ids or [])
    ]
    return Bill.model_validate(
        {
            "Id": id_,
            "TotalAmount": total_amount,
            "IsPaid": is_paid,
            "DueDate": due_date.isoformat() if due_date else None,
            "Lines": lines,
        }
    )


def _property(id_: int, name: str | None = None, address_line1: str | None = None) -> Any:
    p = MagicMock()
    p.id = id_
    p.name = name
    if address_line1:
        addr = MagicMock()
        addr.address_line1 = address_line1
        addr.city = None
        addr.state = None
        p.address = addr
    else:
        p.address = None
    return p


@pytest.mark.asyncio
async def test_list_unpaid_bills_totals_and_count() -> None:
    """The unpaid filter returns just unpaid bills, summed correctly."""
    bills = [
        _bill(1, total_amount=200.0, is_paid=False),
        _bill(2, total_amount=340.0, is_paid=False),
        _bill(3, total_amount=999.0, is_paid=True),  # filtered
    ]
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl()

    assert "2 unpaid bills" in result
    assert "$540.00" in result


@pytest.mark.asyncio
async def test_list_unpaid_bills_excludes_null_amount_from_total() -> None:
    """A bill with no TotalAmount must NOT be silently summed as $0 — the total
    covers only priced bills and states how many it excluded, so the PM is never
    shown a confidently-wrong figure."""
    bills = [
        _bill(1, total_amount=200.0, is_paid=False),
        _bill(2, total_amount=None, is_paid=False),  # null amount from Buildium
    ]
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl()

    assert "2 unpaid bills" in result
    assert "$200.00" in result  # NOT presented as the complete total
    assert "excludes 1 bill with no amount recorded" in result


@pytest.mark.asyncio
async def test_list_unpaid_bills_filters_by_property_name() -> None:
    """``property_name`` is resolved to a property id via the rentals
    list (substring, case-insensitive), then bills are filtered to
    those whose ``Lines[]`` reference that property id."""
    bills = [
        _bill(1, total_amount=200.0, is_paid=False, property_ids=[100]),
        _bill(2, total_amount=340.0, is_paid=False, property_ids=[200]),
    ]
    properties = [
        _property(100, name="Gardens Row Apartments"),
        _property(200, name="Sunset Manor"),
    ]
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)
    api.rentals = MagicMock()
    api.rentals.list_properties = AsyncMock(return_value=properties)

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl(property_name="gardens row")

    assert "1 unpaid bill" in result
    assert "$200.00" in result


@pytest.mark.asyncio
async def test_list_unpaid_bills_unknown_property_returns_clean_message() -> None:
    bills = [_bill(1, total_amount=200.0, is_paid=False, property_ids=[100])]
    properties = [_property(100, name="Sunset Manor")]
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)
    api.rentals = MagicMock()
    api.rentals.list_properties = AsyncMock(return_value=properties)

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl(property_name="atlantis")

    assert "couldn't find a property" in result
    assert "atlantis" in result
    # No id leaks.
    assert "(#" not in result


@pytest.mark.asyncio
async def test_list_unpaid_bills_filters_by_days_overdue() -> None:
    """``days_overdue=30`` keeps only bills with ``DueDate <
    now - 30 days``. Bills due in the future or due within the
    last 30 days are excluded."""
    now = datetime.now(UTC)
    bills = [
        # Overdue 60 days — keep.
        _bill(1, total_amount=400.0, is_paid=False, due_date=now - timedelta(days=60)),
        # Overdue 10 days — exclude (within the 30-day window).
        _bill(2, total_amount=100.0, is_paid=False, due_date=now - timedelta(days=10)),
        # Not yet due — exclude.
        _bill(3, total_amount=500.0, is_paid=False, due_date=now + timedelta(days=5)),
    ]
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl(days_overdue=30)

    assert "1 unpaid bill" in result
    assert "$400.00" in result


@pytest.mark.asyncio
async def test_list_unpaid_bills_zero_matches_message() -> None:
    """Empty filtered result reads cleanly, mentions the filters used."""
    api = MagicMock()
    api.bills = MagicMock()
    api.bills.list = AsyncMock(return_value=[])

    with _patch(_client(api), api):
        result = await bt._list_unpaid_bills_impl(days_overdue=90)

    assert "No unpaid bills" in result
    assert "90+ days overdue" in result


# ─────────────────────────────────────────────────────────────────────
# Tool factory
# ─────────────────────────────────────────────────────────────────────


def test_pr77_tools_are_registered_in_factory() -> None:
    """The three new tools must be wired into ``buildium_tools()``."""
    tools = bt.buildium_tools()
    names = {t.name for t in tools}
    assert "buildium_list_vendor_categories" in names
    assert "buildium_search_vendors" in names
    assert "buildium_list_unpaid_bills" in names


# ─────────────────────────────────────────────────────────────────────
# Diagnostic admin route
# ─────────────────────────────────────────────────────────────────────


def _make_admin_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")
    return app


def test_diag_outstanding_balances_requires_auth() -> None:
    """Without a valid bearer the endpoint must NOT return data —
    Brandon needs the diag, but every other authenticated tenant
    must not be able to read each other's Buildium data through it."""
    app = _make_admin_app()
    client = TestClient(app)
    resp = client.get("/api/v1/admin/diag/outstanding_balances")
    # ``get_current_user`` raises 401/403 when no bearer is provided.
    assert resp.status_code in (401, 403)


def test_diag_outstanding_balances_returns_three_shapes_plus_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diag route dumps the raw Buildium response for the three
    call shapes plus per-lease transactions sample. Each entry
    carries either a body_preview or an error."""
    app = _make_admin_app()

    test_user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-1"],
    )

    def _stub_user() -> AuthenticatedUser:
        return test_user

    app.dependency_overrides[get_current_user] = _stub_user

    # Mock the client.request to capture each shape with a different
    # body so the test can pin the per-shape unwrap.
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    request_calls: list[dict[str, Any]] = []

    async def _fake_request(method: str, path: str, **kwargs: Any) -> Any:
        request_calls.append({"path": path, "kwargs": kwargs})
        # Vary the response by query so the three shapes are distinct.
        query = kwargs.get("query") or {}
        if "balanceduration" in query:
            return [{"LeaseId": 1, "Balance": 100.0}]
        if "leasestatuses" in query:
            return []
        return [{"LeaseId": 7}]

    fake_client.request = _fake_request

    fake_api = MagicMock()
    fake_api.leases = MagicMock()
    lease_obj = MagicMock()
    lease_obj.id = 1
    fake_api.leases.list = AsyncMock(return_value=[lease_obj])
    fake_api.leases.list_transactions = AsyncMock(return_value=[MagicMock(), MagicMock()])

    monkeypatch.setattr(
        admin_routes,
        "load_buildium_settings_for_company",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(admin_routes, "BuildiumClient", lambda settings: fake_client)
    monkeypatch.setattr(admin_routes, "BuildiumApi", lambda client: fake_api)

    client = TestClient(app)
    resp = client.get("/api/v1/admin/diag/outstanding_balances")
    assert resp.status_code == 200
    body = resp.json()
    results = body["results"]
    # Three balance shapes + one per-lease sample = four entries.
    assert len(results) == 4
    shapes = [r["shape"] for r in results]
    assert shapes == [
        "active_only",
        "active_total_balance",
        "bare",
        "per_lease_transactions_sample",
    ]
    # active_only call gets [] back → row_count is 0.
    assert results[0]["status"] == 200
    assert results[0]["row_count_if_list"] == 0
    # The TotalBalance fallback returns 1 row.
    assert results[1]["row_count_if_list"] == 1
    # bare returns 1 row.
    assert results[2]["row_count_if_list"] == 1
    # Per-lease sample carries the active lease count + a tx_count.
    sample = results[3]
    assert sample["active_lease_count"] == 1
    assert sample["sample"][0]["tx_count"] == 2
