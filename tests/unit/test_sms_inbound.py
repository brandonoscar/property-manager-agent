"""Twilio customer-care SMS webhooks (api/routes/twilio.py) + Stage-2 pieces
of comms/sms.py (outbound logging, StatusCallback).

Every test is offline: the DB "pool" is an in-memory fake that interprets
the exact SQL shapes the code issues, and httpx is monkeypatched for the
outbound tests. Signature fixtures are computed IN THE TEST with the raw
hmac/hashlib recipe (not the production helper), so the validator is pinned
against Twilio's documented scheme rather than against itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from helixis.api.routes import twilio as twilio_route
from helixis.comms import sms as sms_mod
from helixis.comms.sms import send_sms
from helixis.comms.twilio_signature import validate_twilio_signature
from helixis.main import create_app
from helixis.settings import Settings

AUTH_TOKEN = "super-secret-twilio-token-000"
BASE_URL = "https://your-api.onrender.com"
PHONE = "+15551234567"
COMPANY_A = "11111111-1111-1111-1111-111111111111"
COMPANY_B = "22222222-2222-2222-2222-222222222222"

_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

RENT_PARAMS = {
    "first_name": "Dana",
    "amount": "$1,450.00",
    "property_name": "Cedar Ridge #4B",
    "due_date": "Sep 1",
}
# Noon Eastern on a summer date — inside the quiet-hours window.
_DAYTIME = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


def _twilio_sign(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's documented scheme, computed independently of production code:
    URL + sorted key/value concatenation → HMAC-SHA1 → base64."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


# ── in-memory pool speaking the exact SQL the code issues ─────────────


def _consent_row(
    company_id: str,
    phone: str = PHONE,
    *,
    revoked_at: datetime | None = None,
    revocation_source: str | None = None,
) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "phone": phone,
        "revoked_at": revoked_at,
        "revocation_source": revocation_source,
        "consented_at": _T0,
        "source": "app_settings",
        "timezone": "America/New_York",
    }


class FakePool:
    """Interprets the module's four SQL shapes against in-memory rows, so
    tests assert real ledger semantics (idempotence, cross-company scope,
    no-create-on-START) instead of SQL strings."""

    def __init__(self, consent: list[dict[str, Any]] | None = None) -> None:
        self.consent = consent or []
        self.log_rows: list[dict[str, Any]] = []
        # Numbers this deployment has mapped to companies (migration 0082).
        # EMPTY is the single-tenant state, where the deployment-global
        # ``from_number`` still applies — which is what these tests assume.
        self.numbers: list[dict[str, Any]] = []

    def acquire(self) -> Any:
        """``telephony_for_company`` / ``resolve_company_for_number`` take a
        connection out of the pool. Same object, since this fake interprets
        SQL rather than holding a session."""
        pool = self

        class _Acquire:
            async def __aenter__(self) -> Any:
                return pool

            async def __aexit__(self, *_a: Any) -> bool:
                return False

        return _Acquire()

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "UPDATE public.helixis_sms_consent" in q and "revoked_at = now()" in q:
            (phone,) = args
            n = 0
            for r in self.consent:
                if r["phone"] == phone and r["revoked_at"] is None:
                    r["revoked_at"] = _NOW
                    r["revocation_source"] = "twilio_stop_webhook"
                    n += 1
            return f"UPDATE {n}"
        if "UPDATE public.helixis_sms_consent" in q and "revoked_at = NULL" in q:
            (phone,) = args
            n = 0
            for r in self.consent:
                if r["phone"] == phone and r["revoked_at"] is not None:
                    r["revoked_at"] = None
                    r["revocation_source"] = None
                    r["consented_at"] = _NOW
                    r["source"] = "twilio_start_webhook"
                    n += 1
            return f"UPDATE {n}"
        if "INSERT INTO public.helixis_sms_log" in q and "'outbound'" in q:
            company_id, phone, template_id, sid = args
            self.log_rows.append(
                {
                    "company_id": company_id,
                    "direction": "outbound",
                    "phone": phone,
                    "template_id": template_id,
                    "twilio_sid": sid,
                    "status": "queued",
                    "error_code": None,
                    "body": None,
                }
            )
            return "INSERT 0 1"
        if "UPDATE public.helixis_sms_log" in q:
            sid, status, error_code = args
            n = 0
            for r in self.log_rows:
                if r["twilio_sid"] == sid:
                    r["status"] = status
                    r["error_code"] = error_code
                    n += 1
            return f"UPDATE {n}"
        raise AssertionError(f"FakePool got unexpected SQL: {q}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if "INSERT INTO public.helixis_sms_log" in q and "'inbound'" in q:
            # RETURNING created_at: the route gates the Inbox bridge on a FRESH
            # row, so this must return None on a duplicate SID exactly as
            # ON CONFLICT ... DO NOTHING does — that None IS the idempotency.
            company_id, phone, body, sid = args
            if sid is not None and any(r["twilio_sid"] == sid for r in self.log_rows):
                return None
            self.log_rows.append(
                {
                    "company_id": company_id,
                    "direction": "inbound",
                    "phone": phone,
                    "template_id": None,
                    "twilio_sid": sid,
                    "status": "received",
                    "error_code": None,
                    "body": body,
                }
            )
            return {"created_at": _NOW}
        if "SELECT company_id FROM public.helixis_sms_consent" in q:
            (phone,) = args
            matches = sorted(
                (r for r in self.consent if r["phone"] == phone),
                key=lambda r: r["consented_at"],
            )
            return {"company_id": matches[0]["company_id"]} if matches else None
        if "SELECT revoked_at, timezone FROM public.helixis_sms_consent" in q:
            company_id, phone = args
            for r in self.consent:
                if r["company_id"] == company_id and r["phone"] == phone:
                    return {"revoked_at": r["revoked_at"], "timezone": r["timezone"]}
            return None
        # Migration 0082/0083 — which company owns which number. ``self.numbers``
        # is empty by default: the single-tenant state, where the global
        # ``from_number`` still applies and these tests' assumptions hold.
        if "FROM public.helixis_twilio_numbers" in q and "company_id = $1" in q:
            (company_id,) = args
            for r in self.numbers:
                if r["company_id"] == company_id:
                    return r
            return None
        if "FROM public.helixis_twilio_numbers" in q:  # "does ANY mapping exist"
            return {"?column?": 1} if self.numbers else None
        # White-label A2P: no registration row = the single-tenant deployment,
        # where the global ``enabled`` flag alone decides. That is the state
        # these tests describe, and it is a REAL state — an operator who
        # registered directly with Twilio never gets a row.
        if "FROM public.helixis_a2p_registrations" in q:
            return None
        raise AssertionError(f"FakePool got unexpected SQL: {q}")


# ── app/client plumbing (mirrors test_crm_routes.py) ─────────────────

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"

_TWILIO_CONFIGURED = {
    "enabled": True,
    "account_sid": "ACxxx",
    "auth_token": AUTH_TOKEN,
    "from_number": "+18885550000",
    "webhook_base_url": BASE_URL,
}


def _settings(twilio: dict[str, Any] | None = None) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        twilio=_TWILIO_CONFIGURED if twilio is None else twilio,
    )


def _post(
    settings: Settings,
    pool: Any,
    path: str,
    data: dict[str, str],
    *,
    signature: str | None = "auto",
) -> Any:
    """POST to the app with an X-Twilio-Signature. ``signature='auto'``
    computes the correct one; None omits the header; any other string is
    sent verbatim (a bad signature)."""
    headers: dict[str, str] = {}
    if signature == "auto":
        headers["X-Twilio-Signature"] = _twilio_sign(AUTH_TOKEN, f"{BASE_URL}{path}", data)
    elif signature is not None:
        headers["X-Twilio-Signature"] = signature
    with (
        patch("helixis.api.routes.twilio.get_pool", return_value=pool) as get_pool_mock,
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(path, data=data, headers=headers)
    resp.pool_was_touched = get_pool_mock.called  # type: ignore[attr-defined]
    return resp


INBOUND = "/api/v1/twilio/inbound"
STATUS = "/api/v1/twilio/status"


# ── signature validator (unit, known-good fixture) ───────────────────


def test_validator_accepts_known_good_fixture() -> None:
    url = f"{BASE_URL}{INBOUND}"
    params = {"From": PHONE, "Body": "STOP", "MessageSid": "SM1"}
    good = _twilio_sign(AUTH_TOKEN, url, params)
    assert validate_twilio_signature(auth_token=AUTH_TOKEN, url=url, params=params, signature=good)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p: {**p, "Body": "START"}, "tampered param value"),
        (lambda p: {**p, "Extra": "x"}, "injected param"),
        (lambda p: {k: v for k, v in p.items() if k != "MessageSid"}, "dropped param"),
    ],
)
def test_validator_rejects_any_param_tampering(mutate: Any, reason: str) -> None:
    url = f"{BASE_URL}{INBOUND}"
    params = {"From": PHONE, "Body": "STOP", "MessageSid": "SM1"}
    good = _twilio_sign(AUTH_TOKEN, url, params)
    assert not validate_twilio_signature(
        auth_token=AUTH_TOKEN, url=url, params=mutate(params), signature=good
    ), reason


def test_validator_rejects_url_mismatch_and_fails_closed_on_missing_inputs() -> None:
    url = f"{BASE_URL}{INBOUND}"
    params = {"From": PHONE, "Body": "hi"}
    good = _twilio_sign(AUTH_TOKEN, url, params)
    # Signed for a different host/path → invalid (the URL is in the HMAC).
    assert not validate_twilio_signature(
        auth_token=AUTH_TOKEN,
        url="https://evil.example.com" + INBOUND,
        params=params,
        signature=good,
    )
    # Missing inputs are False, never exceptions.
    assert not validate_twilio_signature(auth_token="", url=url, params=params, signature=good)
    assert not validate_twilio_signature(
        auth_token=AUTH_TOKEN, url="", params=params, signature=good
    )
    assert not validate_twilio_signature(
        auth_token=AUTH_TOKEN, url=url, params=params, signature=None
    )


# ── route guard: 403 fail-closed, before any DB work ─────────────────


@pytest.mark.parametrize("path", [INBOUND, STATUS])
def test_valid_signature_is_accepted(path: str) -> None:
    resp = _post(_settings(), FakePool(), path, {"From": PHONE, "Body": "hello"})
    assert resp.status_code in (200, 204)


@pytest.mark.parametrize("path", [INBOUND, STATUS])
@pytest.mark.parametrize(
    ("twilio_cfg", "signature"),
    [
        (None, "bm90LXRoZS1yaWdodC1zaWduYXR1cmU="),  # configured, bad signature
        (None, None),  # configured, missing header
        ({**_TWILIO_CONFIGURED, "auth_token": ""}, "auto-ish"),  # empty auth token
        ({**_TWILIO_CONFIGURED, "webhook_base_url": ""}, "auto-ish"),  # empty base URL
    ],
)
def test_bad_or_unverifiable_signature_403s_before_db(
    path: str, twilio_cfg: dict[str, Any] | None, signature: str | None
) -> None:
    if signature == "auto-ish":  # a correctly-computed signature can't save unconfigured
        signature = _twilio_sign(AUTH_TOKEN, f"{BASE_URL}{path}", {"From": PHONE, "Body": "STOP"})
    resp = _post(
        _settings(twilio_cfg),
        FakePool([_consent_row(COMPANY_A)]),
        path,
        {"From": PHONE, "Body": "STOP"},
        signature=signature,
    )
    assert resp.status_code == 403
    assert resp.pool_was_touched is False  # rejected before ANY DB work


# ── STOP / START / HELP ledger semantics ─────────────────────────────


def test_stop_revokes_across_all_companies_and_is_idempotent() -> None:
    settings = _settings()
    pool = FakePool([_consent_row(COMPANY_A), _consent_row(COMPANY_B)])

    resp = _post(settings, pool, INBOUND, {"From": PHONE, "Body": "STOP", "MessageSid": "SM1"})
    assert resp.status_code == 200
    assert all(r["revoked_at"] == _NOW for r in pool.consent)  # BOTH companies
    assert all(r["revocation_source"] == "twilio_stop_webhook" for r in pool.consent)

    # Idempotent: a second STOP leaves already-revoked rows untouched.
    for r in pool.consent:
        r["revoked_at"] = _T0  # distinguishable original revocation instant
    resp = _post(settings, pool, INBOUND, {"From": PHONE, "Body": "STOP", "MessageSid": "SM2"})
    assert resp.status_code == 200
    assert all(r["revoked_at"] == _T0 for r in pool.consent)  # not re-stamped


@pytest.mark.parametrize(
    "body",
    [
        "stop",
        "  STOP  ",
        "UNSUBSCRIBE",
        "StopAll",
        "quit",
        "END",
        "CANCEL",
        # FCC April-2025 standardized revocation keywords beyond Twilio's
        # Advanced Opt-Out defaults — including the two-word and hyphenated
        # forms of opt out, which the route normalizes before matching.
        "REVOKE",
        "revoke",
        "OPT OUT",
        "opt-out",
        "Opt Out",
        "OPTOUT",
    ],
)
def test_stop_family_is_case_and_whitespace_insensitive(body: str) -> None:
    pool = FakePool([_consent_row(COMPANY_A)])
    resp = _post(_settings(), pool, INBOUND, {"From": PHONE, "Body": body})
    assert resp.status_code == 200
    assert pool.consent[0]["revoked_at"] == _NOW


def test_start_restores_only_previously_revoked_rows() -> None:
    revoked = _consent_row(COMPANY_A, revoked_at=_T0, revocation_source="twilio_stop_webhook")
    live = _consent_row(COMPANY_B)  # never revoked — must keep its original grant
    pool = FakePool([revoked, live])

    resp = _post(_settings(), pool, INBOUND, {"From": PHONE, "Body": "START"})
    assert resp.status_code == 200
    assert revoked["revoked_at"] is None
    assert revoked["revocation_source"] is None
    assert revoked["consented_at"] == _NOW
    assert revoked["source"] == "twilio_start_webhook"
    # The never-revoked row is untouched (its original consent record stands).
    assert live["consented_at"] == _T0
    assert live["source"] == "app_settings"


def test_start_with_no_matching_row_creates_no_consent() -> None:
    pool = FakePool([])  # no prior relationship
    resp = _post(_settings(), pool, INBOUND, {"From": PHONE, "Body": "YES"})
    assert resp.status_code == 200
    assert pool.consent == []  # nothing created
    # …but the inbound is still logged (unattributed).
    assert len(pool.log_rows) == 1
    assert pool.log_rows[0]["company_id"] is None


def test_help_touches_no_ledger_but_is_logged() -> None:
    row = _consent_row(COMPANY_A)
    pool = FakePool([row])
    resp = _post(_settings(), pool, INBOUND, {"From": PHONE, "Body": "HELP", "MessageSid": "SM3"})
    assert resp.status_code == 200
    assert row["revoked_at"] is None and row["consented_at"] == _T0  # untouched
    assert len(pool.log_rows) == 1
    assert pool.log_rows[0]["body"] == "HELP"


# ── inbound logging + TwiML response ─────────────────────────────────


def test_every_inbound_gets_a_log_row_with_body_and_company() -> None:
    pool = FakePool([_consent_row(COMPANY_A)])
    resp = _post(
        _settings(),
        pool,
        INBOUND,
        {"From": PHONE, "Body": "when is my rent due?", "MessageSid": "SM77"},
    )
    assert resp.status_code == 200
    assert len(pool.log_rows) == 1
    row = pool.log_rows[0]
    assert row["direction"] == "inbound"
    assert row["status"] == "received"
    assert row["body"] == "when is my rent due?"
    assert row["phone"] == PHONE
    assert row["company_id"] == COMPANY_A  # attributed via the consent row
    assert row["twilio_sid"] == "SM77"
    assert pool.consent[0]["revoked_at"] is None  # a normal text changes no consent


def test_inbound_responds_with_empty_twiml_as_xml() -> None:
    resp = _post(_settings(), FakePool(), INBOUND, {"From": PHONE, "Body": "hi"})
    assert resp.status_code == 200
    assert resp.text == "<Response/>"
    assert resp.headers["content-type"].startswith("application/xml")


# ── delivery receipts (/twilio/status) ───────────────────────────────


def test_status_callback_updates_status_and_error_code() -> None:
    pool = FakePool()
    pool.log_rows.append(
        {
            "company_id": COMPANY_A,
            "direction": "outbound",
            "phone": PHONE,
            "template_id": "rent_reminder",
            "twilio_sid": "SM900",
            "status": "queued",
            "error_code": None,
            "body": None,
        }
    )
    resp = _post(
        _settings(),
        pool,
        STATUS,
        {"MessageSid": "SM900", "MessageStatus": "undelivered", "ErrorCode": "30003"},
    )
    assert resp.status_code == 204
    assert pool.log_rows[0]["status"] == "undelivered"
    assert pool.log_rows[0]["error_code"] == "30003"


def test_status_callback_unknown_sid_warns_and_204s(caplog: pytest.LogCaptureFixture) -> None:
    pool = FakePool()
    with caplog.at_level(logging.WARNING, logger="helixis.api.routes.twilio"):
        resp = _post(
            _settings(), pool, STATUS, {"MessageSid": "SMnope", "MessageStatus": "delivered"}
        )
    assert resp.status_code == 204  # Twilio must NOT retry — noise, not an error
    assert "unknown sid" in caplog.text


def test_status_callback_skips_unmodeled_status() -> None:
    pool = FakePool()
    pool.log_rows.append(
        {
            "company_id": COMPANY_A,
            "direction": "outbound",
            "phone": PHONE,
            "template_id": "rent_reminder",
            "twilio_sid": "SM901",
            "status": "queued",
            "error_code": None,
            "body": None,
        }
    )
    resp = _post(_settings(), pool, STATUS, {"MessageSid": "SM901", "MessageStatus": "sending"})
    assert resp.status_code == 204
    assert pool.log_rows[0]["status"] == "queued"  # CHECK-constraint set only


# ── outbound send: log row + StatusCallback (comms/sms.py Stage 2) ───


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    captured: dict[str, Any] = {}

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, data: Any = None, auth: Any = None) -> _FakeResponse:
        _FakeClient.captured = {"url": url, "data": data, "auth": auth}
        return _FakeResponse(201, {"sid": "SM123", "status": "queued"})


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeClient]:
    _FakeClient.captured = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _send_settings(webhook_base_url: str = BASE_URL) -> Settings:
    s = Settings()
    s.twilio.enabled = True
    s.twilio.account_sid = "ACxxx"
    s.twilio.auth_token = AUTH_TOKEN
    s.twilio.from_number = "+18885550000"
    s.twilio.webhook_base_url = webhook_base_url
    return s


async def test_successful_send_inserts_outbound_log_row(fake_http: type[_FakeClient]) -> None:
    pool = FakePool([_consent_row(COMPANY_A)])
    result = await send_sms(
        settings=_send_settings(),
        pool=pool,
        company_id=COMPANY_A,
        to_phone=PHONE,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_DAYTIME,
    )
    assert result.sid == "SM123"
    assert len(pool.log_rows) == 1
    row = pool.log_rows[0]
    assert row["direction"] == "outbound"
    assert row["template_id"] == "rent_reminder"
    assert row["twilio_sid"] == "SM123"
    assert row["status"] == "queued"
    assert row["company_id"] == COMPANY_A
    assert row["body"] is None  # outbound rows never store a body


async def test_log_insert_failure_never_fails_the_send(
    fake_http: type[_FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenLogPool(FakePool):
        async def execute(self, query: str, *args: Any) -> str:
            if "helixis_sms_log" in query:
                raise RuntimeError("db went away mid-insert")
            return await super().execute(query, *args)

    pool = BrokenLogPool([_consent_row(COMPANY_A)])
    with caplog.at_level(logging.WARNING, logger=sms_mod.logger.name):
        result = await send_sms(
            settings=_send_settings(),
            pool=pool,
            company_id=COMPANY_A,
            to_phone=PHONE,
            template_id="rent_reminder",
            params=RENT_PARAMS,
            now=_DAYTIME,
        )
    assert result.sid == "SM123"  # the send stands — the message already left
    assert "sms log insert failed" in caplog.text
    assert pool.log_rows == []


async def test_status_callback_form_param_present_when_base_url_set(
    fake_http: type[_FakeClient],
) -> None:
    pool = FakePool([_consent_row(COMPANY_A)])
    await send_sms(
        settings=_send_settings(webhook_base_url=BASE_URL + "/"),  # trailing slash normalized
        pool=pool,
        company_id=COMPANY_A,
        to_phone=PHONE,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_DAYTIME,
    )
    form = fake_http.captured["data"]
    assert form["StatusCallback"] == f"{BASE_URL}/api/v1/twilio/status"


async def test_no_status_callback_when_base_url_empty(fake_http: type[_FakeClient]) -> None:
    pool = FakePool([_consent_row(COMPANY_A)])
    await send_sms(
        settings=_send_settings(webhook_base_url=""),
        pool=pool,
        company_id=COMPANY_A,
        to_phone=PHONE,
        template_id="rent_reminder",
        params=RENT_PARAMS,
        now=_DAYTIME,
    )
    assert "StatusCallback" not in fake_http.captured["data"]


# ── the Inbox bridge gate (webhooks/comms_ingest) ────────────────────


def test_a_twilio_redelivery_does_not_card_the_message_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route bridges an inbound text into the Inbox, and gates that on a
    FRESH log row: ``ON CONFLICT (twilio_sid) DO NOTHING`` returns nothing for
    a redelivery, so the retry cannot produce a second card.

    Twilio retries on any non-2xx and on its own schedule, so this is a real
    delivery mode, not a hypothetical. Asserted at the ROUTE because the gate
    lives in the route — ``bridge_inbound_comms`` itself is happy to be called
    twice, and testing it alone would miss this entirely.
    """
    calls: list[str] = []

    async def _fake_bridge(_pool: Any, **kwargs: Any) -> None:
        calls.append(kwargs["sid"])
        return None

    monkeypatch.setattr(twilio_route, "bridge_inbound_comms", _fake_bridge)
    pool = FakePool([_consent_row(COMPANY_A)])
    params = {"From": PHONE, "Body": "the sink is leaking", "MessageSid": "SM88"}

    assert _post(_settings(), pool, INBOUND, params).status_code == 200
    assert _post(_settings(), pool, INBOUND, params).status_code == 200

    assert calls == ["SM88"], "a redelivered MessageSid must not card twice"
    assert len(pool.log_rows) == 1


def test_a_message_without_a_sid_is_logged_but_never_carded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``twilio_sid`` is nullable and NULLs do not collide in a unique index,
    so a SID-less message inserts fresh EVERY time — the idempotency the bridge
    gate depends on does not exist for it. Log it, never card it."""
    calls: list[str] = []

    async def _fake_bridge(_pool: Any, **kwargs: Any) -> None:
        calls.append(kwargs["sid"])
        return None

    monkeypatch.setattr(twilio_route, "bridge_inbound_comms", _fake_bridge)
    pool = FakePool([_consent_row(COMPANY_A)])
    params = {"From": PHONE, "Body": "no sid on this one"}

    assert _post(_settings(), pool, INBOUND, params).status_code == 200
    assert _post(_settings(), pool, INBOUND, params).status_code == 200

    assert calls == []
    assert len(pool.log_rows) == 2, "both inserted — that is exactly why we don't card"
