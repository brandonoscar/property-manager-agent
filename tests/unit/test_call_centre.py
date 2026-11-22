"""The voice lane with five customers on the deployment.

The SMS half got the opt-out record, the clock and the per-company gate. Three
things in the CALL lane were still wrong, and none of them failed a test:

1. **Click-to-call had no opt-out check and no clock.** The 8am-9pm rule was
   written for CALLS, and a phone ringing at 3am is the version of this people
   actually wake up for.
2. **A missed call played the DEPLOYMENT-global greeting.** Gotcha 69 made the
   greeting per-company on ``/voice/inbound`` and never reached ``/voice/status``
   — which is the common path, since a missed call is the whole reason
   voicemail exists. Customer B's caller heard customer A's greeting.
3. **``skip_signature_check`` reached all four real voice webhooks.** Gotcha 68
   closed the identical bypass on the SMS lane; the voice half was missed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from tests.unit.test_crm_recipient_gate import MIDDAY_ET, THREE_AM_ET, _FrozenClock
from tests.unit.test_crm_routes import (
    _TWILIO_VOICE,
    COMPANY_ID,
    _make_token,
    _mock_pool,
    _pool_with_company_number,
    _sample_lead,
    _settings,
)

OTHER_COMPANY = "22222222-2222-2222-2222-222222222222"


# ── 1. click-to-call: the recipient gate ──────────────────────────────


def _call(*, opted_out: bool, now: datetime, create_call: Any) -> Any:
    settings = _settings(True, _TWILIO_VOICE)
    lead = _sample_lead()
    pool = _pool_with_company_number()
    inner = pool.fetchrow

    async def _fetchrow(query: str = "", *args: object, **kw: object) -> object:
        if "helixis_sms_opt_outs" in str(query):
            return {"?column?": 1} if opted_out else None
        return await inner(query, *args, **kw)

    pool.fetchrow = _fetchrow
    pool.acquire.return_value.__aenter__.return_value.fetchrow = _fetchrow
    with (
        patch("helixis.api.routes.company.require_role", new=AsyncMock(return_value="manager")),
        patch("helixis.api.routes.crm.get_pool", return_value=pool),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m")),
        patch("helixis.api.routes.crm.twilio_client.create_call", create_call),
        patch("helixis.comms.opt_out.datetime", _FrozenClock(now)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            return c.post(
                f"/api/v1/crm/leads/{lead.id}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={},
            )


def test_a_lead_who_opted_out_is_not_called() -> None:
    """⚠ An SMS opt-out blocks the CALL too, deliberately. Somebody who told us
    to stop contacting them did not mean "stop texting but do ring me"."""
    create_call = AsyncMock(return_value="CA-nope")
    resp = _call(opted_out=True, now=MIDDAY_ET, create_call=create_call)
    assert resp.status_code == 409, resp.text
    create_call.assert_not_awaited()
    # Names the channel that still works — a refusal that only says no is a
    # support ticket.
    assert "email" in resp.json()["detail"].lower()


def test_a_lead_is_not_called_at_three_in_the_morning() -> None:
    """There was no clock on this lane at all. TCPA's 8am-9pm window is
    older than the texting rule and applies to calls first."""
    create_call = AsyncMock(return_value="CA-nope")
    resp = _call(opted_out=False, now=THREE_AM_ET, create_call=create_call)
    assert resp.status_code == 409, resp.text
    create_call.assert_not_awaited()
    assert "8am" in resp.json()["detail"]


def test_the_same_call_goes_through_during_the_day() -> None:
    """⚠ The other direction. A gate tested only on what it blocks is
    indistinguishable from one that blocks everything, and this one sits on
    the button an agent presses all day."""
    create_call = AsyncMock(return_value="CA-ok")
    resp = _call(opted_out=False, now=MIDDAY_ET, create_call=create_call)
    assert resp.status_code == 200, resp.text
    create_call.assert_awaited_once()


# ── 2. the voicemail greeting is per company ──────────────────────────


def _voice_status(*, greeting: str, resolves_to: str | None = COMPANY_ID) -> str:
    """POST the Dial action callback for a MISSED call; return the TwiML."""
    settings = _settings(True, {**_TWILIO_VOICE, "voice_greeting": "ACME PROPERTIES SPEAKING"})
    pool = _pool_with_company_number(greeting=greeting)
    with (
        patch("helixis.api.routes.crm.twilio_client.validate_signature", return_value=True),
        patch("helixis.api.routes.crm.get_pool", return_value=pool),
        patch(
            "helixis.api.routes.crm.resolve_company_for_number",
            AsyncMock(return_value=resolves_to),
        ),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/status",
                data={"DialCallStatus": "no-answer", "To": "+17775550001"},
            )
    assert resp.status_code == 200
    return resp.text


def test_a_missed_call_plays_this_companys_greeting() -> None:
    """⚠ THE COMMON PATH. /voice/inbound got the per-company greeting in
    gotcha 69 and this one kept the deployment global — but a missed call is
    the whole reason voicemail exists, so this is the greeting most callers
    actually hear."""
    twiml = _voice_status(greeting="Thanks for calling Redbud Property Group")
    assert "Redbud Property Group" in twiml
    assert "ACME PROPERTIES" not in twiml, (
        "played the deployment-global greeting — every customer's callers hear "
        "whichever company's greeting is in the env var"
    )


def test_an_unknown_number_gets_a_neutral_greeting_not_someone_elses() -> None:
    """⚠ Falls back to a greeting that names NOBODY. A greeting that claims
    the wrong company is worse than one that claims none — and the global
    value belongs to at most one customer."""
    twiml = _voice_status(greeting="", resolves_to=None)
    assert "ACME PROPERTIES" not in twiml
    assert "leave a message" in twiml.lower()
    assert "<Record" in twiml, "stopped offering voicemail entirely"


def test_a_company_with_no_greeting_set_still_gets_voicemail() -> None:
    """An empty per-company greeting is not a failure — it is a company that
    never customised one."""
    twiml = _voice_status(greeting="")
    assert "leave a message" in twiml.lower()
    assert "ACME PROPERTIES" not in twiml


def _voice_inbound(*, greeting: str, forward_to: str = "") -> str:
    """POST the INBOUND webhook for a company with no forwarding number, so
    the call goes straight to voicemail. Returns the TwiML."""
    settings = _settings(True, {**_TWILIO_VOICE, "voice_greeting": "ACME PROPERTIES SPEAKING"})
    pool = _pool_with_company_number(greeting=greeting, forward_to=forward_to)
    with (
        patch("helixis.api.routes.crm.twilio_client.validate_signature", return_value=True),
        patch("helixis.api.routes.crm.get_pool", return_value=pool),
        patch(
            "helixis.api.routes.crm.resolve_company_for_number",
            AsyncMock(return_value=COMPANY_ID),
        ),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="lead-1")),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+14155551234", "To": "+17775550001", "CallSid": "CA1"},
            )
    assert resp.status_code == 200, resp.text
    return resp.text


def test_the_straight_to_voicemail_branch_also_avoids_the_global_greeting() -> None:
    """⚠ FOUND BY MUTATION, not by review. Both greeting branches had to be
    covered separately: /voice/status is the missed-call path and this is the
    no-forwarding-number path, and a test of one says nothing about the other.
    A company that has not set a greeting must get the NEUTRAL one, never the
    deployment global — which belongs to at most one customer."""
    twiml = _voice_inbound(greeting="")
    assert "ACME PROPERTIES" not in twiml, (
        "a company with no greeting of its own played the deployment-global "
        "one — which is some other customer's brand"
    )
    assert "leave a message" in twiml.lower()


def test_the_straight_to_voicemail_branch_uses_this_companys_greeting() -> None:
    twiml = _voice_inbound(greeting="Thanks for calling Redbud Property Group")
    assert "Redbud Property Group" in twiml


# ── 3. the voice webhooks are signature-authenticated ─────────────────


@pytest.mark.parametrize(
    "path",
    ["/voice/inbound", "/voice/status", "/voice/voicemail", "/voice/transcription"],
)
def test_an_unsigned_voice_webhook_is_refused_even_with_the_testing_flag(path: str) -> None:
    """⚠ ``skip_signature_check`` used to return early from
    ``_verify_twilio_or_403``, so a deployment carrying the flag accepted any
    unsigned POST to all four of these — forged inbound calls, forged
    voicemails with an attacker's recording URL, forged transcriptions
    attributed to somebody else's company. Gotcha 68 closed exactly this on
    the SMS lane and the voice half was missed.

    The flag is passed here ON PURPOSE: the point is that it no longer helps.
    """
    settings = _settings(True, {**_TWILIO_VOICE, "skip_signature_check": True})
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(f"/api/v1/crm{path}", data={"From": "+14155551234"})
    assert resp.status_code == 403, resp.text


def test_the_simulators_still_require_the_testing_flag() -> None:
    """The three ``/simulate`` routes are gated ON the flag, not BY it — they
    404 without it, which is the safe direction and must not regress into a
    signature bypass."""
    settings = _settings(True, _TWILIO_VOICE)  # flag absent
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_number": "+14155551234", "body": ""},
            )
    assert resp.status_code == 404


def test_no_route_reads_the_retired_global_greeting() -> None:
    """⚠ ``TwilioSettings.greeting_or_default`` was DELETED, not just unused.
    It returned the deployment-global greeting, which is the leak — and a
    convenient accessor left lying around is the leak one attribute away."""
    from helixis.settings import TwilioSettings

    assert not hasattr(TwilioSettings, "greeting_or_default"), (
        "the global greeting accessor is back — every caller who dials a "
        "different company can hear one customer's greeting again"
    )


def test_the_datetime_import_is_used() -> None:
    """Keeps ruff honest about the clock imports this module relies on."""
    assert datetime.now(UTC).tzinfo is UTC
