"""A STOP from somebody who never consented — the record that had nowhere to go.

``apply_consent_keywords`` mirrors STOP with an UPDATE over
``helixis_sms_consent``. Every CRM lead has no row there (no code path writes
one), so ``WHERE phone = $1`` matched nothing, the STOP left no trace anywhere,
and the Leasing lane kept texting them. That is the violation carriers audit
for, and with several customers each working a pipeline it is a matter of
weeks.

Migration 0086 is the record that does not need a prior grant to exist.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms.consent_keywords import apply_consent_keywords
from helixis.comms.opt_out import (
    OPTED_OUT,
    QUIET_HOURS,
    UNSUPPORTED_REGION,
    clear_opt_out,
    has_opted_out,
    recipient_block,
    record_opt_out,
    spellings,
)
from helixis.comms.phone import normalize_e164

_ROOT = Path(__file__).resolve().parents[2]
COMPANY = "11111111-1111-1111-1111-111111111111"
LEAD = "+12125551234"  # New York, so the clock below is unambiguous
#: 14:00 Eastern — comfortably inside every window, so a quiet-hours block in
#: these tests can only come from the opt-out logic under test.
MIDDAY = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)


def _pool(*, row: Any = None, raises: bool = False) -> MagicMock:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down") if raises else None)
    if not raises:
        pool.fetchrow.return_value = row
    pool.execute = AsyncMock(return_value="UPDATE 0")
    return pool


# ── the lookup ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_number_that_said_stop_is_blocked() -> None:
    assert await has_opted_out(_pool(row={"?column?": 1}), LEAD) is True


@pytest.mark.asyncio
async def test_a_number_that_never_did_is_not() -> None:
    assert await has_opted_out(_pool(row=None), LEAD) is False


@pytest.mark.asyncio
async def test_an_unreadable_table_blocks() -> None:
    """⚠ Fails CLOSED. "We could not check whether they opted out" must never
    resolve to "so we sent it" — the same posture as the consent ledger and
    the role floor."""
    assert await has_opted_out(_pool(raises=True), LEAD) is True


@pytest.mark.asyncio
async def test_no_phone_blocks() -> None:
    assert await has_opted_out(_pool(row=None), "") is True


@pytest.mark.asyncio
async def test_the_lookup_ignores_resumed_rows() -> None:
    """A START must be honourable, or somebody who changed their mind is
    unreachable forever with no way for anyone to fix it."""
    pool = _pool(row=None)
    await has_opted_out(pool, LEAD)
    sql = " ".join(pool.fetchrow.await_args.args[0].split())
    assert "resumed_at IS NULL" in sql


@pytest.mark.asyncio
async def test_the_lookup_is_not_company_scoped() -> None:
    """⚠ Deliberate, and gotcha 47 requires it stated: one STOP silences every
    sender. An over-broad revocation is a customer-service problem, a missed
    one is a statutory penalty — the same call ``apply_consent_keywords``
    already made for the ledger."""
    pool = _pool(row=None)
    await has_opted_out(pool, LEAD)
    sql = " ".join(pool.fetchrow.await_args.args[0].split())
    assert "company_id" not in sql
    # The parameter is the number's SPELLINGS, not the raw string — the point
    # of this assertion is that no company narrows the query, so it checks the
    # number is in there rather than that it is alone.
    (candidates,) = pool.fetchrow.await_args.args[1:]
    assert LEAD in candidates


# ── writing it ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recording_keeps_the_message_as_evidence() -> None:
    """A TCPA defense is a contemporaneous record. "The system says they opted
    out" is not one; Twilio's own SID for the inbound message is."""
    pool = _pool()
    assert await record_opt_out(
        pool, company_id=COMPANY, phone=LEAD, evidence_sid="SM1", evidence_body="STOP"
    )
    args = pool.execute.await_args.args
    assert args[1:] == (COMPANY, LEAD, "inbound_keyword", "SM1", "STOP")


@pytest.mark.asyncio
async def test_a_repeat_stop_does_not_move_the_original_date() -> None:
    """⚠ ``opted_out_at`` is the evidence of how long we had been told to
    stop. Refreshing it on every repeat quietly shortens that record, so the
    upsert only fires on a row that had been RESUMED."""
    pool = _pool()
    await record_opt_out(pool, company_id=COMPANY, phone=LEAD)
    sql = " ".join(pool.execute.await_args.args[0].split())
    assert "WHERE public.helixis_sms_opt_outs.resumed_at IS NOT NULL" in sql, (
        "a second STOP would overwrite the original opt-out timestamp"
    )


@pytest.mark.asyncio
async def test_a_failed_write_never_raises_into_the_webhook() -> None:
    """Twilio retries a non-2xx, which would re-run the whole delivery. The
    write returns False and logs loudly instead."""
    pool = _pool()
    pool.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await record_opt_out(pool, company_id=COMPANY, phone=LEAD) is False


