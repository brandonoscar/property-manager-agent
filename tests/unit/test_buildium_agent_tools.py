"""Unit tests for the Buildium agent tools.

These tools are what let the orchestrator answer "how many properties do
I have" with a single API call instead of falling back to the (now
disabled) browser specialist. The tools should:

* Return a friendly "not connected" message when the company has no
  Buildium credentials row \u2014 NOT raise.
* Otherwise call the in-house BuildiumApi and format the result as
  human-readable text.

We test the underscored ``_*_impl`` coroutines directly because the
public symbols are wrapped by the OpenAI Agents SDK's ``@function_tool``
decorator into ``FunctionTool`` objects (no longer plain callables).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.memory.context import memory_context

# --- helpers ---


def _prop(id_: int, name: str, line1: str | None = None, city: str | None = None) -> Any:
    """Build a tiny mock property object that matches the attribute shape
    our tools read (avoids depending on Buildium schemas + asyncpg)."""
    address = MagicMock(address_line1=line1, city=city, state=None)
    p = MagicMock()
    p.id = id_
    p.name = name
    p.address = address
    return p


def _ctx_company(company_id: str = "company-1") -> Any:
    """Bind a memory context for the duration of a tool call."""
    return memory_context(company_id=company_id, user_id="u1", session_id="s1")


def _api_with_properties(properties: list[Any]) -> tuple[Any, Any]:
    api_mock = MagicMock()
    api_mock.rentals = MagicMock()
    api_mock.rentals.list_properties = AsyncMock(return_value=properties)

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock


# --- not-connected paths ---


@pytest.mark.asyncio
async def test_count_properties_returns_not_connected_when_no_credentials() -> None:
    with (
        _ctx_company("missing-co"),
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=None)),
    ):
        result = await bt._count_properties_impl()
    assert isinstance(result, str)
    assert result.startswith("Buildium is not connected")


@pytest.mark.asyncio
async def test_list_properties_returns_not_connected_when_no_company() -> None:
    """Empty memory context (no company_id) must not raise \u2014 it must
    return the same friendly not-connected message so the orchestrator
    can pattern-match on it."""
    # No memory_context bound on purpose.
    result = await bt._list_properties_impl()
    assert result.startswith("Buildium is not connected")


# --- happy paths ---


@pytest.mark.asyncio
async def test_count_properties_returns_pluralized_count() -> None:
    client_mock, api_mock = _api_with_properties([_prop(1, "A"), _prop(2, "B"), _prop(3, "C")])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._count_properties_impl()

    assert result == "You have 3 properties in Buildium."


@pytest.mark.asyncio
async def test_count_properties_singular_when_one() -> None:
    client_mock, api_mock = _api_with_properties([_prop(1, "Solo")])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._count_properties_impl()

    assert result == "You have 1 property in Buildium."


@pytest.mark.asyncio
async def test_list_properties_formats_addresses_and_truncates() -> None:
    properties = [_prop(i, f"Prop {i}", line1=f"{i} Main St", city="Austin") for i in range(1, 31)]
    client_mock, api_mock = _api_with_properties(properties)
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_properties_impl()

    # Header carries the total (30), not the truncated count (25).
    assert "You have 30 properties in Buildium." in result
    # First property is shown with its formatted address. PR #75 Bug B
    # \u2014 Buildium internal IDs are no longer leaked into the formatted
    # output; the row carries the property name + address only.
    assert "- Prop 1 \u2014 1 Main St, Austin" in result
    assert "Prop 1 (#1)" not in result
    # Truncation note is present.
    assert "and 5 more not shown." in result


# --- work-order status filter (Buildium API spec compliance) ---


@pytest.mark.asyncio
async def test_list_open_work_orders_uses_correct_buildium_status_enum() -> None:
    """Buildium's GET /v1/workorders accepts ``statuses`` (plural array)
    with the case-sensitive enum {New, InProgress, Completed, Deferred,
    Closed}.

    A previous version of this tool sent ``status="Open"`` which Buildium
    silently rejected (or returned all rows for) — this regression test
    locks in the spec-correct behaviour: plural key, exact-case values.

    ⚠ ``Deferred`` IS REQUESTED NOW, and this is a deliberate semantic choice
    rather than a fix. The docstring here used to say "the web UI's Open
    filter is the union of New + InProgress" and send exactly those two, while
    the MIRROR counted anything not Completed/Closed/Cancelled — so the same
    question got different numbers from ``portfolio_overview`` than from
    ``buildium_open_maintenance_by_unit``, and only ``Deferred`` separated
    them.

    Unified on Deferred = OPEN, because deferred work is postponed, not
    finished, and because this codebase consistently errs toward a visible
    over-count rather than a clean-looking omission (gotchas 26/55/57). The
    cost is that Occupella's number can exceed the one Buildium's own UI
    shows, which a manager may read as Occupella being wrong.

    The other choice — match the UI exactly and treat Deferred as closed on
    both sides — is one line in ``buildium/maintenance_status.CLOSED_STATUSES``
    and is Brandon's to make. There are ZERO Deferred rows on the live mirror
    today (verified 2026-08-29: work orders 88 New / 1 InProgress / 4
    Completed; tasks 171 / 4 / 4), so the choice costs nothing right now and
    should be made on purpose rather than by whichever list someone read first.
    """
    api_mock = MagicMock()
    api_mock.work_orders = MagicMock()
    api_mock.work_orders.list = AsyncMock(return_value=[])

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        await bt._list_open_work_orders_impl()

    api_mock.work_orders.list.assert_awaited_once()
    kwargs = api_mock.work_orders.list.await_args.kwargs
    assert "status" not in kwargs, "must use plural 'statuses', not 'status'"
    # Sourced from the one vocabulary rather than re-typed, so this cannot
    # drift back into a second spelling (gotcha 28's both-sides rule).
    from helixis.buildium.maintenance_status import OPEN_STATUSES

    assert kwargs["statuses"] == list(OPEN_STATUSES)
    assert kwargs["statuses"] == ["New", "InProgress", "Deferred"]


# --- tool list factory ---


def test_buildium_tools_factory_returns_full_expert_surface() -> None:
    tools = bt.buildium_tools()
    names = {t.name for t in tools}
    expected = {
        # Counts
        "buildium_count_properties",
        "buildium_count_units",
        "buildium_count_tenants",
        # Properties
        "buildium_list_properties",
        "buildium_get_property",
        # Tenants
        "buildium_list_tenants",
        "buildium_get_tenant",
        "buildium_find_tenant_for_unit",
        # Leases
        "buildium_list_active_leases",
        "buildium_get_lease",
        "buildium_list_recent_lease_transactions",
        "buildium_list_upcoming_lease_expirations",  # PR #73
        "buildium_get_rent_roll",  # PR #73
        # Units
        "buildium_list_vacant_units",  # PR #73
        # Owners
        "buildium_list_owners",
        "buildium_get_owner",  # PR #73
        # Vendors
        "buildium_list_vendors",
        "buildium_get_vendor",
        # Read-back for writes the agent already makes — the
        # write-without-read census (test_read_coverage_census.py).
        "buildium_list_vendor_notes",
        "buildium_get_task_history",
        "buildium_list_lease_departures",
        "buildium_get_unit",
        "buildium_list_bill_payments",
        "buildium_list_vendor_categories",  # PR #77
        "buildium_search_vendors",  # PR #77
        # Work orders
        "buildium_list_open_work_orders",
        "buildium_get_work_order",
        "buildium_list_work_orders_for_unit",
        "buildium_maintenance_history_by_unit",  # #7 wo-history
        # Portfolio-wide open maintenance in ONE call — tasks AND work
        # orders, grouped by unit. The read that lets Q5-shaped questions
        # ("check each of these tenants for open maintenance") finish.
        "buildium_open_maintenance_by_unit",
        # Staff (read — surfaces user ids for assignment)
        "buildium_list_staff",
        # Tasks (read — surfaces task ids for status updates)
        "buildium_list_tasks",
        # Writes — confirm + idempotency + verify
        "buildium_create_to_do_task",  # PR #76 — first user-write
        "buildium_create_work_order",  # work-order create
        "buildium_update_task_status",  # status update
        "buildium_close_work_order",  # close a work order (via backing task)
        "buildium_update_work_order",  # reassign vendor / priority / due date
        "buildium_post_charge",  # post a charge to a lease ledger (financial)
        "buildium_record_move_out",  # file a tenant move-out (reversible)
        "buildium_undo_move_out",  # …and the reversal that claim depends on
        "buildium_record_payment",  # record a resident payment (immutable — role floor)
        "buildium_add_task_note",  # add note (task)
        "buildium_add_work_order_note",  # add note (work order)
        "buildium_upload_document",  # PR6 — upload a company document
        "buildium_share_file",  # PR6 — share/unshare with tenants/owners
        # buildium_create_bill intentionally absent — bills are read-only
        # in Helixis for now (2026-07-06 surface-model decision).
        "buildium_add_vendor_note",  # vendor note (real endpoint)
        # buildium_update_applicant_status intentionally absent — Helixis does not
        # accept/reject/defer housing applications at release (FHA/SafeRent, 2026-07-26).
        # Financial
        "buildium_list_overdue_balances",
        "buildium_list_unpaid_bills",  # PR #77 — AP, not rent
        # The drill-down behind a bill's total, and the cash position. Both
        # endpoints had existed for months with no agent tool, so "why is this
        # bill $2,400?" and "what's in the operating account?" had no route at
        # all. ⚠ This exact-set pin is what caught them being added, and it is
        # doing its job — re-point it, never relax it to a subset check
        # (gotchas 89/93/95): a registry that accepts anything extra is how an
        # unreviewed tool reaches the model.
        "buildium_get_bill",
        "buildium_list_bank_accounts",
        # Requests (read — Buildium task subtypes)
        "buildium_list_resident_requests",
        "buildium_list_rental_owner_requests",
        "buildium_list_contact_requests",
        # General Ledger (read-only)
        "buildium_list_gl_accounts",
        "buildium_get_gl_account_balances",
        "buildium_list_gl_transactions",
        # Communications (read)
        "buildium_list_announcements",
        "buildium_list_sent_emails",
        "buildium_list_phone_logs",
        # Listings (read)
        "buildium_list_listings",
        # ⚠ `buildium_list_appliances` was REMOVED on 2026-09-22 with the whole
        # appliance surface, four weeks ahead of Buildium's 2026-10-19
        # retirement (buildium/api.py names the decision). This is an
        # EXACT-SET pin, so re-adding the tool against the old routes fails
        # here first — which is the intended friction.
    }
    assert names == expected, f"missing: {expected - names}, extra: {names - expected}"
    assert len(tools) == len(expected)


# ─────────────────────────────────────────────────────────────────────
# Request tasks (read-only)
# ─────────────────────────────────────────────────────────────────────


def _request_task(
    title: str,
    *,
    status: str = "New",
    priority: str | None = None,
    unit: str | None = None,
    category: str | None = None,
) -> Any:
    t = MagicMock()
    t.title = title
    t.task_status = status
    t.priority = priority
    t.unit_number = unit
    t.due_date = None
    t.category = MagicMock(name=category) if category else None
    if category:
        t.category.name = category
    return t


def _requests_api(**attrs: Any) -> tuple[Any, Any]:
    api_mock = MagicMock()
    api_mock.requests = MagicMock()
    for key, value in attrs.items():
        setattr(api_mock.requests, key, AsyncMock(return_value=value))
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock


@pytest.mark.asyncio
async def test_list_resident_requests_formats_rows() -> None:
    client_mock, api_mock = _requests_api(
        list_resident=[
            _request_task("Leaky faucet", status="New", priority="High", unit="4B"),
        ]
    )
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_request_tasks_impl("resident")

    assert "Leaky faucet" in result
    assert "[New]" in result
    assert "Unit 4B" in result


@pytest.mark.asyncio
async def test_request_status_filter_passes_taskstatus() -> None:
    """A status arg maps to Buildium's ``taskstatus`` query param."""
    client_mock, api_mock = _requests_api(list_rental_owner=[])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        await bt._list_request_tasks_impl("rental_owner", "InProgress")

    kwargs = api_mock.requests.list_rental_owner.await_args.kwargs
    assert kwargs["taskstatus"] == "InProgress"


