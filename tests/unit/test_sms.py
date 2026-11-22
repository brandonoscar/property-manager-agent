"""Outbound customer-care SMS (comms/sms.py) — guardrail + smoke-check tests.

Every test is offline: httpx is monkeypatched, the consent "pool" is an
AsyncMock. The guardrails under test are compliance invariants (dark by
default, consent ledger fail-closed, TCPA quiet hours, templates-only), so
each block reason gets its own pinned test rather than one happy path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from helixis.comms import sms as sms_mod
from helixis.comms.sms import (
    TEMPLATES,
    SmsBlocked,
    SmsSendError,
    redact_phone,
    render_template,
    send_sms,
)
from helixis.smoke import checks as _checks

AUTH_TOKEN = "super-secret-twilio-token-000"
PHONE = "+15551234567"
COMPANY = "11111111-1111-1111-1111-111111111111"

RENT_PARAMS = {
    "first_name": "Dana",
    "amount": "$1,450.00",
    "property_name": "Cedar Ridge #4B",
    "due_date": "Sep 1",
}


def _settings(
    *,
    enabled: bool = True,
    sid: str = "ACxxx",
    token: str = AUTH_TOKEN,
    from_number: str = "+18885550000",
    messaging_service_sid: str = "",
):  # noqa: ANN202
    from helixis.settings import Settings

    s = Settings()
    s.twilio.enabled = enabled
    s.twilio.account_sid = sid
    s.twilio.auth_token = token
    s.twilio.from_number = from_number
    s.twilio.messaging_service_sid = messaging_service_sid
    return s


def _pool(
    row: Any = None,
    *,
    error: Exception | None = None,
    company_number: Any = None,
) -> MagicMock:
    """A pool that answers the consent lookup AND the telephony lookup.

    ⚠ ``send_sms`` resolves THIS company's own sending number (migration
    0082/0083) before it renders anything — a shared ``from_number`` is a
    bidirectional leak, because the recipient's reply comes back to whichever
    company owns the number they see. That lookup goes through
    ``pool.acquire()``, which a bare MagicMock cannot satisfy.

    ``company_number=None`` leaves the mapping table EMPTY, which is the
    single-tenant state where the deployment-global settings still apply — so
    these tests keep exercising the guard order they were written for.
    """
    pool = MagicMock()

    async def _pool_fetchrow(query: str = "", *args: Any, **_kw: Any) -> Any:
        # ⚠ SQL-AWARE, and it has to be. Two different reads now land on
        # ``pool.fetchrow`` — the consent row and this company's A2P
        # registration — so a fixture that answers every query with the consent
        # row hands the campaign gate a row with no ``status`` and every send
        # in this file is refused for a reason unrelated to what it tests.
        if "helixis_a2p_registrations" in str(query):
            # No row = the single-tenant deployment, where the global flag
            # alone decides. That is the state these tests describe.
            return None
        if error is not None:
            raise error
        return row

    pool.fetchrow = AsyncMock(side_effect=_pool_fetchrow)

    conn = MagicMock()
    # telephony_for_company asks for the company's row, then (only when there
    # is none) whether ANY row exists at all.
    conn.fetchrow = AsyncMock(side_effect=lambda *a, **k: _telephony_answer(company_number, a))
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _telephony_answer(company_number: Any, args: tuple[Any, ...]) -> Any:
    """Row for the company query; None for the any-mapping-exists query."""
    if company_number is None:
        return None
    if len(args) >= 2:  # the company-scoped query carries a company_id param
        return {
            "phone_number": company_number,
            "forward_to_number": None,
            "voice_greeting": None,
        }
    return None


def _consent(tz: str = "America/New_York", revoked_at: Any = None) -> dict[str, Any]:
    return {"revoked_at": revoked_at, "timezone": tz}


def _local_utc(tz: str, hour: int, minute: int) -> datetime:
    """A UTC instant that is ``hour:minute`` local time in ``tz`` (a fixed
    summer date, so US zones are in DST — the conversion stays honest)."""
    return datetime(2026, 8, 18, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


class _NetworkForbidden:
    """AsyncClient stand-in that fails the test if any network path runs."""

    def __init__(self, **_kwargs: object) -> None:
        raise AssertionError("guardrail must block BEFORE any network call")


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Captures the POST/GET a check or send makes and returns a canned reply."""

    captured: dict[str, Any] = {}
    response: _FakeResponse = _FakeResponse(201, {"sid": "SM123", "status": "queued"})

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, data: Any = None, auth: Any = None) -> _FakeResponse:
        _FakeClient.captured = {"url": url, "data": data, "auth": auth}
        return _FakeClient.response

    async def get(self, url: str, *, auth: Any = None, headers: Any = None) -> _FakeResponse:
        _FakeClient.captured = {"url": url, "auth": auth}
        return _FakeClient.response


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.captured = {}
    _FakeClient.response = _FakeResponse(201, {"sid": "SM123", "status": "queued"})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)


