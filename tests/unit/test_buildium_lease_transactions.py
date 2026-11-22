"""Lease-ledger writes — charge + payment (both live), with the one-letter
casing that separates them.

The whole trap (gotcha 21): charge lines use ``GlAccountId`` (lower-l), payment
lines use ``GLAccountId`` (capital-L). Both are confirm-gated + idempotency-
claimed; payment ALSO carries a Manager-or-Admin role floor (fail-closed)
because it's immutable. Harness mirrors ``test_buildium_close_work_order``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context

_LEASE = 99799
_GL = 3


def _api() -> tuple[Any, Any, AsyncMock, AsyncMock]:
    charge = AsyncMock(return_value={"Id": 871446, "TotalAmount": 50.0})
    payment = AsyncMock(return_value={"Id": 900001, "TotalAmount": -50.0})
    api_mock = MagicMock()
    api_mock.leases = MagicMock()
    api_mock.leases.post_charge = charge
    api_mock.leases.record_payment = payment
    api_mock.leases.renew_lease = AsyncMock(return_value={"Id": 99800})
    api_mock.leases.record_move_out = AsyncMock(return_value={"Id": 99799})
    # The confirm card derives its labels from the RECORD (_gate_label), so the
    # lease read has to be mocked or every card falls back to the id form. The
    # tenant here is Elena — deliberately NOT the "J. Rivera" the tests pass as
    # lease_label, so a card that renders the model's string is visibly wrong.
    tenant = MagicMock(id=12)
    tenant.first_name, tenant.last_name = "Elena", "Vasquez"
    lease_rec = MagicMock(id=99799, unit_number="4B", current_tenants=[tenant])
    api_mock.leases.get = AsyncMock(return_value=lease_rec)
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, charge, payment


def _enter_patches(
    stack: ExitStack, client_mock: Any, api_mock: Any, *, role: str = "admin"
) -> None:
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(
        patch.object(bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object()))
    )
    stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client_mock))
    stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api_mock))
    # The payment tool floors on team role via a DB lookup; there's no pool in
    # unit tests, so stub the caller's role (default: an admin who may record).
    stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)))


async def _next_confirm_payload() -> dict:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload was emitted by the write tool")


# ── registration: charge / payment / move-out live, renewal dark ──────


def test_charge_payment_moveout_live_renewal_dark() -> None:
    names = {t.name for t in bt.buildium_tools()}
    # Live writes: charge (editable), move-out (reversible), payment (immutable
    # but role-floored + confirm-gated), all prod-probed.
    for live in ("buildium_post_charge", "buildium_record_move_out", "buildium_record_payment"):
        assert live in names, f"{live} should be registered live"
    # Renewal stays DARK: it creates a real lease term with no clean undo.
    assert "buildium_renew_lease" not in names, "renew_lease should stay dark (irreversible)"


# ── charge (live) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_charge_approve_posts_with_lower_l_casing() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._post_charge_impl(
                _LEASE,
                50.0,
                _GL,
                date="2026-08-01",
                memo="August late fee",
                lease_label="Unit 4B — J. Rivera",
                gl_account_label="Late Fee Income",
            )
        )
        payload = await _next_confirm_payload()
        charge.assert_not_called()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    charge.assert_awaited_once()
    assert charge.await_args.args[0] == _LEASE
    line = charge.await_args.kwargs["lines"][0]
    assert line["GlAccountId"] == _GL  # lower-l for a CHARGE
    assert "GLAccountId" not in line
    assert line["Amount"] == 50.0
    assert charge.await_args.kwargs["date"] == "2026-08-01"
    assert "$50" in result and str(_LEASE) not in result


@pytest.mark.asyncio
async def test_charge_confirm_card_shows_labels_not_ids() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._post_charge_impl(
                _LEASE,
                50.0,
                _GL,
                date="2026-08-01",
                lease_label="Unit 4B — J. Rivera",
                gl_account_label="Late Fee Income",
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=1.0)
    items = " | ".join(payload["items"])
    # ⚠ REWRITTEN 2026-08-24. This used to assert the SUPPLIED labels appear —
    # i.e. it pinned the vulnerability as a requirement. The card must render
    # the RECORD: the fixture's lease belongs to Elena Vasquez, and the caller
    # above passes "J. Rivera", so the model's string must lose.
    assert "Elena Vasquez at unit 4B" in items, items
    assert "J. Rivera" not in items, (
        "the confirm card rendered the model-supplied lease_label — that is the "
        "Whitfield misattribution: the human approves a name the record does not say"
    )
    # The GL account has no getter, so it honestly falls back to the id form
    # rather than echoing "Late Fee Income" on the model's say-so.
    assert f"gl account {_GL}" in items.lower(), items
    assert "Late Fee Income" not in items


@pytest.mark.asyncio
async def test_charge_deny_posts_nothing() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(bt._post_charge_impl(_LEASE, 50.0, _GL, date="2026-08-01"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    charge.assert_not_called()
    assert "didn't post" in result.lower() or "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_charge_rejects_bad_amount_and_date() -> None:
    client, api, charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        neg = await bt._post_charge_impl(_LEASE, -5.0, _GL, date="2026-08-01")
        baddate = await bt._post_charge_impl(_LEASE, 50.0, _GL, date="Aug 1")
    charge.assert_not_called()
    assert "positive" in neg.lower()
    assert "yyyy-mm-dd" in baddate.lower()


@pytest.mark.asyncio
async def test_charge_role_floor_denies_member() -> None:
    """A Member cannot post a charge — refused BEFORE any confirm gate or
    Buildium call. A charge is editable but NOT deletable (gotcha 21)."""
    client, api, charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api, role="member")
        result = await bt._post_charge_impl(_LEASE, 50.0, _GL, date="2026-08-01")
    charge.assert_not_called()
    assert "manager or admin" in result.lower()


@pytest.mark.asyncio
async def test_charge_role_floor_fails_closed_when_role_unknown() -> None:
    """Unverifiable role (no pool / no row → None) refuses the charge."""
    client, api, charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=None)))
        result = await bt._post_charge_impl(_LEASE, 50.0, _GL, date="2026-08-01")
    charge.assert_not_called()
    assert "manager or admin" in result.lower()


# ── payment (live — confirm gate + role floor) ───────────────────────


@pytest.mark.asyncio
async def test_payment_approve_records_with_capital_l_casing() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._record_payment_impl(
                _LEASE,
                50.0,
                _GL,
                date="2026-08-01",
                payment_method="Check",
                lease_label="Unit 4B — J. Rivera",
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    payment.assert_awaited_once()
    line = payment.await_args.kwargs["lines"][0]
    assert line["GLAccountId"] == _GL  # capital-L for a PAYMENT
    assert "GlAccountId" not in line
    assert payment.await_args.kwargs["payment_method"] == "Check"
    assert payment.await_args.kwargs["send_email_receipt"] is False
    assert "$50" in result


@pytest.mark.asyncio
async def test_payment_rejects_bad_method_and_amount() -> None:
    client, api, _charge, payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        badmethod = await bt._record_payment_impl(
            _LEASE, 50.0, _GL, date="2026-08-01", payment_method="Venmo"
        )
        badamt = await bt._record_payment_impl(
            _LEASE, 0.0, _GL, date="2026-08-01", payment_method="Check"
        )
    payment.assert_not_called()
    assert "must be one of" in badmethod.lower()
    assert "positive" in badamt.lower()


@pytest.mark.asyncio
async def test_payment_role_floor_denies_member() -> None:
    """A Member cannot record a payment — the tool refuses BEFORE any confirm
    gate or Buildium call (immutable write, so the floor is the guard)."""
    client, api, _charge, payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api, role="member")
        result = await bt._record_payment_impl(
            _LEASE, 50.0, _GL, date="2026-08-01", payment_method="Check"
        )
    payment.assert_not_called()
    assert "manager or admin" in result.lower()


@pytest.mark.asyncio
async def test_payment_role_floor_fails_closed_when_role_unknown() -> None:
    """If the caller's role can't be verified (no pool / no row → None), the
    payment is refused, never allowed — fail closed on an irreversible write."""
    client, api, _charge, payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=None)))
        result = await bt._record_payment_impl(
            _LEASE, 50.0, _GL, date="2026-08-01", payment_method="Check"
        )
    payment.assert_not_called()
    assert "manager or admin" in result.lower()


@pytest.mark.asyncio
async def test_payment_deny_records_nothing() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._record_payment_impl(_LEASE, 50.0, _GL, date="2026-08-01", payment_method="Check")
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=1.0)
    payment.assert_not_called()
    assert "didn't record" in result.lower() or "nothing was changed" in result.lower()


# ── lifecycle: renew + move-out (dark, tested via _impl) ─────────────


@pytest.mark.asyncio
async def test_renew_approve_shapes_nested_rent() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._renew_lease_impl(
                _LEASE,
                "Fixed",
                1850.0,
                _GL,
                "2026-09-01",
                cycle="Monthly",
                lease_label="Unit 4B — J. Rivera",
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    api.leases.renew_lease.assert_awaited_once()
    kw = api.leases.renew_lease.await_args.kwargs
    assert kw["lease_type"] == "Fixed"
    assert kw["send_welcome_email"] is False
    charge = kw["rent"]["Charges"][0]
    assert charge == {"Amount": 1850.0, "GlAccountId": _GL, "NextDueDate": "2026-09-01"}
    assert kw["rent"]["Cycle"] == "Monthly"
    assert "Renewed" in result


@pytest.mark.asyncio
async def test_renew_rejects_bad_type_amount_date() -> None:
    client, api, _charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        badtype = await bt._renew_lease_impl(_LEASE, "Weekly", 1850.0, _GL, "2026-09-01")
        badamt = await bt._renew_lease_impl(_LEASE, "Fixed", 0.0, _GL, "2026-09-01")
        baddate = await bt._renew_lease_impl(_LEASE, "Fixed", 1850.0, _GL, "Sept")
    api.leases.renew_lease.assert_not_called()
    assert "must be one of" in badtype.lower()
    assert "positive" in badamt.lower()
    assert "yyyy-mm-dd" in baddate.lower()


@pytest.mark.asyncio
async def test_move_out_approve_posts_tenant_and_date() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        task = asyncio.create_task(
            bt._record_move_out_impl(
                _LEASE,
                12,
                "2026-08-31",
                tenant_label="J. Rivera",
                lease_label="Unit 4B",
            )
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=1.0)
    api.leases.record_move_out.assert_awaited_once()
    kw = api.leases.record_move_out.await_args.kwargs
    assert kw["tenant_id"] == 12 and kw["move_out_date"] == "2026-08-31"
    items = " | ".join(payload["items"])
    # ⚠ REWRITTEN 2026-08-24 — see the charge test. Tenant 12 IS Elena on the
    # fixture lease; the caller passes "J. Rivera". The card names whoever the
    # write actually targets, which is the whole point of the fix.
    assert "Elena Vasquez" in items, items
    assert "J. Rivera" not in items, (
        "the card rendered the model-supplied label beside a contradicting id"
    )
    assert "move-out" in result.lower()


@pytest.mark.asyncio
async def test_move_out_rejects_bad_date_and_deny() -> None:
    await agent_tools.drain_pending_confirmations()
    client, api, _charge, _payment = _api()
    with ExitStack() as stack:
        _enter_patches(stack, client, api)
        baddate = await bt._record_move_out_impl(_LEASE, 12, "Aug 31")
        task = asyncio.create_task(bt._record_move_out_impl(_LEASE, 12, "2026-08-31"))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        denied = await asyncio.wait_for(task, timeout=1.0)
    api.leases.record_move_out.assert_not_called()
    assert "yyyy-mm-dd" in baddate.lower()
    assert "didn't record" in denied.lower() or "nothing was changed" in denied.lower()
