"""SMS consent management API (api/routes/sms.py) — Stage 3.

Offline: the DB "pool" is an in-memory fake that interprets the exact SQL
shapes the route issues (same approach as test_sms_inbound.py), so the
tests pin real ledger semantics — company scoping, upsert/re-consent,
revoke-not-delete, server-set source — not SQL strings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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


def _consent_row(
    company_id: str,
    phone: str,
    *,
    recipient_kind: str = "manager",
    source: str = "app_settings",
    revoked_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "phone": phone,
        "display_name": "Dana Ops",
        "recipient_kind": recipient_kind,
        "source": source,
        "timezone": "America/New_York",
        "consented_at": _T0,
        "revoked_at": revoked_at,
        "revocation_source": "app_settings" if revoked_at else None,
    }


class FakePool:
    """Interprets the route's four SQL shapes against in-memory rows."""

    def __init__(
        self,
        consent: list[dict[str, Any]] | None = None,
        sms_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self.consent = consent or []
        #: Outbound rows the delivery-health reads see. Empty by default, so
        #: every pre-existing test keeps its old answers — a fixture has to
        #: say which state it MEANS (gotchas 67/72/73), and "no messages have
        #: been sent" is the honest default for a channel that is still dark.
        self.sms_log = sms_log or []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        # ⚠ Serve the sms_log shapes rather than letting the assert below
        # fire. `summarize` catches broadly and fails open, so an unhandled
        # query here would silently exercise the DEGRADED path and every
        # delivery count would read None for a reason unrelated to the test.
        if "helixis_sms_log" in q:
            company_id = args[0]
            rows = [
                r
                for r in self.sms_log
                if r["company_id"] == company_id and r.get("direction", "outbound") == "outbound"
            ]
            if "DISTINCT ON (phone)" in q:
                latest: dict[str, dict[str, Any]] = {}
                for r in sorted(rows, key=lambda r: r["created_at"]):
                    latest[r["phone"]] = r
                return list(latest.values())
            since = args[1]
            return [r for r in rows if r["created_at"] >= since]
        assert "FROM public.helixis_sms_consent WHERE company_id" in q
        company_id = args[0]
        rows = [r for r in self.consent if r["company_id"] == company_id]
        return sorted(rows, key=lambda r: r["consented_at"], reverse=True)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if q.startswith("INSERT INTO public.helixis_sms_consent"):
            (
                company_id,
                phone,
                display_name,
                recipient_kind,
                timezone,
                evidence,
                owner_user_id,
            ) = args
            for row in self.consent:
                if row["company_id"] == company_id and row["phone"] == phone:
                    row.update(
                        display_name=display_name,
                        recipient_kind=recipient_kind,
                        timezone=timezone,
                        source="app_settings",
                        consented_at=datetime.now(UTC),
                        revoked_at=None,
                        revocation_source=None,
                        evidence=evidence,
                    )
                    # ⚠ Modelled from the ROUTE'S OWN SQL, not from what the
                    # fixture author thinks it should do. Re-consent must not
                    # ERASE an ownership somebody already claimed — a START
                    # text and an admin re-adding both land here, and EXCLUDED
                    # alone would null it out, after which no departure could
                    # revoke it. A fake that hardcoded the intent would agree
                    # with itself whichever operator the route used (gotcha 72).
                    if "COALESCE(EXCLUDED.owner_user_id" in q:
                        row["owner_user_id"] = owner_user_id or row.get("owner_user_id")
                    else:
                        row["owner_user_id"] = owner_user_id
                    return dict(row)
            row = {
                "id": str(uuid.uuid4()),
                "company_id": company_id,
                "phone": phone,
                "display_name": display_name,
                "recipient_kind": recipient_kind,
                "source": "app_settings",
                "timezone": timezone,
                "consented_at": datetime.now(UTC),
                "revoked_at": None,
                "revocation_source": None,
                "evidence": evidence,
                "owner_user_id": owner_user_id,
            }
            self.consent.append(row)
            return dict(row)
        if q.startswith("UPDATE public.helixis_sms_consent"):
            consent_id, company_id = args
            for row in self.consent:
                if row["id"] == consent_id and row["company_id"] == company_id:
                    if row["revoked_at"] is None:
                        row["revoked_at"] = datetime.now(UTC)
                        row["revocation_source"] = "app_settings"
                    return dict(row)
            return None
        if "COUNT(*) FILTER" in q:
            company_id = args[0]
            rows = [r for r in self.consent if r["company_id"] == company_id]
            return {
                "live": sum(1 for r in rows if r["revoked_at"] is None),
                "revoked": sum(1 for r in rows if r["revoked_at"] is not None),
            }
        if "FROM public.helixis_a2p_registrations" in q:
            # No registration row = the single-tenant deployment, where the
            # global flag alone decides whether this company may send. That is
            # the state these tests describe.
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {q}")


def _make_app(
    pool: FakePool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    authed: bool = True,
    twilio: SimpleNamespace | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(sms_routes.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(
        twilio=twilio
        or SimpleNamespace(
            enabled=False, account_sid="", auth_token="", from_number="", messaging_service_sid=""
        )
    )
    monkeypatch.setattr(sms_routes, "get_pool", lambda: pool)
    if authed:
        test_user = AuthenticatedUser(
            user_id="user-a", email="a@example.com", company_ids=[COMPANY_A]
        )
        app.dependency_overrides[get_current_user] = lambda: test_user
    return app


# ── auth ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/sms/consent"),
        ("POST", "/api/v1/sms/consent"),
        ("DELETE", f"/api/v1/sms/consent/{uuid.uuid4()}"),
        ("GET", "/api/v1/sms/status"),
    ],
)
def test_all_endpoints_require_auth(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    app = _make_app(FakePool(), monkeypatch, authed=False)
    resp = TestClient(app).request(method, path, json={"phone": "+15551234567"})
    assert resp.status_code in (401, 403)


# ── company scoping ───────────────────────────────────────────────────


def test_list_returns_only_callers_company(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool(
        [
            _consent_row(COMPANY_A, "+15551230001"),
            _consent_row(COMPANY_B, "+15551230002"),
        ]
    )
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).get("/api/v1/sms/consent")
    assert resp.status_code == 200
    consents = resp.json()["consents"]
    assert [c["phone"] for c in consents] == ["+15551230001"]
    # And the wire shape carries the ledger fields the card renders.
    assert set(consents[0]) >= {
        "id",
        "phone",
        "display_name",
        "recipient_kind",
        "source",
        "timezone",
        "consented_at",
        "revoked_at",
    }


def test_revoke_other_companys_row_is_404_and_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b_row = _consent_row(COMPANY_B, "+15551230002")
    pool = FakePool([b_row])
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).delete(f"/api/v1/sms/consent/{b_row['id']}")
    assert resp.status_code == 404
    assert b_row["revoked_at"] is None  # B's consent untouched


# ── POST validation + normalization ───────────────────────────────────


def test_post_normalizes_human_format_phone_to_e164(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "(555) 123-4567", "display_name": "Dana"},
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == "+15551234567"
    assert pool.consent[0]["phone"] == "+15551234567"
    # Defaults hold: manager kind, Eastern quiet-hours zone.
    assert resp.json()["recipient_kind"] == "manager"
    assert resp.json()["timezone"] == "America/New_York"


def test_post_rejects_junk_phone_422(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "555-1234"})
    assert resp.status_code == 422
    assert pool.consent == []  # nothing written


