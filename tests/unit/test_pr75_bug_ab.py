"""PR #75 Bugs A residual + B — outstanding-balances fix and ID redaction.

**Bug A residual.** Pre-PR-#75 the agent answered "everyone's paid up"
to "who is delinquent?" against a sandbox with 17 active delinquent
leases and 2 pending evictions. PR #74 landed ``leasestatuses=["Active"]``
on ``list_outstanding_balances`` but the endpoint still returned empty.
PR #75 layers two more nudges in this order:

1. Always pass ``entitytype="Rental"`` (docs list it as optional but
   in Brandon's sandbox it gates whether rows come back at all).
2. If the filtered call still returns empty, retry with
   ``balanceduration="TotalBalance"`` — Buildium's docs imply the
   default duration only counts current-period charges, which can
   miss aged balances.
3. If BOTH return empty, never reply "everyone's paid up". Return
   an honest "I queried Buildium and got no rows back" so the
   property manager knows to escalate.

**Bug B (ID redaction).** Brandon's rule: NEVER expose Buildium
internal IDs in chat. "Lease #24188", "Tenant #X", "(#5)",
"Work order #2916" — all banned. Refer to people by name and places
by property name + unit number. The ``[ref:X]`` tag is the only
remaining ID surface in tool output, and orchestrator instructions
forbid the agent from echoing it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.memory.context import memory_context

# ── Test helpers (lightweight; mirror existing test files) ─────────


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


def _balance(
    lease_id: int,
    *,
    outstanding: float = 0,
    days_late: int | None = None,
    unit_number: str | None = None,
    eviction: datetime | None = None,
) -> Any:
    b = MagicMock()
    b.lease_id = lease_id
    b.outstanding_balance = outstanding
    b.days_late = days_late
    b.unit_number = unit_number
    b.balance_0_to_30_days = 0
    b.balance_31_to_60_days = 0
    b.balance_61_to_90_days = 0
    b.balance_over_90_days = 0
    b.eviction_pending_date = eviction
    b.is_notice_given = None
    return b


def _lease(
    id_: int,
    *,
    rent: float | None = None,
    unit_number: str | None = None,
    tenants: list[tuple[int, str, str]] | None = None,
) -> Any:
    le = MagicMock()
    le.id = id_
    le.rent = rent
    le.unit_id = None
    le.unit_number = unit_number
    le.property_id = None
    le.lease_to_date = None
    le.lease_from_date = None
    le.term_type = None
    le.status = "Active"
    le.is_eviction_pending = False
    le.cosigners = []
    if tenants:
        cts = []
        for tid, fn, ln in tenants:
            ct = MagicMock()
            ct.id = tid
            ct.first_name = fn
            ct.last_name = ln
            ct.email = None
            cts.append(ct)
        le.current_tenants = cts
    else:
        le.current_tenants = []
    return le


# ── Bug A residual — _list_overdue_balances_impl ───────────────────


@pytest.mark.asyncio
async def test_overdue_balances_first_call_uses_leasestatuses_only() -> None:
    """The first list_outstanding_balances call must use
    ``leasestatuses=["Active"]`` ONLY. PR #75 added ``entitytype="Rental"``
    here but Buildium 400s when ``entitytype`` is sent without a matching
    ``entityid`` — PR #76 drops the param. This test pins the corrected
    shape so we don't regress."""
    balances = [_balance(10, outstanding=1850, unit_number="4B")]
    le10 = _lease(10, tenants=[(1, "Sarah", "Chen")])
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)
    api.leases.get = AsyncMock(return_value=le10)

    with _patch(_client(api), api):
        await bt._list_overdue_balances_impl()

    # The endpoint was called exactly once (no fallback triggered)
    # AND it was called with leasestatuses only — no entitytype, no
    # entityid, no balanceduration on the first pass.
    api.leases.list_outstanding_balances.assert_awaited_once()
    kwargs = api.leases.list_outstanding_balances.await_args.kwargs
    assert kwargs.get("leasestatuses") == ["Active"]
    assert "entitytype" not in kwargs
    assert "entityid" not in kwargs
    assert "balanceduration" not in kwargs


