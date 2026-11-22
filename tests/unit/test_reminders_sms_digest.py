"""Manager alert digest (reminders/sms_digest.py) — Stage 3.

Pins the digest contract: at most one account_notice per consented manager
per UTC day (cap read from helixis_sms_log — no new table), every send
rides comms/sms.send_sms (so a SmsBlocked is expected, logged, and never a
sweep failure), the summary param keeps the rendered template within two
SMS segments, and companies with no consent / a dark twilio flag / an
empty sweep are skipped before any send.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.comms.sms import SmsBlocked, SmsResult, render_template
from helixis.db.repositories.reminders import ReminderRow
from helixis.reminders import sms_digest, sweep
from helixis.reminders.sms_digest import (
    MAX_RENDERED_CHARS,
    build_notice,
    send_reminder_digests,
)

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
PHONE = "+15551234567"
_NOW = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)  # noon Eastern — in-window

LIVE_SETTINGS = SimpleNamespace(twilio=SimpleNamespace(enabled=True))
DARK_SETTINGS = SimpleNamespace(twilio=SimpleNamespace(enabled=False))


def _card(title: str = "no heat — Unit 7") -> ReminderRow:
    return ReminderRow(
        id="r1",
        producer="rent_missing",
        entity_key="k",
        status="open",
        severity="high",
        title=title,
        body=None,
        link_path=None,
        payload={},
        snoozed_until=None,
        escalation=0,
        first_fired_at=None,
        last_fired_at=None,
    )


class FakePool:
    """fetch → the manager-consent roster; fetchrow → the daily-cap lookup
    against previously "logged" sends (in-memory)."""

    def __init__(self, managers: list[str] | None = None) -> None:
        self.managers = managers if managers is not None else [PHONE]
        #: (phone, created_at, twilio status). The status matters now: a send
        #: Twilio reported `undelivered` must NOT count against the daily cap,
        #: or a message that never arrived silently buys a day of silence.
        self.sent_log: list[tuple[str, datetime, str]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "recipient_kind = 'manager'" in query
        assert "revoked_at IS NULL" in query
        return [{"phone": p} for p in self.managers]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """The daily-cap lookup.

        ⚠ Reads the QUERY'S OWN parameter list rather than unpacking a fixed
        count. The cap gained a fourth argument — the terminal-failure statuses
        that no longer spend the day's notice — and a fixture that unpacks
        three raised `ValueError` on every call, which is the gate working
        (gotcha 89). A fake that hardcodes the shape agrees with itself
        whatever the route actually sends (gotcha 72).
        """
        assert "helixis_sms_log" in query
        phone, _template, midnight, *rest = args
        excluded = set(rest[0]) if rest else set()
        for logged_phone, created_at, status in self.sent_log:
            if logged_phone == phone and created_at >= midnight and status not in excluded:
                return {"?column?": 1}
        return None


def _ok_send(pool: FakePool, status: str = "queued") -> AsyncMock:
    async def _send(**kwargs: Any) -> SmsResult:
        pool.sent_log.append((kwargs["to_phone"], kwargs["now"], status))
        return SmsResult(
            sid="SM1",
            to_phone=kwargs["to_phone"],
            template_id=kwargs["template_id"],
            status=status,
        )

    return AsyncMock(side_effect=_send)


def _patch_cards(cards: list[ReminderRow]) -> Any:
    return patch.object(sms_digest.reminders_repo, "list_active", AsyncMock(return_value=cards))


# ── the daily cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sends_once_then_capped_for_the_day() -> None:
    pool = FakePool()
    send = _ok_send(pool)
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()] * 3):
        first = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
        second = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
    assert first == 1
    assert second == 0  # capped by the log row from the first send
    assert send.await_count == 1
    # The one send used the account_notice template through send_sms.
    kwargs = send.await_args.kwargs
    assert kwargs["template_id"] == "account_notice"
    assert kwargs["company_id"] == COMPANY_ID
    assert "3 items need attention" in kwargs["params"]["notice"]
    assert "no heat — Unit 7" in kwargs["params"]["notice"]


@pytest.mark.asyncio
async def test_a_digest_that_never_arrived_does_not_spend_the_day() -> None:
    """Twilio accepted it, we wrote it down, and it did not arrive.

    Counting that against the one-per-day cap means the manager gets NOTHING
    that day and nobody is told why — the cap turned from a courtesy bound
    into a failure to notify. `helixis_sms_log` had the status the whole time
    and no query read it.
    """
    pool = FakePool()
    send = _ok_send(pool, status="undelivered")
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()] * 3):
        first = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
        second = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
    assert (first, second) == (1, 1), "an undelivered digest still spent the day"
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_a_digest_with_no_receipt_yet_still_spends_the_day() -> None:
    """⚠ The other direction, and the one a real person pays for. A row at
    `queued`/`sent` has no outcome; re-sending on a receipt that has not come
    back is how one slow carrier becomes two texts (gotchas 55/57 — unknown
    counts as spent). Both directions, or the fix is just "always re-send"."""
    pool = FakePool()
    send = _ok_send(pool, status="sent")
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()] * 3):
        first = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
        second = await send_reminder_digests(
            pool, LIVE_SETTINGS, COMPANY_ID, {"rent_missing": 3}, now=_NOW
        )
    assert (first, second) == (1, 0)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_cap_resets_at_utc_midnight() -> None:
    pool = FakePool()
    send = _ok_send(pool)
    yesterday = datetime(2026, 8, 17, 23, 0, tzinfo=UTC)
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()]):
        assert (
            await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 1}, now=yesterday)
            == 1
        )
        # Next UTC day: yesterday's log row no longer counts.
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 1}, now=_NOW) == 1


# ── guard short-circuits ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_twilio_disabled_or_settings_missing() -> None:
    pool = FakePool()
    send = AsyncMock()
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()]):
        assert await send_reminder_digests(pool, DARK_SETTINGS, COMPANY_ID, {"p": 1}) == 0
        assert await send_reminder_digests(pool, None, COMPANY_ID, {"p": 1}) == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_sweep_produced_nothing() -> None:
    pool = FakePool()
    send = AsyncMock()
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()]):
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {}) == 0
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 0}) == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_company_with_no_live_manager_consent() -> None:
    pool = FakePool(managers=[])
    send = AsyncMock()
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()]):
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 1}) == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_no_active_cards_remain() -> None:
    """The sweep produced candidates but the desk drained (all handled) —
    nothing to digest."""
    pool = FakePool()
    send = AsyncMock()
    with patch.object(sms_digest, "send_sms", send), _patch_cards([]):
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 2}) == 0
    send.assert_not_awaited()


# ── SmsBlocked / failure posture ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sms_blocked_is_swallowed_never_raised() -> None:
    pool = FakePool()
    send = AsyncMock(side_effect=SmsBlocked("quiet_hours"))
    with patch.object(sms_digest, "send_sms", send), _patch_cards([_card()]):
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 1}) == 0
    # No log row was written (send_sms raised before logging), so a later
    # in-window sweep the same day may retry — desired.
    assert pool.sent_log == []


@pytest.mark.asyncio
async def test_send_failure_does_not_stop_other_managers() -> None:
    pool = FakePool(managers=["+15550000001", "+15550000002"])
    calls: list[str] = []

    async def _send(**kwargs: Any) -> SmsResult:
        calls.append(kwargs["to_phone"])
        if kwargs["to_phone"] == "+15550000001":
            raise RuntimeError("twilio 500")
        return SmsResult(
            sid="SM2", to_phone=kwargs["to_phone"], template_id="account_notice", status="queued"
        )

    with (
        patch.object(sms_digest, "send_sms", AsyncMock(side_effect=_send)),
        _patch_cards([_card()]),
    ):
        assert await send_reminder_digests(pool, LIVE_SETTINGS, COMPANY_ID, {"p": 1}) == 1
    assert calls == ["+15550000001", "+15550000002"]


# ── truncation ────────────────────────────────────────────────────────


def test_notice_truncation_keeps_rendered_message_within_two_segments() -> None:
    notice = build_notice(12, "x" * 500)
    rendered = render_template("account_notice", {"notice": notice})
    assert len(rendered) <= MAX_RENDERED_CHARS
    assert notice.endswith("…")


def test_notice_short_titles_untouched_and_singular_grammar() -> None:
    assert build_notice(3, "no heat — Unit 7") == "3 items need attention; top: no heat — Unit 7"
    assert build_notice(1, None) == "1 item needs attention"


# ── sweep wiring ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_cycle_survives_a_digest_crash() -> None:
    """A digest failure must never fail the sweep (same isolation as a
    failing producer)."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    with (
        patch.object(sweep, "_companies_with_mirror", AsyncMock(return_value=[COMPANY_ID])),
        patch.object(sweep, "run_reminder_sweep", AsyncMock(return_value={"rent_missing": 2})),
        patch(
            "helixis.reminders.sms_digest.send_reminder_digests",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        sweep._last_swept.clear()
        results = await sweep.run_reminder_sweep_cycle(pool, settings=LIVE_SETTINGS)
    assert results == {COMPANY_ID: {"rent_missing": 2}}


@pytest.mark.asyncio
async def test_sweep_cycle_passes_counts_to_the_digest() -> None:
    digest = AsyncMock(return_value=1)
    with (
        patch.object(sweep, "_companies_with_mirror", AsyncMock(return_value=[COMPANY_ID])),
        patch.object(sweep, "run_reminder_sweep", AsyncMock(return_value={"bill_due": 1})),
        patch("helixis.reminders.sms_digest.send_reminder_digests", digest),
    ):
        sweep._last_swept.clear()
        await sweep.run_reminder_sweep_cycle(AsyncMock(), settings=LIVE_SETTINGS)
    digest.assert_awaited_once()
    assert digest.await_args.args[2] == COMPANY_ID
    assert digest.await_args.args[3] == {"bill_due": 1}


@pytest.mark.asyncio
async def test_sweep_cycle_without_settings_never_imports_the_digest() -> None:
    """The default (settings=None) call — e.g. tests or a settings-less
    caller — keeps the exact pre-Stage-3 behavior."""
    with (
        patch.object(sweep, "_companies_with_mirror", AsyncMock(return_value=[COMPANY_ID])),
        patch.object(sweep, "run_reminder_sweep", AsyncMock(return_value={"bill_due": 1})),
        patch("helixis.reminders.sms_digest.send_reminder_digests", AsyncMock()) as digest,
    ):
        sweep._last_swept.clear()
        await sweep.run_reminder_sweep_cycle(AsyncMock())
    digest.assert_not_awaited()
