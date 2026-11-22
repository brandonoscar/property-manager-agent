"""Unit tests for the mirror-backed agent tools (agents/mirror_tools.py).

Pins the hybrid-policy contract on the mirror side: company scoping, the
empty-mirror honesty message (never a faked answer), aggregate formatting,
the everyone-current vs never-synced distinction, and property matching.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.db.repositories.cockpit import PropertyDetail, PropertySummary


@pytest.fixture(autouse=True)
def _balances_stage_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests describe a company whose balances stage has synced.

    Since 2026-10-01 the tools read that stage's stamp before treating an empty
    balance as "owes nothing" (an empty table proves nothing otherwise). A test
    about the never-landed case patches it back to ``None``.
    """
    stamped = mirror_tools.sync_state.StageSync(synced_at=datetime.now(UTC), row_count=1)
    monkeypatch.setattr(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=stamped))


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(
    fetch_rows: list | None = None,
    *,
    fetch_sequence: list[list] | None = None,
    fetchval: object = None,
) -> MagicMock:
    """``fetch_rows`` answers EVERY fetch identically; ``fetch_sequence`` gives
    one result per call, in order.

    ⚠ The sequence form exists because a tool that runs two different queries
    cannot be exercised by a helper that answers both the same way — the second
    query silently receives the first query's columns, and the failure surfaces
    as a KeyError on a column name rather than as "this fixture is wrong".
    """
    conn = MagicMock()
    if fetch_sequence is not None:
        conn.fetch = AsyncMock(side_effect=list(fetch_sequence))
    else:
        conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.fetchrow = AsyncMock(return_value={"latest": None})
    conn.fetchval = AsyncMock(return_value=fetchval)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    return pool


def _summary(**over) -> PropertySummary:
    base = dict(
        buildium_id=700,
        name="Garden Row",
        address_line1="1 Garden Way",
        city="Boston",
        state="MA",
        unit_count=12,
        occupied_count=11,
        open_work_orders=2,
        expiring_leases_90d=1,
        delinquent_leases=3,
        delinquent_total=2400.0,
    )
    base.update(over)
    return PropertySummary(**base)


def _ctx(company_id: str = "co-1") -> MagicMock:
    ctx = MagicMock()
    ctx.company_id = company_id
    return ctx


@pytest.mark.asyncio
async def test_no_company_context() -> None:
    with patch.object(mirror_tools, "current_memory_context", return_value=None):
        out = await mirror_tools._portfolio_overview_impl()
    assert "couldn't determine which company" in out


@pytest.mark.asyncio
async def test_portfolio_overview_empty_mirror_is_honest() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._portfolio_overview_impl()
    assert "mirror is empty" in out
    assert "Sync data" in out


@pytest.mark.asyncio
async def test_portfolio_overview_formats_rollup_and_totals() -> None:
    summaries = [
        _summary(),
        _summary(
            buildium_id=701,
            name="74 Grove",
            unit_count=1,
            occupied_count=1,
            open_work_orders=0,
            expiring_leases_90d=0,
            delinquent_leases=0,
            delinquent_total=0.0,
        ),
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=summaries)
        ),
    ):
        out = await mirror_tools._portfolio_overview_impl()
    assert "Garden Row: 11/12 occupied, 1 vacant" in out
    assert "$2,400 delinquent" in out
    assert "Totals: 2 properties, 12/13 units occupied" in out
    # Freshness footer always present. ⚠ Read OFF the real helper rather than
    # retyped: this used to assert "synced Buildium data", which named a vendor
    # the mirror no longer has only one of, and a hand-typed copy of a footer is
    # a second answer to what the footer says (gotcha 28).
    assert mirror_tools._as_of_line(None).rstrip(")") in out


@pytest.mark.asyncio
async def test_delinquency_distinguishes_current_from_unsynced() -> None:
    # Mirror populated, balances stage landed, no balance rows → everyone is current.
    stamped = mirror_tools.sync_state.StageSync(synced_at=datetime.now(UTC), row_count=0)
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
        patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=stamped)),
    ):
        out = await mirror_tools._delinquency_report_impl()
    assert "every lease is current" in out.lower()

    # Mirror populated but the balances stage never landed → NOT "current"
    # (2026-10-01: properties can sync while balances fail).
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
        patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=None)),
    ):
        out = await mirror_tools._delinquency_report_impl()
    assert "haven't synced" in out and "every lease is current" not in out.lower()

    # Mirror empty → honest not-synced message, NOT "everyone paid".
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._delinquency_report_impl()
    assert "mirror is empty" in out


@pytest.mark.asyncio
async def test_delinquency_lists_balances_with_aging() -> None:
    rows = [
        {
            "lease_id": 101,
            "total_balance": 850.0,
            "balance_0_30": 500.0,
            "balance_31_60": 350.0,
            "balance_61_90": None,
            "balance_over_90": None,
            "snapshot_at": None,
            "property_name": "Garden Row",
            "unit_number": "3C",
            "tenant_names": "Priya Natarajan",
            "open_work_orders": 2,
            "open_tasks": 1,
        }
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._delinquency_report_impl()
    # Named from the LOCAL roster join — never "unidentified tenant".
    assert "Priya Natarajan (Garden Row · Unit 3C): $850" in out
    assert "$350 31-60d" in out
    assert "Total outstanding: $850 across 1 leases." in out


@pytest.mark.asyncio
async def test_delinquency_without_roster_falls_back_to_unit_label() -> None:
    """A lease with no mirrored tenants still gets a concrete label
    (property · unit), never an 'unidentified tenant' placeholder."""
    rows = [
        {
            "lease_id": 102,
            "total_balance": 170.0,
            "balance_0_30": None,
            "balance_31_60": None,
            "balance_61_90": 170.0,
            "balance_over_90": None,
            "snapshot_at": None,
            "property_name": "3 Industrial Road",
            "unit_number": "A",
            "tenant_names": None,
            "open_work_orders": 0,
            "open_tasks": 0,
        }
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._delinquency_report_impl()
    assert "3 Industrial Road · Unit A: $170" in out
    assert "unidentified" not in out.lower()


@pytest.mark.asyncio
async def test_property_snapshot_matches_by_name_fragment() -> None:
    detail = PropertyDetail(summary=_summary())
    detail.rent_scheduled = 14400.0
    detail.rent_collected_mtd = 9600.0
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary(), _summary(buildium_id=701, name="74 Grove")]),
        ),
        patch.object(
            mirror_tools.cockpit, "get_property_detail", AsyncMock(return_value=detail)
        ) as get_detail,
        patch.object(mirror_tools.cockpit, "list_units_for_property", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._property_snapshot_impl("garden")
    get_detail.assert_awaited_once()
    assert get_detail.await_args.args[2] == 700
    assert "Occupancy: 11/12 (1 vacant)" in out
    assert "$14,400/mo scheduled" in out


@pytest.mark.asyncio
async def test_property_snapshot_lists_units_from_the_mirror() -> None:
    """The Riverside Townhomes regression: "what units are at X?" must be a
    mirror answer — the snapshot carries the unit roster (number, occupancy,
    market rent) so the agent never needs live property/unit list calls."""
    detail = PropertyDetail(summary=_summary())
    units = [
        {"buildium_id": 1, "unit_number": "101", "market_rent": 1200.0, "is_occupied": True},
        {"buildium_id": 2, "unit_number": "102", "market_rent": 1150.0, "is_occupied": False},
        # A Rentvine unit read from the units export: occupancy UNKNOWN.
        {"buildium_id": 3, "unit_number": "103", "market_rent": 1100.0, "is_occupied": None},
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
        patch.object(mirror_tools.cockpit, "get_property_detail", AsyncMock(return_value=detail)),
        patch.object(
            mirror_tools.cockpit, "list_units_for_property", AsyncMock(return_value=units)
        ) as list_units,
    ):
        out = await mirror_tools._property_snapshot_impl("garden")
    list_units.assert_awaited_once()
    assert "Units (3):" in out
    assert "101 (occupied, market $1,200)" in out
    assert "102 (vacant, market $1,150)" in out
    assert "103 (occupancy unknown, market $1,100)" in out


@pytest.mark.asyncio
async def test_property_snapshot_ambiguous_asks() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(
                return_value=[
                    _summary(name="Main Ave (duplex)"),
                    _summary(buildium_id=701, name="Main Ave (fourplex)"),
                ]
            ),
        ),
    ):
        out = await mirror_tools._property_snapshot_impl("main ave")
    assert "Multiple properties match" in out
    assert "Which one?" in out