@pytest.mark.asyncio
async def test_contact_requests_empty_is_friendly() -> None:
    client_mock, api_mock = _requests_api(list_contact=[])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_request_tasks_impl("contact")

    assert "No contact requests" in result


# ─────────────────────────────────────────────────────────────────────
# General Ledger (read-only)
# ─────────────────────────────────────────────────────────────────────


def _gl_account(name: str, number: str, sub_type: str) -> Any:
    a = MagicMock()
    a.name = name
    a.account_number = number
    a.type = "Asset"
    a.sub_type = sub_type
    return a


def _gl_balance(name: str, number: str, balance: float) -> Any:
    ref = MagicMock()
    ref.name = name
    ref.account_number = number
    b = MagicMock()
    b.gl_account = ref
    b.total_balance = balance
    return b


def _gl_api(**attrs: Any) -> tuple[Any, Any]:
    api_mock = MagicMock()
    api_mock.general_ledger = MagicMock()
    for key, value in attrs.items():
        setattr(api_mock.general_ledger, key, AsyncMock(return_value=value))
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock


@pytest.mark.asyncio
async def test_list_gl_accounts_formats_chart_of_accounts() -> None:
    client_mock, api_mock = _gl_api(
        list_accounts=[
            _gl_account("Operating Cash", "1000", "Bank"),
            _gl_account("Rent Income", "4000", "Income"),
        ]
    )
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_gl_accounts_impl()

    assert "Operating Cash" in result
    assert "#1000" in result
    assert "Rent Income" in result


