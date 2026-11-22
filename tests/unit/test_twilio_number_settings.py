"""Changing a number's settings after it exists — forwarding, above all.

``forward_to_number`` and ``voice_greeting`` have been columns since migration
0083 and were writable from **nowhere**. The A2P sweep's ``_record_number``
inserts ``(company_id, phone_number, label)``; ``POST /sms/numbers`` accepted
the same three. ``crm.py`` reads ``telephony.forward_to_number``, gets ``''``,
and sends the call to voicemail — so a company that finished setup, saw its
number on screen and answered nothing had no surface anywhere to fix it.

⚠ **The upsert semantics are the load-bearing part.** This one endpoint both
claims a number and edits it, so "absent" and "empty" must mean different
things: an absent field LEAVES the stored value (``COALESCE``), an explicit
``""`` CLEARS it back to voicemail. With ``EXCLUDED`` the two collapse and
renaming a number's label silently wipes its forwarding — a failure the caller
cannot see, on the field whose whole job is making the phone ring.

The fake pool below reads the ROUTE'S OWN SQL to decide which columns coalesce,
rather than re-implementing the intent. A fake that hardcodes the behaviour
would agree with itself whichever operator the route actually used.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import sms as sms_routes

COMPANY_A = "11111111-1111-1111-1111-111111111111"
COMPANY_B = "22222222-2222-2222-2222-222222222222"
_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_TWILIO = SimpleNamespace(
    enabled=True,
    account_sid="ACxxxx",
    auth_token="tok",
    from_number="",
    messaging_service_sid="",
    webhook_base_url="https://api.example.com",
    default_company_id="",
)

_COLUMNS = ("label", "forward_to_number", "voice_greeting")


def _coalesced(query: str) -> set[str]:
    """Which update columns the ROUTE'S SQL keeps on conflict.

    Reads the real statement so switching a column to a bare ``EXCLUDED.x``
    changes what this fake does. Hardcoding the answer would make the test
    pass against either operator, which is the whole thing under test.
    """
    q = " ".join(query.split())
    return {
        col
        for col in _COLUMNS
        if re.search(
            rf"{col}\s*=\s*COALESCE\(\s*EXCLUDED\.{col}\s*,"
            rf"\s*public\.helixis_twilio_numbers\.{col}\s*\)",
            q,
        )
    }


class _Pool:
    """Interprets the number-settings SQL against in-memory rows."""

    def __init__(self, *, numbers: list[dict[str, Any]] | None = None, role: str = "admin") -> None:
        self.numbers = numbers or []
        self.role = role

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM public.helixis_twilio_numbers" in query
        return [r for r in self.numbers if r["company_id"] == args[0]]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if "FROM public.company_users" in q:
            return {"role": self.role}
        if q.startswith("SELECT company_id FROM public.helixis_twilio_numbers"):
            rows = [r for r in self.numbers if r["phone_number"] == args[0]]
            return {"company_id": rows[0]["company_id"]} if rows else None
        if q.startswith("INSERT INTO public.helixis_twilio_numbers"):
            company_id, phone, label, forward, greeting = args
            incoming = {
                "label": label,
                "forward_to_number": forward,
                "voice_greeting": greeting,
            }
            keep = _coalesced(q)
            for row in self.numbers:
                if row["phone_number"] == phone:  # ON CONFLICT (phone_number)
                    for col, value in incoming.items():
                        if value is None and col in keep:
                            continue  # COALESCE → the stored value stands
                        row[col] = value
                    return row
            row = {
                "id": str(uuid.uuid4()),
                "company_id": company_id,
                "phone_number": phone,
                "created_at": _T0,
                **incoming,
            }
            self.numbers.append(row)
            return row
        raise AssertionError(f"unexpected SQL: {q}")


@pytest.fixture(autouse=True)
def _wiring_succeeds(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Claiming a number now POINTS ITS WEBHOOKS at us before recording it.

    ⚠ Autouse because it is the DEFAULT state, not a special case: a number
    that cannot be wired is refused, so every test about the upsert needs the
    wiring to succeed or it never reaches the SQL under test. The failure
    directions get their own tests below, which override this.
    """
    calls: list[dict[str, Any]] = []

    async def _configure(**kw: Any) -> str:
        calls.append(kw)
        return "PN" + "0" * 32

    monkeypatch.setattr(sms_routes.twilio_client, "configure_number", _configure)
    return calls