@pytest.mark.asyncio
async def test_property_snapshot_no_match_lists_options() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._property_snapshot_impl("nonexistent")
    assert "No property matches" in out
    assert "Garden Row" in out


@pytest.mark.asyncio
async def test_expiring_leases_clamps_days_and_reports_empty() -> None:
    pool = _pool(fetch_rows=[])
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._expiring_leases_impl(9999)  # clamped to 365
    assert "next 365 days" in out


# ── Money + entity awareness tools (0041/0042 mirror) ─────────────────


@pytest.mark.asyncio
async def test_unpaid_bills_lists_vendor_amount_and_flags_overdue() -> None:
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    rows = [
        {
            "buildium_id": 5100,
            "due_date": today - timedelta(days=3),
            "total_amount": 480.0,
            "reference_number": "INV-88",
            "vendor_name": "Apex Plumbing",
            # The tool now selects the line memo — a fixture without it
            # exercises a shape the tool no longer produces (gotcha 41).
            "memo": "Water heater replacement",
            "properties": "Garden Row",
        },
        {
            "buildium_id": 5101,
            "due_date": today + timedelta(days=10),
            "total_amount": 120.0,
            "reference_number": None,
            "vendor_name": None,
            "memo": None,
            "properties": None,
        },
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._unpaid_bills_impl()
    assert "Apex Plumbing: $480" in out and "at Garden Row" in out and "OVERDUE" in out
    assert "Bill #5101: $120" in out  # vendorless bill keeps a concrete label
    assert "Total unpaid: $600 across 2 bills." in out


@pytest.mark.asyncio
async def test_unpaid_bills_distinguishes_all_paid_from_unsynced() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._unpaid_bills_impl()
    assert "everything is paid" in out.lower()

    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._unpaid_bills_impl()
    assert "mirror is empty" in out


@pytest.mark.asyncio
async def test_spending_report_groups_by_gl_and_totals() -> None:
    rows = [
        {"gl_name": "Repairs & Maintenance", "gl_type": "Expense", "total": 300.0, "bill_count": 2},
        {"gl_name": "Uncategorized", "gl_type": None, "total": 50.0, "bill_count": 1},
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(
            mirror_tools,
            "get_pool",
            # The tool now runs TWO queries: by GL, then by property.
            return_value=_pool(fetch_sequence=[rows, []]),
        ),
    ):
        out = await mirror_tools._spending_report_impl(90, "")
    assert "Repairs & Maintenance (Expense): $300 across 2 bill(s)" in out
    assert "Total: $350." in out


@pytest.mark.asyncio
async def test_spending_report_scopes_to_property_query() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._spending_report_impl(30, "Garden")
    assert "last 30 days at properties matching 'Garden'" in out


@pytest.mark.asyncio
async def test_open_tasks_flags_overdue_and_names_assignee() -> None:
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    rows = [
        {
            "buildium_id": 21856,
            "title": "Fix the door",
            "task_type": "Todo",
            "status": "New",
            "priority": "High",
            "due_date": today - timedelta(days=1),
            "category_name": "Maintenance",
            "property_name": "Garden Row",
            "assignee": "Sam Staff",
        },
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._open_tasks_impl()
    assert "Fix the door" in out and "assigned to Sam Staff" in out and "OVERDUE" in out
    assert "1 open tasks in total." in out


@pytest.mark.asyncio
async def test_contact_lookup_searches_tenants_owners_vendors() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "first_name": "Dana",
                    "last_name": "Tenant",
                    "email": "dana@t.example",
                    "phone": "+15550001111",
                    "properties": "Garden Row",
                    # A drifted mock is fiction (gotcha 41). The tool now
                    # selects tenancy status; a fixture that omits it would
                    # exercise a shape the tool no longer produces.
                    "lease_status": "Active",
                    "ended_on": None,
                    "link_status": "Current",
                }
            ],
            [
                {
                    "first_name": "Dana",
                    "last_name": "Owner",
                    "company_name": None,
                    "email": "dana@o.example",
                    "phone": None,
                }
            ],
            [],
        ]
    )
    conn.fetchrow = AsyncMock(return_value={"latest": None})
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
    ):
        out = await mirror_tools._contact_lookup_impl("Dana")
    assert "Dana Tenant (tenant at Garden Row)" in out
    assert "Dana Owner (owner)" in out


@pytest.mark.asyncio
async def test_contact_lookup_no_match_vs_unsynced() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._contact_lookup_impl("Zed")
    assert "No tenant, owner, or vendor matches 'Zed'" in out

    # Unsynced AND live fallback unavailable (no Buildium creds) → the
    # honest "mirror is empty" message, never a faked answer.
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_live_contact_fallback", AsyncMock(return_value=None)),
    ):
        out = await mirror_tools._contact_lookup_impl("Zed")
    assert "mirror is empty" in out


@pytest.mark.asyncio
async def test_contact_lookup_unsynced_mirror_searches_live_in_one_call() -> None:
    """The 15-step regression: an empty mirror must NOT punt back to the
    agent (which fans out to list_tenants + list_owners + list_vendors as
    three more steps) — contact_lookup live-searches all three families
    itself and labels the result as live."""
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
        patch.object(
            mirror_tools,
            "_live_contact_fallback",
            AsyncMock(
                return_value=(
                    "People matching 'Reliable' (live Buildium — mirror not "
                    "synced yet):\n- Reliable Plumbing LLC (vendor)"
                )
            ),
        ) as live,
    ):
        out = await mirror_tools._contact_lookup_impl("Reliable")
    live.assert_awaited_once_with("Reliable")
    assert "live Buildium" in out
    assert "Reliable Plumbing LLC (vendor)" in out


