"""Unit tests for PR #73 Buildium agent tools.

Covers the four new instinct-fix tools:
- ``buildium_get_owner`` — fuzzy match + full owner card
- ``buildium_list_upcoming_lease_expirations`` — date-filtered list
- ``buildium_list_vacant_units`` — join units + active leases in one call
- ``buildium_get_rent_roll`` — join active leases + outstanding balances

Plus the aging-bucket / eviction-flag additions to
``buildium_list_overdue_balances`` that fix the screenshot-3
"3 pending evictions but no outstanding balances" contradiction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.memory.context import memory_context


def _ctx_company(company_id: str = "company-1") -> Any:
    return memory_context(company_id=company_id, user_id="u1", session_id="s1")


def _client_with(api: Any) -> Any:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _settings() -> Any:
    return MagicMock()


# ── buildium_get_owner ──────────────────────────────────────────


def _owner(
    id_: int,
    *,
    company_name: str | None = None,
    last_name: str | None = None,
    first_name: str | None = None,
    email: str | None = None,
    property_ids: list[int] | None = None,
) -> Any:
    o = MagicMock()
    o.id = id_
    o.company_name = company_name
    o.last_name = last_name
    o.first_name = first_name
    o.email = email
    o.property_ids = property_ids
    o.phone_numbers = None
    o.address = None
    return o


def test_owner_card_renders_phone_from_phonenumbers_extra() -> None:
    # RentalOwner doesn't declare ``phone_numbers``; the ``PhoneNumbers`` list
    # rides along as a BuildiumModel extra under its alias. Reading the
    # snake_case attr (the old bug) showed no phone. Card must render it from a
    # real RentalOwner so a PM can actually call the owner.
    from helixis.buildium.schemas import RentalOwner

    owner = RentalOwner.model_validate(
        {
            "Id": 7,
            "CompanyName": "Ocean Properties LLC",
            "PhoneNumbers": [{"Number": "(555) 123-4567", "Type": "Cell"}],
        }
    )
    card = bt._format_owner_card(owner)
    assert "555) 123-4567" in card


def test_owner_card_phone_helper_handles_dict_and_missing() -> None:
    assert bt._owner_phone_numbers({"PhoneNumbers": [{"Number": "1"}]}) == [{"Number": "1"}]
    assert bt._owner_phone_numbers(MagicMock(phone_numbers=None, PhoneNumbers=None)) == []


@pytest.mark.asyncio
async def test_get_owner_fuzzy_matches_unique_company_name() -> None:
    owners = [
        _owner(101, company_name="Ocean Properties LLC", email="ocean@example.com"),
        _owner(102, last_name="Smith"),
    ]
    api = MagicMock()
    api.rentals = MagicMock()
    api.rentals.list_owners = AsyncMock(return_value=owners)
    api.rentals.get_owner = AsyncMock(return_value=owners[0])

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._get_owner_impl("ocean")

    assert "Ocean Properties LLC" in result
    # PR #75 Bug B — owner card no longer leaks the internal owner id.
    assert "(#101)" not in result
    assert "ocean@example.com" in result
    api.rentals.get_owner.assert_awaited_with(101)


@pytest.mark.asyncio
async def test_get_owner_disambiguates_when_multiple_match() -> None:
    owners = [
        _owner(1, last_name="Smith"),
        _owner(2, last_name="Smithson"),
        _owner(3, last_name="Other"),
    ]
    api = MagicMock()
    api.rentals = MagicMock()
    api.rentals.list_owners = AsyncMock(return_value=owners)
    api.rentals.get_owner = AsyncMock()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._get_owner_impl("smith")

    assert "2 owners match" in result
    api.rentals.get_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_owner_returns_not_connected_without_company() -> None:
    result = await bt._get_owner_impl("ocean")
    assert result.startswith("Buildium is not connected")


@pytest.mark.asyncio
async def test_get_owner_handles_empty_query() -> None:
    api = MagicMock()
    api.rentals = MagicMock()
    api.rentals.list_owners = AsyncMock(return_value=[])
    api.rentals.get_owner = AsyncMock()
    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._get_owner_impl("")
    assert "Please specify" in result


# ── buildium_list_upcoming_lease_expirations ────────────────────


def _lease(
    id_: int,
    *,
    rent: float | None = None,
    lease_to_date: datetime | None = None,
    tenants: list[str] | None = None,
    unit_id: int | None = None,
) -> Any:
    le = MagicMock()
    le.id = id_
    le.rent = rent
    le.lease_to_date = lease_to_date
    le.unit_id = unit_id
    le.current_tenants = (
        [MagicMock(first_name=n, last_name="X", id=999) for n in (tenants or [])]
        if tenants
        else None
    )
    le.unit_number = None
    return le


@pytest.mark.asyncio
async def test_list_upcoming_expirations_passes_date_filter_and_sorts() -> None:
    soon = datetime.now(UTC).replace(microsecond=0)
    leases = [
        _lease(2, rent=1500, lease_to_date=soon.replace(day=28), tenants=["Bob"]),
        _lease(1, rent=2000, lease_to_date=soon.replace(day=15), tenants=["Ana"]),
    ]
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._list_upcoming_lease_expirations_impl(90)

    api.leases.list.assert_awaited_once()
    kwargs = api.leases.list.await_args.kwargs
    assert kwargs.get("leasestatuses") == ["Active"]
    assert "leasetodatefrom" in kwargs
    assert "leasetodateto" in kwargs
    # Sorted soonest first → Ana (15th) before Bob (28th).
    ana_pos = result.index("Ana")
    bob_pos = result.index("Bob")
    assert ana_pos < bob_pos


@pytest.mark.asyncio
async def test_list_upcoming_expirations_returns_calm_message_when_empty() -> None:
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=[])

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._list_upcoming_lease_expirations_impl(30)

    assert "No active leases expiring" in result


# ── buildium_list_vacant_units ──────────────────────────────────


def _unit(id_: int, unit_number: str | None = None, market_rent: float | None = None) -> Any:
    u = MagicMock()
    u.id = id_
    u.unit_number = unit_number
    u.market_rent = market_rent
    u.address = None
    return u


@pytest.mark.asyncio
async def test_list_vacant_units_filters_out_leased_units_in_one_call() -> None:
    units = [_unit(1, "A"), _unit(2, "B"), _unit(3, "C")]
    leases = [_lease(100, unit_id=2)]  # Unit 2 is leased; 1 and 3 are vacant.
    api = MagicMock()
    api.rentals = MagicMock()
    api.rentals.list_units = AsyncMock(return_value=units)
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._list_vacant_units_impl()

    assert "2 vacant units" in result
    # PR #75 Bug B — units are surfaced by their unit-number ("A", "C"),
    # never by internal ids.
    assert "Unit A" in result
    assert "Unit C" in result
    assert "Unit B" not in result
    assert "(#1)" not in result
    assert "(#3)" not in result


@pytest.mark.asyncio
async def test_list_vacant_units_returns_friendly_message_when_full() -> None:
    units = [_unit(1, "A")]
    leases = [_lease(100, unit_id=1)]
    api = MagicMock()
    api.rentals = MagicMock()
    api.rentals.list_units = AsyncMock(return_value=units)
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._list_vacant_units_impl()
    assert "No vacant units" in result


# ── buildium_get_rent_roll ──────────────────────────────────────


def _balance(lease_id: int, *, outstanding: float = 0, eviction: datetime | None = None) -> Any:
    b = MagicMock()
    b.lease_id = lease_id
    b.outstanding_balance = outstanding
    b.eviction_pending_date = eviction
    return b


@pytest.mark.asyncio
async def test_get_rent_roll_sorts_by_rent_desc_and_joins_outstanding() -> None:
    leases = [
        _lease(1, rent=1500, tenants=["Bob"]),
        _lease(2, rent=2500, tenants=["Ana"]),
    ]
    balances = [_balance(2, outstanding=500, eviction=datetime.now(UTC))]
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._get_rent_roll_impl()

    # Ana ($2500) sorts above Bob ($1500).
    assert result.index("Ana") < result.index("Bob")
    # Eviction surfaces on Ana's row.
    ana_line = next(line for line in result.splitlines() if "Ana" in line)
    assert "EVICTION PENDING" in ana_line
    assert "$500" in ana_line
    # Total rent in header.
    assert "$4,000" in result


@pytest.mark.asyncio
async def test_get_rent_roll_returns_empty_message_with_no_leases() -> None:
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=[])
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._get_rent_roll_impl()
    assert "rent roll is empty" in result


# ── overdue_balances aging-bucket / eviction surfacing ──────────


def _full_balance(
    lease_id: int,
    *,
    outstanding: float,
    days_late: int | None = None,
    aging: dict[str, float] | None = None,
    eviction: datetime | None = None,
    is_notice_given: bool | None = None,
) -> Any:
    b = MagicMock()
    b.lease_id = lease_id
    b.outstanding_balance = outstanding
    b.days_late = days_late
    b.unit_number = "3B"
    aging = aging or {}
    b.balance_0_to_30_days = aging.get("0-30", 0)
    b.balance_31_to_60_days = aging.get("31-60", 0)
    b.balance_61_to_90_days = aging.get("61-90", 0)
    b.balance_over_90_days = aging.get(">90", 0)
    b.eviction_pending_date = eviction
    b.is_notice_given = is_notice_given
    return b


@pytest.mark.asyncio
async def test_overdue_balances_surfaces_aging_buckets_and_eviction_flag() -> None:
    balances = [
        _full_balance(
            10,
            outstanding=1850,
            days_late=92,
            aging={">90": 1850.0},
            eviction=datetime.now(UTC),
            is_notice_given=True,
        ),
    ]
    lease_obj = _lease(10, tenants=["Mei"])
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)
    api.leases.list = AsyncMock(return_value=[lease_obj])
    api.leases.get = AsyncMock(return_value=lease_obj)

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        result = await bt._list_overdue_balances_impl()

    # The aging bucket label, the eviction flag, and the notice flag
    # should all appear on the row for lease 10.
    assert ">90d $1,850.00" in result
    assert "EVICTION PENDING" in result
    assert "(notice given)" in result
    assert "92 days overdue" in result


# ── PR #74 — outstandingbalances must pass leasestatuses=Active ─


@pytest.mark.asyncio
async def test_overdue_balances_passes_leasestatuses_active_filter() -> None:
    """Regression for PR #74 Bug 2: Brandon's sandbox had 17 delinquent
    active leases + 2 evictions pending, and the tool reported
    "everyone's paid up" because Buildium's outstandingbalances
    endpoint doesn't default to Active. The fix pins
    ``leasestatuses=["Active"]`` on the call.
    """
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])
    api.leases.list = AsyncMock(return_value=[])
    api.leases.get = AsyncMock()

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        await bt._list_overdue_balances_impl()

    # PR #76 — Buildium 400s when ``entitytype`` is sent without a
    # matching ``entityid`` (it's a per-property filter, not a list
    # filter). The PR #75 attempt to add ``entitytype="Rental"`` was
    # wrong; PR #76 drops it. First call is ``leasestatuses=["Active"]``
    # only; if that returns empty (as it does here), the impl falls back
    # to a second call with ``balanceduration="TotalBalance"`` —
    # still no ``entitytype``.
    assert api.leases.list_outstanding_balances.await_count == 2
    first_kwargs = api.leases.list_outstanding_balances.await_args_list[0].kwargs
    assert first_kwargs == {
        "leasestatuses": ["Active"],
    }, f"expected leasestatuses=Active only on first call, got {first_kwargs!r}"
    second_kwargs = api.leases.list_outstanding_balances.await_args_list[1].kwargs
    assert second_kwargs == {
        "leasestatuses": ["Active"],
        "balanceduration": "TotalBalance",
    }, f"expected balanceduration=TotalBalance fallback on second call, got {second_kwargs!r}"
    # And explicitly assert ``entitytype`` is gone — this is the
    # regression we're pinning.
    assert "entitytype" not in first_kwargs
    assert "entitytype" not in second_kwargs


@pytest.mark.asyncio
async def test_rent_roll_passes_leasestatuses_active_to_balances() -> None:
    """Same fix shape on the rent-roll tool — pre-PR-#74 the balances
    join silently returned empty so the rent roll's "owes" column was
    blank for every row."""
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=[])
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
    ):
        await bt._get_rent_roll_impl()

    # PR #76 — same two-call shape as the overdue-balances tool, with
    # ``entitytype`` removed (Buildium 400s without a matching entityid).
    assert api.leases.list_outstanding_balances.await_count == 2
    first_kwargs = api.leases.list_outstanding_balances.await_args_list[0].kwargs
    assert first_kwargs == {
        "leasestatuses": ["Active"],
    }
    second_kwargs = api.leases.list_outstanding_balances.await_args_list[1].kwargs
    assert second_kwargs == {
        "leasestatuses": ["Active"],
        "balanceduration": "TotalBalance",
    }
    assert "entitytype" not in first_kwargs
    assert "entitytype" not in second_kwargs


@pytest.mark.asyncio
async def test_rent_roll_logs_when_balances_empty_against_active_leases(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive diagnostic line: if Buildium returns an empty balance
    list despite a non-empty active-lease list, log an INFO so the
    bug surfaces in Render logs without needing to run the agent."""
    leases = [_lease(1, rent=1500, tenants=["A"]), _lease(2, rent=2000, tenants=["B"])]
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])

    import logging

    with (
        _ctx_company(),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=_settings())
        ),
        patch.object(bt, "BuildiumClient", return_value=_client_with(api)),
        patch.object(bt, "BuildiumApi", return_value=api),
        caplog.at_level(logging.INFO, logger="helixis.agents.buildium_tools"),
    ):
        await bt._get_rent_roll_impl()

    # PR #76 — the retry log line now says
    # "empty with leasestatuses=[Active] — retrying with
    #  balanceduration=TotalBalance". PR #75's "entitytype=Rental"
    # phrasing is gone (we no longer send that param). Accept either
    # the new line or PR #74's legacy "against N active leases"
    # phrasing for back-compat.
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        (
            "outstandingbalances empty" in msg
            and ("leasestatuses=[Active]" in msg or "against 2 active leases" in msg)
        )
        for msg in messages
    ), f"expected diagnostic log, got: {messages}"
