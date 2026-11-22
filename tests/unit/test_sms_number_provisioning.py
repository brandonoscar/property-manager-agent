"""Buying a company its own phone number, in-app.

**Why in-app and not onboarding.** A company needs its own number (migrations
0082/0083) so inbound routing and the outbound sender agree. Doing it by hand
means searching the Twilio console, buying, then pasting three webhook URLs —
per company, forever. Putting that in the onboarding wizard would add friction
to the one flow that can least afford it, so Leasing walls itself instead and
the setup lives behind that wall.

⚠ **This endpoint SPENDS MONEY.** A Twilio number is a recurring monthly
charge, so the guards here are about cost as much as correctness:

1. an existing number short-circuits BEFORE any Twilio call, so a double-click
   or an impatient retry cannot buy a second one;
2. admin only — this both routes other people's messages and bills the account;
3. no credentials / no public webhook URL → refuse, because a number we cannot
   wire receives messages that go nowhere and the charge is already incurred.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import sms as sms_routes
from helixis.connectors import twilio_client

COMPANY_A = "11111111-1111-1111-1111-111111111111"
_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_LIVE_TWILIO = SimpleNamespace(
    enabled=True,
    account_sid="ACxxxx",
    auth_token="tok",
    from_number="",
    messaging_service_sid="",
    webhook_base_url="https://api.example.com",
    default_company_id="",
)


class _Pool:
    """Interprets the provisioning route's SQL against in-memory rows."""

    def __init__(self, *, numbers: list[dict[str, Any]] | None = None, role: str = "admin") -> None:
        self.numbers = numbers or []
        self.role = role
        self.inserts = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if "FROM public.company_users" in q:
            return {"role": self.role}
        if q.startswith("SELECT id, phone_number, label, forward_to_number"):
            company_id = args[0]
            rows = [r for r in self.numbers if r["company_id"] == company_id]
            return rows[0] if rows else None
        if q.startswith("INSERT INTO public.helixis_twilio_numbers"):
            self.inserts += 1
            company_id, phone, label, forward = args
            row = {
                "id": str(uuid.uuid4()),
                "company_id": company_id,
                "phone_number": phone,
                "label": label,
                "forward_to_number": forward,
                "voice_greeting": None,
                "created_at": _T0,
            }
            self.numbers.append(row)
            return row
        raise AssertionError(f"unexpected SQL: {q}")


def _client(pool: _Pool, monkeypatch: pytest.MonkeyPatch, twilio: Any = _LIVE_TWILIO) -> TestClient:
    app = FastAPI()
    app.include_router(sms_routes.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(twilio=twilio)
    monkeypatch.setattr(sms_routes, "get_pool", lambda: pool)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="user-a", email="a@example.com", company_ids=[COMPANY_A]
    )
    return TestClient(app)


def _stub_twilio(
    monkeypatch: pytest.MonkeyPatch, *, bought: str = "+17775550001"
) -> dict[str, Any]:
    """Records what the route asked Twilio to do, without touching Twilio."""
    seen: dict[str, Any] = {"searches": [], "buys": []}

    async def _search(**kw: Any) -> list[dict[str, str]]:
        seen["searches"].append(kw)
        if kw.get("area_code") == "999":  # a sold-out area code
            return []
        return [{"phone_number": bought, "friendly_name": bought, "locality": "Austin"}]

    async def _buy(**kw: Any) -> str:
        seen["buys"].append(kw)
        return bought

    monkeypatch.setattr(twilio_client, "search_available_numbers", _search)
    monkeypatch.setattr(twilio_client, "buy_number", _buy)
    return seen


# ── the money guard ───────────────────────────────────────────────────


def test_a_company_that_already_has_a_number_never_buys_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The double-click guard, and it runs BEFORE any Twilio call.

    Two numbers for one company is both a recurring charge nobody asked for
    and an ambiguous outbound identity — ``telephony_for_company`` picks by
    ``created_at`` and the other number keeps receiving replies nobody reads.
    """
    existing = {
        "id": str(uuid.uuid4()),
        "company_id": COMPANY_A,
        "phone_number": "+16665550000",
        "label": "already here",
        "forward_to_number": None,
        "voice_greeting": None,
        "created_at": _T0,
    }
    pool = _Pool(numbers=[existing])
    seen = _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["purchased"] is False
    assert body["number"]["phone_number"] == "+16665550000"
    assert seen["buys"] == [], "bought a second number for a company that already had one"
    assert seen["searches"] == [], "even searched — the guard must precede any Twilio call"


def test_a_member_cannot_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin only: this routes other people's inbound messages AND bills the
    account."""
    pool = _Pool(role="member")
    seen = _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 403
    assert seen["buys"] == []