def test_post_rejects_a_number_we_can_never_text_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """`normalize_e164` deliberately preserves a foreign country code, because
    matching an INBOUND number needs that. Without this check a +44 number was
    accepted, stored, and listed as an active consent — a row that looks live
    for somebody the registered campaign can never reach, whose every send
    then failed where nobody was looking."""
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "+442071234567"})
    assert resp.status_code == 422
    assert "US and Canadian" in resp.json()["detail"]
    assert pool.consent == []  # nothing written


def test_post_still_accepts_an_unmapped_us_area_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ The other direction. 850 is deliberately absent from the timezone
    table (split-zone Florida), and refusing every number the table misses
    would break texting for a large chunk of two states — a fix that looks
    like the one above and is not."""
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "+18505551234"})
    assert resp.status_code == 200
    assert len(pool.consent) == 1


def test_post_rejects_junk_timezone_422(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567", "timezone": "America/Not_A_Place"},
    )
    assert resp.status_code == 422
    assert pool.consent == []


def test_post_rejects_unknown_recipient_kind_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _make_app(FakePool(), monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567", "recipient_kind": "robot"},
    )
    assert resp.status_code == 422


def test_source_is_always_app_settings_even_when_client_sends_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567", "source": "resident_portal"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "app_settings"
    assert pool.consent[0]["source"] == "app_settings"


# ── whose number is it (migration 0096) ───────────────────────────────
#
# The manager digest texts every live manager-kind row, so a departed
# colleague kept receiving portfolio content. `comms/consent_ownership.py`
# revokes on departure — but ONLY rows whose owner is known, and the only
# thing that makes an owner known is the claim exercised here.


def test_an_unclaimed_number_stays_unattributed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DEFAULT, and the direction that matters most.

    Recording whoever submitted the form as the owner is the obvious
    shortcut and it is wrong for every number entered on somebody else's
    behalf — after which their departure silently revokes a colleague's
    alerts. Absent claim, absent owner.
    """
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "+15551234567"})
    assert resp.status_code == 200
    assert resp.json()["owner_user_id"] is None
    assert pool.consent[0]["owner_user_id"] is None


