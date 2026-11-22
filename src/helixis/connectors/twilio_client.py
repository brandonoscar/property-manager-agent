"""Twilio SMS client — outbound send + inbound webhook signature check.

Deliberately dependency-light: no ``twilio`` SDK. Outbound goes through the
REST API with ``httpx`` (already a base dep); inbound signature validation
is the small, well-documented HMAC-SHA1 algorithm implemented inline. This
keeps the image lean and the crypto auditable.

Wired from :mod:`helixis.api.routes.crm`:
- :func:`send_sms` — a user-approved reply to a lead (never auto-sent).
- :func:`validate_signature` — proves an inbound ``POST /crm/sms/inbound``
  really came from Twilio before it's allowed to create a lead.

All functions are self-contained and take the values they need, so they're
unit-testable without a live account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from xml.sax.saxutils import escape as _xml_escape

import httpx

logger = logging.getLogger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
_SEND_TIMEOUT_S = 15.0


class TwilioSendError(RuntimeError):
    """Raised when an outbound SMS could not be sent (auth, network, 4xx/5xx).

    ``pre_side_effect`` is True only when the message provably was NOT created:
    local validation/config failures (nothing left the process) and Twilio 4xx
    REST errors (the resource was rejected, not created). Network failures and
    5xx stay False — the send MAY have landed, so the idempotency guard must
    treat a retry as ambiguous, never as safe.
    """

    def __init__(self, message: str, *, pre_side_effect: bool = False) -> None:
        super().__init__(message)
        self.pre_side_effect = pre_side_effect


class TwilioCallError(RuntimeError):
    """Raised when an outbound (click-to-call) call could not be placed.

    ``pre_side_effect`` semantics identical to :class:`TwilioSendError`.
    """

    def __init__(self, message: str, *, pre_side_effect: bool = False) -> None:
        super().__init__(message)
        self.pre_side_effect = pre_side_effect


# ── TwiML builders (voice call flow) ─────────────────────────────────
#
# Twilio answers a voice webhook by executing the TwiML we return. We build
# the small XML by hand (no SDK) and keep the pieces pure + unit-testable.
# ``action`` URLs are ROOT-RELATIVE ("/api/v1/…") on purpose: Twilio resolves
# them against the public https URL it called, so we never have to know our
# own hostname (dodges the proxy/base_url problem).


def dial_twiml(*, to: str, action_url: str, timeout: int = 20) -> str:
    """TwiML that forwards the call to ``to`` (the PM's phone). When the dial
    finishes — answered-then-hung-up, or never answered — Twilio POSTs
    ``action_url`` with ``DialCallStatus`` so we can fall through to voicemail
    on a miss. The caller's own number passes through as caller ID."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial timeout="{int(timeout)}" action="{_xml_escape(action_url)}">'
        f"{_xml_escape(to)}</Dial>"
        "</Response>"
    )


def voicemail_twiml(
    *,
    greeting: str,
    action_url: str,
    max_length: int = 120,
    transcribe_callback: str | None = None,
) -> str:
    """TwiML that plays ``greeting`` then records a voicemail, POSTing the
    recording to ``action_url`` when the caller hangs up or ``max_length`` is
    hit. When ``transcribe_callback`` is given, Twilio transcribes the message
    and POSTs the text (``TranscriptionText`` + ``RecordingSid``) to that URL."""
    transcribe_attrs = (
        f' transcribe="true" transcribeCallback="{_xml_escape(transcribe_callback)}"'
        if transcribe_callback
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{_xml_escape(greeting)}</Say>"
        f'<Record maxLength="{int(max_length)}" playBeep="true" '
        f'action="{_xml_escape(action_url)}"{transcribe_attrs} />'
        "<Say>We didn't get a recording. Goodbye.</Say>"
        "</Response>"
    )