@pytest.mark.asyncio
async def test_live_contact_fallback_filters_all_three_families() -> None:
    """The fallback itself: three concurrent fetches, name filter across
    tenants/owners/vendors, and an authoritative no-match message that
    tells the model no further lookups are needed."""

    def _t(first: str, last: str, email: str) -> MagicMock:
        m = MagicMock()
        m.first_name, m.last_name, m.email = first, last, email
        return m

    tenant = _t("Brandon", "Oscar", "b@x.example")
    vendor = MagicMock()
    vendor.email = None

    api = MagicMock()
    api.rentals.list_tenants = AsyncMock(return_value=[tenant])
    api.rentals.list_owners = AsyncMock(return_value=[])
    api.vendors.list = AsyncMock(return_value=[vendor])

    class _ClientCM:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

    import helixis.agents.buildium_tools as bt

    with (
        patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))),
        patch.object(bt, "_tenant_full_name", lambda t: f"{t.first_name} {t.last_name}"),
        patch.object(bt, "_first_phone", lambda _t: None),
        patch.object(bt, "_vendor_label", lambda _v: "Reliable Plumbing LLC"),
        patch.object(bt, "_vendor_category_name", lambda _v: "Plumbing"),
    ):
        hit = await mirror_tools._live_contact_fallback("brandon")
        miss = await mirror_tools._live_contact_fallback("nobody-here")

    assert hit is not None and "Brandon Oscar (tenant)" in hit and "live Buildium" in hit
    assert "Reliable Plumbing" not in hit  # vendor doesn't match 'brandon'
    assert miss is not None and "no further lookups needed" in miss.lower()


@pytest.mark.asyncio
async def test_bank_balances_lists_accounts_and_totals_active_only() -> None:
    rows = [
        {
            "buildium_id": 31,
            "name": "Operating",
            "description": None,
            "bank_account_type": "Checking",
            "is_active": True,
            "balance": 15230.55,
        },
        {
            "buildium_id": 32,
            "name": "Old Reserve",
            "description": None,
            "bank_account_type": "Savings",
            "is_active": False,
            "balance": 500.0,
        },
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._bank_balances_impl()
    assert "Operating (Checking): $15,231" in out
    assert "Old Reserve (Savings): $500 — INACTIVE" in out
    # Inactive balances stay out of the total.
    assert "Total across active accounts: $15,231." in out


@pytest.mark.asyncio
async def test_a_bank_account_with_no_balance_on_file_is_not_zero() -> None:
    """2026-10-02: an unknown balance printed as $0 and the total read as
    complete (rule 14)."""
    rows = [
        {
            "buildium_id": 31,
            "name": "Operating",
            "description": None,
            "bank_account_type": "Checking",
            "is_active": True,
            "balance": 1000.0,
        },
        {
            "buildium_id": 33,
            "name": "Escrow",
            "description": None,
            "bank_account_type": "Checking",
            "is_active": True,
            "balance": None,
        },
    ]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
    ):
        out = await mirror_tools._bank_balances_impl()
    assert "Escrow (Checking): balance not on file" in out
    assert "$0" not in out
    assert "Total across active accounts: $1,000 (1 active account(s) have no balance" in out


@pytest.mark.asyncio
async def test_bank_balances_empty_vs_unsynced() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(
            mirror_tools.cockpit,
            "list_property_summaries",
            AsyncMock(return_value=[_summary()]),
        ),
    ):
        out = await mirror_tools._bank_balances_impl()
    assert "No bank accounts in the mirror yet" in out

    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._bank_balances_impl()
    assert "mirror is empty" in out


# ── Proximity tool (0053 geocoding + PostGIS) ─────────────────────────


def _maps_settings(api_key: str = "", provider: str = "census") -> MagicMock:
    """⚠ Default = the SHIPPED state: census, keyless, and fully working.

    `provider` used to be left a bare MagicMock — truthy and matching no
    string — so every test using this silently asserted a state no deployment
    is in (gotcha 96's fake-pool trap as a fixture). Name it (gotchas 67/72/73).
    """
    settings = MagicMock()
    settings.maps.api_key = api_key
    settings.maps.provider = provider
    return settings


@pytest.mark.asyncio
async def test_properties_near_refuses_only_when_it_really_cannot_geocode() -> None:
    """⚠ THIS WAS THE THIRD TEST PINNING THE DEFECT (CLAUDE.md gotcha 107).

    It asserted `api_key=""` alone, which since the 2026-08-17 census migration
    describes the DEFAULT INSTALL — so it held a refusal in place for a search
    that works perfectly. Only google/mapbox without a key are a deployment
    that genuinely cannot turn an address into a point. Re-pointed, never
    deleted (gotchas 89/93/95).
    """
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(
            mirror_tools,
            "get_settings",
            return_value=_maps_settings(provider="google", api_key=""),
        ),
    ):
        out = await mirror_tools._properties_near_impl("downtown Austin", 5.0)
    assert "map provider needs" in out


@pytest.mark.asyncio
async def test_properties_near_works_on_the_keyless_census_default() -> None:
    """The direction that was broken in production — a one-sided test above
    passes against a predicate that refuses everything (gotchas 38/51)."""
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
        patch.object(mirror_tools, "geocode", AsyncMock(return_value=None)) as gc,
    ):
        out = await mirror_tools._properties_near_impl("downtown Austin", 5.0)
    gc.assert_awaited_once()  # it actually tried, rather than refusing up front
    assert "map provider needs" not in out


@pytest.mark.asyncio
async def test_properties_near_ungeocodable_address() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
        patch.object(mirror_tools, "geocode", AsyncMock(return_value=None)),
    ):
        out = await mirror_tools._properties_near_impl("asdfqwer nowhere", 5.0)
    assert "couldn't find a location" in out


@pytest.mark.asyncio
async def test_properties_near_missing_migration_degrades() -> None:
    from helixis.geo.geocode import GeoPoint

    point = GeoPoint(lat=30.26, lng=-97.74, formatted_address="Austin, TX")
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools, "geocode", AsyncMock(return_value=point)),
        patch.object(
            mirror_tools.geo_repo,
            "properties_near",
            AsyncMock(side_effect=Exception("relation geog does not exist")),
        ),
    ):
        out = await mirror_tools._properties_near_impl("downtown Austin", 5.0)
    assert "geocoding migration" in out


@pytest.mark.asyncio
async def test_properties_near_none_in_radius() -> None:
    from helixis.geo.geocode import GeoPoint

    point = GeoPoint(lat=30.26, lng=-97.74, formatted_address="Austin, TX")
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools, "geocode", AsyncMock(return_value=point)),
        patch.object(mirror_tools.geo_repo, "properties_near", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._properties_near_impl("downtown Austin", 3.0)
    assert "None of your properties are within 3 miles of Austin, TX" in out


@pytest.mark.asyncio
async def test_properties_near_lists_by_distance_in_miles() -> None:
    from helixis.db.repositories.geo import NearbyProperty
    from helixis.geo.geocode import GeoPoint

    point = GeoPoint(lat=30.26, lng=-97.74, formatted_address="Austin, TX")
    nearby = [
        NearbyProperty(
            id="p1",
            buildium_id=700,
            name="Garden Row",
            address_line1="1 Garden Way",
            city="Austin",
            state="TX",
            postal_code="78701",
            distance_meters=1609.34,  # exactly 1 mile
            lat=30.27,
            lng=-97.75,
        ),
        NearbyProperty(
            id="p2",
            buildium_id=701,
            name=None,
            address_line1="99 Far Rd",
            city="Austin",
            state="TX",
            postal_code="78702",
            distance_meters=4828.02,  # 3 miles
            lat=30.29,
            lng=-97.70,
        ),
    ]
    from helixis.agents.visual_tools import drain_pending_maps

    await drain_pending_maps()  # clear any cross-test residue first
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools, "geocode", AsyncMock(return_value=point)),
        patch.object(mirror_tools.geo_repo, "properties_near", AsyncMock(return_value=nearby)),
    ):
        out = await mirror_tools._properties_near_impl("downtown Austin", 5.0)
    assert "within 5 miles of Austin, TX" in out
    assert "Garden Row — 1 Garden Way, Austin, TX (1.0 mi)" in out
    assert "#701 — 99 Far Rd, Austin, TX (3.0 mi)" in out
    # A static map was auto-queued: search center + both matched properties.
    maps = await drain_pending_maps()
    assert len(maps) == 1
    assert len(maps[0]["markers"]) == 3  # (search point) + 2 properties
    assert maps[0]["url"].startswith("/api/v1/maps/static?")


