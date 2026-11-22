"""PR #78 — outstanding-balances schema correction.

The agent's "who's behind on rent?" answer has been wrong since PR #73
shipped the typed ``OutstandingBalance`` model with the wrong alias.

Brandon pulled the published Buildium OpenAPI spec from the
LeadSimple/buildium SDK and confirmed:

- The field is ``TotalBalance`` on the wire, NOT ``OutstandingBalance``.
- Several other fields PR #73 typed (``UnitNumber``, ``Status``,
  ``DaysLate``, ``RentAmount``) don't appear in the published
  ``LeaseOutstandingBalanceMessage`` shape at all.
- There IS a per-GL-account breakdown under ``Balances`` (array of
  ``OutstandingBalancesLineMessage``) that PR #73 never typed.

PR #78 corrects the schema (adds ``total_balance`` aliased to the
real ``TotalBalance``; keeps the maybe-synthesized fields
defensively until the diag confirms they really don't ship) and
flips every reader to the new field, with a back-compat fallback
to the legacy ``outstanding_balance`` so cached fixtures still parse.
This file pins both the new behavior and the back-compat path so we
don't regress the wrong way again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import OutstandingBalance
from helixis.memory.context import memory_context

# ── Shared patch helper (mirrors test_pr76_/test_pr77_) ────────────


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


# ─────────────────────────────────────────────────────────────────────
# Schema: ``TotalBalance`` is the real field
# ─────────────────────────────────────────────────────────────────────


def test_outstanding_balance_typed_alias_is_total_balance() -> None:
    """``Field(alias=...)`` introspection — pin the alias for the
    money field so a future "rename" can't silently put us back on
    the wrong wire-name."""
    fields = OutstandingBalance.model_fields
    assert "total_balance" in fields
    assert fields["total_balance"].alias == "TotalBalance"
    # The legacy alias was a hallucination — explicitly assert it
    # is NOT used by any typed field. (extra="allow" still lets
    # unknown keys round-trip on the model, but they don't populate
    # any reader path.)
    typed_aliases = {f.alias for f in fields.values() if f.alias}
    assert "OutstandingBalance" not in typed_aliases


def test_outstanding_balance_parses_spec_payload_correctly() -> None:
    """End-to-end: a spec-shaped payload populates ``total_balance``
    AND the aging buckets AND the GL breakdown."""
    payload = {
        "LeaseId": 5678,
        "PropertyId": 100,
        "UnitId": 200,
        "TotalBalance": 1850.00,
        "Balance0To30Days": 1850.00,
        "Balance31To60Days": 0.0,
        "Balance61To90Days": 0.0,
        "BalanceOver90Days": 0.0,
        "Balances": [
            {"GLAccountId": 4001, "Balance": 1500.0},
            {"GLAccountId": 4002, "Balance": 350.0},
        ],
        "EvictionPendingDate": "2026-06-01",
        "IsNoticeGiven": False,
    }
    row = OutstandingBalance.model_validate(payload)
    assert row.total_balance == 1850.00
    assert row.balance_0_to_30_days == 1850.00
    assert row.balance_over_90_days == 0.0
    assert row.balances is not None
    assert len(row.balances) == 2
    assert row.is_notice_given is False


# ─────────────────────────────────────────────────────────────────────
# _balance_of helper — strict numeric semantics
# ─────────────────────────────────────────────────────────────────────


def test_balance_of_prefers_total_balance_over_legacy() -> None:
    """The new spec field wins when both are set — the live
    Buildium API only emits ``TotalBalance``, never the legacy alias."""
    row = MagicMock()
    row.total_balance = 1850.0
    row.outstanding_balance = 999.0  # would never coexist in real data
    assert bt._balance_of(row) == 1850.0


def test_balance_of_falls_back_to_legacy_outstanding_balance() -> None:
    """A pre-PR-#78 cached row / fixture sets ``outstanding_balance``
    only. The fallback path is what keeps the old tests green during
    the migration."""

    class _Stub:
        outstanding_balance = 1200.0

    assert bt._balance_of(_Stub()) == 1200.0


def test_balance_of_rejects_non_numeric_total_balance() -> None:
    """A MagicMock auto-attribute happens to be truthy AND supports
    ``__float__`` (returns 1.0), which would silently corrupt sort
    order. ``_balance_of`` must type-check first."""
    row = MagicMock()
    # Don't touch ``total_balance`` on purpose — MagicMock will
    # auto-create one as a child MagicMock.
    row.outstanding_balance = 555.0
    # The MagicMock attr must NOT short-circuit the fallback.
    assert bt._balance_of(row) == 555.0


def test_balance_of_treats_missing_as_zero() -> None:
    class _Stub:
        pass

    assert bt._balance_of(_Stub()) == 0.0


def test_balance_of_rejects_bool_values() -> None:
    """``isinstance(True, int)`` is True in Python; explicitly reject
    bools so a boolean field never masquerades as a balance."""
    row = MagicMock()
    row.total_balance = True
    row.outstanding_balance = 720.0
    # bool rejected → fallback wins.
    assert bt._balance_of(row) == 720.0


def test_balance_of_parses_numeric_strings() -> None:
    """Some downstream tooling stringifies money fields; accept the
    string form so a legitimate "1850.00" doesn't get treated as zero."""

    class _Stub:
        total_balance = "1850.00"

    assert bt._balance_of(_Stub()) == 1850.00