@pytest.mark.asyncio
async def test_start_lifts_the_block_across_companies() -> None:
    """⚠ Blocking everywhere but resuming only where the START landed would
    leave the person permanently unreachable from every other company — the
    exact failure the resumed column exists to prevent."""
    pool = _pool()
    assert await clear_opt_out(pool, LEAD) is True
    sql = " ".join(pool.execute.await_args.args[0].split())
    assert "SET resumed_at = now()" in sql
    assert "company_id" not in sql


# ── the combined recipient gate ───────────────────────────────────────


@pytest.mark.asyncio
async def test_opt_out_outranks_quiet_hours() -> None:
    """Telling somebody who asked to be left alone that we will text them
    later is worse than the original message."""
    three_am = datetime(2026, 6, 17, 7, 0, tzinfo=UTC)  # 03:00 Eastern
    assert await recipient_block(_pool(row={"?column?": 1}), phone=LEAD, now=three_am) == OPTED_OUT


@pytest.mark.asyncio
async def test_a_reachable_person_at_a_legal_hour_is_not_blocked() -> None:
    assert await recipient_block(_pool(row=None), phone=LEAD, now=MIDDAY) is None


@pytest.mark.asyncio
async def test_three_in_the_morning_is_blocked() -> None:
    three_am = datetime(2026, 6, 17, 7, 0, tzinfo=UTC)  # 03:00 Eastern
    assert await recipient_block(_pool(row=None), phone=LEAD, now=three_am) == QUIET_HOURS


@pytest.mark.asyncio
async def test_a_number_we_cannot_reach_says_so_rather_than_naming_the_hour() -> None:
    """⚠ A DIFFERENT reason from quiet hours, and the wording is the point.

    "Outside their local window, try later" is the wrong instruction for a
    number this product can never text — it sends an agent to wait for
    something that will not arrive (gotchas 39/46: a message written for one
    reader landing on another, the same split `outbound_channel_block` makes
    between dark and unconfigured).

    It is also not merely academic: the clock answer for a foreign number was
    computed against US zones, so 19:59 Eastern read as legal for somebody in
    London at 00:59.
    """
    uk = "+442071234567"
    assert await recipient_block(_pool(row=None), phone=uk, now=MIDDAY) == UNSUPPORTED_REGION


@pytest.mark.asyncio
async def test_opt_out_still_outranks_an_unreachable_number() -> None:
    """Both are refusals, so the order only shows in the WORDING — and
    somebody who asked to be left alone is told that, not that their country
    is unsupported."""
    uk = "+442071234567"
    assert await recipient_block(_pool(row={"?column?": 1}), phone=uk, now=MIDDAY) == OPTED_OUT


# ── the inbound half: a lead's STOP is recorded at all ────────────────


@pytest.mark.asyncio
async def test_a_lead_with_no_consent_row_still_gets_an_opt_out_record() -> None:
    """⚠ THE GAP. The ledger UPDATE reports ``UPDATE 0`` for a lead — nobody
    ever writes them a consent row — so before this the STOP vanished."""
    pool = _pool()
    assert (
        await apply_consent_keywords(
            pool, from_number=LEAD, body="STOP", company_id=COMPANY, message_sid="SM9"
        )
        == "stop"
    )
    statements = [" ".join(c.args[0].split()) for c in pool.execute.await_args_list]
    assert any("helixis_sms_consent" in s for s in statements), "stopped updating the ledger"
    assert any("helixis_sms_opt_outs" in s for s in statements), (
        "a lead's STOP was not recorded anywhere — the Leasing lane will keep texting them"
    )


@pytest.mark.asyncio
async def test_start_clears_both_records() -> None:
    pool = _pool()
    assert await apply_consent_keywords(pool, from_number=LEAD, body="START") == "start"
    statements = [" ".join(c.args[0].split()) for c in pool.execute.await_args_list]
    assert any("helixis_sms_consent" in s for s in statements)
    assert any("resumed_at = now()" in s for s in statements), (
        "START restored consent but left the opt-out record blocking — the "
        "person is unreachable forever"
    )


@pytest.mark.asyncio
async def test_an_ordinary_message_writes_nothing() -> None:
    pool = _pool()
    assert await apply_consent_keywords(pool, from_number=LEAD, body="is 4B still open?") is None
    pool.execute.assert_not_awaited()


# ── the call sites (gotcha 62: a predicate nothing calls is the bug) ──


def _calls_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_the_crm_reply_route_checks_the_recipient() -> None:
    """⚠ A behavioural test cannot see a caller that skips the gate when the
    fixture happens to be reachable — and this lane went live with no opt-out
    check and no clock at all."""
    assert "recipient_block" in _calls_in(_ROOT / "src/helixis/api/routes/crm.py"), (
        "the Leasing reply route no longer checks whether this person opted "
        "out or whether it is a legal hour where they are"
    )


