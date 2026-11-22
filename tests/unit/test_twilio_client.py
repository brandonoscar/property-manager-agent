"""Twilio inbound-signature validation + outbound send-guard tests.

The signature algorithm is Twilio's documented one: the request URL with
each POST param appended (sorted by key, key immediately followed by
value), HMAC-SHA1 with the account auth token, base64-encoded. These tests
pin a computed vector (regression guard) and exercise the tamper / wrong-
token / missing-input rejections.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from helixis.connectors.twilio_client import (
    TwilioSendError,
    send_sms,
    validate_signature,
)

AUTH = "12345"
URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}


def _sign(auth: str, url: str, params: dict[str, str]) -> str:
    payload = url
    for key in sorted(params):
        payload += key + params[key]
    return base64.b64encode(
        hmac.new(auth.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


# ── signature validation ─────────────────────────────────────────────


def test_valid_signature_roundtrips():
    sig = _sign(AUTH, URL, PARAMS)
    assert validate_signature(AUTH, URL, PARAMS, sig) is True


def test_pinned_vector():
    # Regression pin — computed from the documented algorithm for the fixed
    # input above. A change here means the signing bytes moved.
    assert _sign(AUTH, URL, PARAMS) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_tampered_param_rejected():
    sig = _sign(AUTH, URL, PARAMS)
    assert validate_signature(AUTH, URL, {**PARAMS, "Digits": "9999"}, sig) is False


def test_wrong_auth_token_rejected():
    sig = _sign(AUTH, URL, PARAMS)
    assert validate_signature("wrong-token", URL, PARAMS, sig) is False


def test_missing_signature_rejected():
    assert validate_signature(AUTH, URL, PARAMS, None) is False


def test_missing_auth_token_rejected():
    sig = _sign(AUTH, URL, PARAMS)
    assert validate_signature("", URL, PARAMS, sig) is False


# ── outbound send guards ─────────────────────────────────────────────


async def test_send_sms_raises_when_unconfigured():
    with pytest.raises(TwilioSendError):
        await send_sms(account_sid="", auth_token="", from_number="", to="+1", body="hi")


async def test_send_sms_raises_on_empty_body():
    with pytest.raises(TwilioSendError):
        await send_sms(account_sid="AC", auth_token="tok", from_number="+1", to="+1999", body="")


# ── the sender: a Messaging Service is a number POOL ──────────────────
#
# A Messaging Service holds many numbers and Twilio picks one, which is what
# lets a new number be added in the console instead of as a Render env var per
# number. ``comms/sms.py`` has always supported it; this client hard-required
# ``from_number`` and unconditionally posted ``From``, so a pool-only
# deployment failed EVERY CRM reply while the customer-care lane sent fine.
# Same shape as gotcha 67: two outbound lanes, one not told about a capability
# the other had.


class _CapturedPost:
    """Captures the form Twilio would have received, and 201s."""

    def __init__(self) -> None:
        self.form: dict[str, str] | None = None

    def __call__(self, *a, **kw):
        self.form = dict(kw.get("data") or {})

        class _Resp:
            status_code = 201
            text = '{"sid": "SM_captured"}'

            @staticmethod
            def json():
                return {"sid": "SM_captured"}

        return _Resp()


def _patch_post(monkeypatch, captured: _CapturedPost) -> None:
    import httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return captured(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_a_messaging_service_is_sent_as_the_sender(monkeypatch):
    captured = _CapturedPost()
    _patch_post(monkeypatch, captured)
    await send_sms(
        account_sid="AC",
        auth_token="tok",
        from_number="",
        messaging_service_sid="MG123",
        to="+19995551234",
        body="hi",
    )
    assert captured.form is not None
    assert captured.form.get("MessagingServiceSid") == "MG123"
    # ⚠ And NOT ``From``. Twilio rejects a request carrying both, so posting
    # From unconditionally is not merely redundant — it breaks the send.
    assert "From" not in captured.form


async def test_a_plain_number_is_still_sent_as_from_param(monkeypatch):
    """The other direction. Every existing single-number deployment must keep
    working — a fix that only serves the pool case is a fresh outage."""
    captured = _CapturedPost()
    _patch_post(monkeypatch, captured)
    await send_sms(
        account_sid="AC",
        auth_token="tok",
        from_number="+18005551212",
        to="+19995551234",
        body="hi",
    )
    assert captured.form is not None
    assert captured.form.get("From") == "+18005551212"
    assert "MessagingServiceSid" not in captured.form


async def test_a_messaging_service_alone_counts_as_configured(monkeypatch):
    """``from_number`` used to be hard-required, so this raised before any
    request was made — the actual failure a pool-only deployment hit."""
    captured = _CapturedPost()
    _patch_post(monkeypatch, captured)
    sid = await send_sms(
        account_sid="AC",
        auth_token="tok",
        from_number="",
        messaging_service_sid="MG123",
        to="+19995551234",
        body="hi",
    )
    assert sid == "SM_captured"


async def test_neither_sender_still_raises():
    with pytest.raises(TwilioSendError):
        await send_sms(
            account_sid="AC",
            auth_token="tok",
            from_number="",
            messaging_service_sid="",
            to="+19995551234",
            body="hi",
        )


# ── number provisioning ───────────────────────────────────────────────


class _CapturedGet:
    def __init__(self, payload: dict) -> None:
        self.params: dict | None = None
        self._payload = payload

    def __call__(self, *a, **kw):
        self.params = dict(kw.get("params") or {})
        payload = self._payload

        class _Resp:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return payload

        return _Resp()


def _patch_http(monkeypatch, *, get=None, post=None):
    import httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return get(*a, **kw)

        async def post(self, *a, **kw):
            return post(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


async def test_number_search_requires_both_sms_and_voice(monkeypatch):
    """⚠ A voice-only number silently breaks texting, and the failure surfaces
    much later as "messages don't send" with nothing pointing back here."""
    from helixis.connectors.twilio_client import search_available_numbers

    cap = _CapturedGet({"available_phone_numbers": [{"phone_number": "+17775550001"}]})
    _patch_http(monkeypatch, get=cap)
    rows = await search_available_numbers(account_sid="AC", auth_token="tok", area_code="512")
    assert rows and rows[0]["phone_number"] == "+17775550001"
    assert cap.params is not None
    assert cap.params.get("SmsEnabled") == "true", "a voice-only number cannot text"
    assert cap.params.get("VoiceEnabled") == "true"
    assert cap.params.get("AreaCode") == "512"


async def test_number_search_omits_an_empty_area_code(monkeypatch):
    from helixis.connectors.twilio_client import search_available_numbers

    cap = _CapturedGet({"available_phone_numbers": []})
    _patch_http(monkeypatch, get=cap)
    await search_available_numbers(account_sid="AC", auth_token="tok")
    assert cap.params is not None
    assert "AreaCode" not in cap.params, "an empty area code must not narrow the search to ''"


async def test_buying_refuses_without_a_webhook_url():
    """A number that exists but is not wired receives messages that go
    nowhere — and by then the recurring charge is already incurred. Refuse
    BEFORE spending."""
    from helixis.connectors.twilio_client import TwilioProvisionError, buy_number

    # ⚠ The message matters. Without it this test passes for the WRONG reason:
    # delete the guard and the call falls through to a real HTTP request, which
    # also raises TwilioProvisionError — a network error wearing the costume of
    # a refusal (gotcha 33: an attack that never runs is not one that failed).
    with pytest.raises(TwilioProvisionError, match="webhook base URL"):
        await buy_number(
            account_sid="AC",
            auth_token="tok",
            phone_number="+17775550001",
            webhook_base_url="",
        )


async def test_buying_wires_all_three_webhooks_in_the_same_request(monkeypatch):
    """The gap between "bought" and "configured" is exactly where a
    half-finished setup lives, so there must not be one."""
    captured = _CapturedPost()
    _patch_http(monkeypatch, post=captured)

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"phone_number": "+17775550001"}

    def _post(*a, **kw):
        captured.form = dict(kw.get("data") or {})
        return _Resp()

    _patch_http(monkeypatch, post=_post)
    from helixis.connectors.twilio_client import buy_number

    got = await buy_number(
        account_sid="AC",
        auth_token="tok",
        phone_number="+17775550001",
        webhook_base_url="https://api.example.com/",  # trailing slash on purpose
    )
    assert got == "+17775550001"
    form = captured.form or {}
    # ⚠ The trailing slash must be stripped: the FULL url participates in the
    # signature HMAC, so "//api/v1/..." 403s every inbound request.
    assert form["SmsUrl"] == "https://api.example.com/api/v1/twilio/inbound"
    assert form["VoiceUrl"] == "https://api.example.com/api/v1/crm/voice/inbound"
    assert form["StatusCallback"] == "https://api.example.com/api/v1/twilio/status"
    # Inbound SMS goes to the route with NO signature bypass (gotcha 68).
    assert "/crm/sms/inbound" not in form["SmsUrl"]