@pytest.mark.asyncio
async def test_gl_account_type_filter_passes_accounttypes_array() -> None:
    """A type filter maps to the case-correct ``accounttypes`` array param."""
    client_mock, api_mock = _gl_api(list_accounts=[])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        await bt._list_gl_accounts_impl("income")

    kwargs = api_mock.general_ledger.list_accounts.await_args.kwargs
    assert kwargs["accounttypes"] == ["Income"]


@pytest.mark.asyncio
async def test_gl_balances_default_as_of_today_and_sorts_by_magnitude() -> None:
    client_mock, api_mock = _gl_api(
        list_account_balances=[
            _gl_balance("Petty Cash", "1010", 25.0),
            _gl_balance("Operating Cash", "1000", -9000.0),
        ]
    )
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._gl_account_balances_impl()

    # asofdate was supplied (default = today), and the largest-magnitude
    # account is listed first.
    assert api_mock.general_ledger.list_account_balances.await_args.kwargs.get("asofdate")
    assert result.index("Operating Cash") < result.index("Petty Cash")


@pytest.mark.asyncio
async def test_gl_transactions_requires_a_date_range() -> None:
    """No API call when the range is missing — the tool asks for it."""
    client_mock, api_mock = _gl_api(list_transactions=[])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_gl_transactions_impl("", "")

    assert "date range" in result.lower()
    api_mock.general_ledger.list_transactions.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
# Helpers for the expanded tool surface
# ─────────────────────────────────────────────────────────────────────


def _tenant(
    id_: int,
    first: str,
    last: str,
    email: str | None = None,
    *,
    alternate_email: str | None = None,
    phone: str | None = None,
    leases: list[Any] | None = None,
) -> Any:
    t = MagicMock()
    t.id = id_
    t.first_name = first
    t.last_name = last
    t.email = email
    t.alternate_email = alternate_email
    if phone is not None:
        phone_obj = MagicMock()
        phone_obj.number = phone
        phone_obj.type = "Mobile"
        t.phone_numbers = [phone_obj]
    else:
        t.phone_numbers = []
    t.address = None
    t.leases = leases or []
    return t


def _current_tenant(id_: int, first: str, last: str, email: str | None = None) -> Any:
    ct = MagicMock()
    ct.id = id_
    ct.first_name = first
    ct.last_name = last
    ct.email = email
    return ct


def _unit(id_: int, unit_number: str, line1: str | None = None) -> Any:
    addr = MagicMock(address_line1=line1, city=None, state=None)
    u = MagicMock()
    u.id = id_
    u.unit_number = unit_number
    u.address = addr
    u.property_id = 100
    return u