def _client(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(sms_routes.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(twilio=_TWILIO)
    monkeypatch.setattr(sms_routes, "get_pool", lambda: pool)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="user-a", email="a@example.com", company_ids=[COMPANY_A]
    )
    return TestClient(app)


def _row(**over: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "company_id": COMPANY_A,
        "phone_number": "+16665550000",
        "label": "Main line",
        "forward_to_number": "+15125550143",
        "voice_greeting": None,
        "created_at": _T0,
        **over,
    }


# ── the gap: forwarding is settable at all ────────────────────────────


def test_forwarding_can_be_set_on_a_number_that_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ THE FIX. A sweep-provisioned number arrives with this column NULL, so
    without this endpoint the company's phone never rings and nothing anywhere
    can change it."""
    pool = _Pool(numbers=[_row(forward_to_number=None)])
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": "(512) 555-0143"},
    )
    assert resp.status_code == 201
    assert resp.json()["forward_to_number"] == "+15125550143"
    assert pool.numbers[0]["forward_to_number"] == "+15125550143"


def test_the_forwarding_number_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call is DIALED to this value. Stored unnormalized it either fails to
    connect or reaches somebody else — the same reason the number column is
    normalized, with a worse failure."""
    pool = _Pool()
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "666-555-0000", "forward_to_number": "512.555.0143"},
    )
    assert resp.status_code == 201
    assert pool.numbers[0]["forward_to_number"] == "+15125550143"


def test_a_junk_forwarding_number_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool()
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": "call the office"},
    )
    assert resp.status_code == 422
    assert pool.numbers == [], "recorded a number whose forwarding was rejected"


def test_the_list_reports_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI decides whether to nag about voicemail from this. A list that
    omits the column makes every company look unforwarded, so the nudge could
    never clear."""
    resp = _client(_Pool(numbers=[_row()]), monkeypatch).get("/api/v1/sms/numbers")
    assert resp.status_code == 200
    assert resp.json()["numbers"][0]["forward_to_number"] == "+15125550143"


# ── absent vs empty ───────────────────────────────────────────────────


def test_renaming_a_number_leaves_its_forwarding_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The one that ``EXCLUDED`` breaks. Editing the label posts no
    forwarding field; with ``EXCLUDED`` that writes NULL and the phone stops
    ringing, silently, on a request that looked like a rename."""
    pool = _Pool(numbers=[_row()])
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "label": "Leasing line"},
    )
    assert resp.status_code == 201
    assert resp.json()["label"] == "Leasing line"
    assert resp.json()["forward_to_number"] == "+15125550143", (
        "a rename wiped the forwarding number — calls now go to voicemail"
    )


def test_an_explicit_blank_clears_back_to_voicemail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, and it has to stay expressible: "stop ringing my
    cell" is a real request, and COALESCE would swallow it if blank were
    treated as absent."""
    pool = _Pool(numbers=[_row()])
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": ""},
    )
    assert resp.status_code == 201
    assert resp.json()["forward_to_number"] == ""
    assert pool.numbers[0]["forward_to_number"] == ""


def test_setting_forwarding_leaves_the_greeting_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both optional columns coalesce, not just the one this change is about."""
    pool = _Pool(numbers=[_row(voice_greeting="Thanks for calling Redbud.")])
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": "+15125559999"},
    )
    assert resp.status_code == 201
    assert resp.json()["voice_greeting"] == "Thanks for calling Redbud."


# ── claiming a number WIRES it ────────────────────────────────────────
#
# ⚠ THE GAP. ``buy_number`` points a number at us in the same request that
# buys it, so everything Occupella purchases is configured by construction.
# This route claims a number that ALREADY exists — bought by hand in the
# console — and nothing had ever wired it. It appeared in the app, the Leasing
# gate opened because the company now had a number, and every call and text
# went nowhere. Silent until a real person rings.


