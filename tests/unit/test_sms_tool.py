"""``send_text_message`` (agents/sms_tools.py) — SMS Stage 4, the agent-facing
confirm-gated text send.

Pins the full gate order the tool promises:

* dark state refuses BEFORE any consent lookup and BEFORE any confirm gate;
* recipients resolve ONLY against LIVE consent-ledger rows (unknown → refuse
  with the Settings pointer; revoked → refuse naming the revocation;
  ambiguous name → an honest "which one?" list, no gate, no send; lookup
  failure → fail closed);
* the render is strict (unknown template / missing / extra params refuse
  before any gate — free-form is impossible even through the agent);
* the confirm gate shows the RENDERED message + display name + last-4 (and
  never the full phone number anywhere in its payload);
* denial at the gate blocks the send; a double-approval replays instead of
  double-sending (run_guarded idempotency);
* an in-flight ``SmsBlocked`` (e.g. quiet hours) surfaces as an honest reply.

Gate-driving mirrors test_buildium_write_confirmation.py: run the impl as a
task, drain the confirm emission queue the way the orchestrator does, resolve
via the captured confirm_id.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import idempotency as idem
from helixis.agents import sms_tools as st
from helixis.agents import tools as agent_tools
from helixis.comms import local_time
from helixis.comms.sms import SmsBlocked, SmsResult
from helixis.db.repositories.action_idempotency import (
    STATUS_ABORTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
)
from helixis.memory.context import memory_context

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
PHONE = "+15551234567"
OTHER_PHONE = "+15559879876"

LIVE_SETTINGS = SimpleNamespace(twilio=SimpleNamespace(enabled=True))
DARK_SETTINGS = SimpleNamespace(twilio=SimpleNamespace(enabled=False))

NOTICE_PARAMS = '{"notice": "The garage will be repainted Tuesday."}'
RENDERED_NOTICE = (
    "Occupella: The garage will be repainted Tuesday. Questions? Contact your "
    "property manager. Reply STOP to cancel, HELP for help."
)


def _row(
    phone: str = PHONE,
    display_name: str | None = "Sarah Chen",
    revoked_at: datetime | None = None,
    timezone: str = "America/New_York",
    recipient_kind: str = "tenant",
) -> dict[str, Any]:
    return {
        "phone": phone,
        "display_name": display_name,
        "recipient_kind": recipient_kind,
        "timezone": timezone,
        "revoked_at": revoked_at,
    }


class FakePool:
    """Answers the tool's consent-ledger queries from an in-memory row list."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "helixis_sms_consent" in query
        _company_id, needle = args
        if "lower(display_name)" in query:
            return [r for r in self.rows if (r["display_name"] or "").lower() == needle.lower()]
        return [r for r in self.rows if r["phone"] == needle]


class ExplodingPool:
    """Any consent lookup errors → the tool must fail closed."""

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ledger down")


class ForbiddenPool:
    """A pool that must never be consulted (the dark path returns first)."""

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        raise AssertionError("consent ledger must not be consulted while the channel is dark")


def _ok_send() -> AsyncMock:
    return AsyncMock(
        return_value=SmsResult(
            sid="SM1", to_phone=PHONE, template_id="account_notice", status="queued"
        )
    )


def _wired(
    stack: ExitStack,
    *,
    settings: Any = LIVE_SETTINGS,
    pool: Any = None,
    send: AsyncMock | None = None,
) -> AsyncMock:
    """Bind company context + patch the tool's seams. Returns the send mock."""
    send = send if send is not None else _ok_send()
    stack.enter_context(memory_context(company_id=COMPANY_ID, user_id="u1", session_id="s1"))
    stack.enter_context(patch.object(st, "_settings", return_value=settings))
    stack.enter_context(patch.object(st, "_pool", return_value=pool))
    stack.enter_context(patch.object(st, "send_sms", send))
    return send


async def _drain_all() -> list[dict[str, Any]]:
    return await agent_tools.drain_pending_confirmations()


async def _next_confirm_payload() -> dict[str, Any]:
    """Yield until the in-flight tool pushes its confirm payload (the way the
    orchestrator drains), then return the full payload."""
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await _drain_all()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload was emitted by the write tool")


# ── 1. dark mode refuses BEFORE any gate (and before any ledger read) ────────