_DAYTIME = _local_utc("America/New_York", 12, 0)  # comfortably inside quiet hours


# ── Dark by default / configuration ─────────────────────────────────


async def test_disabled_blocks_before_any_network_call(no_network: None) -> None:
    pool = _pool(_consent())
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(enabled=False),
            pool=pool,
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "disabled"
    pool.fetchrow.assert_not_awaited()  # blocked before even the consent lookup


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sid": ""},
        {"token": ""},
        {"from_number": "", "messaging_service_sid": ""},
    ],
)
async def test_missing_creds_or_sender_blocks_not_configured(
    no_network: None, kwargs: dict[str, str]
) -> None:
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(**kwargs),
            pool=_pool(_consent()),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "not_configured"


async def test_messaging_service_sid_alone_is_a_valid_sender(fake_http: type[_FakeClient]) -> None:
    result = await send_sms(
        settings=_settings(from_number="", messaging_service_sid="MGxxx"),
        pool=_pool(_consent()),
        company_id=COMPANY,
        to_phone=PHONE,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_DAYTIME,
    )
    assert result.sid == "SM123"
    form = fake_http.captured["data"]
    assert form["MessagingServiceSid"] == "MGxxx"
    assert "From" not in form


# ── Reachable region ─────────────────────────────────────────────────


async def test_a_non_north_american_number_is_refused_before_the_ledger(
    no_network: None,
) -> None:
    """The A2P 10DLC campaign is a US programme — its samples, consent
    language and brand are all US — and Twilio's geo permissions are off
    elsewhere by default. So this is not a recipient we are blocked from
    texting right now; it is one the product cannot reach, and the honest
    failure is a named refusal rather than a 21408 nobody reads.

    ⚠ Ahead of the consent lookup deliberately: `_pool(None)` here means "no
    consent row", so a refusal ordered after it would answer `no_consent` and
    send somebody to fix a ledger entry when the number is the problem.
    """
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(None),
            company_id=COMPANY,
            to_phone="+442071234567",
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "unsupported_region"


# ── Consent ledger (fail closed) ─────────────────────────────────────


async def test_no_consent_row_blocks(no_network: None) -> None:
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(None),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "no_consent"


async def test_consent_lookup_error_fails_closed(no_network: None) -> None:
    """A broken pool must read as NO consent — never as 'send anyway'."""
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(error=RuntimeError("db down")),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "no_consent"


async def test_revoked_consent_blocks(no_network: None) -> None:
    row = _consent(revoked_at=datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(row),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "revoked"


async def test_unparseable_timezone_fails_closed(no_network: None) -> None:
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz="Not/AZone")),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert exc.value.reason == "quiet_hours"


# ── Quiet hours — federal-or-stricter, by RECIPIENT state ────────────
#
# ⚠ These used PHONE (+1555…), an area code no state owns, so every case ran
# against the unplaceable-number fallback and NONE of them exercised the real
# per-state windows. The numbers below are real NPAs in the state each case
# names, so the assertion is about the statute it claims to be about.

_NY = "+12125550100"  # New York — federal 8am-9pm
_FL = "+13055550100"  # Florida — FTSA, 8am-8PM
_TX = "+12145550100"  # Texas — SB 140, 9AM-9pm Mon-Sat, noon Sunday


@pytest.mark.parametrize("tz", ["America/New_York", "America/Los_Angeles"])
@pytest.mark.parametrize(("hour", "minute"), [(7, 59), (21, 1)])
async def test_quiet_hours_blocks_outside_the_federal_window(
    no_network: None, tz: str, hour: int, minute: int
) -> None:
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz=tz)),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_local_utc(tz, hour, minute),
        )
    assert exc.value.reason == "quiet_hours"