def test_an_unverifiable_role_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken(_Pool):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            if "company_users" in query:
                raise RuntimeError("db down")
            return await super().fetchrow(query, *args)

    seen = _stub_twilio(monkeypatch)
    resp = _client(_Broken(), monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 403
    assert seen["buys"] == []


def test_no_webhook_url_refuses_before_spending(monkeypatch: pytest.MonkeyPatch) -> None:
    """A number we cannot wire receives messages that go nowhere — and the
    charge is already incurred by the time anyone notices."""
    twilio = SimpleNamespace(**{**vars(_LIVE_TWILIO), "webhook_base_url": ""})
    seen = _stub_twilio(monkeypatch)
    resp = _client(_Pool(), monkeypatch, twilio).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 503
    assert seen["buys"] == []


def test_no_credentials_refuses_before_spending(monkeypatch: pytest.MonkeyPatch) -> None:
    twilio = SimpleNamespace(**{**vars(_LIVE_TWILIO), "account_sid": "", "auth_token": ""})
    seen = _stub_twilio(monkeypatch)
    resp = _client(_Pool(), monkeypatch, twilio).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 503
    assert seen["buys"] == []


# ── the happy path ────────────────────────────────────────────────────


def test_provisioning_buys_wires_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool()
    seen = _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["purchased"] is True
    assert body["number"]["phone_number"] == "+17775550001"
    assert pool.inserts == 1, "the bought number must be recorded, or it is a charge for nothing"
    # ⚠ Wired in the SAME call as the purchase — the gap between "bought" and
    # "configured" is exactly where a half-finished setup lives.
    assert len(seen["buys"]) == 1
    assert seen["buys"][0]["webhook_base_url"] == "https://api.example.com"


def test_an_area_code_preference_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_twilio(monkeypatch)
    resp = _client(_Pool(), monkeypatch).post(
        "/api/v1/sms/numbers/provision", json={"area_code": "512"}
    )
    assert resp.status_code == 200
    assert seen["searches"][0]["area_code"] == "512"


def test_a_sold_out_area_code_falls_back_nationally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setup that fails because one area code is sold out is a worse outcome
    than a number in the next town."""
    seen = _stub_twilio(monkeypatch)
    resp = _client(_Pool(), monkeypatch).post(
        "/api/v1/sms/numbers/provision", json={"area_code": "999"}
    )
    assert resp.status_code == 200
    assert len(seen["searches"]) == 2, "did not retry without the area code"
    assert seen["searches"][1].get("area_code", "") == ""


def test_a_purchase_failure_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed purchase must not leave a row claiming the company has a
    number — every later send would fail with nothing pointing here."""
    _stub_twilio(monkeypatch)

    async def _boom(**_kw: Any) -> str:
        raise twilio_client.TwilioProvisionError("Twilio said no")

    monkeypatch.setattr(twilio_client, "buy_number", _boom)
    pool = _Pool()
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 502
    assert pool.inserts == 0
    assert pool.numbers == []


def test_no_available_numbers_is_an_honest_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(**_kw: Any) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(twilio_client, "search_available_numbers", _none)
    pool = _Pool()
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 502
    assert pool.inserts == 0


# ── where the calls actually ring ─────────────────────────────────────
#
# ⚠ THE GAP. ``forward_to_number`` was on the table (0083) and settable from
# NOWHERE: neither this route nor the A2P sweep's ``_record_number`` wrote it,
# and ``POST /sms/numbers`` did not accept it. ``crm.py`` reads
# ``telephony.forward_to_number``, gets ``''``, and sends every inbound call
# straight to voicemail — so a company could complete setup, see a number on
# screen, and have its phone never ring, with no surface anywhere to fix it.


def test_forwarding_is_recorded_at_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one moment a human is present. The sweep provisions unattended on
    carrier approval and has nobody to ask."""
    pool = _Pool()
    _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers/provision",
        json={"forward_to_number": "(512) 555-0143"},
    )
    assert resp.status_code == 200
    assert pool.numbers[0]["forward_to_number"] == "+15125550143", (
        "the forwarding number was dropped — the company's phone will never ring"
    )
    assert resp.json()["number"]["forward_to_number"] == "+15125550143"


def test_a_junk_forwarding_number_refuses_before_spending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Validated BEFORE the buy. Discovered after, it leaves a charged,
    half-configured number behind a 422 that reads like the purchase failed."""
    pool = _Pool()
    seen = _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers/provision", json={"forward_to_number": "ring me maybe"}
    )
    assert resp.status_code == 422
    assert seen["buys"] == [], "bought a number, then rejected the request"
    assert pool.inserts == 0


def test_omitting_forwarding_still_provisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voicemail is a legitimate choice and the documented default — a
    required field here would block the setup it exists to complete."""
    pool = _Pool()
    _stub_twilio(monkeypatch)
    resp = _client(pool, monkeypatch).post("/api/v1/sms/numbers/provision", json={})
    assert resp.status_code == 200
    assert pool.numbers[0]["forward_to_number"] is None


def test_an_existing_number_reports_where_it_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The idempotent branch returns a row the UI reads to decide whether to
    nag about voicemail. Selecting only id/phone/label made every already-set-up
    company look unforwarded, so the nudge would never clear."""
    existing = {
        "id": str(uuid.uuid4()),
        "company_id": COMPANY_A,
        "phone_number": "+16665550000",
        "label": "already here",
        "forward_to_number": "+15125550143",
        "voice_greeting": None,
        "created_at": _T0,
    }
    resp = _client(_Pool(numbers=[existing]), monkeypatch).post(
        "/api/v1/sms/numbers/provision", json={}
    )
    assert resp.status_code == 200
    assert resp.json()["number"]["forward_to_number"] == "+15125550143"