@pytest.mark.parametrize(
    "route", ["src/helixis/api/routes/crm.py", "src/helixis/api/routes/twilio.py"]
)
def test_both_inbound_routes_pass_the_receiving_company(route: str) -> None:
    """⚠ Without ``company_id`` the opt-out record cannot be written and the
    keyword handler silently degrades to the ledger-only behaviour that missed
    every lead. A Twilio number has ONE inbound URL, so whichever route the
    console names has to do the whole job (gotcha 68)."""
    src = (_ROOT / route).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_consent_keywords"
    ]
    assert calls, f"{route} no longer applies consent keywords at all"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "company_id" in kwargs, (
            f"{route} calls apply_consent_keywords without the receiving "
            "company, so a lead's STOP is recorded nowhere"
        )


# ── one number, however it was typed ──────────────────────────────────


def test_the_spellings_of_one_number_all_denote_that_number() -> None:
    """Twilio writes ``+14155550134``; a manager typing a lead into the CRM
    form writes whatever they type. An exact string compare never met them, so
    a STOP recorded from an inbound text did not stop the outbound reply."""
    typed = spellings("(415) 555-0134")
    assert "+14155550134" in typed, "the E.164 form the webhook actually stored"
    assert "(415) 555-0134" in typed, "and the form the row might already hold"
    assert "4155550134" in typed
    assert "14155550134" in typed


def test_every_spelling_of_one_number_agrees_with_every_other() -> None:
    """The set has to be reflexive or the fix only works in one direction: a
    STOP stored in one form and looked up in another is the original bug."""
    forms = ["+14155550134", "(415) 555-0134", "415-555-0134", "1 415 555 0134"]
    for stored in forms:
        for looked_up in forms:
            assert stored in spellings(looked_up) or normalize_e164(stored) in spellings(
                looked_up
            ), f"a STOP stored as {stored!r} is invisible to a lookup for {looked_up!r}"


def test_a_non_nanp_number_does_not_manufacture_a_us_candidate() -> None:
    """⚠ The direction that would have been a fresh bug. ``phone_match_key``
    takes the last ten digits whatever the country code, so UK +442071838750
    yields 2071838750 — a real Maine number as a NANP subscriber. Blocking that
    person, who never texted anybody, is inventing a fact rather than missing
    one (gotcha 55)."""
    uk = spellings("+442071838750")
    assert "+12071838750" not in uk
    assert "2071838750" not in uk
    assert "+442071838750" in uk


def test_something_that_is_not_a_number_is_unverifiable_not_clear() -> None:
    """An empty candidate list is what makes ``has_opted_out`` refuse. If junk
    came back carrying itself, the lookup would find nothing and read as
    "never opted out" — a pass, in a module whose whole posture is fail-closed.
    """
    for junk in ("", "   ", "nope", "555-1234", "+"):
        assert spellings(junk) == [] or junk.strip() == "", f"{junk!r} looked up cleanly"


@pytest.mark.asyncio
async def test_a_stop_from_an_inbound_text_blocks_a_hand_typed_lead() -> None:
    """The whole defect, end to end: the ledger row exists under Twilio's
    spelling and the CRM asks under the manager's."""
    pool = _pool(row={"?column?": 1})
    assert await has_opted_out(pool, "(415) 555-0134") is True
    (candidates,) = pool.fetchrow.await_args.args[1:]
    assert "+14155550134" in candidates


@pytest.mark.asyncio
async def test_the_record_is_stored_canonically() -> None:
    """``ON CONFLICT (company_id, phone)`` keys on this string, so two
    spellings of one number would otherwise become two rows — and a START
    lifting one of them would leave the other still blocking."""
    pool = _pool(row=None)
    await record_opt_out(pool, company_id=COMPANY, phone="(415) 555-0134")
    assert pool.execute.await_args.args[2] == "+14155550134"


@pytest.mark.asyncio
async def test_start_lifts_a_block_recorded_under_another_spelling() -> None:
    """⚠ Both halves or neither. Matching on spellings when blocking but not
    when resuming leaves somebody permanently unreachable with no way to fix
    it — worse than the bug, because it has no visible cause."""
    pool = _pool(row=None)
    await clear_opt_out(pool, "415-555-0134")
    (candidates,) = pool.execute.await_args.args[1:]
    assert "+14155550134" in candidates
    # ⚠ And the STATEMENT has to consume the list. A mutation that left the
    # resume on `phone = $1` while still passing the candidates survived the
    # assertion above — the parameter was right and the SQL ignored it, which
    # is the shape gotcha 59 records for a guard that pins a list without
    # pinning the operator that reads it.
    sql = " ".join(pool.execute.await_args.args[0].split())
    assert "phone = ANY($1::text[])" in sql, (
        "the resume matches one spelling while the block matches all of them — "
        "somebody who said START stays blocked under their other spelling"
    )
