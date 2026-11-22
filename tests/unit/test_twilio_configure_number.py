"""Pointing an EXISTING Twilio number at us, and proving it took.

``buy_number`` wires a number in the same request that buys it, so anything
Occupella purchases is configured by construction. This is the other path — a
number bought by hand in the console and then claimed in the app.

⚠ **The load-bearing part is the read-back.** Twilio answers **200 and ignores
``VoiceUrl``** when the number carries a ``VoiceApplicationSid``: an
application attached to a number takes precedence over its URL fields. A
number that came from somebody's earlier project can carry one, so a POST that
"succeeded" can leave every call routing to a dead app. That is gotcha 20/70's
200-but-lie, on the field that decides whether anyone's phone rings.

Sources for the API shape (twilio.com is egress-blocked from this container,
so these were confirmed via secondary documentation):
  POST /Accounts/{AccountSid}/IncomingPhoneNumbers/{Sid}.json  — update
  GET  /Accounts/{AccountSid}/IncomingPhoneNumbers.json?PhoneNumber=  — find
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from helixis.connectors import twilio_client

BASE = "https://api.example.com"
NUMBER = "+16665550000"
PN = "PN" + "a" * 32


def _wired(**over: Any) -> dict[str, Any]:
    """What Twilio echoes back when the write DID take."""
    return {
        "sid": PN,
        "phone_number": NUMBER,
        "sms_url": f"{BASE}/api/v1/twilio/inbound",
        "voice_url": f"{BASE}/api/v1/crm/voice/inbound",
        **over,
    }


class _Twilio:
    """Stands in for the REST API. Records what was sent."""

    def __init__(self, *, found: bool = True, applied: dict[str, Any] | None = None) -> None:
        self.found = found
        self.applied = applied if applied is not None else _wired()
        self.posted: dict[str, str] = {}
        self.looked_up: dict[str, Any] = {}

    async def __aenter__(self) -> _Twilio:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, url: str, params: Any = None, **_: Any) -> httpx.Response:
        self.looked_up = dict(params or {})
        rows = [{"sid": PN, "phone_number": NUMBER}] if self.found else []
        return httpx.Response(200, json={"incoming_phone_numbers": rows})

    async def post(self, url: str, data: Any = None, **_: Any) -> httpx.Response:
        self.posted = dict(data or {})
        return httpx.Response(200, json=self.applied)


@pytest.fixture
def twilio(monkeypatch: pytest.MonkeyPatch) -> _Twilio:
    fake = _Twilio()
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    return fake


# ── the happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_finds_the_number_then_points_it_at_us(twilio: _Twilio) -> None:
    sid = await twilio_client.configure_number(
        account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
    )
    assert sid == PN
    assert twilio.looked_up["PhoneNumber"] == NUMBER
    assert twilio.posted["SmsUrl"] == f"{BASE}/api/v1/twilio/inbound"
    assert twilio.posted["VoiceUrl"] == f"{BASE}/api/v1/crm/voice/inbound"
    assert twilio.posted["StatusCallback"] == f"{BASE}/api/v1/twilio/status"


@pytest.mark.asyncio
async def test_it_clears_any_attached_application(twilio: _Twilio) -> None:
    """⚠ An ApplicationSid takes PRECEDENCE over the URL fields. Leaving one
    in place means Twilio stores our URLs, returns 200, and goes on routing
    every call to the old application — configured on paper, dead in fact."""
    await twilio_client.configure_number(
        account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
    )
    assert twilio.posted["SmsApplicationSid"] == ""
    assert twilio.posted["VoiceApplicationSid"] == ""


@pytest.mark.asyncio
async def test_a_trailing_slash_does_not_double_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full URL is in the signature HMAC, so `//api/v1/...` 403s every
    inbound request — silently, and forever."""
    fake = _Twilio()
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    await twilio_client.configure_number(
        account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE + "/"
    )
    # ⚠ Assert the WHOLE url, not "no double slash" — every https:// URL
    # contains one, so that check passes against the bug it names.
    assert fake.posted["VoiceUrl"] == f"{BASE}/api/v1/crm/voice/inbound"
    assert fake.posted["SmsUrl"] == f"{BASE}/api/v1/twilio/inbound"


# ── the write that did not take ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_200_that_ignored_the_voice_url_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ THE ONE THAT MATTERS. Twilio accepts the request and stores nothing,
    which is exactly what happens when an application is attached. Reporting
    that as success is worse than failing: the number reads as set up and
    never rings."""
    fake = _Twilio(applied=_wired(voice_url="https://demo.twilio.com/welcome/voice/"))
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    with pytest.raises(twilio_client.TwilioProvisionError) as exc:
        await twilio_client.configure_number(
            account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
        )
    # Names the likely cause, because the fix is in the Twilio console.
    assert "application" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_a_200_that_ignored_the_sms_url_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions of the read-back. Checking only the voice URL would
    report a half-wired number as configured."""
    fake = _Twilio(applied=_wired(sms_url=""))
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    with pytest.raises(twilio_client.TwilioProvisionError):
        await twilio_client.configure_number(
            account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
        )


# ── refusing rather than half-configuring ─────────────────────────────


@pytest.mark.asyncio
async def test_a_number_this_account_does_not_own_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under white-label every customer number lives on our account, so a miss
    is a typo or somebody else's number — never a reason to carry on."""
    fake = _Twilio(found=False)
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    with pytest.raises(twilio_client.TwilioProvisionError) as exc:
        await twilio_client.configure_number(
            account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
        )
    assert "does not own" in str(exc.value)
    assert fake.posted == {}, "tried to configure a number it could not find"


@pytest.mark.asyncio
async def test_no_base_url_refuses_before_calling_twilio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to point the number AT. Wiring it to an empty string
    would leave it accepting traffic and delivering it nowhere — the exact
    state this function exists to end."""
    fake = _Twilio()
    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: fake)
    with pytest.raises(twilio_client.TwilioProvisionError):
        await twilio_client.configure_number(
            account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=""
        )
    assert fake.looked_up == {}


@pytest.mark.asyncio
async def test_a_network_error_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Down(_Twilio):
        async def get(self, url: str, params: Any = None, **_: Any) -> httpx.Response:
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(twilio_client.httpx, "AsyncClient", lambda **_kw: _Down())
    with pytest.raises(twilio_client.TwilioProvisionError):
        await twilio_client.configure_number(
            account_sid="AC1", auth_token="tok", phone_number=NUMBER, webhook_base_url=BASE
        )