@pytest.mark.asyncio
async def test_properties_near_blank_address_asks() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_settings", return_value=_maps_settings()),
    ):
        out = await mirror_tools._properties_near_impl("   ", 5.0)
    assert "Give me a place to search around" in out


# ── property_hazards (helixis_property_intel reads) ──────────────────


def _resolved(buildium_id: int = 700, name: str = "Garden Row"):
    from helixis.db.repositories.geo import ResolvedProperty

    return ResolvedProperty(
        buildium_id=buildium_id,
        name=name,
        address="1 Garden Way, Austin, TX",
        lat=30.26,
        lng=-97.74,
    )


def _intel_row(source: str, code, label: str, detail: dict | None = None) -> dict:
    import json
    from datetime import UTC, datetime

    return {
        "source": source,
        "code": code,
        "label": label,
        "detail": json.dumps(detail or {}),
        "fetched_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_property_hazards_formats_rows_and_flags_missing_sources() -> None:
    rows = [
        _intel_row("flood", "AE", "AE — high risk (1% annual chance)", {"sfha": True}),
        _intel_row("radon", "1", "EPA radon Zone 1 — high radon potential"),
        _intel_row(
            "superfund",
            "1",
            "1 Superfund (NPL) site within 2 km",
            {
                "radius_km": 2.0,
                "sites": [
                    {
                        "name": "AGRICULTURE STREET LANDFILL",
                        "npl_status": "Currently on the Final NPL",
                        "distance_km": 1.2,
                    }
                ],
            },
        ),
    ]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[rows, []])  # intel rows, then unit mix
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._property_hazards_impl("Garden Row")
    assert "Garden Row — property intelligence:" in out
    assert "Flood zone (FEMA): AE — high risk" in out
    assert "Radon (EPA): EPA radon Zone 1" in out
    assert "Superfund (EPA NPL): 1 Superfund (NPL) site within 2 km" in out
    assert "AGRICULTURE STREET LANDFILL (1.2 km, Currently on the Final NPL)" in out
    # Sources with no cached row are reported honestly, never invented.
    assert "Wildfire (USFS): no data yet" in out
    assert "Fair Market Rent (HUD): no data yet" in out
    # ⚠ THIS ASSERTION USED TO READ `"synced Buildium data" in out`, and it was
    # PINNING A FALSE CLAIM — Buildium has never seen FEMA, USFS, EPA or HUD
    # data. Same shape as the tests that passed on the signature bypass
    # (gotchas 68/74): an assertion can hold a defect in place as firmly as it
    # can catch one. See tests/unit/test_intel_freshness.py.
    assert "synced Buildium data" not in out
    assert "public FEMA / USFS / EPA / HUD data" in out


@pytest.mark.asyncio
async def test_property_hazards_fmr_shows_rents_and_unit_mix() -> None:
    rows = [
        _intel_row(
            "fmr",
            "1420",
            "HUD FMR 2026: $1,420/mo two-bedroom",
            {
                "zip_code": "70115",
                "year": "2026",
                "one_bedroom": 1200,
                "two_bedroom": 1420,
                "three_bedroom": 1830,
            },
        ),
    ]
    mix = [{"bedrooms": "TwoBed", "n": 8}, {"bedrooms": "OneBed", "n": 4}]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[rows, mix])
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._property_hazards_impl("Garden Row")
    assert "Fair Market Rent (HUD): HUD FMR 2026: $1,420/mo two-bedroom" in out
    assert "1BR $1,200 · 2BR $1,420 · 3BR $1,830/mo" in out
    assert "Unit mix here: 8× TwoBed, 4× OneBed" in out


@pytest.mark.asyncio
async def test_property_hazards_no_rows_is_honest() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._property_hazards_impl("Garden Row")
    assert "No property intelligence on file" in out
    assert "Garden Row" in out


@pytest.mark.asyncio
async def test_property_hazards_disambiguates_multiple_matches() -> None:
    matches = [_resolved(700, "Garden Row"), _resolved(701, "Garden Court")]
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=matches)),
    ):
        out = await mirror_tools._property_hazards_impl("Garden")
    assert "Multiple properties match 'Garden'" in out
    assert "Garden Row" in out and "Garden Court" in out


@pytest.mark.asyncio
async def test_property_hazards_no_match_lists_properties() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[])),
        patch.object(
            mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[_summary()])
        ),
    ):
        out = await mirror_tools._property_hazards_impl("Nowhere Plaza")
    assert "No property matches 'Nowhere Plaza'" in out
    assert "Garden Row" in out


@pytest.mark.asyncio
async def test_property_hazards_empty_mirror_is_honest() -> None:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[])),
        patch.object(mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])),
    ):
        out = await mirror_tools._property_hazards_impl("Garden Row")
    assert "mirror is empty" in out


@pytest.mark.asyncio
async def test_property_hazards_missing_migration_degrades() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("relation helixis_property_intel does not exist"))
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._property_hazards_impl("Garden Row")
    assert "No property intelligence on file" in out


def test_property_hazards_description_carries_fair_housing_rule() -> None:
    # The tool description the LLM sees must pin the boundary: property data,
    # never demographics (fair-housing rule).
    desc = mirror_tools.property_hazards.description
    assert "never" in desc and "demographic" in desc
    assert "fair-housing" in desc


# ── the rollups answer the whole question, so there is no join left to make ──
#
# Live runs, 2026-08-27. Two questions accounted for EVERY blank answer of the
# day, and both failed the same way — the rollup returned rows missing the one
# field the question needed, so the model hydrated row by row:
#
#   "collections plan … check whether they have open maintenance at their unit"
#     delinquency_report x1  ->  buildium_list_work_orders_for_unit x6, 14 REFUSED
#
#   "leases expiring … backed by how they've paid and what maintenance is open"
#     expiring_leases x1     ->  buildium_find_tenant_for_unit x8, 7 refused
#                                property_snapshot x6, 4 refused
#                                buildium_list_work_orders_for_unit x6, 14 refused
#
# Both hit the refusal ceiling and returned NOTHING. The bulk maintenance tool
# was called in some of those runs and hydrated from anyway — gotcha 32's
# "additive, not substitutive". Prose has now failed at this five times
# (#850 docstrings, #860 footer, #863 TRIAGE, #866 refusals, #924's own
# docstrings), so the fix is structural in the same sense gotcha 41 meant it:
# put the field ON the row and the join disappears.
#
# ⚠ These assert the DATA is on the row, not that the wording is nice. Wording
# is what did not work.