# ─────────────────────────────────────────────────────────────────────
# End-to-end: ``_list_overdue_balances_impl`` reads TotalBalance
# ─────────────────────────────────────────────────────────────────────


def _spec_balance(lease_id: int, total: float, unit_number: str | None = None) -> Any:
    """Spec-shaped balance fixture — populates ``total_balance`` only
    (the way real Buildium responses do post-PR-#78). Differs from
    the legacy ``_balance`` helper in test_buildium_agent_tools.py
    that still pre-populates ``outstanding_balance`` for back-compat
    testing."""
    b = MagicMock()
    b.lease_id = lease_id
    b.total_balance = total
    # Explicitly None the legacy alias so the fallback path is NOT
    # what's being exercised — this test pins that the SPEC field
    # is what the tool reads on a real response.
    b.outstanding_balance = None
    b.days_late = None
    b.unit_number = unit_number
    b.balance_0_to_30_days = 0
    b.balance_31_to_60_days = 0
    b.balance_61_to_90_days = 0
    b.balance_over_90_days = 0
    b.eviction_pending_date = None
    b.is_notice_given = None
    return b


@pytest.mark.asyncio
async def test_overdue_balances_renders_total_balance_from_spec_field() -> None:
    """Smoke test for the full path: Buildium returns rows carrying
    ``TotalBalance`` (the schema's new alias) → the tool's filter,
    sort, and rendered output all use that value. Pre-PR-#78 this
    test would have produced ``everyone's paid up`` because the
    misnamed schema field stayed None and the filter dropped the
    row."""
    balances = [
        _spec_balance(10, 1850.00, unit_number="4B"),
        _spec_balance(11, 920.50, unit_number="2A"),
    ]
    lease_10 = MagicMock()
    lease_10.current_tenants = [_named_tenant("Sarah", "Chen")]
    lease_11 = MagicMock()
    lease_11.current_tenants = [_named_tenant("John", "Doe")]

    async def fake_get(lid: int) -> Any:
        return {10: lease_10, 11: lease_11}[lid]

    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)
    api.leases.get = AsyncMock(side_effect=fake_get)

    with _patch(_client(api), api):
        result = await bt._list_overdue_balances_impl()

    # Both rows surface, sorted highest-balance first.
    assert "2 leases with an outstanding balance" in result
    assert "Sarah Chen" in result
    assert "John Doe" in result
    assert result.index("Sarah Chen") < result.index("John Doe")
    # Dollar amounts come from ``TotalBalance``, not the legacy field.
    assert "$1,850.00" in result
    assert "$920.50" in result


@pytest.mark.asyncio
async def test_overdue_balances_zero_total_balance_row_is_filtered() -> None:
    """A row with ``TotalBalance == 0`` (Buildium occasionally returns
    these on credit-balance leases despite the spec saying it shouldn't)
    must NOT count toward the delinquency list."""
    balances = [
        _spec_balance(10, 1850.00, unit_number="4B"),
        _spec_balance(20, 0.0),  # zero — filter out
    ]
    lease_10 = MagicMock()
    lease_10.current_tenants = [_named_tenant("Sarah", "Chen")]

    async def fake_get(lid: int) -> Any:
        return {10: lease_10}[lid]

    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(return_value=balances)
    api.leases.get = AsyncMock(side_effect=fake_get)

    with _patch(_client(api), api):
        result = await bt._list_overdue_balances_impl()

    assert "1 lease with an outstanding balance" in result
    assert "Sarah Chen" in result


@pytest.mark.asyncio
async def test_rent_roll_owed_column_reads_total_balance() -> None:
    """Rent roll's "owes $X" column must use ``total_balance`` —
    pre-PR-#78 it read ``outstanding_balance``, which the wrong-alias
    schema left at None, so every row showed no balance even when
    Buildium had real data."""
    lease = MagicMock()
    lease.id = 1
    lease.rent = 1500
    lease.unit_number = "4B"
    lease.unit_id = None
    lease.current_tenants = [_named_tenant("Sarah", "Chen")]
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=[lease])
    api.leases.list_outstanding_balances = AsyncMock(return_value=[_spec_balance(1, 240.0)])

    with _patch(_client(api), api):
        result = await bt._get_rent_roll_impl()

    assert "Sarah Chen" in result
    assert "owes $240" in result


def _named_tenant(first: str, last: str) -> Any:
    t = MagicMock()
    t.first_name = first
    t.last_name = last
    t.email = None
    return t