def test_claiming_a_number_points_its_webhooks_at_us(
    monkeypatch: pytest.MonkeyPatch, _wiring_succeeds: list[dict[str, Any]]
) -> None:
    resp = _client(_Pool(), monkeypatch).post(
        "/api/v1/sms/numbers", json={"phone_number": "+16665550000"}
    )
    assert resp.status_code == 201
    assert len(_wiring_succeeds) == 1, "claimed a number without wiring it"
    # ⚠ The NORMALIZED number, not what the caller typed. Twilio looks the
    # number up by exact E.164; a raw "666-555-0000" finds nothing and the
    # claim would refuse a number the account really owns.
    assert _wiring_succeeds[0]["phone_number"] == "+16665550000"


def test_a_number_that_cannot_be_wired_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Ordered before the insert on purpose. A row recorded for a number we
    could not configure is the same lie in a different place — the app would
    show it as set up and the Leasing gate would open on it."""

    async def _boom(**_kw: Any) -> str:
        raise sms_routes.twilio_client.TwilioProvisionError("an application is attached")

    monkeypatch.setattr(sms_routes.twilio_client, "configure_number", _boom)
    pool = _Pool()
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers", json={"phone_number": "+16665550000"}
    )
    assert resp.status_code == 502
    assert pool.numbers == [], "recorded a number whose webhooks point nowhere"
    # Says WHY, because the fix is in the Twilio console and nowhere else.
    assert "application is attached" in resp.json()["detail"]


def test_no_credentials_refuses_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without credentials there is no way to wire anything, so claiming can
    only produce a row that lies."""
    app = FastAPI()
    app.include_router(sms_routes.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(
        twilio=SimpleNamespace(**{**vars(_TWILIO), "account_sid": "", "auth_token": ""})
    )
    pool = _Pool()
    monkeypatch.setattr(sms_routes, "get_pool", lambda: pool)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="user-a", email="a@example.com", company_ids=[COMPANY_A]
    )
    resp = TestClient(app).post("/api/v1/sms/numbers", json={"phone_number": "+16665550000"})
    assert resp.status_code == 503
    assert pool.numbers == []


def test_the_two_paths_point_a_number_at_the_same_places(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Both-sides (gotcha 28). ``buy_number`` and ``configure_number`` decide
    where a number answers, and two copies of that decision drift — leaving
    half the numbers on the account pointing somewhere else, which is
    invisible until one of them receives something."""
    from helixis.connectors.twilio_client import webhook_form

    form = webhook_form("https://api.example.com")
    assert form["SmsUrl"] == "https://api.example.com/api/v1/twilio/inbound"
    assert form["VoiceUrl"] == "https://api.example.com/api/v1/crm/voice/inbound"
    assert form["StatusCallback"] == "https://api.example.com/api/v1/twilio/status"
    # ⚠ An ApplicationSid on a number takes PRECEDENCE over these URLs, so a
    # number carrying one accepts the write with a 200 and keeps routing to
    # the old application. Clearing it is the difference between configured
    # and silently unchanged.
    assert form["SmsApplicationSid"] == ""
    assert form["VoiceApplicationSid"] == ""


# ── the guards this must not have loosened ────────────────────────────


def test_a_member_cannot_change_where_calls_ring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Still admin only. Redirecting a company's calls is the same power as
    claiming the number in the first place."""
    pool = _Pool(numbers=[_row()], role="member")
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": "+15125559999"},
    )
    assert resp.status_code == 403
    assert pool.numbers[0]["forward_to_number"] == "+15125550143"


def test_another_companys_number_is_still_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Not a takeover, and now not a redirect either: pointing someone
    else's number at your own phone would be a live interception, not merely a
    misfiled row."""
    pool = _Pool(numbers=[_row(company_id=COMPANY_B)])
    resp = _client(pool, monkeypatch).post(
        "/api/v1/sms/numbers",
        json={"phone_number": "+16665550000", "forward_to_number": "+15125559999"},
    )
    assert resp.status_code == 409
    assert pool.numbers[0]["forward_to_number"] == "+15125550143"