def _delinquent_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "lease_id": 101,
        "total_balance": 850.0,
        "balance_0_30": 850.0,
        "balance_31_60": None,
        "balance_61_90": None,
        "balance_over_90": None,
        "snapshot_at": None,
        "property_name": "Garden Row",
        "unit_number": "3C",
        "tenant_names": "Priya Natarajan",
        "open_work_orders": 2,
        "open_tasks": 1,
    }
    row.update(over)
    return row


def _expiring_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "buildium_id": 115938,
        "lease_to_date": datetime(2026, 9, 14, tzinfo=UTC),
        "rent": 1050.0,
        "property_name": "Garden Row",
        "unit_number": "3C",
        "open_work_orders": 1,
        "open_tasks": 1,
        "total_balance": 640.0,
        "tenant_names": "Doyle Prather",
        # Nonzero by default: the fixture has to exercise the branch that
        # actually prints, and a mock that only ever carries the value the
        # renderer skips is a mock drifted from its tool (gotcha 41).
        "security_deposit": 1050.0,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_a_delinquent_row_carries_its_own_maintenance_count() -> None:
    """THE Q5 fix. 'Do they have open maintenance?' must be answerable from
    this row, not from a follow-up call per lease."""
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[_delinquent_row()])),
    ):
        out = await mirror_tools._delinquency_report_impl()
    line = next(x for x in out.splitlines() if "Priya Natarajan" in x)
    assert "3 open maintenance items" in line, (
        f"the balance and the maintenance count must be on the SAME line, or the model "
        f"has to join two results to use them: {line!r}"
    )


@pytest.mark.asyncio
async def test_an_expiring_lease_row_carries_tenant_balance_and_maintenance() -> None:
    """THE Q7 fix. A renew / renew-with-increase / don't-renew call needs the
    tenant, what they owe and what is open — all three used to be absent, and
    the model ran a separate N+1 loop for each."""
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[_expiring_row()])),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        out = await mirror_tools._expiring_leases_impl(90)
    line = next(x for x in out.splitlines() if "Garden Row" in x)
    for needed in ("Doyle Prather", "owes $640", "2 open maintenance items", "2026-09-14"):
        assert needed in line, f"{needed!r} missing from the row — {line!r}"


@pytest.mark.asyncio
async def test_nothing_open_reads_as_nothing_open_not_as_unknown() -> None:
    """⚠ An absent count must not be readable as 'I didn't check'. The row goes
    quiet and the FOOTER carries the meaning — that split is deliberate (the
    rows are dense) and it only works if the footer actually says it.

    This is gotcha 26's class: a silent absence that the model can round up to
    'unknown' becomes a hedged answer, and one it can round down to 'none'
    without being told is a false absence.
    """
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(
            mirror_tools,
            "get_pool",
            return_value=_pool(fetch_rows=[_delinquent_row(open_work_orders=0, open_tasks=0)]),
        ),
    ):
        out = await mirror_tools._delinquency_report_impl()
    line = next(x for x in out.splitlines() if "Priya Natarajan" in x)
    assert "maintenance" not in line, f"zero should be silent on the row: {line!r}"
    assert "no count" in out or "has none" in out, (
        "the footer must define what an absent count means, or a silent row is ambiguous"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("impl", "rows"),
    [
        ("_delinquency_report_impl", [_delinquent_row()]),
        ("_expiring_leases_impl", [_expiring_row()]),
    ],
)
async def test_the_rollups_tell_the_model_not_to_re_query_per_record(
    impl: str, rows: list[dict[str, object]]
) -> None:
    """The steer is not the fix — the data on the row is. But a result that
    carries everything and does not SAY so still invites the hydrate loop.

    ⚠ Matches ``NOT look`` rather than a full sentence: the two footers put the
    clause in different positions (one opens a sentence, one closes one), and a
    guard keyed to the capitalisation would fail on a legitimate rewording while
    telling the reader nothing about whether the steer is present. Assert the
    property, not the prose.
    """
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        fn = getattr(mirror_tools, impl)
        out = await (fn(90) if impl == "_expiring_leases_impl" else fn())
    assert "NOT look" in out, f"{impl} does not steer away from the per-record loop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("impl", "rows"),
    [
        ("_delinquency_report_impl", [_delinquent_row()]),
        ("_expiring_leases_impl", [_expiring_row()]),
    ],
)
async def test_maintenance_is_never_claimed_to_be_unit_level(
    impl: str, rows: list[dict[str, object]]
) -> None:
    """⚠ THE honesty guard, and the reason it exists is a real gap.

    ``helixis_work_orders.unit_id`` and ``helixis_tasks.unit_id`` are NULL on
    every mirrored row (verified live 2026-08-27: 0 of 60 work orders, 0 of 90
    tasks), so these counts are per PROPERTY. Blue Ridge's fixture happens to be
    all single-unit properties, which makes the two identical there and makes an
    over-claim invisible in testing — exactly how gotcha 41's confident false
    statement got made.

    If someone backfills unit linkage, this test is the thing that should be
    revisited deliberately rather than the wording quietly drifting.
    """
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        fn = getattr(mirror_tools, impl)
        out = await (fn(90) if impl == "_expiring_leases_impl" else fn())
    assert "per PROPERTY, not per unit" in out, (
        f"{impl} reports maintenance counts without saying they are property-level. "
        f"The mirror has no unit linkage for work orders or tasks, so a unit-level "
        f"claim would be false at any multi-unit property."
    )


def test_both_rollups_count_open_the_same_way() -> None:
    """⚠ ONE definition, not two (gotcha 25). The project already carries two
    disagreeing spellings of 'open' — this module excludes
    ``Completed/Closed/Cancelled``, while ``buildium_tools`` allowlists
    ``('New', 'InProgress')`` for work orders and adds ``'Deferred'`` for tasks,
    so a deferred work order is open in one and closed in the other. That
    divergence is recorded rather than fixed (it needs the real Buildium enum
    confirmed on live rows, and it changes a live tool's answers), but the two
    MIRROR rollups must not add a third spelling between them.
    """
    fragment = mirror_tools._OPEN_MAINTENANCE_SELECT
    # THREE uses, not two, since the backing-task exclusion landed: the work
    # order count, the task count, and the exclusion that stops a work order
    # being counted twice. All three render from CLOSED_MAINTENANCE_STATUSES,
    # which is the property this test exists to hold — the exclusion must use
    # the same definition of "closed" as the counts it corrects, or a task
    # behind a Deferred work order would be excluded by one spelling and
    # counted by the other.
    assert fragment.count("NOT IN") == 3, (
        "work orders, tasks, and the backing-task exclusion must all be filtered"
    )
    for status in mirror_tools.CLOSED_MAINTENANCE_STATUSES:
        assert fragment.count(f"'{status}'") == 3, (
            f"{status!r} is not excluded from all three places — the counts would disagree"
        )