def _lease(
    id_: int,
    *,
    unit_id: int | None = None,
    unit_number: str | None = None,
    rent: float | None = None,
    current_tenants: list[Any] | None = None,
    cosigners: list[Any] | None = None,
    status: str | None = "Active",
    is_eviction_pending: bool = False,
) -> Any:
    lease = MagicMock()
    lease.id = id_
    lease.unit_id = unit_id
    lease.unit_number = unit_number
    lease.property_id = 100
    lease.rent = rent
    lease.current_tenants = current_tenants or []
    lease.cosigners = cosigners or []
    lease.status = status
    lease.is_eviction_pending = is_eviction_pending
    lease.lease_from_date = None
    lease.lease_to_date = None
    lease.term_type = None
    return lease


def _vendor(id_: int, name: str, *, company: str | None = None, active: bool = True) -> Any:
    v = MagicMock()
    v.id = id_
    v.name = name
    v.company_name = company
    v.is_active = active
    return v


def _work_order(
    id_: int,
    title: str,
    *,
    status: str = "New",
    priority: str = "High",
    vendor_id: int | None = None,
    description: str | None = None,
) -> Any:
    w = MagicMock()
    w.id = id_
    w.title = title
    w.status = status
    w.priority = priority
    w.property_id = 100
    w.unit_id = 200
    w.vendor_id = vendor_id
    w.category_id = None
    w.description = description
    return w


def _balance(
    lease_id: int,
    *,
    amount: float,
    days_late: int = 0,
    unit_number: str | None = None,
) -> Any:
    b = MagicMock()
    b.lease_id = lease_id
    b.outstanding_balance = amount
    b.days_late = days_late
    b.unit_number = unit_number
    b.property_id = 100
    b.unit_id = None
    b.status = None
    b.rent_amount = None
    return b


def _api_with(**attrs: Any) -> tuple[Any, Any]:
    api_mock = MagicMock()
    api_mock.rentals = MagicMock()
    api_mock.leases = MagicMock()
    api_mock.work_orders = MagicMock()
    api_mock.vendors = MagicMock()
    for path, value in attrs.items():
        # path like "rentals.list_tenants" or "vendors.get"
        parts = path.split(".")
        target = api_mock
        for p in parts[:-1]:
            target = getattr(target, p)
        setattr(target, parts[-1], value)

    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock


def _patch_api(client_mock: Any, api_mock: Any) -> Any:
    """Return a context manager combining all the patches needed to run
    a tool against the mocked client/api pair."""
    from contextlib import ExitStack

    stack = ExitStack()

    class _Combined:
        def __enter__(self) -> Any:
            stack.enter_context(_ctx_company())
            stack.enter_context(
                patch.object(
                    bt,
                    "load_buildium_settings_for_company",
                    new=AsyncMock(return_value=object()),
                )
            )
            stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client_mock))
            stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api_mock))
            return None

        def __exit__(self, *exc: Any) -> None:
            stack.__exit__(*exc)

    return _Combined()


# ─────────────────────────────────────────────────────────────────────
# Tenant tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tenants_not_connected() -> None:
    with (
        _ctx_company("missing"),
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=None)),
    ):
        result = await bt._list_tenants_impl()
    assert result.startswith("Buildium is not connected")


@pytest.mark.asyncio
async def test_list_tenants_formats_and_truncates() -> None:
    tenants = [
        _tenant(i, f"First{i}", f"Last{i}", email=f"t{i}@example.com", phone=f"555-000-{i:04d}")
        for i in range(1, 31)
    ]
    client_mock, api_mock = _api_with(**{"rentals.list_tenants": AsyncMock(return_value=tenants)})

    with _patch_api(client_mock, api_mock):
        result = await bt._list_tenants_impl()

    assert "You have 30 tenants in Buildium." in result
    # PR #75 Bug B — names only; no internal tenant ids leaked.
    assert "First1 Last1" in result
    assert "First1 Last1 (#1)" not in result
    assert "t1@example.com" in result
    assert "555-000-0001" in result
    assert "and 5 more not shown." in result


@pytest.mark.asyncio
async def test_get_tenant_by_id_returns_full_profile() -> None:
    tenant = _tenant(
        42,
        "Sarah",
        "Chen",
        email="sarah@example.com",
        alternate_email="schen@personal.com",
        phone="555-1212",
    )
    client_mock, api_mock = _api_with(**{"rentals.get_tenant": AsyncMock(return_value=tenant)})

    with _patch_api(client_mock, api_mock):
        result = await bt._get_tenant_impl("42")

    # PR #75 Bug B — name only, no internal id.
    assert "Sarah Chen" in result
    assert "(#42)" not in result
    assert "sarah@example.com" in result
    assert "schen@personal.com" in result
    assert "555-1212" in result


@pytest.mark.asyncio
async def test_get_tenant_fuzzy_match_disambiguates_two_sarahs() -> None:
    sarah_chen = _tenant(1, "Sarah", "Chen", email="chen@example.com")
    sarah_chang = _tenant(2, "Sarah", "Chang", email="chang@example.com")
    other = _tenant(3, "Bob", "Smith")
    client_mock, api_mock = _api_with(
        **{"rentals.list_tenants": AsyncMock(return_value=[sarah_chen, sarah_chang, other])}
    )

    with _patch_api(client_mock, api_mock):
        result = await bt._get_tenant_impl("Sarah")

    assert "Multiple tenants match" in result
    assert "Sarah Chen" in result
    assert "Sarah Chang" in result
    assert "Bob Smith" not in result


@pytest.mark.asyncio
async def test_get_tenant_fuzzy_match_unique_returns_profile() -> None:
    tenants = [
        _tenant(1, "Sarah", "Chen", email="chen@example.com"),
        _tenant(2, "Bob", "Smith"),
    ]
    client_mock, api_mock = _api_with(**{"rentals.list_tenants": AsyncMock(return_value=tenants)})

    with _patch_api(client_mock, api_mock):
        result = await bt._get_tenant_impl("Chen")

    # PR #75 Bug B — name only.
    assert "Sarah Chen" in result
    assert "(#1)" not in result
    assert "chen@example.com" in result


