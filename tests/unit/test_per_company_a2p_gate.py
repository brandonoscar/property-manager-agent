"""A2P approval is per COMPANY. ``twilio.enabled`` is one flag for the deploy.

``outbound_channel_block``'s own docstring calls that flag *"carriers have
approved the campaign"* — **singular** — because it was written when there was
one. Under white-label registration each customer gets their own brand and
campaign, approved on their own date, so a single global flag is wrong in both
directions:

* leave it off and a customer whose campaign cleared cannot text;
* turn it on and every customer still in carrier review starts sending.

The second is the violation carriers audit for, and it is what a global flag
produces by default the moment a second customer exists.

⚠ Both outbound lanes read ONE predicate. Two lanes send from the same
registered number, and gotcha 67 is exactly this repo shipping a rule that
reached ``send_sms`` and not the CRM reply route.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms.sms import outbound_block_for_company, outbound_channel_block

_ROOT = Path(__file__).resolve().parents[2]
_CRM = _ROOT / "src" / "helixis" / "api" / "routes" / "crm.py"

_APPROVED_ACCOUNT = SimpleNamespace(enabled=True, account_sid="AC1", auth_token="tok")
_DARK_ACCOUNT = SimpleNamespace(enabled=False, account_sid="AC1", auth_token="tok")


def _pool(row: dict[str, Any] | None, *, raises: bool = False) -> Any:
    """The gate reads ``pool.fetchrow`` directly, like every other query in
    ``comms/sms.py`` — deliberately, so a caller's fixture has one shape to
    model rather than two."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down") if raises else None)
    if not raises:
        pool.fetchrow.return_value = row
    return pool


# ── the gap this closes ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_company_still_in_carrier_review_cannot_send() -> None:
    """⚠ THE FIX. Customer C registered, the carriers have not cleared them,
    and customer B's approval flipped the deployment flag on. Before this,
    C sent."""
    block = await outbound_block_for_company(
        _pool({"status": "pending"}), "company-c", _APPROVED_ACCOUNT
    )
    assert block == "campaign_pending"


@pytest.mark.asyncio
async def test_an_approved_company_sends() -> None:
    assert (
        await outbound_block_for_company(
            _pool({"status": "approved"}), "company-b", _APPROVED_ACCOUNT
        )
        is None
    )


@pytest.mark.asyncio
async def test_every_non_approved_status_blocks() -> None:
    """Only ``approved`` is approval. A failed registration is not a
    permission, and neither is an in-flight one."""
    for status in ("queued", "submitted", "in_review", "failed", "", "APPROVED_LATER"):
        assert (
            await outbound_block_for_company(_pool({"status": status}), "co", _APPROVED_ACCOUNT)
            == "campaign_pending"
        ), status


# ── the single-tenant path this must not break ────────────────────────


@pytest.mark.asyncio
async def test_a_company_that_never_registered_falls_back_to_the_global_flag() -> None:
    """⚠ The operator's own company. They registered directly with Twilio, so
    they have no row in a table built for the white-label flow, and blocking
    them would break the deployment that exists today.

    This is not a bypass: the only way to get telephony without registering is
    the operator configuring a number by hand, and a company provisioned by
    ``ensure_number`` ALWAYS has a row — that step only runs on an approved
    one."""
    assert await outbound_block_for_company(_pool(None), "operator", _APPROVED_ACCOUNT) is None


@pytest.mark.asyncio
async def test_the_account_level_check_runs_first_and_its_wording_wins() -> None:
    """⚠ On a dark deployment the honest message is "texting is not enabled",
    NOT "your campaign is pending" — telling a customer to wait on the
    carriers when the operator has not turned the channel on sends them to
    wait for something that will never arrive. Same ordering reason as
    ``outbound_channel_block``'s dark-before-unconfigured."""
    assert (
        await outbound_block_for_company(_pool({"status": "pending"}), "co", _DARK_ACCOUNT)
        == "disabled"
    )
    # And it short-circuits: a dark deployment must not even query.
    pool = _pool({"status": "approved"})
    await outbound_block_for_company(pool, "co", _DARK_ACCOUNT)
    pool.fetchrow.assert_not_awaited()


# ── failing closed ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unreadable_registration_refuses() -> None:
    """Same posture as the consent ledger and the role floor: unverifiable is
    invalid. A compliance gate that fails OPEN on a database blip is not a
    gate."""
    assert (
        await outbound_block_for_company(_pool(None, raises=True), "co", _APPROVED_ACCOUNT)
        == "campaign_pending"
    )


@pytest.mark.asyncio
async def test_no_company_refuses() -> None:
    assert (
        await outbound_block_for_company(_pool(None), "", _APPROVED_ACCOUNT) == "campaign_pending"
    )


# ── the account-level predicate is unchanged ──────────────────────────


def test_the_channel_predicate_still_answers_only_the_account_question() -> None:
    """The per-company gate WRAPS it rather than replacing it — a sender is
    still deliberately not checked here (gotcha 69: requiring a global
    ``from_number`` would block every company on a correct multi-tenant
    install)."""
    assert outbound_channel_block(_DARK_ACCOUNT) == "disabled"
    assert outbound_channel_block(_APPROVED_ACCOUNT) is None
    assert (
        outbound_channel_block(SimpleNamespace(enabled=True, account_sid="", auth_token=""))
        == "not_configured"
    )


# ── both lanes, one rule (gotcha 67) ──────────────────────────────────


def test_the_crm_reply_lane_uses_the_same_predicate() -> None:
    """⚠ A behavioural test of the predicate cannot see a caller that ignores
    it (gotcha 62), and this exact route is where gotcha 67 happened: it
    checked ``configured`` while ``send_sms`` checked ``enabled``, so the
    Leasing lane would have texted real leads pre-approval."""
    tree = ast.parse(_CRM.read_text(encoding="utf-8"))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "outbound_block_for_company" in called, (
        "the CRM reply lane no longer consults the per-company gate — a "
        "customer in carrier review can text their leads"
    )
    assert "outbound_channel_block" not in called, (
        "the CRM lane is back on the ACCOUNT-level predicate alone, which "
        "cannot see that this company's own campaign is still pending"
    )


def test_send_sms_uses_it_too() -> None:
    src = (_ROOT / "src" / "helixis" / "comms" / "sms.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_sms"
    )
    called = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "outbound_block_for_company" in called
    assert "outbound_channel_block" not in called, (
        "send_sms went back to the account-level check only"
    )


def test_the_pending_reason_has_a_message() -> None:
    """A blocked send raises ``SmsBlocked(reason, _CHANNEL_BLOCK_DETAIL[reason])``
    — a reason with no entry is a KeyError on the refusal path, which turns a
    clean block into a 500."""
    from helixis.comms.sms import _CHANNEL_BLOCK_DETAIL

    assert "campaign_pending" in _CHANNEL_BLOCK_DETAIL
    assert _CHANNEL_BLOCK_DETAIL["campaign_pending"].strip()