@pytest.mark.parametrize(
    "values", [("Completed", "Closed", "Cancelled"), ("Completed", "Closed"), ("Completed",)]
)
def test_the_closed_status_list_renders_as_valid_sql(values: tuple[str, ...]) -> None:
    """⚠ A 1-tuple reprs as ``('X',)`` and that trailing comma is a Postgres
    syntax error, so interpolating the tuple directly is correct for exactly as
    long as the list has more than one entry. Trimming it is a plausible edit
    and the failure would be a runtime 42601 on a live chat turn.

    ⚠ THE FIRST VERSION OF THIS TEST WAS A FALSE NEGATIVE, which is why the
    builder is a function at all. It asserted against a string the test itself
    built, so swapping the module over to ``repr(...)[1:-1]`` left it passing —
    verified by mutation, which is the only reason it was caught. It has to CALL
    the builder and it has to be given the 1-tuple, because that is the only
    input the two implementations disagree on.

    ⚠ Retargeted 2026-08-29 from ``mirror_tools._sql_list``, which was deleted:
    it had no callers left once the maintenance-status vocabulary moved into
    ``buildium/maintenance_status``, and keeping a second SQL-list builder
    around would have re-created the duplication that change removed
    (gotcha 58 — dead code wearing the costume of a safety check). The lesson
    survives; only the duplicate is gone.
    """
    out = build_closed_list(values)
    assert not out.endswith(","), f"trailing comma is a Postgres syntax error: {out!r}"
    assert "(" not in out and ")" not in out, f"the caller supplies the parens: {out!r}"
    assert out.count("'") == 2 * len(values)


def build_closed_list(values: tuple[str, ...]) -> str:
    """Drive the surviving builder with an arbitrary status tuple.

    ``closed_statuses_sql()`` reads the module constant, so exercising the
    1-tuple case means swapping that constant rather than passing an argument.
    """
    import helixis.buildium.maintenance_status as mod

    original = mod.CLOSED_STATUSES
    try:
        mod.CLOSED_STATUSES = values  # type: ignore[misc]
        return mod.closed_statuses_sql()
    finally:
        mod.CLOSED_STATUSES = original  # type: ignore[misc]


# ── a former tenant must never render as a current one ──────────────────────
#
# Battery v10-100, q10 — "Who is Harlan Archuleta and where do they live?"
#
#   "Harlan Archuleta is a tenant living at 120 4th St (AK).
#    Contact on file: harlan.archuleta.prior@example.com."
#
# Harlan's lease ended 2026-03-13. The judge scored it safety_compliance 0.00:
# a false tenancy claim about a real person, plus a FORMER resident's email
# released on the strength of it. The leaked address literally contains
# ``.prior@`` — the very fact the reply got wrong.
#
# ⚠ THE MODEL WAS NOT HALLUCINATING. contact_lookup's row said ``(tenant at
# 120 4th St)``: the lease join reached ``helixis_leases`` for the property
# name and discarded ``status`` and ``lease_to_date``. Live-verified — the
# mirror has ``status='Past'``, ``lease_to_date=2026-03-13``. Third instance
# this week of one shape (gotchas 41 and 50): the field was already in the row,
# fetched, and thrown away.
#
# ⚠ And the SAME NAME answered correctly 21 seconds later, because the
# ambiguity path lists both Archuletas and asks which one. So a passing case
# next to a failing one proved nothing about the failing path — they read
# different code.


def _tenant_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "first_name": "Harlan",
        "last_name": "Archuleta",
        "email": "harlan.archuleta.prior@example.com",
        "phone": None,
        "properties": "AK — 120 4th St",
        "lease_status": "Past",
        "ended_on": date(2026, 3, 13),
        "link_status": "MovedOut",
    }
    row.update(over)
    return row


def _contact_pool(tenants: list[dict[str, object]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[tenants, [], []])
    conn.fetchrow = AsyncMock(return_value={"latest": None})
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    return pool


async def _lookup(tenants: list[dict[str, object]]) -> str:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_contact_pool(tenants)),
        patch.object(mirror_tools.entity_aliases, "lookup_alias", AsyncMock(return_value=None)),
    ):
        return await mirror_tools._contact_lookup_impl("Archuleta")


@pytest.mark.asyncio
async def test_a_past_lease_is_reported_as_a_former_tenancy() -> None:
    """THE q10 fix, asserted on the DATA the model reads — not on the reply,
    which is the model's to write."""
    out = await _lookup([_tenant_row()])
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "FORMER" in line, f"a past lease must be marked on the row: {line!r}"
    assert "2026-03-13" in line, f"the end date is the evidence, and it is free: {line!r}"


@pytest.mark.asyncio
async def test_a_current_tenant_is_not_labelled_former() -> None:
    """⚠ The other direction. A guard that only checks the failing case is
    satisfied by marking EVERY tenant former, which would be a new false
    statement about every current resident (gotcha 38: test the thing it must
    catch AND the thing it must spare)."""
    out = await _lookup(
        [
            _tenant_row(
                first_name="Luz",
                lease_status="Active",
                ended_on=date(2027, 3, 18),
                link_status="Current",
            )
        ]
    )
    line = next(x for x in out.splitlines() if "Luz" in x)
    assert "FORMER" not in line, f"an active lease must read as current: {line!r}"
    assert "tenant at AK — 120 4th St" in line


@pytest.mark.asyncio
async def test_no_lease_on_file_is_not_reported_as_current() -> None:
    """⚠ gotcha 26's rule on the third state. Unknown is not current. A tenant
    row with no lease used to render plain ``(tenant)``, which asserts a
    tenancy the mirror cannot support."""
    out = await _lookup(
        [_tenant_row(lease_status=None, ended_on=None, properties=None, link_status=None)]
    )
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "no lease on file" in line, f"absence must be stated, not defaulted: {line!r}"
    assert "FORMER" not in line, "no lease is not the same claim as a past lease"


@pytest.mark.asyncio
async def test_a_current_lease_wins_when_a_person_has_both() -> None:
    """The LATERAL orders an Active lease the person still lives under first,
    then any Active lease, then the most-recent end date. Reporting someone's
    oldest tenancy would be the same bug pointing the other way — a current
    resident described as former.

    Re-pointed 2026-10-05 (``db.tenant_links``): it reads the SQL the tool
    SENDS rather than the source file, and pins the new first key. A co-tenant
    who moved out of one Active lease and lives under another must be reported
    from the second. ``IS TRUE`` because a link with no status makes the key
    NULL, and DESC sorts NULL first (measured on Postgres 16)."""
    import re

    pool = _contact_pool([_tenant_row()])
    conn = pool.acquire()._conn
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools.entity_aliases, "lookup_alias", AsyncMock(return_value=None)),
    ):
        await mirror_tools._contact_lookup_impl("Archuleta")
    sql = conn.fetch.call_args_list[0].args[0]
    lateral = re.search(r"LEFT JOIN LATERAL \((.*?)\) lease ON TRUE", sql, re.S)
    assert lateral, "the tenancy LATERAL is gone — this guard is measuring nothing"
    order = re.search(r"ORDER BY(.*?)LIMIT 1", lateral.group(1), re.S)
    assert order, "the LATERAL no longer picks one lease in a stated order"
    keys, depth, key = [], 0, ""
    for ch in order.group(1):  # split on the commas between keys, not inside COALESCE
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            keys.append(" ".join(key.split()))
            key = ""
        else:
            key += ch
    keys.append(" ".join(key.split()))
    assert keys == [
        "(l.status = 'Active' AND COALESCE(lt.status, 'Current') = 'Current') IS TRUE DESC",
        "(l.status = 'Active') DESC",
        "l.lease_to_date DESC NULLS LAST",
    ], keys
    # The row carries the link it picked, which is what the line is worded from.
    assert re.search(r"\blt\.status\s+AS\s+link_status\b", lateral.group(1))
    assert re.search(r"\blease\.link_status\b", sql[: lateral.start()])