@pytest.mark.asyncio
async def test_overdue_balances_falls_back_to_total_balance_when_first_empty() -> None:
    """If the leasestatuses=Active call still returns empty, retry with
    balanceduration=TotalBalance. PR #76 — the retry still must NOT
    include ``entitytype``; widening the duration is the safe nudge."""
    balances_second = [_balance(11, outstanding=2400, days_late=60, unit_number="2A")]
    le11 = _lease(11, tenants=[(2, "Pat", "Smith")])

    call_count = {"n": 0}

    async def _fake_balances(**kwargs: Any) -> list[Any]:
        call_count["n"] += 1
        return [] if call_count["n"] == 1 else balances_second

    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(side_effect=_fake_balances)
    api.leases.get = AsyncMock(return_value=le11)

    with _patch(_client(api), api):
        result = await bt._list_overdue_balances_impl()

    assert api.leases.list_outstanding_balances.await_count == 2
    # The retry must include balanceduration=TotalBalance + the active
    # filter — and explicitly NOT include the dropped entitytype param.
    second_kwargs = api.leases.list_outstanding_balances.await_args_list[1].kwargs
    assert second_kwargs.get("balanceduration") == "TotalBalance"
    assert second_kwargs.get("leasestatuses") == ["Active"]
    assert "entitytype" not in second_kwargs
    assert "entityid" not in second_kwargs
    # And the retry's data made it into the rendered output.
    assert "Pat Smith" in result
    assert "$2,400" in result


@pytest.mark.asyncio
async def test_overdue_balances_returns_honest_failure_when_both_empty() -> None:
    """Pre-PR-#75 the agent said "everyone's paid up" when Buildium
    returned empty — even with 17 active delinquencies visible in the
    UI. That confident lie was the bug. The new path returns an
    honest "I got no rows back" so the user can escalate."""
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])
    api.leases.get = AsyncMock()

    with _patch(_client(api), api):
        result = await bt._list_overdue_balances_impl()

    assert "everyone's paid up" not in result.lower()
    assert "all current" not in result.lower()
    # The honest hedge — both branches acknowledge the uncertainty.
    assert "got no rows back" in result.lower() or "couldn't reconcile" in result.lower()


# ── Bug A residual — _get_rent_roll_impl ──────────────────────────


@pytest.mark.asyncio
async def test_rent_roll_first_balances_call_uses_leasestatuses_only() -> None:
    """Same shape fix as the overdue-balances tool — PR #76 drops
    ``entitytype="Rental"`` because Buildium 400s when entitytype is
    sent without a matching entityid. First call carries
    ``leasestatuses=["Active"]`` only."""
    leases = [_lease(1, rent=1500, tenants=[(1, "Bob", "Z")])]
    balances = [_balance(1, outstanding=120)]
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)

    with _patch(_client(api), api):
        await bt._get_rent_roll_impl()

    api.leases.list_outstanding_balances.assert_awaited_once()
    kwargs = api.leases.list_outstanding_balances.await_args.kwargs
    assert kwargs.get("leasestatuses") == ["Active"]
    assert "entitytype" not in kwargs
    assert "entityid" not in kwargs


@pytest.mark.asyncio
async def test_rent_roll_falls_back_to_total_balance_when_first_empty() -> None:
    """The rent-roll join uses the same fallback chain as the overdue
    tool — first call returns empty, retry with
    balanceduration=TotalBalance (still no entitytype per PR #76)."""
    leases = [_lease(1, rent=1500, tenants=[(1, "Bob", "Z")])]
    balances_second = [_balance(1, outstanding=200)]

    call_count = {"n": 0}

    async def _fake_balances(**kwargs: Any) -> list[Any]:
        call_count["n"] += 1
        return [] if call_count["n"] == 1 else balances_second

    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=leases)
    api.leases.list_outstanding_balances = AsyncMock(side_effect=_fake_balances)

    with _patch(_client(api), api):
        result = await bt._get_rent_roll_impl()

    assert api.leases.list_outstanding_balances.await_count == 2
    second_kwargs = api.leases.list_outstanding_balances.await_args_list[1].kwargs
    assert second_kwargs.get("balanceduration") == "TotalBalance"
    assert second_kwargs.get("leasestatuses") == ["Active"]
    assert "entitytype" not in second_kwargs
    assert "entityid" not in second_kwargs
    # The retry's $200 owed surfaces on the row.
    assert "$200" in result


# ── Bug B — ID redaction across the surface ───────────────────────