def test_claiming_your_own_number_records_you_as_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567", "is_own_number": True},
    )
    assert resp.status_code == 200
    assert resp.json()["owner_user_id"] == "user-a"
    assert pool.consent[0]["owner_user_id"] == "user-a"


def test_reconsent_does_not_erase_an_existing_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A START text and an admin re-adding a number both land on the same
    upsert. Nulling the ownership there would leave a row no departure can
    ever revoke — silently, and only for people who had done the right thing.
    """
    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    client = TestClient(app)
    client.post("/api/v1/sms/consent", json={"phone": "+15551234567", "is_own_number": True})
    resp = client.post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567", "display_name": "Dana"},
    )
    assert resp.status_code == 200
    assert resp.json()["owner_user_id"] == "user-a", "the claim was erased by a re-consent"
    assert pool.consent[0]["display_name"] == "Dana", "the rest of the row still updates"


# ── upsert / re-consent / revoke ──────────────────────────────────────


def test_reconsent_of_revoked_row_clears_revocation_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked = _consent_row(COMPANY_A, "+15551234567", source="twilio_start_webhook", revoked_at=_T0)
    pool = FakePool([revoked])
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "+1 555 123 4567"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["revoked_at"] is None
    assert body["source"] == "app_settings"
    assert len(pool.consent) == 1  # upsert, not a second row
    assert pool.consent[0]["revoked_at"] is None
    assert pool.consent[0]["consented_at"] > _T0  # consent timestamp refreshed


def test_revoke_marks_row_but_never_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _consent_row(COMPANY_A, "+15551234567")
    pool = FakePool([row])
    app = _make_app(pool, monkeypatch)
    client = TestClient(app)
    resp = client.delete(f"/api/v1/sms/consent/{row['id']}")
    assert resp.status_code == 200
    assert resp.json()["revoked_at"] is not None
    # The ledger keeps the row — it still lists, marked revoked.
    assert len(pool.consent) == 1
    listed = client.get("/api/v1/sms/consent").json()["consents"]
    assert listed[0]["revoked_at"] is not None
    assert pool.consent[0]["revocation_source"] == "app_settings"


# ── status ────────────────────────────────────────────────────────────


def test_status_counts_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool(
        [
            _consent_row(COMPANY_A, "+15551230001"),
            _consent_row(COMPANY_A, "+15551230002", revoked_at=_T0),
            _consent_row(COMPANY_B, "+15551230003"),  # not the caller's
        ]
    )
    twilio = SimpleNamespace(
        enabled=False,
        account_sid="AC123",
        auth_token="tok",
        from_number="+15550009999",
        messaging_service_sid="",
    )
    app = _make_app(pool, monkeypatch, twilio=twilio)
    resp = TestClient(app).get("/api/v1/sms/status")
    assert resp.status_code == 200
    assert resp.json() == {
        # ⚠ `enabled` now means "may THIS COMPANY send" and `block` says which
        # gate refused. It used to echo the deployment flag, so on a
        # white-label install the first customer's approval turned it true for
        # every customer — four of five saw a green Leasing gate and then a 503
        # on their first reply.
        "enabled": False,
        "block": "disabled",
        "campaign_status": "",
        "configured": True,
        "consent_count": 1,
        "revoked_count": 1,
        # Delivery health over the last 7 days. Zero, not None — the log was
        # read and held nothing. `None` would mean the query never ran, and
        # the panel says opposite things about the two (gotcha 89).
        "delivered_count": 0,
        "undelivered_count": 0,
        "no_receipt_count": 0,
        "pending_count": 0,
        "delivery_window_days": 7,
    }


def test_status_unconfigured_without_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    twilio = SimpleNamespace(
        enabled=True,
        account_sid="AC123",
        auth_token="tok",
        from_number="",
        messaging_service_sid="",
    )
    app = _make_app(FakePool(), monkeypatch, twilio=twilio)
    body = TestClient(app).get("/api/v1/sms/status").json()
    # ⚠ Still sendable. A missing global sender is NOT a block — under
    # white-label each company sends from its own number, so requiring a
    # deployment-wide `from_number` would refuse every company on a correct
    # multi-tenant install (gotcha 69). `configured` reports it; the gate does
    # not act on it.
    assert body["enabled"] is True
    assert body["block"] == ""
    assert body["configured"] is False


def test_a_company_in_carrier_review_is_not_told_it_can_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ THE FIVE-CUSTOMER BUG. `twilio.enabled` is one switch for the
    deployment and A2P approval is per company, so the first customer to clear
    turned this true for everyone: four of five saw a green Leasing gate and
    got a 503 on their first reply. A status field the send path disagrees
    with is worse than no status field at all.
    """

    class _Pending(FakePool):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            if "helixis_a2p_registrations" in query:
                return {"status": "campaign_pending"}
            return await super().fetchrow(query, *args)

    twilio = SimpleNamespace(
        enabled=True,  # another customer's approval flipped the deployment flag
        account_sid="AC123",
        auth_token="tok",
        from_number="+15550009999",
        messaging_service_sid="",
    )
    body = (
        TestClient(_make_app(_Pending(), monkeypatch, twilio=twilio))
        .get("/api/v1/sms/status")
        .json()
    )
    assert body["enabled"] is False, "told a company in carrier review that it could text"
    assert body["block"] == "campaign_pending"
    # The raw status too, so the UI can separate "still waiting" from
    # "rejected — fix and resubmit". They are the same `block` value and need
    # opposite instructions.
    assert body["campaign_status"] == "campaign_pending"