@pytest.mark.asyncio
async def test_get_tenant_no_match() -> None:
    tenants = [_tenant(1, "Bob", "Smith")]
    client_mock, api_mock = _api_with(**{"rentals.list_tenants": AsyncMock(return_value=tenants)})
    with _patch_api(client_mock, api_mock):
        result = await bt._get_tenant_impl("Wrong")
    assert result.startswith("No tenant matches")


@pytest.mark.asyncio
async def test_find_tenant_for_unit_lists_current_tenants() -> None:
    unit = _unit(99, "4B", "123 Main St")
    tenants = [
        _current_tenant(12345, "Sarah", "Chen", "sarah@example.com"),
        _current_tenant(12346, "John", "Chen", "john@example.com"),
    ]
    lease = _lease(5, unit_id=99, unit_number="4B", current_tenants=tenants)

    client_mock, api_mock = _api_with(
        **{
            "rentals.get_unit": AsyncMock(return_value=unit),
            "rentals.list_units": AsyncMock(return_value=[unit]),
            "leases.list": AsyncMock(return_value=[lease]),
        }
    )

    with _patch_api(client_mock, api_mock):
        result = await bt._find_tenant_for_unit_impl("99")

    # PR #75 Bug B — names only; no tenant ids in user-facing text.
    assert "Sarah Chen" in result
    assert "John Chen" in result
    assert "(#12345)" not in result
    assert "(#12346)" not in result
    api_mock.leases.list.assert_awaited_once()
    kwargs = api_mock.leases.list.await_args.kwargs
    assert kwargs == {"unitids": [99], "leasestatuses": ["Active"]}


@pytest.mark.asyncio
async def test_find_tenant_for_unit_no_active_lease() -> None:
    unit = _unit(99, "4B", "123 Main St")
    client_mock, api_mock = _api_with(
        **{
            "rentals.get_unit": AsyncMock(return_value=unit),
            "rentals.list_units": AsyncMock(return_value=[unit]),
            "leases.list": AsyncMock(return_value=[]),
        }
    )
    with _patch_api(client_mock, api_mock):
        result = await bt._find_tenant_for_unit_impl("99")
    assert "no active lease" in result.lower()


@pytest.mark.asyncio
async def test_find_tenant_for_unit_unit_not_found() -> None:
    from helixis.buildium.errors import BuildiumApiError

    units = [_unit(99, "4B", "123 Main St")]
    client_mock, api_mock = _api_with(
        **{
            "rentals.get_unit": AsyncMock(side_effect=BuildiumApiError(status=404, message="x")),
            "rentals.list_units": AsyncMock(return_value=units),
        }
    )
    with _patch_api(client_mock, api_mock):
        result = await bt._find_tenant_for_unit_impl("nonexistent")
    assert "Couldn't find a unit" in result


# ─────────────────────────────────────────────────────────────────────
# Lease tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_leases_includes_tenant_names_and_truncates() -> None:
    leases = []
    for i in range(1, 31):
        tenants = [_current_tenant(1000 + i, f"First{i}", f"Last{i}")]
        leases.append(
            _lease(i, unit_id=200 + i, unit_number=str(i), rent=1850, current_tenants=tenants)
        )
    client_mock, api_mock = _api_with(**{"leases.list": AsyncMock(return_value=leases)})

    with _patch_api(client_mock, api_mock):
        result = await bt._list_active_leases_impl()

    assert "30 active leases" in result
    assert "First1 Last1" in result
    assert "$1,850.00/mo" in result
    assert "and 5 more not shown" in result


@pytest.mark.asyncio
async def test_get_lease_renders_full_detail() -> None:
    lease = _lease(
        5678,
        unit_id=99,
        unit_number="4B",
        rent=1850,
        current_tenants=[_current_tenant(1, "Sarah", "Chen", "sarah@example.com")],
        cosigners=[_current_tenant(2, "Mom", "Chen")],
        is_eviction_pending=True,
    )
    # PR #75 — _get_lease_impl now resolves property_id to a name so
    # the user-facing line reads "Lease at 100 Main Ave, unit 4B" not
    # "Property: #100". The mock just needs an awaitable.
    prop_mock = MagicMock()
    prop_mock.name = "100 Main Ave"
    prop_mock.address = MagicMock(address_line1="100 Main Ave", city=None, state=None)
    client_mock, api_mock = _api_with(
        **{
            "leases.get": AsyncMock(return_value=lease),
            "rentals.get_property": AsyncMock(return_value=prop_mock),
        }
    )

    with _patch_api(client_mock, api_mock):
        result = await bt._get_lease_impl(5678)

    # PR #75 Bug B — the human-facing header no longer shouts the
    # internal lease ID. The lease is identified by property name +
    # unit instead; ``[ref:5678]`` is the internal chaining handle
    # the agent uses to pass into a follow-up tool call.
    assert "Lease at 100 Main Ave, unit 4B" in result
    assert "Lease #5678" not in result
    assert "[ref:5678]" in result
    assert "Sarah Chen" in result
    assert "(#1)" not in result
    assert "sarah@example.com" in result
    assert "Mom Chen" in result
    assert "Eviction pending" in result
    assert "$1,850.00/mo" in result