@pytest.mark.parametrize(("hour", "minute"), [(8, 0), (20, 59)])
async def test_the_federal_edges_still_send_for_a_federal_state(
    fake_http: type[_FakeClient], hour: int, minute: int
) -> None:
    """New York has no mini-TCPA, so 8:00 and 20:59 remain legal. Tightening
    every state to the strictest window would be safe and would also make the
    product worse everywhere for the sake of four states."""
    result = await send_sms(
        settings=_settings(),
        pool=_pool(_consent(tz="America/New_York")),
        company_id=COMPANY,
        to_phone=_NY,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_local_utc("America/New_York", hour, minute),
    )
    assert result.status == "queued"


async def test_florida_blocks_an_hour_the_federal_rule_allows(no_network: None) -> None:
    """⚠ THE ONE THE OLD WINDOW GOT WRONG. 20:30 is legal federally and a
    per-message FTSA penalty in Florida, which has a private right of action."""
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz="America/New_York")),
            company_id=COMPANY,
            to_phone=_FL,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_local_utc("America/New_York", 20, 30),
        )
    assert exc.value.reason == "quiet_hours"


async def test_texas_blocks_the_federal_morning_hour(no_network: None) -> None:
    """The other direction: SB 140 starts at 9am, so 8:30 is federally fine
    and $5,000/violation in Texas. A window wrong at ONE end is still wrong."""
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz="America/Chicago")),
            company_id=COMPANY,
            to_phone=_TX,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_local_utc("America/Chicago", 8, 30),
        )
    assert exc.value.reason == "quiet_hours"


async def test_texas_sunday_waits_until_noon(no_network: None) -> None:
    """SB 140's Sunday rule, read on the RECIPIENT's clock. 2026-09-06 is a
    Sunday; 10:00 is legal in Texas Mon-Sat and not on a Sunday."""
    sunday_10am = _local_utc("America/Chicago", 10, 0).replace(year=2026, month=9, day=6)
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz="America/Chicago")),
            company_id=COMPANY,
            to_phone=_TX,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=sunday_10am,
        )
    assert exc.value.reason == "quiet_hours"


async def test_an_unplaceable_number_takes_the_strictest_window(no_network: None) -> None:
    """PHONE's NPA belongs to no state, so no statute can be ruled out and
    every one of them must hold at once (gotcha 55: a guard that cannot see
    declines rather than assuming the permissive reading)."""
    with pytest.raises(SmsBlocked) as exc:
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent(tz="America/New_York")),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_local_utc("America/New_York", 8, 30),
        )
    assert exc.value.reason == "quiet_hours"


# ── Templates-only rendering (strict) ────────────────────────────────


def test_registered_templates_carry_brand_and_opt_out() -> None:
    assert set(TEMPLATES) == {
        "rent_reminder",
        "maintenance_update",
        "appointment_confirmation",
        "account_notice",
    }
    for body in TEMPLATES.values():
        assert body.startswith("Occupella: ")
        assert "Reply STOP to cancel" in body
    for tid in ("rent_reminder", "account_notice"):
        assert ", HELP for help" in TEMPLATES[tid]


def test_render_unknown_template_raises() -> None:
    with pytest.raises(ValueError, match="unknown SMS template"):
        render_template("marketing_blast", {})


def test_render_missing_param_raises() -> None:
    params = dict(RENT_PARAMS)
    del params["amount"]
    with pytest.raises(ValueError, match="missing params.*amount"):
        render_template("rent_reminder", params)


def test_render_extra_param_raises() -> None:
    with pytest.raises(ValueError, match="unexpected params.*free_text"):
        render_template("rent_reminder", {**RENT_PARAMS, "free_text": "click here!"})


def test_render_fills_placeholders() -> None:
    body = render_template("rent_reminder", RENT_PARAMS)
    assert "Dana" in body and "$1,450.00" in body and "Cedar Ridge #4B" in body
    assert body.startswith("Occupella: ")


def test_module_has_no_free_form_send_path() -> None:
    """send_sms takes template_id+params only — no body/message kwarg exists."""
    import inspect

    sig = inspect.signature(send_sms)
    assert "body" not in sig.parameters
    assert "message" not in sig.parameters
    assert "template_id" in sig.parameters