def test_an_approved_company_is_told_it_can_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ The other direction. A gate tested only on what it blocks is
    indistinguishable from one that blocks everyone."""

    class _Approved(FakePool):
        async def fetchrow(self, query: str, *args: Any) -> Any:
            if "helixis_a2p_registrations" in query:
                return {"status": "approved"}
            return await super().fetchrow(query, *args)

    twilio = SimpleNamespace(
        enabled=True,
        account_sid="AC123",
        auth_token="tok",
        from_number="+15550009999",
        messaging_service_sid="",
    )
    body = (
        TestClient(_make_app(_Approved(), monkeypatch, twilio=twilio))
        .get("/api/v1/sms/status")
        .json()
    )
    assert body["enabled"] is True
    assert body["block"] == ""


# ── consent evidence (migration 0070) ─────────────────────────────────


def test_add_consent_stamps_capture_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each grant stores server-stamped evidence: who recorded it, the exact
    disclosure text shown, and the request origin — the artifact an A2P
    vetting appeal (code 30555) or a TCPA defense asks for."""
    import json as _json

    pool = FakePool()
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post(
        "/api/v1/sms/consent",
        json={"phone": "+15551234567"},
        headers={"user-agent": "TestBrowser/1.0", "x-forwarded-for": "203.0.113.9, 10.0.0.1"},
    )
    assert resp.status_code == 200
    ev = _json.loads(pool.consent[0]["evidence"])
    assert ev["method"] == "app_settings_form"
    assert ev["recorded_by"] == "user-a"
    assert ev["recorded_by_email"] == "a@example.com"
    assert ev["language_version"] == 1
    # First X-Forwarded-For hop, not the proxy.
    assert ev["ip"] == "203.0.113.9"
    assert ev["user_agent"] == "TestBrowser/1.0"