# ─────────────────────────────────────────────────────────────────────
# Owner / vendor tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_owners_handles_property_count_and_truncates() -> None:
    owners = []
    for i in range(1, 31):
        o = MagicMock()
        o.id = i
        o.first_name = f"Owner{i}"
        o.last_name = "X"
        o.company_name = None
        o.email = f"o{i}@example.com"
        o.property_ids = list(range(i))
        owners.append(o)
    client_mock, api_mock = _api_with(**{"rentals.list_owners": AsyncMock(return_value=owners)})

    with _patch_api(client_mock, api_mock):
        result = await bt._list_owners_impl()

    assert "30 rental owners" in result
    # PR #75 Bug B — names only.
    assert "Owner1 X" in result
    assert "(#1)" not in result
    assert "5 more not shown" in result


@pytest.mark.asyncio
async def test_list_vendors_filters_inactive_and_truncates() -> None:
    vendors = [_vendor(i, f"V{i}", company=f"Co{i}") for i in range(1, 31)]
    vendors.append(_vendor(99, "Inactive", company="Z", active=False))
    client_mock, api_mock = _api_with(**{"vendors.list": AsyncMock(return_value=vendors)})

    with _patch_api(client_mock, api_mock):
        result = await bt._list_vendors_impl()

    assert "30 active vendors" in result
    assert "Inactive" not in result
    assert "5 more not shown" in result


@pytest.mark.asyncio
async def test_get_vendor_by_id() -> None:
    vendor = _vendor(7, "Acme Plumbing", company="Acme Plumbing LLC")
    client_mock, api_mock = _api_with(**{"vendors.get": AsyncMock(return_value=vendor)})
    with _patch_api(client_mock, api_mock):
        result = await bt._get_vendor_impl("7")
    # PR #75 Bug B — vendor name only.
    assert "Acme Plumbing LLC" in result
    assert "(#7)" not in result


@pytest.mark.asyncio
async def test_get_vendor_disambiguates() -> None:
    vendors = [
        _vendor(1, "Acme Plumbing"),
        _vendor(2, "Acme Electric"),
    ]
    client_mock, api_mock = _api_with(**{"vendors.list": AsyncMock(return_value=vendors)})
    with _patch_api(client_mock, api_mock):
        result = await bt._get_vendor_impl("Acme")
    assert "Multiple vendors match" in result


# ─────────────────────────────────────────────────────────────────────
# Work-order tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_work_order_resolves_vendor_label() -> None:
    wo = _work_order(123, "HVAC failing", vendor_id=7, description="No cool")
    vendor = _vendor(7, "Acme HVAC")
    # The work order fixture has property_id=100, unit_id=200 — the new
    # output resolves those via rentals.get_property / rentals.get_unit
    # so the chat-bound formatting can show property name + Unit number
    # instead of the numeric ids.
    prop = MagicMock(name="prop")
    prop.id = 100
    prop.name = "100 Main Ave"
    prop.address = MagicMock(address_line1="100 Main Ave", city=None, state=None)
    unit = MagicMock(name="unit")
    unit.id = 200
    unit.unit_number = "3"
    unit.address = None
    client_mock, api_mock = _api_with(
        **{
            "work_orders.get": AsyncMock(return_value=wo),
            "vendors.get": AsyncMock(return_value=vendor),
            "rentals.get_property": AsyncMock(return_value=prop),
            "rentals.get_unit": AsyncMock(return_value=unit),
        }
    )
    with _patch_api(client_mock, api_mock):
        result = await bt._get_work_order_impl(123)
    # PR #75 Bug B — title leads, internal id only as a [ref:X] handle.
    assert "HVAC failing" in result
    assert "Work order #123" not in result
    assert "[ref:123]" in result
    assert "Acme HVAC" in result
    assert "(#7)" not in result
    # Property and unit references resolved to human-readable labels.
    assert "100 Main Ave" in result
    assert "Unit 3" in result
    assert "Property: #100" not in result
    assert "Unit: #200" not in result
    assert "No cool" in result


@pytest.mark.asyncio
async def test_list_work_orders_for_unit_truncates() -> None:
    # ⚠ The unit asked about and the unit the work orders belong to must AGREE.
    # This fixture used to ask for unit 99 and hand back 30 work orders stamped
    # unit_id=200 — an incoherence that was invisible for as long as the tool
    # trusted Buildium's ``?unitids`` filter and never checked. The moment
    # _only_for_unit landed, the tool correctly reported "no work orders on
    # record" and this test failed. The mock was wrong, not the guard.
    unit = _unit(99, "4B", "123 Main St")
    wos = [_work_order(i, f"Issue {i}") for i in range(1, 31)]
    for w in wos:
        w.unit_id = 99
    client_mock, api_mock = _api_with(
        **{
            "rentals.get_unit": AsyncMock(return_value=unit),
            "rentals.list_units": AsyncMock(return_value=[unit]),
            "work_orders.list": AsyncMock(return_value=wos),
        }
    )
    with _patch_api(client_mock, api_mock):
        result = await bt._list_work_orders_for_unit_impl("99")
    assert "30 work order(s)" in result
    assert "5 more not shown" in result


