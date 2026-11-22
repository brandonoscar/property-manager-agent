"""The Leasing reply route, at the two refusals it never had.

``test_sms_opt_out.py`` proves the predicate is right and that the route calls
it. This proves what the route DOES with each answer — that the send is
actually stopped, and that the sentence the agent reads tells them something
they can act on.

⚠ The clock is patched, not frozen by luck. ``recipient_block`` reads real
wall-clock time because the TCPA window is recipient-local and real; a test
that let it run live would pass or fail depending on when CI ran.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from tests.unit.test_crm_routes import (
    _TWILIO_SMS_APPROVED,
    COMPANY_ID,
    _make_token,
    _pool_with_company_number,
    _sample_lead,
    _settings,
)

#: 03:00 Eastern. Outside the window in every US zone, so a send here is
#: unambiguously illegal wherever the lead is.
THREE_AM_ET = datetime(2026, 6, 17, 7, 0, tzinfo=UTC)
#: 14:00 Eastern / 11:00 Pacific — inside the window even under the
#: conservative fallback, so a block at this instant means the gate fired.
MIDDAY_ET = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)


def _reply(
    *,
    opted_out: bool,
    now: datetime,
    send_sms: Any,
    texted_in: bool = True,
    consented: bool = False,
) -> Any:
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    lead = _sample_lead()
    pool = _pool_with_company_number()
    conn = pool.acquire.return_value.__aenter__.return_value
    inner = conn.fetchrow

    async def _fetchrow(query: str = "", *args: object, **kw: object) -> object:
        if "helixis_sms_opt_outs" in str(query):
            return {"?column?": 1} if opted_out else None
        if "crm_lead_messages" in str(query):  # may this lead be texted at all
            return {"texted_in": texted_in, "consented": consented}
        return await inner(query, *args, **kw)

    conn.fetchrow = _fetchrow
    # ⚠ ``has_opted_out`` reads the POOL directly while everything else in
    # this route goes through ``acquire()``. Both have to answer, and the role
    # gate below is patched separately — routing it through this fake instead
    # would 403 every send for a reason that has nothing to do with the gate
    # under test.
    pool.fetchrow = _fetchrow
    with (
        patch("helixis.api.routes.company.require_role", new=AsyncMock(return_value="manager")),
        patch("helixis.api.routes.crm.get_pool", return_value=pool),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m")),
        patch("helixis.api.routes.crm.leads_repo.update_stage", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        patch("helixis.comms.opt_out.datetime", _FrozenClock(now)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            return c.post(
                f"/api/v1/crm/leads/{lead.id}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "Yes, 4B is available — want to tour it?"},
            )


class _FrozenClock:
    """Stands in for ``datetime`` inside ``comms.opt_out`` so ``now(UTC)`` is
    the instant under test. Only ``now`` is overridden; everything else falls
    through, or the module's other datetime uses would break invisibly."""

    def __init__(self, when: datetime) -> None:
        self._when = when

    def now(self, _tz: Any = None) -> datetime:
        return self._when

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime, name)


# ── opted out ─────────────────────────────────────────────────────────


def test_a_lead_who_texted_stop_is_never_texted_again() -> None:
    """⚠ THE GAP, at the route. A lead has no consent row for the ledger to
    revoke, so before migration 0086 their STOP was recorded nowhere and this
    lane sent anyway."""
    send_sms = AsyncMock(return_value="SM-nope")
    resp = _reply(opted_out=True, now=MIDDAY_ET, send_sms=send_sms)
    assert resp.status_code == 409, resp.text
    send_sms.assert_not_awaited()


def test_the_opt_out_message_says_what_the_agent_can_still_do() -> None:
    """A refusal that only says no generates a support ticket. This one names
    the recovery (they text START) and the channels that still work."""
    detail = _reply(opted_out=True, now=MIDDAY_ET, send_sms=AsyncMock(return_value="x")).json()[
        "detail"
    ]
    assert "stop texting" in detail.lower()
    assert "START" in detail
    assert "call" in detail.lower()


# ── quiet hours ───────────────────────────────────────────────────────


def test_a_lead_is_not_texted_at_three_in_the_morning() -> None:
    """There was NO quiet-hours check on this lane at all — the window lived
    in ``send_sms``, which the CRM never calls."""
    send_sms = AsyncMock(return_value="SM-nope")
    resp = _reply(opted_out=False, now=THREE_AM_ET, send_sms=send_sms)
    assert resp.status_code == 409, resp.text
    send_sms.assert_not_awaited()
    assert "8am" in resp.json()["detail"]


def test_the_same_reply_sends_during_the_day() -> None:
    """⚠ The other direction, and the one that keeps this from being a
    permanent outage wearing a compliance costume. A gate tested only on what
    it blocks is indistinguishable from one that blocks everything."""
    send_sms = AsyncMock(return_value="SM-ok")
    resp = _reply(opted_out=False, now=MIDDAY_ET, send_sms=send_sms)
    assert resp.status_code == 200, resp.text
    send_sms.assert_awaited_once()


@pytest.mark.parametrize("opted_out", [True, False])
def test_neither_refusal_is_a_five_hundred(opted_out: bool) -> None:
    """409, not 500: this is a legitimate state of the world, not a fault.
    A 500 would also read as retryable to any client that retries."""
    resp = _reply(opted_out=opted_out, now=THREE_AM_ET, send_sms=AsyncMock(return_value="x"))
    assert resp.status_code == 409


# ── first contact ─────────────────────────────────────────────────────


def test_a_lead_who_never_texted_and_never_consented_is_not_texted() -> None:
    """⚠ THE GAP. A lead added by hand, or from a listing site or an email, has
    never texted in and has no consent row, and the route used to text them
    anyway: the public SMS terms say no message goes without a live consent
    record. Daytime and not opted out, so this is the only gate that fires."""
    send_sms = AsyncMock(return_value="SM-nope")
    resp = _reply(
        opted_out=False, now=MIDDAY_ET, send_sms=send_sms, texted_in=False, consented=False
    )
    assert resp.status_code == 409, resp.text
    send_sms.assert_not_awaited()
    detail = resp.json()["detail"].lower()
    assert "email or call" in detail and "text your number first" in detail


def test_a_lead_with_a_consent_record_can_be_texted() -> None:
    send_sms = AsyncMock(return_value="SM-ok")
    resp = _reply(
        opted_out=False, now=MIDDAY_ET, send_sms=send_sms, texted_in=False, consented=True
    )
    assert resp.status_code == 200, resp.text
    send_sms.assert_awaited_once()


async def test_the_permission_check_fails_closed_and_reads_one_company() -> None:
    from helixis.api.routes.crm import _lead_may_be_texted

    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Pool:
        def __init__(self, answer: object) -> None:
            self.answer = answer

        async def fetchrow(self, sql: str, *args: object) -> object:
            calls.append((sql, args))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    kw = {"company_id": "co-1", "lead_id": "lead-1", "phone": "+14155551234"}
    assert await _lead_may_be_texted(_Pool(RuntimeError("db down")), **kw) is False
    assert await _lead_may_be_texted(_Pool(None), **kw) is False
    assert await _lead_may_be_texted(_Pool({"texted_in": False, "consented": False}), **kw) is False
    assert await _lead_may_be_texted(_Pool({"texted_in": True, "consented": False}), **kw) is True
    sql, args = calls[-1]
    assert args == ("co-1", "lead-1", "+14155551234")
    assert sql.count("company_id = $1::uuid") == 2
    assert "direction = 'inbound' AND channel = 'sms'" in sql
    assert "revoked_at IS NULL" in sql