_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "Lease #",
    "Tenant #",
    "Property #",
    "Owner #",
    "Vendor #",
    "Work order #",
)
# Generic ``(#42)`` style — banned everywhere.
_FORBIDDEN_REGEX = re.compile(r"\(#\d+\)")
# A bare ``#`` followed immediately by a digit anywhere in user-facing
# text is also banned (the agent should never read "#12345" off a row).
_BARE_HASH_DIGIT = re.compile(r"#\d")


def _assert_no_id_leak(label: str, text: str) -> None:
    for phrase in _FORBIDDEN_PHRASES:
        assert phrase not in text, (
            f"{label} leaked banned phrase '{phrase}'.\n--- tool output ---\n{text}"
        )
    assert not _FORBIDDEN_REGEX.search(text), (
        f"{label} leaked (#N) style id.\n--- tool output ---\n{text}"
    )
    # Bare ``#42`` is banned EXCEPT for the internal ``[ref:N]`` token
    # which uses no hash. Anything else with ``#digit`` is a leak.
    leaked = [m.group() for m in _BARE_HASH_DIGIT.finditer(text)]
    assert leaked == [], (
        f"{label} leaked bare #digit token(s) {leaked}.\n--- tool output ---\n{text}"
    )


# Build a stable set of richly-typed fixtures, then run every tool that
# renders user-facing strings through the leak assertion. One test per
# tool keeps the failure mode obvious.


def _make_api_with_rich_fixtures() -> tuple[Any, dict[str, Any]]:
    """One mock API loaded with realistic fixtures + the underlying
    objects for ad-hoc assertions in tests."""
    api = MagicMock()
    api.rentals = MagicMock()
    api.leases = MagicMock()
    api.vendors = MagicMock()
    api.work_orders = MagicMock()

    # Properties
    prop = MagicMock()
    prop.id = 100
    prop.name = "100 Main Ave"
    prop.address = MagicMock(address_line1="100 Main Ave", city="Austin", state="TX")
    prop.rental_owner_ids = [50]
    # Owners
    owner = MagicMock()
    owner.id = 50
    owner.first_name = "Alice"
    owner.last_name = "Owner"
    owner.company_name = "Alice Owner Holdings LLC"
    owner.email = "alice@example.com"
    owner.phone_numbers = None
    owner.address = None
    owner.property_ids = [100]
    # Units
    unit = MagicMock()
    unit.id = 200
    unit.unit_number = "4B"
    unit.market_rent = 1850
    unit.address = MagicMock(address_line1="100 Main Ave", city="Austin", state="TX")
    unit.property_id = 100
    # Tenants
    sarah = MagicMock()
    sarah.id = 12345
    sarah.first_name = "Sarah"
    sarah.last_name = "Chen"
    sarah.email = "sarah@example.com"
    sarah.alternate_email = None
    sarah.phone_numbers = []
    sarah.address = None
    sarah.leases = []
    # Leases
    lease = MagicMock()
    lease.id = 5678
    lease.unit_id = 200
    lease.unit_number = "4B"
    lease.property_id = 100
    lease.rent = 1850
    lease.current_tenants = [sarah]
    lease.cosigners = []
    lease.status = "Active"
    lease.is_eviction_pending = False
    lease.lease_from_date = None
    lease.lease_to_date = None
    lease.term_type = "Fixed"
    # Vendors
    vendor = MagicMock()
    vendor.id = 7
    vendor.name = "Acme HVAC"
    vendor.company_name = "Acme HVAC LLC"
    vendor.is_active = True
    # Work orders
    wo = MagicMock()
    wo.id = 2916
    wo.title = "Leaky faucet"
    wo.status = "New"
    wo.priority = "Normal"
    wo.property_id = 100
    wo.unit_id = 200
    wo.vendor_id = 7
    wo.category_id = None
    wo.description = "Kitchen sink drips"
    # Balances
    bal = MagicMock()
    bal.lease_id = 5678
    bal.outstanding_balance = 1850
    bal.days_late = 30
    bal.unit_number = "4B"
    bal.balance_0_to_30_days = 1850
    bal.balance_31_to_60_days = 0
    bal.balance_61_to_90_days = 0
    bal.balance_over_90_days = 0
    bal.eviction_pending_date = None
    bal.is_notice_given = False

    api.rentals.list_properties = AsyncMock(return_value=[prop])
    api.rentals.get_property = AsyncMock(return_value=prop)
    api.rentals.list_owners = AsyncMock(return_value=[owner])
    api.rentals.get_owner = AsyncMock(return_value=owner)
    api.rentals.list_units = AsyncMock(return_value=[unit])
    api.rentals.get_unit = AsyncMock(return_value=unit)
    api.rentals.list_tenants = AsyncMock(return_value=[sarah])
    api.rentals.get_tenant = AsyncMock(return_value=sarah)
    api.leases.list = AsyncMock(return_value=[lease])
    api.leases.get = AsyncMock(return_value=lease)
    api.leases.list_outstanding_balances = AsyncMock(return_value=[bal])
    api.leases.list_transactions = AsyncMock(return_value=[])
    api.vendors.list = AsyncMock(return_value=[vendor])
    api.vendors.get = AsyncMock(return_value=vendor)
    api.work_orders.list = AsyncMock(return_value=[wo])
    api.work_orders.get = AsyncMock(return_value=wo)

    return api, {
        "prop": prop,
        "owner": owner,
        "unit": unit,
        "tenant": sarah,
        "lease": lease,
        "vendor": vendor,
        "wo": wo,
        "bal": bal,
    }