def test_reconsent_stamps_fresh_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-grant through the form is a NEW capture — evidence is replaced,
    not preserved from the original grant."""
    import json as _json

    row = _consent_row(COMPANY_A, "+15551234567", revoked_at=_T0)
    row["evidence"] = '{"method": "old"}'
    pool = FakePool([row])
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/sms/consent", json={"phone": "+15551234567"})
    assert resp.status_code == 200
    ev = _json.loads(pool.consent[0]["evidence"])
    assert ev["method"] == "app_settings_form"
    assert pool.consent[0]["revoked_at"] is None


def test_consent_language_carries_required_disclosures() -> None:
    """The disclosure text is the compliance surface: message type,
    frequency, rates, and the VERBATIM Sept-2026 opt-out phrase
    ("Reply STOP to cancel" — rejection code 30539). Must stay in sync with
    CONSENT_LANGUAGE in frontend SmsDetail.tsx (same version number)."""
    lang = sms_routes._CONSENT_LANGUAGE
    assert "text messages" in lang  # message type
    assert "Message frequency varies" in lang  # frequency
    assert "message and data rates may apply" in lang  # rates
    assert "Reply STOP to cancel" in lang  # verbatim opt-out
    assert "HELP" in lang  # help keyword


# ── delivery health reaches the client ───────────────────────────────
#
# `helixis_sms_log` was written by three paths and read by ONE `SELECT 1`.
# A landline came back `undelivered` with error 30006, got written down, and
# the number stayed in the consent table looking perfectly healthy — the
# column that said otherwise was never selected.


def _log_row(
    company_id: str,
    phone: str,
    status: str,
    *,
    error_code: str | None = None,
    age_hours: int = 1,
) -> dict:
    """⚠ Timestamped RELATIVE TO NOW, not at the module's fixed `_T0`.

    `_T0` is 2026-08-01, outside the status endpoint's 7-day window, so rows
    stamped there vanish from the summary and every count reads 0 — a test
    that then passes has measured the window, not the classifier. The default
    age is also past `NO_RECEIPT_AFTER_MINUTES`, so a `sent` row here means
    "never confirmed" rather than "still in flight".
    """
    return {
        "company_id": company_id,
        "phone": phone,
        "direction": "outbound",
        "status": status,
        "error_code": error_code,
        "created_at": datetime.now(UTC) - timedelta(hours=age_hours),
    }


def test_the_consent_list_shows_a_landline_as_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist #89, closed where somebody can act on it. 30006 means the
    number cannot receive texts at all, so the fix is replacing it — not
    re-sending."""
    pool = FakePool(
        [_consent_row(COMPANY_A, "+15551230001"), _consent_row(COMPANY_A, "+15551230002")],
        sms_log=[_log_row(COMPANY_A, "+15551230001", "undelivered", error_code="30006")],
    )
    app = _make_app(pool, monkeypatch)
    rows = {r["phone"]: r for r in TestClient(app).get("/api/v1/sms/consent").json()["consents"]}
    assert rows["+15551230001"]["last_outcome"] == "undelivered"
    assert rows["+15551230001"]["last_error_code"] == "30006"
    # ⚠ The other direction. A number nobody has texted must not read as
    # failing — that is the same guard switched off by crying wolf.
    assert rows["+15551230002"]["last_outcome"] is None
    assert rows["+15551230002"]["last_error_code"] is None


def test_a_message_with_no_receipt_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist #77. A carrier that filters a message sends no receipt at
    all, so the row sits at `sent`. Twilio reported nothing — calling that
    `undelivered` would be a claim it never made."""
    pool = FakePool(
        [_consent_row(COMPANY_A, "+15551230001")],
        sms_log=[_log_row(COMPANY_A, "+15551230001", "sent")],
    )
    app = _make_app(pool, monkeypatch)
    row = TestClient(app).get("/api/v1/sms/consent").json()["consents"][0]
    assert row["last_outcome"] == "no_receipt"
    assert row["last_error_code"] is None


def test_the_status_endpoint_separates_the_two_kinds_of_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(
        [_consent_row(COMPANY_A, "+15551230001")],
        sms_log=[
            _log_row(COMPANY_A, "+15551230001", "delivered"),
            _log_row(COMPANY_A, "+15551230002", "undelivered", error_code="30006"),
            _log_row(COMPANY_A, "+15551230003", "sent"),
            _log_row(COMPANY_B, "+15551230004", "undelivered"),  # not the caller's
        ],
    )
    app = _make_app(pool, monkeypatch)
    body = TestClient(app).get("/api/v1/sms/status").json()
    assert body["delivered_count"] == 1
    assert body["undelivered_count"] == 1
    assert body["no_receipt_count"] == 1, (
        "a stuck message folded into undelivered_count would report a failure Twilio never sent"
    )
    assert body["delivery_window_days"] == 7
