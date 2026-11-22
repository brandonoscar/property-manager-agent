"""One Twilio number, two inbound handlers, and one global company id.

Three defects, all in the inbound path, all found together:

1. **A number has exactly ONE "A message comes in" URL** and this deployment
   shipped two handlers that both needed it. Point it at the CRM route and a
   resident's STOP was never honoured — the TCPA violation carriers audit for.
   Point it at the consent route and no lead ever arrived. Both routes now do
   the whole job, consent first, so a misconfigured console costs nothing.

2. **``default_company_id`` was ONE id for the whole deployment.** Every
   inbound text filed into whichever company it named, so a second customer's
   residents and leads landed in the first customer's account. Routing is by
   the number the message arrived AT, and declines when nothing owns it.

3. **``skip_signature_check`` reached the real CRM inbound route**, so a
   deployment carrying the testing flag accepted any unsigned POST. Covered in
   ``test_crm_routes.py`` alongside the route it protects.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms.consent_keywords import apply_consent_keywords, classify_keyword
from helixis.comms.number_routing import (
    resolve_company_for_number,
    telephony_for_company,
)

COMPANY_A = "11111111-1111-1111-1111-111111111111"
COMPANY_B = "22222222-2222-2222-2222-222222222222"


def _pool(*, row=None, any_mapping=None, raises: bool = False):
    """A pool whose ``fetchrow`` answers the owner query then the any-row query."""
    conn = MagicMock()
    if raises:
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        conn.fetchrow = AsyncMock(side_effect=[row, any_mapping])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _twilio(default_company_id: str = ""):
    return SimpleNamespace(default_company_id=default_company_id)


# ── routing by the receiving number ──────────────────────────────────


@pytest.mark.asyncio
async def test_the_owning_company_is_resolved_from_the_to_number() -> None:
    pool = _pool(row={"company_id": COMPANY_A})
    got = await resolve_company_for_number(pool, "+18005551212", twilio_settings=_twilio())
    assert got == COMPANY_A


@pytest.mark.asyncio
async def test_an_unowned_number_declines_when_other_numbers_are_mapped() -> None:
    """The cross-tenant guarantee. Company A has mapped its number; a text to
    an unmapped one must route NOWHERE rather than fall back to a default and
    land in A's account."""
    pool = _pool(row=None, any_mapping={"?column?": 1})
    got = await resolve_company_for_number(pool, "+19998887777", twilio_settings=_twilio(COMPANY_A))
    assert got is None


@pytest.mark.asyncio
async def test_the_legacy_default_applies_only_when_nothing_is_mapped() -> None:
    """A deployment with an empty mapping table and a default set IS
    single-tenant, so the old behaviour is preserved. Both directions matter:
    this must work, and the test above must keep failing closed once one row
    exists."""
    pool = _pool(row=None, any_mapping=None)
    got = await resolve_company_for_number(pool, "+18005551212", twilio_settings=_twilio(COMPANY_B))
    assert got == COMPANY_B


@pytest.mark.asyncio
async def test_no_mapping_and_no_default_routes_nowhere() -> None:
    pool = _pool(row=None, any_mapping=None)
    assert await resolve_company_for_number(pool, "+18005551212", twilio_settings=_twilio()) is None


@pytest.mark.asyncio
async def test_a_lookup_failure_declines_rather_than_guessing() -> None:
    """Fails CLOSED. An unreadable mapping cannot establish ownership, and
    routing on the assumption that it would have matched is the same mistake
    as routing on a guess (gotcha 55)."""
    pool = _pool(raises=True)
    got = await resolve_company_for_number(pool, "+18005551212", twilio_settings=_twilio(COMPANY_A))
    assert got is None


@pytest.mark.asyncio
async def test_a_missing_to_number_routes_nowhere() -> None:
    assert await resolve_company_for_number(_pool(), "", twilio_settings=_twilio(COMPANY_A)) is None


# ── the consent keywords both routes now share ───────────────────────


@pytest.mark.parametrize(
    "body,expected",
    [
        ("STOP", "stop"),
        ("  stop  ", "stop"),
        ("Opt Out", "stop"),
        ("opt-out", "stop"),
        ("UNSUBSCRIBE", "stop"),
        ("START", "start"),
        ("HELP", "help"),
        ("is the unit still available?", None),
        ("", None),
    ],
)
def test_keyword_classification_survives_the_normalisation(body: str, expected) -> None:
    assert classify_keyword(body) == expected