# ─────────────────────────────────────────────────────────────────────
# Financial tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_overdue_balances_hydrates_tenant_names() -> None:
    balances = [
        _balance(10, amount=1850.0, days_late=15, unit_number="4B"),
        _balance(11, amount=900.0, days_late=5, unit_number="2A"),
        _balance(12, amount=0.0),  # filtered
    ]
    lease_10 = _lease(
        10,
        unit_number="4B",
        current_tenants=[_current_tenant(1, "Sarah", "Chen")],
    )
    lease_11 = _lease(
        11,
        unit_number="2A",
        current_tenants=[_current_tenant(2, "John", "Doe")],
    )

    async def fake_get(lid: int) -> Any:
        return {10: lease_10, 11: lease_11}[lid]

    client_mock, api_mock = _api_with(
        **{
            "leases.list_outstanding_balances": AsyncMock(return_value=balances),
            "leases.get": AsyncMock(side_effect=fake_get),
        }
    )

    with _patch_api(client_mock, api_mock):
        result = await bt._list_overdue_balances_impl()

    assert "2 leases with an outstanding balance" in result
    assert "Sarah Chen" in result
    assert "John Doe" in result
    assert "$1,850.00 owed" in result
    # Sorted highest-first.
    assert result.index("Sarah Chen") < result.index("John Doe")


@pytest.mark.asyncio
async def test_list_overdue_balances_when_buildium_returns_empty() -> None:
    """PR #75 Bug A residual — pre-PR-#75 we replied "everyone's paid up"
    even when Brandon's sandbox had 17 active delinquencies and 2
    pending evictions in the UI. The Buildium endpoint just returned
    empty (wrong shape, not "all current"). The honest answer is "I
    couldn't reconcile the data" — never lie that everyone's paid up."""
    client_mock, api_mock = _api_with(
        **{"leases.list_outstanding_balances": AsyncMock(return_value=[])}
    )
    with _patch_api(client_mock, api_mock):
        result = await bt._list_overdue_balances_impl()
    # The lie message is gone.
    assert "everyone's paid up" not in result.lower()
    # The honest message acknowledges uncertainty.
    assert "got no rows back" in result.lower() or "couldn't reconcile" in result.lower()


@pytest.mark.asyncio
async def test_list_recent_lease_transactions_returns_top_n() -> None:
    txns = []
    from datetime import datetime

    for i in range(1, 21):
        t = MagicMock()
        t.id = i
        t.date = datetime(2026, 1, i)
        t.transaction_type = "Charge" if i % 2 else "Payment"
        t.total_amount = 100.0 + i
        txns.append(t)
    client_mock, api_mock = _api_with(**{"leases.list_transactions": AsyncMock(return_value=txns)})
    with _patch_api(client_mock, api_mock):
        result = await bt._list_recent_lease_transactions_impl(5678, limit=5)
    # Most recent five: 2026-01-20 down to 2026-01-16. PR #75 Bug B —
    # the header no longer prints the lease ID; the agent already
    # knows which lease this is from the prior turn.
    assert "Recent transactions" in result
    assert "lease #5678" not in result.lower()
    assert "2026-01-20" in result
    assert "2026-01-16" in result
    assert "2026-01-15" not in result


# ─────────────────────────────────────────────────────────────────────
# Property tools
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_property_by_id_includes_unit_and_lease_counts() -> None:
    prop = MagicMock()
    prop.id = 100
    prop.name = "123 Main"
    prop.address = MagicMock(address_line1="123 Main St", city="Austin", state="TX")
    prop.rental_owner_ids = [50]

    owner = MagicMock()
    owner.id = 50
    owner.first_name = "Alice"
    owner.last_name = "Owner"
    owner.company_name = None

    client_mock, api_mock = _api_with(
        **{
            "rentals.get_property": AsyncMock(return_value=prop),
            "rentals.list_units": AsyncMock(
                return_value=[_unit(1, "1A"), _unit(2, "1B"), _unit(3, "1C")]
            ),
            "rentals.get_owner": AsyncMock(return_value=owner),
            "leases.list": AsyncMock(return_value=[_lease(5), _lease(6)]),
        }
    )

    with _patch_api(client_mock, api_mock):
        result = await bt._get_property_impl("100")

    # PR #75 Bug B — property name only.
    assert "123 Main" in result
    assert "(#100)" not in result
    assert "Units: 3" in result
    assert "Active leases: 2" in result
    assert "Alice Owner" in result


# ─────────────────────────────────────────────────────────────────────
# Communications (read-only)
# ─────────────────────────────────────────────────────────────────────


def _comms_api(**attrs: Any) -> tuple[Any, Any]:
    api_mock = MagicMock()
    api_mock.communications = MagicMock()
    for key, value in attrs.items():
        setattr(api_mock.communications, key, AsyncMock(return_value=value))
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock


@pytest.mark.asyncio
async def test_list_announcements_formats_rows() -> None:
    ann = MagicMock()
    ann.subject = "Pool closed for repairs"
    ann.announcement_date = None
    ann.channels = ["Email", "TenantPortal"]
    client_mock, api_mock = _comms_api(list_announcements=[ann])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_announcements_impl()
    assert "Pool closed for repairs" in result
    assert "Email" in result


@pytest.mark.asyncio
async def test_list_sent_emails_renders_sender_name() -> None:
    email = MagicMock()
    email.subject = "Rent reminder"
    email.sent_date_time = None
    sender = MagicMock()
    sender.name = None
    sender.first_name = "Pat"
    sender.last_name = "Manager"
    email.sender = sender
    client_mock, api_mock = _comms_api(list_emails=[email])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_sent_emails_impl()
    assert "Rent reminder" in result
    assert "Pat Manager" in result


@pytest.mark.asyncio
async def test_phone_logs_empty_is_friendly() -> None:
    client_mock, api_mock = _comms_api(list_phone_logs=[])
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_phone_logs_impl()
    assert "No phone logs" in result