def say_hangup_twiml(message: str) -> str:
    """TwiML that says one line and hangs up — the end of the voicemail flow."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{_xml_escape(message)}</Say></Response>"
    )


def empty_twiml() -> str:
    """An empty TwiML response — accept + do nothing (the forwarded call
    already connected and completed on its own)."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def bridge_twiml(*, to: str, message: str = "Connecting your call.") -> str:
    """TwiML that greets the PM (when Helixis's click-to-call rings them) then
    dials the lead — bridging the two legs. Runs on the PM's leg once they
    answer."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{_xml_escape(message)}</Say>"
        f"<Dial>{_xml_escape(to)}</Dial>"
        "</Response>"
    )


def validate_signature(
    auth_token: str,
    url: str,
    params: dict[str, str],
    signature: str | None,
) -> bool:
    """Verify Twilio's ``X-Twilio-Signature`` on an inbound webhook.

    Twilio signs the request by concatenating the full request URL with each
    POST param (sorted by key, ``key`` immediately followed by ``value``, no
    separators), HMAC-SHA1'ing that with the account ``auth_token``, and
    base64-encoding the result. We recompute it and constant-time compare.

    Returns False on any missing input rather than raising, so the caller
    can uniformly 403 a bad/absent signature.
    """
    if not auth_token or not signature:
        return False
    payload = url
    for key in sorted(params):
        payload += key + (params[key] or "")
    digest = hmac.new(
        auth_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def send_sms(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to: str,
    body: str,
    messaging_service_sid: str = "",
) -> str:
    """Send an SMS and return the Twilio message SID.

    Raises :class:`TwilioSendError` on any failure (missing creds, network,
    non-2xx) so the calling route can surface a 502 instead of silently
    dropping a reply the user approved.

    ⚠ **A Messaging Service is a valid sender and used to be rejected here.**
    ``from_number`` was hard-required and ``From`` always posted, so a
    deployment configured with ``messaging_service_sid`` and no single number
    — the setup that lets you add numbers in the Twilio console instead of
    editing Render env vars for every one — failed every CRM reply while
    ``comms/sms.py`` sent fine. Same split as gotcha 67: two outbound lanes,
    one of them not told about a capability the other had.

    The service takes precedence when both are set, matching ``comms/sms.py``.
    """
    sender = (from_number or "").strip()
    service = (messaging_service_sid or "").strip()
    if not (account_sid and auth_token and (sender or service)):
        raise TwilioSendError(
            "Twilio is not configured (missing account_sid/auth_token, or neither "
            "from_number nor messaging_service_sid).",
            pre_side_effect=True,
        )
    if not to or not body:
        raise TwilioSendError(
            "send_sms requires both a destination number and a body.", pre_side_effect=True
        )

    endpoint = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages.json"
    form = {"To": to, "Body": body}
    if service:
        form["MessagingServiceSid"] = service
    else:
        form["From"] = sender
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            # httpx form-encodes a dict passed as ``data`` and sets the
            # x-www-form-urlencoded content type itself.
            resp = await client.post(endpoint, data=form, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        logger.warning("Twilio send failed (network): %s", exc)
        raise TwilioSendError(f"Twilio request failed: {exc}") from exc

    if resp.status_code >= 300:
        # Twilio returns a JSON error body ({code, message}); include it for
        # debugging without leaking the auth token. A 4xx means the message
        # resource was rejected (not created) → provably not sent; 5xx means
        # Twilio's side failed after receiving it → ambiguous.
        detail = resp.text[:300]
        logger.warning("Twilio send returned %s: %s", resp.status_code, detail)
        raise TwilioSendError(
            f"Twilio returned {resp.status_code}: {detail}",
            pre_side_effect=400 <= resp.status_code < 500,
        )

    sid = ""
    try:
        sid = str(resp.json().get("sid") or "")
    except Exception:  # noqa: BLE001 — a 2xx with an unexpected body still "sent"
        logger.warning("Twilio send succeeded but response JSON was unparseable.")
    return sid


async def create_call(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to: str,
    twiml: str,
) -> str:
    """Place an outbound call to ``to`` and return the Twilio call SID.

    Click-to-call rings the PM first (``to`` = the PM's cell); when they
    answer, Twilio executes ``twiml`` on that leg (which ``<Dial>``s the lead,
    bridging the two). The call originates from ``from_number`` — the business
    number — so the lead sees that, and it's logged/recordable. Raises
    :class:`TwilioCallError` on any failure so the route can 502.
    """
    if not (account_sid and auth_token and from_number):
        raise TwilioCallError(
            "Twilio is not configured (missing account_sid/auth_token/from_number).",
            pre_side_effect=True,
        )
    if not to or not twiml:
        raise TwilioCallError(
            "create_call requires both a destination number and TwiML.", pre_side_effect=True
        )

    endpoint = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Calls.json"
    form = {"To": to, "From": from_number, "Twiml": twiml}
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(endpoint, data=form, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        logger.warning("Twilio call failed (network): %s", exc)
        raise TwilioCallError(f"Twilio request failed: {exc}") from exc

    if resp.status_code >= 300:
        detail = resp.text[:300]
        logger.warning("Twilio call returned %s: %s", resp.status_code, detail)
        raise TwilioCallError(
            f"Twilio returned {resp.status_code}: {detail}",
            pre_side_effect=400 <= resp.status_code < 500,
        )

    sid = ""
    try:
        sid = str(resp.json().get("sid") or "")
    except Exception:  # noqa: BLE001
        logger.warning("Twilio call placed but response JSON was unparseable.")
    return sid


# ── number provisioning ──────────────────────────────────────────────
#
# A company gets its OWN number (migrations 0082/0083) so inbound routing and
# the outbound sender agree. Doing that by hand means: search the console, buy,
# then paste three webhook URLs — per company, forever. These two calls do it
# in-app instead, and set the webhooks AT PURCHASE so nobody ever opens the
# console.
#
# ⚠ ``buy_number`` SPENDS MONEY (~$1.15/month, recurring). Its callers must be
# admin-gated and must check for an existing number FIRST — a double-click that
# buys a second number is a real cost and an ambiguous routing state.


class TwilioProvisionError(RuntimeError):
    """Number search or purchase failed. Carries Twilio's message, never the
    auth token."""


async def search_available_numbers(
    *,
    account_sid: str,
    auth_token: str,
    area_code: str = "",
    country: str = "US",
    limit: int = 5,
) -> list[dict[str, str]]:
    """Local numbers available to buy: ``[{phone_number, friendly_name, locality}]``.

    Read-only and free. ``area_code`` is a preference, not a requirement — an
    unavailable area code returns an empty list rather than raising, so the
    caller can retry nationally instead of failing the setup.

    ⚠ SMS **and** voice capability are both required: a voice-only number
    silently breaks texting, and the failure would surface much later as
    "messages don't send" with nothing pointing here.
    """
    endpoint = (
        f"{_TWILIO_API_BASE}/Accounts/{account_sid}/AvailablePhoneNumbers/{country}/Local.json"
    )
    params: dict[str, str] = {
        "SmsEnabled": "true",
        "VoiceEnabled": "true",
        "PageSize": str(max(1, min(limit, 20))),
    }
    if area_code.strip():
        params["AreaCode"] = area_code.strip()
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.get(endpoint, params=params, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        raise TwilioProvisionError(f"Twilio number search failed: {exc}") from exc
    if resp.status_code >= 300:
        raise TwilioProvisionError(f"Twilio returned {resp.status_code}: {resp.text[:300]}")
    try:
        rows = resp.json().get("available_phone_numbers") or []
    except Exception as exc:  # noqa: BLE001
        raise TwilioProvisionError("Twilio number search returned unparseable JSON") from exc
    return [
        {
            "phone_number": str(r.get("phone_number") or ""),
            "friendly_name": str(r.get("friendly_name") or ""),
            "locality": str(r.get("locality") or ""),
        }
        for r in rows
        if r.get("phone_number")
    ]


async def buy_number(
    *,
    account_sid: str,
    auth_token: str,
    phone_number: str,
    webhook_base_url: str,
    friendly_name: str = "",
) -> str:
    """Buy ``phone_number`` and point its webhooks at us. Returns the E.164 number.

    ⚠ **This is a purchase.** It recurs monthly and there is no undo in this
    function — releasing a number is a separate, deliberate act.

    The webhook URLs are set in the SAME request as the purchase, on purpose:
    a number that exists but is not wired receives messages that go nowhere,
    and the gap between "bought" and "configured" is exactly where a
    half-finished setup lives. ``webhook_base_url`` must match
    ``HELIXIS_TWILIO__WEBHOOK_BASE_URL`` exactly — the full URL participates
    in the signature HMAC, so a trailing slash or scheme mismatch 403s every
    inbound request.

    Inbound SMS goes to ``/api/v1/twilio/inbound``: BOTH inbound routes now do
    the whole job (consent + leads), and that one has no signature bypass.
    """
    base = (webhook_base_url or "").strip().rstrip("/")
    if not base:
        raise TwilioProvisionError(
            "A public webhook base URL is required before buying a number — "
            "an unwired number receives messages that go nowhere."
        )
    if not (account_sid and auth_token and phone_number):
        raise TwilioProvisionError("buy_number requires credentials and a phone number.")

    endpoint = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    # ⚠ ONE definition of where a number points, shared with
    # ``configure_number``. Two copies drift, and a drifted copy means half
    # the numbers on the account answer somewhere else.
    form = {"PhoneNumber": phone_number, **webhook_form(base)}
    if friendly_name.strip():
        form["FriendlyName"] = friendly_name.strip()[:64]
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(endpoint, data=form, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        # ⚠ A network error here is AMBIGUOUS — the purchase may have gone
        # through. Say so; the caller must not silently retry into a second
        # paid number.
        raise TwilioProvisionError(
            f"Twilio purchase request failed and may or may not have completed: {exc}"
        ) from exc
    if resp.status_code >= 300:
        raise TwilioProvisionError(f"Twilio returned {resp.status_code}: {resp.text[:300]}")
    try:
        bought = str(resp.json().get("phone_number") or "")
    except Exception as exc:  # noqa: BLE001
        raise TwilioProvisionError("Number purchased but the response was unparseable") from exc
    if not bought:
        raise TwilioProvisionError("Twilio accepted the purchase but returned no phone number")
    return bought


#: What a number's webhooks must point at, given a base URL. ONE definition,
#: shared by the purchase path and the wire-an-existing-number path — two
#: copies would drift and half your numbers would answer at the wrong place.
def webhook_form(base: str) -> dict[str, str]:
    return {
        "SmsUrl": f"{base}/api/v1/twilio/inbound",
        "SmsMethod": "POST",
        "VoiceUrl": f"{base}/api/v1/crm/voice/inbound",
        "VoiceMethod": "POST",
        "StatusCallback": f"{base}/api/v1/twilio/status",
        "StatusCallbackMethod": "POST",
        # ⚠ CLEARED, not merely unset. An ApplicationSid on a number takes
        # PRECEDENCE over the URL fields, so a number carrying one from a
        # previous project accepts our SmsUrl/VoiceUrl with a 200 and keeps
        # routing everything to the old application — gotcha 20's
        # 200-but-lie, on the one field that decides whether a call reaches
        # anyone. A number we bought never has one; a hand-bought number can.
        "SmsApplicationSid": "",
        "VoiceApplicationSid": "",
    }


async def find_number_sid(*, account_sid: str, auth_token: str, phone_number: str) -> str:
    """The ``PN…`` SID for a number this account owns, or ``""``.

    ⚠ ``""`` means "this account does not own that number", which is a
    legitimate answer: under white-label every customer number lives on our
    account, so a miss is a typo or somebody else's number — never a reason to
    go on and configure something.
    """
    endpoint = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.get(
                endpoint,
                params={"PhoneNumber": phone_number, "PageSize": 1},
                auth=(account_sid, auth_token),
            )
    except httpx.HTTPError as exc:
        raise TwilioProvisionError(f"Could not look the number up: {exc}") from exc
    if resp.status_code >= 300:
        raise TwilioProvisionError(f"Twilio returned {resp.status_code}: {resp.text[:300]}")
    try:
        rows = resp.json().get("incoming_phone_numbers") or []
    except Exception as exc:  # noqa: BLE001
        raise TwilioProvisionError("Number lookup returned an unparseable body") from exc
    return str(rows[0].get("sid") or "") if rows else ""


async def configure_number(
    *,
    account_sid: str,
    auth_token: str,
    phone_number: str,
    webhook_base_url: str,
) -> str:
    """Point an EXISTING number's webhooks at us. Returns its ``PN…`` SID.

    ``buy_number`` wires a number in the same request that buys it, so
    everything Occupella purchases is configured by construction. This is the
    other path: a number bought by hand in the Twilio console and then claimed
    in the app. Nothing wired it, so it accepted calls and texts and delivered
    them nowhere — silently, with the number showing as set up.

    ⚠ **The write is VERIFIED, not assumed.** Twilio answers 200 and ignores
    ``VoiceUrl`` when the number carries a ``VoiceApplicationSid``, so the
    response is re-read and the URLs compared against what was sent. A 2xx is
    not the write (gotcha 70) — and here the difference between "configured"
    and "silently unchanged" is whether anyone's phone ever rings.
    """
    base = (webhook_base_url or "").strip().rstrip("/")
    if not base:
        raise TwilioProvisionError(
            "A public webhook base URL is required before wiring a number — "
            "there is nothing to point it at."
        )
    if not (account_sid and auth_token and phone_number):
        raise TwilioProvisionError("configure_number requires credentials and a phone number.")

    number_sid = await find_number_sid(
        account_sid=account_sid, auth_token=auth_token, phone_number=phone_number
    )
    if not number_sid:
        raise TwilioProvisionError(
            f"This Twilio account does not own {phone_number} — nothing to configure."
        )

    form = webhook_form(base)
    endpoint = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/IncomingPhoneNumbers/{number_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_S) as client:
            resp = await client.post(endpoint, data=form, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        raise TwilioProvisionError(f"Could not configure the number: {exc}") from exc
    if resp.status_code >= 300:
        raise TwilioProvisionError(f"Twilio returned {resp.status_code}: {resp.text[:300]}")

    try:
        applied = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise TwilioProvisionError("Number configured but the response was unparseable") from exc
    # The read-back. Twilio echoes the resource it stored, so a field that did
    # not take is visible right here — no second request needed.
    for sent_key, got_key in (("SmsUrl", "sms_url"), ("VoiceUrl", "voice_url")):
        if str(applied.get(got_key) or "") != form[sent_key]:
            raise TwilioProvisionError(
                f"Twilio accepted the change but {got_key} is still "
                f"{applied.get(got_key)!r} — the number is not wired to us. "
                "This usually means an application is attached to it in the console."
            )
    return number_sid