# ── Successful send + secret hygiene ─────────────────────────────────


async def test_successful_send_posts_correct_form_fields(
    fake_http: type[_FakeClient],
) -> None:
    result = await send_sms(
        settings=_settings(),
        pool=_pool(_consent()),
        company_id=COMPANY,
        to_phone=PHONE,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_DAYTIME,
    )
    assert result.sid == "SM123"
    assert result.to_phone == PHONE
    assert result.template_id == "rent_reminder"
    assert result.status == "queued"

    assert fake_http.captured["url"] == (
        "https://api.twilio.com/2010-04-01/Accounts/ACxxx/Messages.json"
    )
    assert fake_http.captured["auth"] == ("ACxxx", AUTH_TOKEN)  # Basic auth pair
    form = fake_http.captured["data"]
    assert form["To"] == PHONE
    assert form["From"] == "+18885550000"
    assert form["Body"] == render_template("rent_reminder", RENT_PARAMS)


async def test_non_2xx_raises_with_twilio_code_but_never_the_token(
    fake_http: type[_FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    fake_http.response = _FakeResponse(400, {"code": 21610, "message": "blocked recipient"})
    with caplog.at_level(logging.DEBUG, logger=sms_mod.logger.name):
        with pytest.raises(SmsSendError) as exc:
            await send_sms(
                settings=_settings(),
                pool=_pool(_consent()),
                company_id=COMPANY,
                to_phone=PHONE,
                template_id="rent_reminder",
                params=RENT_PARAMS,
                now=_DAYTIME,
            )
    assert "21610" in str(exc.value)
    assert AUTH_TOKEN not in str(exc.value)
    assert AUTH_TOKEN not in caplog.text


async def test_logs_redact_phone_and_omit_body(
    fake_http: type[_FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=sms_mod.logger.name):
        await send_sms(
            settings=_settings(),
            pool=_pool(_consent()),
            company_id=COMPANY,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert "***-**4567" in caplog.text
    assert PHONE not in caplog.text  # full number never logged
    assert "Dana" not in caplog.text and "$1,450.00" not in caplog.text  # no body content


async def test_blocked_attempts_are_logged_with_reason(
    no_network: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=sms_mod.logger.name):
        with pytest.raises(SmsBlocked):
            await send_sms(
                settings=_settings(),
                pool=_pool(None),
                company_id=COMPANY,
                to_phone=PHONE,
                template_id="rent_reminder",
                params=RENT_PARAMS,
                now=_DAYTIME,
            )
    assert "reason=no_consent" in caplog.text
    assert "***-**4567" in caplog.text


def test_redact_phone_shapes() -> None:
    assert redact_phone("+15551234567") == "***-**4567"
    assert redact_phone("(555) 123-4567") == "***-**4567"
    assert redact_phone("") == "***-****"
    assert redact_phone("123") == "***-****"


# ── Smoke check (twilio_sms) ─────────────────────────────────────────


async def test_check_twilio_skips_when_disabled() -> None:
    result = await _checks.check_twilio(_settings(enabled=False))
    assert result.status == "skipped"
    assert "not configured" in result.detail


async def test_check_twilio_skips_when_creds_missing() -> None:
    result = await _checks.check_twilio(_settings(token=""))
    assert result.status == "skipped"


async def test_check_twilio_passes_on_active_account(fake_http: type[_FakeClient]) -> None:
    fake_http.response = _FakeResponse(200, {"status": "active"})
    result = await _checks.check_twilio(_settings())
    assert result.status == "pass"
    assert "active" in result.detail
    # Read-only: it probed the ACCOUNT resource, not Messages.
    assert fake_http.captured["url"].endswith("/Accounts/ACxxx.json")


async def test_check_twilio_fails_on_non_200(fake_http: type[_FakeClient]) -> None:
    fake_http.response = _FakeResponse(401, {"code": 20003, "message": "authenticate"})
    result = await _checks.check_twilio(_settings())
    assert result.status == "fail"
    assert "401" in result.detail


async def test_check_twilio_fails_on_suspended_account(fake_http: type[_FakeClient]) -> None:
    fake_http.response = _FakeResponse(200, {"status": "suspended"})
    result = await _checks.check_twilio(_settings())
    assert result.status == "fail"
    assert "suspended" in result.detail