@pytest.mark.asyncio
async def test_stop_revokes_and_start_never_creates_consent() -> None:
    """Pinned because these two statements moved modules. STOP revokes live
    rows; START touches only previously-revoked ones — no prior relationship
    means there is no consent to restore."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")

    # ⚠ Selected by TABLE, not by "the last call". Each keyword now writes two
    # statements — the consent ledger AND the opt-out record that covers
    # somebody who never consented — so reading `await_args` alone silently
    # started asserting against the wrong one.
    def _ledger_sql() -> str:
        matching = [
            c.args[0] for c in pool.execute.await_args_list if "helixis_sms_consent" in c.args[0]
        ]
        assert matching, "stopped touching the consent ledger entirely"
        return matching[-1]

    assert await apply_consent_keywords(pool, from_number="+14155551234", body="STOP") == "stop"
    stop_sql = _ledger_sql()
    assert "revoked_at = now()" in stop_sql
    assert "revoked_at IS NULL" in stop_sql

    assert await apply_consent_keywords(pool, from_number="+14155551234", body="START") == "start"
    start_sql = _ledger_sql()
    assert "revoked_at IS NOT NULL" in start_sql, (
        "START must restore only previously-revoked rows — a bare UPDATE would "
        "manufacture consent nobody gave"
    )


@pytest.mark.asyncio
async def test_a_non_keyword_message_touches_no_ledger() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    assert await apply_consent_keywords(pool, from_number="+1415", body="hi there") is None
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_changes_no_ledger() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock()
    assert await apply_consent_keywords(pool, from_number="+1415", body="HELP") == "help"
    pool.execute.assert_not_awaited()


# ── the OUTBOUND half: whose number do we send FROM ───────────────────
#
# 0082 fixed inbound attribution. Every outbound path stayed on the
# deployment-global ``from_number``, which is the worse leak because it is
# BIDIRECTIONAL: company B texts its tenant from company A's number, the tenant
# replies to the number they can see, that reply lands on A's number, and
# inbound routing — correctly — files it under A.


def _telephony_pool(row=None, any_mapping=None, raises: bool = False):
    conn = MagicMock()
    if raises:
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        conn.fetchrow = AsyncMock(side_effect=[row, any_mapping])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _twilio_full(**over):
    base = {
        "default_company_id": "",
        "from_number": "+18885550000",
        "messaging_service_sid": "",
        "forward_to_number": "+15125550000",
        "voice_greeting": "global greeting",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_a_company_sends_from_its_own_number() -> None:
    pool = _telephony_pool(
        row={
            "phone_number": "+19995551111",
            "forward_to_number": "+13105559999",
            "voice_greeting": "Blue Ridge here",
        }
    )
    got = await telephony_for_company(pool, COMPANY_A, twilio_settings=_twilio_full())
    assert got is not None
    assert got.from_number == "+19995551111"
    assert got.forward_to_number == "+13105559999"
    assert got.voice_greeting == "Blue Ridge here"
    # ⚠ NOT the global values. That substitution is the whole bug.
    assert got.from_number != "+18885550000"
    assert got.forward_to_number != "+15125550000"


@pytest.mark.asyncio
async def test_a_company_with_no_number_cannot_send_at_all() -> None:
    """Refusing is the point. Falling back to a shared sender would put this
    company's message in another company's conversation and route the reply
    there too."""
    pool = _telephony_pool(row=None, any_mapping={"?column?": 1})
    assert await telephony_for_company(pool, COMPANY_B, twilio_settings=_twilio_full()) is None


@pytest.mark.asyncio
async def test_the_global_config_applies_only_on_an_empty_table() -> None:
    """Single-tenant deployments keep working unchanged — a deployment with no
    mapped numbers has exactly one company's worth of telephony config."""
    pool = _telephony_pool(row=None, any_mapping=None)
    got = await telephony_for_company(pool, COMPANY_A, twilio_settings=_twilio_full())
    assert got is not None
    assert got.from_number == "+18885550000"
    assert got.forward_to_number == "+15125550000"


@pytest.mark.asyncio
async def test_a_lookup_failure_refuses_to_send() -> None:
    """Fails CLOSED, same as the inbound half: an unverifiable identity is no
    identity, and guessing here sends from someone else's number."""
    pool = _telephony_pool(raises=True)
    assert await telephony_for_company(pool, COMPANY_A, twilio_settings=_twilio_full()) is None


@pytest.mark.asyncio
async def test_a_messaging_service_is_never_used_as_a_company_sender() -> None:
    """A Messaging Service is an ACCOUNT-level pool — Twilio picks the sender
    from it, which is precisely the shared-sender problem. It stays available
    to the single-tenant path and must not leak into a mapped company's
    identity."""
    pool = _telephony_pool(
        row={"phone_number": "+19995551111", "forward_to_number": None, "voice_greeting": None}
    )
    got = await telephony_for_company(
        pool, COMPANY_A, twilio_settings=_twilio_full(messaging_service_sid="MG123")
    )
    assert got is not None
    assert got.messaging_service_sid == ""
    assert got.from_number == "+19995551111"


@pytest.mark.asyncio
async def test_an_empty_company_id_never_resolves() -> None:
    assert (
        await telephony_for_company(_telephony_pool(), "", twilio_settings=_twilio_full()) is None
    )