@pytest.mark.asyncio
async def test_no_id_leak_in_list_properties() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_properties_impl()
    _assert_no_id_leak("list_properties", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_tenants() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_tenants_impl()
    _assert_no_id_leak("list_tenants", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_tenant() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_tenant_impl("12345")
    _assert_no_id_leak("get_tenant", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_find_tenant_for_unit() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._find_tenant_for_unit_impl("4B")
    _assert_no_id_leak("find_tenant_for_unit", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_active_leases() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_active_leases_impl()
    _assert_no_id_leak("list_active_leases", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_lease() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_lease_impl(5678)
    _assert_no_id_leak("get_lease", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_owners() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_owners_impl()
    _assert_no_id_leak("list_owners", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_owner() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_owner_impl("alice")
    _assert_no_id_leak("get_owner", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_vendors() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_vendors_impl()
    _assert_no_id_leak("list_vendors", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_vendor() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_vendor_impl("acme")
    _assert_no_id_leak("get_vendor", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_open_work_orders() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_open_work_orders_impl()
    _assert_no_id_leak("list_open_work_orders", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_work_order() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_work_order_impl(2916)
    _assert_no_id_leak("get_work_order", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_work_orders_for_unit() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_work_orders_for_unit_impl("4B")
    _assert_no_id_leak("list_work_orders_for_unit", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_overdue_balances() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_overdue_balances_impl()
    _assert_no_id_leak("list_overdue_balances", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_rent_roll() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_rent_roll_impl()
    _assert_no_id_leak("get_rent_roll", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_get_property() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._get_property_impl("100")
    _assert_no_id_leak("get_property", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_upcoming_lease_expirations() -> None:
    api, fixtures = _make_api_with_rich_fixtures()
    # Give a lease a non-trivial lease_to_date so the rendering path
    # actually exercises the row formatting.
    fixtures["lease"].lease_to_date = datetime.now(UTC)
    with _patch(_client(api), api):
        text = await bt._list_upcoming_lease_expirations_impl(90)
    _assert_no_id_leak("list_upcoming_lease_expirations", text)


@pytest.mark.asyncio
async def test_no_id_leak_in_list_vacant_units() -> None:
    api, _ = _make_api_with_rich_fixtures()
    # Make the unit vacant (no active lease references it).
    api.leases.list = AsyncMock(return_value=[])
    with _patch(_client(api), api):
        text = await bt._list_vacant_units_impl()
    _assert_no_id_leak("list_vacant_units", text)


# ── Sanity — the [ref:X] internal handle still appears where chaining
# matters, so the agent can pass it into a follow-up tool call. The
# orchestrator's HARD PRESENTATION RULES paragraph is what forbids
# echoing it to the user; the *tool* layer keeps it.


@pytest.mark.asyncio
async def test_internal_ref_tag_present_on_lease_list_rows() -> None:
    api, _ = _make_api_with_rich_fixtures()
    with _patch(_client(api), api):
        text = await bt._list_active_leases_impl()
    assert "[ref:5678]" in text