@pytest.mark.asyncio
async def test_dark_mode_refuses_before_any_confirm_gate() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, settings=DARK_SETTINGS, pool=ForbiddenPool())
        out = await st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
    assert "isn't enabled yet" in out
    assert await _drain_all() == []  # no confirm gate was ever surfaced
    send.assert_not_called()


# ── 2-4. consent-ledger resolution ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_recipient_refuses_with_settings_pointer() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([]))
        out = await st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
    assert "no texting consent on file" in out
    assert "Settings" in out and "Twilio SMS" in out
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_name_lists_candidates_without_gating_or_sending() -> None:
    await _drain_all()
    rows = [_row(phone=PHONE), _row(phone=OTHER_PHONE, recipient_kind="owner")]
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool(rows))
        out = await st._send_text_message_impl("sarah chen", "account_notice", NOTICE_PARAMS)
    assert "which one" in out.lower()
    assert "4567" in out and "9876" in out  # last-4 of both candidates
    assert PHONE not in out and OTHER_PHONE not in out  # never the full numbers
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_revoked_consent_refuses() -> None:
    await _drain_all()
    rows = [_row(revoked_at=datetime(2026, 8, 1, tzinfo=UTC))]
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool(rows))
        out = await st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
    assert "revoked" in out
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_ledger_lookup_failure_fails_closed() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=ExplodingPool())
        out = await st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
    assert "couldn't verify texting consent" in out
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_e164_recipient_resolves_against_the_ledger() -> None:
    """A phone-shaped recipient still goes through the ledger (live row only)."""
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        task = asyncio.create_task(
            st._send_text_message_impl("(555) 123-4567", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        out = await asyncio.wait_for(task, timeout=1.0)
    assert "Sent the text to Sarah Chen" in out
    send.assert_awaited_once()
    assert send.await_args.kwargs["to_phone"] == PHONE


# ── 5. gate denial blocks the send ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_denial_blocks_the_send() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        task = asyncio.create_task(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        send.assert_not_called()  # nothing may go out while awaiting approval
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        out = await asyncio.wait_for(task, timeout=1.0)
    assert "didn't send" in out
    send.assert_not_called()


# ── 6. double-approval sends once (idempotency) ───────────────────────────────


class FakeStore:
    """Minimal in-memory IdempotencyStore (same shape as the primitive's fake)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        k = (company_id, idem_key)
        existing = self._rows.get(k)
        if existing is not None:
            return ClaimResult(won=False, row=existing)
        row = ClaimRow(
            company_id=company_id,
            idem_key=idem_key,
            action_type=action_type,
            status=STATUS_IN_PROGRESS,
            result=None,
            error=None,
            attempts=1,
            expires_at=datetime.now(UTC) + window if window else None,
        )
        self._rows[k] = row
        return ClaimResult(won=True, row=row)

    async def _set(self, company_id: str, idem_key: str, **f: Any) -> None:
        row = self._rows[(company_id, idem_key)]
        for key, val in f.items():
            setattr(row, key, val)

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        await self._set(company_id, idem_key, status=STATUS_SUCCEEDED, result=result)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_FAILED_RETRYABLE, error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status="abandoned_unknown", error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABORTED)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        row = self._rows.get((company_id, idem_key))
        if row is None or row.status not in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
            return False
        row.status = STATUS_IN_PROGRESS
        return True

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_double_approval_sends_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same recipient + template + params approved twice → ONE Twilio send; the
    second attempt replays honestly instead of double-texting."""
    await _drain_all()
    store = FakeStore()
    monkeypatch.setattr(idem, "store_from_pool", lambda: store)
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))

        task = asyncio.create_task(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        first = await asyncio.wait_for(task, timeout=1.0)

        # Second identical run: the claim is already SUCCEEDED → replay, no
        # second confirm gate, no second send.
        second = await asyncio.wait_for(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS),
            timeout=1.0,
        )

    assert "Sent the text to Sarah Chen" in first
    assert "already sent" in second and "didn't send it again" in second
    send.assert_awaited_once()
    assert await _drain_all() == []  # the replay never surfaced a second gate


# ── 7 + 10. the gate payload: rendered preview, last-4 only ──────────────────