# ─────────────────────────────────────────────────────────────────────
# Listings (read-only)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_listings_formats_rows() -> None:
    listing = MagicMock()
    listing.unit = MagicMock(unit_number="4B")
    listing.property = MagicMock(name="Grove Street")
    listing.property.name = "Grove Street"
    listing.rent = 1850.0
    listing.available_date = None
    api_mock = MagicMock()
    api_mock.listings = MagicMock()
    api_mock.listings.list = AsyncMock(return_value=[listing])
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_listings_impl()
    assert "Grove Street" in result
    assert "Unit 4B" in result
    assert "$1,850.00/mo" in result


@pytest.mark.asyncio
async def test_list_listings_empty_is_friendly() -> None:
    api_mock = MagicMock()
    api_mock.listings = MagicMock()
    api_mock.listings.list = AsyncMock(return_value=[])
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client_mock),
        patch.object(bt, "BuildiumApi", return_value=api_mock),
    ):
        result = await bt._list_listings_impl()
    assert "No active listings" in result


# ── Portfolio-wide maintenance: the join must be possible in ONE call ──
# Langfuse trace f275d529 (2026-08-20): a collections run asked "who's
# behind AND has open maintenance", and the agent made 78 per-unit
# work-order calls — 73% of a run that then died on the 240s wall-clock
# deadline. Root cause was tool design, not agent judgment: the bulk list
# printed title/priority/due only, so its rows could not be joined against
# tenants, and it capped at 25 rows against 60 open work orders.


class _WorkOrderRow:
    def __init__(self, wo_id: int, property_id: int | None, unit_id: int | None) -> None:
        self.id = wo_id
        self.property_id = property_id
        self.unit_id = unit_id
        self.title = f"Ticket {wo_id}"
        self.priority = "Normal"
        self.due_date = None


def test_open_work_order_rows_carry_their_property_and_unit() -> None:
    """Without location ids the list is un-joinable — the defect that
    forced the per-unit fan-out."""
    from datetime import date

    out = bt._format_open_work_orders([_WorkOrderRow(1, 37948, 143060)], date(2026, 8, 20))
    assert "property 37948" in out
    assert "unit 143060" in out


def test_open_work_orders_are_not_truncated_at_a_portfolio_scale() -> None:
    """A cross-check needs the WHOLE open list; a 25-row cap silently hid
    most of a 60-ticket book and made the bulk path useless."""
    from datetime import date

    rows = [_WorkOrderRow(i, 1000 + i, 2000 + i) for i in range(60)]
    out = bt._format_open_work_orders(rows, date(2026, 8, 20))
    assert "60 open work orders" in out
    assert "not shown" not in out
    # Every row's unit is present, so a local join can resolve all of them.
    for i in range(60):
        assert f"unit {2000 + i}" in out


def test_a_work_order_without_a_location_still_renders() -> None:
    """Buildium returns unassigned work orders with no PropertyId — the
    row must degrade, never crash the whole list."""
    from datetime import date

    out = bt._format_open_work_orders([_WorkOrderRow(9, None, None)], date(2026, 8, 20))
    assert "Ticket 9" in out


def test_the_per_unit_tool_warns_against_portfolio_looping() -> None:
    """The docstring IS the agent's routing signal — the anti-fan-out
    guidance living there is what steers the next collections run.

    Asserted on ``.description`` (what the model actually receives), NOT the
    raw ``__doc__``: the Agents SDK's docstring parser DROPS everything after
    a colon-terminated line followed by an indented block, which silently
    truncated this tool's guidance to its first line. Keep both docstrings
    flat prose, and let this test catch it if that ever regresses.
    """
    doc = bt.buildium_list_work_orders_for_unit.description or ""
    assert "buildium_list_open_work_orders" in doc
    assert "loop" in doc.lower()
    bulk_doc = bt.buildium_list_open_work_orders.description or ""
    assert "join" in bulk_doc.lower()
    assert "do not loop" in bulk_doc.lower()
    # The truncation bug's signature: a one-line description.
    assert len(bulk_doc) > 200


# ── Bill money lives in the LINES, not on the bill ────────────────────
# Live battery, 2026-08-21: the assistant reported "$0 total vendor
# exposure" across 67 unpaid bills when the true figure was $26,000.
# Production Buildium sends NO top-level TotalAmount — the money is in
# Lines[].Amount — so every bill counted as "unpriced". Same class as
# gotcha 24. Note what the agent did right: it refused to invent a
# number, which is why this surfaced as an honest $0 rather than a
# confident wrong total.


class _BillLine:
    def __init__(self, amount: float | None) -> None:
        self.amount = amount


class _Bill:
    def __init__(self, total: float | None = None, lines: list | None = None) -> None:
        self.total_amount = total
        self.lines = lines


def test_bill_amount_falls_back_to_the_line_items() -> None:
    """The production shape: no TotalAmount, money in Lines."""
    assert bt.bill_amount(_Bill(lines=[_BillLine(300.0), _BillLine(150.0)])) == 450.0


def test_bill_amount_prefers_total_amount_when_buildium_sends_it() -> None:
    """Sandbox/older payloads do carry it — that stays authoritative."""
    assert bt.bill_amount(_Bill(total=450.0, lines=[_BillLine(1.0)])) == 450.0


def test_a_bill_with_no_usable_amount_stays_unpriced() -> None:
    """None, not 0.0 — the caller reports these as excluded rather than
    silently understating the manager's exposure."""
    assert bt.bill_amount(_Bill()) is None
    assert bt.bill_amount(_Bill(lines=[])) is None
    assert bt.bill_amount(_Bill(lines=[_BillLine(None)])) is None


def test_multi_line_bills_sum_every_line() -> None:
    bill = _Bill(lines=[_BillLine(100.0), _BillLine(25.5), _BillLine(None), _BillLine(4.5)])
    assert bt.bill_amount(bill) == 130.0