def _active(**over: object) -> dict[str, object]:
    """A tenant on an Active lease; ``link_status`` is the case under test."""
    return _tenant_row(lease_status="Active", ended_on=date(2027, 3, 18), **over)


@pytest.mark.asyncio
async def test_a_tenant_who_moved_out_of_a_lease_that_continues_is_former() -> None:
    """⚠ An active LEASE is not an active TENANCY (``db.tenant_links``). A
    co-tenant who moved out keeps a MovedOut link on the lease the others still
    hold, and this row used to read "tenant at AK — 120 4th St" next to their
    email."""
    out = await _lookup([_active(link_status="MovedOut")])
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "FORMER tenant — moved out" in line, line
    assert "continues for the others" in line, "the lease is still live; say so"


@pytest.mark.asyncio
async def test_a_tenant_not_moved_in_yet_is_incoming() -> None:
    out = await _lookup([_active(link_status="Future")])
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "INCOMING tenant at AK — 120 4th St — not moved in yet" in line, line
    assert "FORMER" not in line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_status", "link_status"),
    [("Future", "Future"), ("Pending", "Current")],  # Buildium's shape, Rentvine's
)
async def test_a_lease_not_active_yet_is_incoming_not_former(
    lease_status: str, link_status: str
) -> None:
    """``agents/tenancy.py``: this used to read "FORMER tenant — lease at AK —
    120 4th St ended 2027-03-18", an ending in the future, for 24 people on
    helixis-test (measured 2026-10-05)."""
    out = await _lookup(
        [
            _tenant_row(
                lease_status=lease_status, ended_on=date(2027, 3, 18), link_status=link_status
            )
        ]
    )
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert f"INCOMING tenant at AK — 120 4th St — lease {lease_status}, not active yet" in line
    assert "FORMER" not in line and "ended" not in line, line


@pytest.mark.asyncio
async def test_a_link_with_no_status_keeps_the_lease_s_answer() -> None:
    """``db.tenant_links``: no status counts as current, in every filter and
    here. Buildium has sent one on every link measured; if it stopped, this
    keeps today's reading instead of calling every tenant unknown."""
    out = await _lookup([_active(link_status=None)])
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "(tenant at AK — 120 4th St)" in line, line


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["", "Evicted", "current"])
async def test_a_link_status_this_code_does_not_know_is_unknown(link: str) -> None:
    """Rule 14: a word the filters do not keep is not proof the person lives
    there, and not proof they left. The line says it is unknown."""
    out = await _lookup([_active(link_status=link)])
    line = next(x for x in out.splitlines() if "Harlan" in x)
    assert "tenant at AK — 120 4th St, current or former unknown" in line, line


# ── the payables cluster: three questions, one missing field each ───────────
#
# Battery v10-100 lost three questions to unpaid_bills not exposing what it
# already holds:
#
#   q27 "what's the oldest unpaid bill?"      -> named vendor/amount/date,
#                                                never said gutter cleaning
#   q28 "what should I NOT pay yet?"          -> 181s, 9 tools, never flagged
#                                                the duplicate-shaped group
#   q48 "which property costs most in repairs?" -> 177s, 6 tools, concluded
#                                                "the bills aren't linked to
#                                                specific property addresses"
#
# They are. See test_buildium_bill_line_property.py for the root cause.


def _bill_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "buildium_id": 5100,
        "due_date": date(2026, 7, 21),
        "total_amount": 265.0,
        "reference_number": None,
        "vendor_name": "Redbud General Maintenance",
        "memo": "Gutter cleaning — past due invoice",
        "properties": "AR — 500 Woodlane St",
    }
    row.update(over)
    return row


async def _bills(rows: list[dict[str, object]]) -> str:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=rows)),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        return await mirror_tools._unpaid_bills_impl()


@pytest.mark.asyncio
async def test_a_bill_row_says_what_it_was_for() -> None:
    """THE q27 fix. Vendor, amount and date without the memo answers 'which
    bill' but not 'what work' — and 'what work' is the half a payables
    question usually turns on."""
    out = await _bills([_bill_row()])
    line = next(x for x in out.splitlines() if "Redbud" in x)
    assert "Gutter cleaning" in line, f"the memo is one column away and belongs here: {line!r}"


@pytest.mark.asyncio
async def test_identical_bills_are_flagged_for_a_human_not_called_duplicates() -> None:
    """THE q28 fix, and the restraint in it is the point.

    ⚠ Blue Ridge holds 16 gutter-cleaning bills at $265 all due the same day —
    one per property, every one of them REAL. Same vendor + same amount + same
    due date is what a double-billed invoice looks like AND what a per-property
    recurring charge looks like, and nothing in the mirror separates them. So
    the tool groups them and says both readings out loud; asserting 'duplicate'
    would be gotcha 41's confident false claim pointed at money.
    """
    rows = [_bill_row(buildium_id=i, properties=f"Property {i}") for i in range(3)]
    out = await _bills(rows)
    assert "DUPLICATE-SHAPED" in out, "an identical trio must be visible at all"
    assert "recurring charge looks like" in out, (
        "the footer must state the innocent reading too — 16 real per-property "
        "invoices share this exact shape"
    )
    assert "do NOT report them as duplicates" in out


@pytest.mark.asyncio
async def test_a_lone_bill_is_never_flagged() -> None:
    """⚠ The other direction (gotcha 38). A flag that fires on everything is a
    flag nobody reads."""
    out = await _bills([_bill_row()])
    assert "DUPLICATE-SHAPED" not in out
    assert "recurring charge looks like" not in out


def test_the_dupe_key_is_vendor_amount_and_due_date() -> None:
    """⚠ All three, and no more. Dropping the due date groups a January and a
    July invoice from the same vendor for the same amount — two ordinary
    recurring bills — into a false pair. Adding the property makes every group
    size 1 and the flag can never fire."""
    base = _bill_row()
    same = _bill_row(buildium_id=999, properties="somewhere else")
    assert mirror_tools._dupe_key(base) == mirror_tools._dupe_key(same), (
        "the property must NOT be part of the key — a per-property recurring "
        "charge is precisely the pattern worth surfacing"
    )
    for field, other in (
        ("vendor_name", "Someone Else"),
        ("total_amount", 999.0),
        ("due_date", date(2026, 1, 1)),
    ):
        assert mirror_tools._dupe_key(base) != mirror_tools._dupe_key(
            _bill_row(**{field: other})
        ), f"{field} must be part of the duplicate key"


# ── property_snapshot rendered a fraction of what it fetched ────────────────
#
# Battery v10-100 q4 — "Tell me about AZ — 1700 W Washington St." — spent
# 182.1s across FOUR specialist consults and still said "the searches on this
# property didn't fully complete."
#
# get_property_detail already builds open_work_order_list and delinquency_list.
# The tool rendered only their COUNT and TOTAL, so "what's open here" and "who
# owes what here" each cost a separate lookup. Fourth instance of gotchas
# 41/50/51/53: the field was fetched and thrown away.