@pytest.mark.asyncio
async def test_gate_shows_rendered_message_and_only_last_four_of_phone() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        task = asyncio.create_task(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        try:
            assert payload["action"] == "send_text_message"
            # The human sees the EXACT rendered text, the name, and the template id.
            assert RENDERED_NOTICE in payload["details"]
            assert "Sarah Chen" in payload["details"]
            assert "account_notice" in payload["details"]
            assert any(RENDERED_NOTICE in item for item in payload["items"])
            # No PII beyond last-4: the raw number appears NOWHERE in the payload.
            flat = json.dumps(payload)
            assert PHONE not in flat
            assert "555123" not in flat  # no more than the last four digits
            assert "***-**4567" in flat
        finally:
            await agent_tools.resolve_confirmation(payload["confirm_id"], True)
            await asyncio.wait_for(task, timeout=1.0)
    send.assert_awaited_once()


# ── 8. SmsBlocked surfaces honestly (quiet hours) ─────────────────────────────


@pytest.mark.asyncio
async def test_quiet_hours_block_surfaces_honestly() -> None:
    await _drain_all()
    # ⚠ The detail is built the way comms/sms.py builds it — from the statute
    # table — not typed out here. This fixture used to carry a hardcoded
    # "08:00-21:00" and the assertion below demanded "8:00 AM"/"9:00 PM", so
    # the test PINNED the pre-2026-09-01 federal default and would have gone on
    # passing while a Texan was quoted the wrong window (gotcha 25).
    window = local_time.describe_window("+15125550100", datetime(2026, 9, 2, 18, 0, tzinfo=UTC))
    blocked_send = AsyncMock(
        side_effect=SmsBlocked(
            "quiet_hours", f"outside the recipient-local calling window ({window})"
        )
    )
    with ExitStack() as stack:
        _wired(stack, pool=FakePool([_row()]), send=blocked_send)
        task = asyncio.create_task(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        out = await asyncio.wait_for(task, timeout=1.0)
    assert "didn't send" in out
    assert "quiet hours" in out
    # The refusal quotes the window that was APPLIED, whatever the statute says
    # today — not a pair of constants this test also has to remember to update.
    assert window in out, f"expected the applied window {window!r} in: {out}"
    # And a Texan's window is NOT the bare federal default, which is the whole
    # reason the hardcoded string was wrong.
    assert window == "9:00 AM-9:00 PM"
    # Names the recipient whose local time governs the window.
    assert "Sarah Chen" in out


@pytest.mark.asyncio
async def test_revoked_mid_flight_block_surfaces_honestly() -> None:
    """Consent revoked between resolution and send (send_sms re-checks) —
    the reply names the reason instead of claiming success."""
    await _drain_all()
    blocked_send = AsyncMock(side_effect=SmsBlocked("revoked", "consent was revoked"))
    with ExitStack() as stack:
        _wired(stack, pool=FakePool([_row()]), send=blocked_send)
        task = asyncio.create_task(
            st._send_text_message_impl("Sarah Chen", "account_notice", NOTICE_PARAMS)
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        out = await asyncio.wait_for(task, timeout=1.0)
    assert "didn't send" in out and "revoked" in out


# ── 9. template strictness through the tool path ──────────────────────────────


@pytest.mark.asyncio
async def test_unknown_template_refuses_naming_the_registered_ids() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        out = await st._send_text_message_impl("Sarah Chen", "free_form", '{"x": "y"}')
    assert "isn't a registered SMS template" in out
    for tid in ("rent_reminder", "maintenance_update", "appointment_confirmation"):
        assert tid in out
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_missing_and_extra_params_refuse_before_any_gate() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        missing = await st._send_text_message_impl("Sarah Chen", "account_notice", "{}")
        extra = await st._send_text_message_impl(
            "Sarah Chen",
            "account_notice",
            '{"notice": "hi", "smuggled": "free-form text"}',
        )
    assert "missing params" in missing and "notice" in missing
    assert "unexpected params" in extra and "smuggled" in extra
    assert await _drain_all() == []
    send.assert_not_called()


@pytest.mark.asyncio
async def test_unparseable_params_json_refuses() -> None:
    await _drain_all()
    with ExitStack() as stack:
        send = _wired(stack, pool=FakePool([_row()]))
        out = await st._send_text_message_impl("Sarah Chen", "account_notice", "not json")
    assert "JSON object" in out and "nothing was sent" in out
    assert await _drain_all() == []
    send.assert_not_called()