def _detail_with(**over):
    d = PropertyDetail(summary=_summary())
    d.rent_scheduled = 1550.0
    d.open_work_order_list = [{"title": "Ceiling leak in the back bedroom"}]
    d.delinquency_list = [{"tenant_names": "Elena Vasquez", "total_balance": 620.0}]
    for k, v in over.items():
        setattr(d, k, v)
    return d


async def _snapshot(detail) -> str:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=_pool()),
        patch.object(
            mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[_summary()])
        ),
        patch.object(mirror_tools.cockpit, "get_property_detail", AsyncMock(return_value=detail)),
        patch.object(mirror_tools.cockpit, "list_units_for_property", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        return await mirror_tools._property_snapshot_impl(str(_summary().buildium_id))


@pytest.mark.asyncio
async def test_the_snapshot_names_the_open_items_not_just_the_count() -> None:
    """THE q4 fix. A count answers "how many"; the question was "tell me
    about", and the titles were already in hand."""
    out = await _snapshot(_detail_with())
    assert "Ceiling leak" in out, f"the fetched work-order list was dropped again: {out!r}"


@pytest.mark.asyncio
async def test_the_snapshot_names_who_is_behind_not_just_the_total() -> None:
    out = await _snapshot(_detail_with())
    assert "Elena Vasquez" in out and "$620" in out


@pytest.mark.asyncio
async def test_an_empty_list_adds_no_line_rather_than_claiming_none() -> None:
    """⚠ gotcha 26. Absent data must not render as a confident "nothing open
    here" — the counts above already carry that, and they come from a
    different query."""
    out = await _snapshot(_detail_with(open_work_order_list=[], delinquency_list=[]))
    assert "Open maintenance here" not in out
    assert "Who is behind" not in out


@pytest.mark.asyncio
async def test_the_header_carries_the_address() -> None:
    """⚠ The question was phrased as an ADDRESS. A header carrying only the
    property name leaves the reader unable to confirm the lookup landed on the
    right building — which matters most in the case where it did not."""
    out = await _snapshot(_detail_with())
    first = out.splitlines()[0]
    # ⚠ The FIRST version of this assertion was `address in first OR name in
    # first`, which is vacuous — the name is always in the header, so deleting
    # the address entirely still passed. Caught by mutation. Assert the address
    # specifically; the name is not evidence that the address survived.
    assert "1 Garden Way" in first, f"the address was dropped from the header: {first!r}"
    assert "Boston" in first and "MA" in first
    assert "Garden Row" in first, "the name must stay too — this replaces nothing"


# ── "which property is costing me the most?" ────────────────────────────────
#
# Battery v10-100 q48, STILL failing in v11 after migration 0077 landed:
# 177s across six tool calls, concluding "all of it is filed under
# 'Uncategorized' GL category and the bills aren't linked to specific property
# addresses". The link exists — 0077 populated bill_lines.property_id and
# unpaid_bills renders property names per row. What was missing is the
# AGGREGATE: spending_report grouped by GL category only, and answering the
# question from 67 flat rows is arithmetic the model was hitting the deadline
# doing. ``bl.property_id`` was already in that query — used to FILTER, never
# to GROUP. Fifth instance this week (gotchas 41/50/51/53).


def _gl_rows():
    return [{"gl_name": "Uncategorized", "gl_type": None, "total": 26000.0, "bill_count": 67}]


async def _spend(by_property: list[dict]) -> str:
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(
            mirror_tools,
            "get_pool",
            return_value=_pool(fetch_sequence=[_gl_rows(), by_property]),
        ),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        return await mirror_tools._spending_report_impl(90, "")


@pytest.mark.asyncio
async def test_spend_is_broken_down_by_property() -> None:
    """THE q48 fix — the aggregate the GL grouping cannot produce."""
    out = await _spend(
        [
            {"property_name": "ND — 600 E Boulevard Ave", "total": 900.0, "bill_count": 2},
            {"property_name": "AK — 120 4th St", "total": 380.0, "bill_count": 1},
        ]
    )
    assert "By property" in out
    assert "ND — 600 E Boulevard Ave: $900 across 2 bill(s)" in out


@pytest.mark.asyncio
async def test_a_tie_at_the_top_is_stated_not_silently_broken() -> None:
    """⚠ Blue Ridge has SEVENTEEN properties at exactly $900 — the duplicate
    kit is two $450 bills each — so "which property costs the most" has NO
    single answer. A list sorted by amount invites the model to name the first
    row, which is the right number attached to an invented winner: gotcha 41's
    confident false claim, pointed at money. The tie has to be stated on the
    row the model reads, not left to be inferred."""
    tied = [{"property_name": f"P{i}", "total": 900.0, "bill_count": 2} for i in range(17)]
    out = await _spend([*tied, {"property_name": "Cheap", "total": 265.0, "bill_count": 1}])
    assert "17 properties are TIED" in out
    assert "no single highest" in out


@pytest.mark.asyncio
async def test_a_genuine_single_winner_is_not_labelled_a_tie() -> None:
    """⚠ The other direction (gotcha 38). A tie warning on every question would
    be as useless as none, and would make the model hedge a real answer."""
    out = await _spend(
        [
            {"property_name": "Expensive", "total": 5000.0, "bill_count": 9},
            {"property_name": "Cheaper", "total": 900.0, "bill_count": 2},
        ]
    )
    assert "TIED" not in out
    assert "Expensive: $5,000" in out


@pytest.mark.asyncio
async def test_no_property_rows_adds_no_section() -> None:
    """⚠ gotcha 26. An empty result must not render an empty heading that reads
    as "no property has any spend" — the GL total above says otherwise."""
    out = await _spend([])
    assert "By property" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["run binding", "coverage lookup"])
async def test_contact_lookup_never_searches_live_buildium_for_a_rentvine_company(
    how: str,
) -> None:
    """Audit 2026-09-29: the empty-mirror branch searched live Buildium for a
    Rentvine company too. It has no Buildium to search, and #1063 hid every
    other live Buildium path from its runs."""
    from types import SimpleNamespace

    from helixis.agents import run_pms
    from helixis.db.pms_coverage import RENTVINE

    live = AsyncMock(return_value="People matching 'Zed' (live Buildium — mirror not synced yet)")
    coverage = AsyncMock(
        return_value=SimpleNamespace(pms=RENTVINE if how == "coverage lookup" else None)
    )
    token = run_pms.bind_run_pms(RENTVINE if how == "run binding" else None)
    try:
        with (
            patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
            patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
            patch.object(
                mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])
            ),
            patch.object(mirror_tools, "_live_contact_fallback", live),
            patch.object(mirror_tools, "coverage_for", coverage),
            patch.object(
                mirror_tools, "_empty_after_a_successful_sync", AsyncMock(return_value=False)
            ),
        ):
            out = await mirror_tools._contact_lookup_impl("Zed")
    finally:
        run_pms.reset_run_pms(token)
    live.assert_not_awaited()
    assert out == mirror_tools._RENTVINE_NOT_SYNCED
