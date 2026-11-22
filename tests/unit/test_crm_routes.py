"""GET /api/v1/crm/board — feature-flag + company-scoping tests.

The CRM section ships behind ``HELIXIS_CRM__ENABLED`` (default OFF). While
the flag is off the route must 404 — not 403 — so the route's existence
isn't leaked, and it must NOT reach the DB. When on, it hands the caller's
primary company_id to the repository (RLS is the second layer). Mirrors
``test_dev_runs_route_rls.py``.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.db.repositories.leads import LeadRow
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL


@pytest.fixture
def _signed_voice():
    """A voice webhook request whose signature VERIFIES.

    ⚠ Not a bypass. ``_verify_twilio_or_403`` no longer honours
    ``skip_signature_check`` (it reached all four real voice webhooks, so a
    deployment carrying the flag accepted forged calls, voicemails and
    transcriptions). These tests are about what the route DOES with an
    authentic request, so they stub the verifier rather than removing it —
    the 403-on-a-bad-signature path has its own test.
    """
    with patch("helixis.api.routes.crm.twilio_client.validate_signature", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _manager_role_floor():
    """Write approval/execution now takes manager+ (approval-pipeline PR).
    These tests exercise the routes' own behavior, so satisfy the role gate;
    the floor itself is covered in test_agent_ownership."""
    with patch("helixis.api.routes.company.require_role", new=AsyncMock(return_value="manager")):
        yield


TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
# Syntactically-valid lead ids. The lead-scoped routes 404 a malformed id
# before it reaches the repo's ``::uuid`` cast (see _require_valid_lead_id),
# so path params must be real UUIDs to exercise the repo-mocked paths.
LEAD_ID = "00000000-0000-0000-0000-0000000000aa"
MISSING_LEAD_ID = "00000000-0000-0000-0000-000000000000"


def _make_token(sub: str = ME_USER_ID) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings_with_crm_flag(flag: bool) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        crm={"enabled": flag},
    )


def _settings(crm: bool, twilio: dict | None = None) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        crm={"enabled": crm},
        twilio=twilio or {},
    )


_TWILIO_LIVE = {
    "account_sid": "ACxxxx",
    "auth_token": "tok",
    "from_number": "+18005551212",
    "default_company_id": COMPANY_ID,
}

# ⚠ ``_TWILIO_LIVE`` is CONFIGURED BUT DARK — credentials present, A2P
# campaign not yet approved. That is the real default state of a deployment
# that has just had its Twilio secrets set, and outbound SMS must not leave
# it. The outbound-reply tests use ``_TWILIO_SMS_APPROVED``; inbound webhook
# and voice paths do not ride the 10DLC registration and keep the dark one.
_TWILIO_SMS_APPROVED = {**_TWILIO_LIVE, "enabled": True}


@contextlib.contextmanager
def _recipient_reachable() -> Any:
    """Patch: this person has not opted out, it is a legal hour for them, and
    they may be texted at all (they texted in, or consented).

    ⚠ Needed because ``recipient_block`` reads the WALL CLOCK — the TCPA
    window is recipient-local and real. Left unpatched, every send test here
    would pass or fail depending on what time CI ran, which is a flake wearing
    a compliance costume. The gate's own behaviour is measured in
    ``test_crm_recipient_gate.py`` with the clock held still.

    ⚠ Re-pointed 2026-10-23: "reachable" now also covers the first-contact
    permission check (``_lead_may_be_texted``), measured on its own in
    ``test_crm_recipient_gate.py``. These tests are about what a permitted
    send does, so they assume permission the same way they assume the hour.
    """
    with (
        patch("helixis.api.routes.crm.opt_out.recipient_block", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm._lead_may_be_texted", AsyncMock(return_value=True)),
    ):
        yield


def _mock_pool() -> MagicMock:
    """The default ``MagicMock()`` pool, plus NO rows in ``helixis_twilio_numbers``.

    That empty table is the single-tenant state, where ``default_company_id`` /
    ``from_number`` / ``forward_to_number`` still apply — which is what these
    tests were written against.

    ⚠ Two traps, both of which produce failures far from their cause:

    1. Do NOT hand-build the connection. ``MagicMock().acquire().__aenter__()``
       yields an **AsyncMock**, so every method on it is awaitable; a plain
       ``MagicMock`` breaks every OTHER await in the route with
       ``TypeError: object MagicMock can't be used in 'await' expression``.
    2. The override must be SQL-AWARE. ``acquire()`` is also how the
       idempotency store claims a key, and blanket-returning ``None`` made
       ``claim`` raise "row vanished between insert and select" — a 500 on a
       route whose actual change was elsewhere.
    """
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    default = conn.fetchrow

    async def _fetchrow(query: str = "", *args: object, **kw: object) -> object:
        if "helixis_twilio_numbers" in str(query):
            return None  # nothing mapped → single-tenant fallback
        if "helixis_a2p_registrations" in str(query):
            # 3. Same shape for the per-company A2P gate: no registration row
            #    is the single-tenant deployment, where the global `enabled`
            #    flag alone decides. Falling through to the default AsyncMock
            #    would return a MagicMock whose `["status"]` is not
            #    "approved", so every send would be refused as pending — the
            #    gate failing closed on a fixture, not on a defect.
            return None
        return await default(query, *args, **kw)

    conn.fetchrow = _fetchrow
    # ⚠ The A2P gate reads ``pool.fetchrow`` directly, not through
    # ``acquire()`` — every other query in comms/sms.py does the same, so
    # callers have ONE shape to model. Left to the default AsyncMock it gets a
    # MagicMock whose ["status"] is not "approved" and every send in this file
    # is refused as pending.
    pool.fetchrow = _fetchrow
    return pool


@contextlib.contextmanager
def _signed_inbound(company_id: str = COMPANY_ID):
    """A Twilio-signed inbound whose number is owned by ``company_id``.

    Both halves have to be stubbed together: the route verifies the signature
    (no bypass any more) and then resolves the company from the ``To`` number
    (never a global default).
    """
    with (
        patch("helixis.api.routes.crm.twilio_client.validate_signature", return_value=True),
        patch(
            "helixis.api.routes.crm.resolve_company_for_number",
            AsyncMock(return_value=company_id),
        ),
        patch("helixis.api.routes.crm.apply_consent_keywords", AsyncMock(return_value=None)),
    ):
        yield


# Voice config with a forwarding number.
#
# ⚠ ``skip_signature_check`` is NOT set here any more, and that is the point.
# It used to short-circuit ``_verify_twilio_or_403``, so these tests exercised
# an UNSIGNED request against all four real voice webhooks — the same hole
# gotcha 68 closed on the SMS lane, and a fixture that quietly depends on it
# is how the hole stays open. The tests below patch ``validate_signature``
# instead: they assert the route's behaviour on a request that PASSED
# verification, rather than on one that skipped it.
_TWILIO_VOICE = {
    **_TWILIO_LIVE,
    "forward_to_number": "+15125550100",
}


def _sample_lead() -> LeadRow:
    return LeadRow(
        id="00000000-0000-0000-0000-0000000000aa",
        company_id=COMPANY_ID,
        name="Jordan Reyes",
        kind="renter",
        stage="new",
        source="zillow",
        email="jordan.r.a1b2@convo.zillow.com",
        phone="+14155551234",
        unit_label="Maple Court 4B",
        note="Is 4B still available?",
        raw={},
    )


def test_board_returns_404_when_flag_off():
    """Production-shaped config: enabled=False → /crm/board 404s even for
    an authenticated user, and never reaches the DB."""
    settings = _settings_with_crm_flag(False)
    list_leads = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.list_leads", list_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/board",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    list_leads.assert_not_called()


def test_board_returns_leads_scoped_to_company_when_on():
    settings = _settings_with_crm_flag(True)
    list_leads = AsyncMock(return_value=[_sample_lead()])

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.list_leads", list_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/board",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert len(body["leads"]) == 1
    assert body["leads"][0]["name"] == "Jordan Reyes"
    assert body["leads"][0]["unit"] == "Maple Court 4B"
    list_leads.assert_awaited_once()
    assert list_leads.await_args.kwargs["company_id"] == COMPANY_ID


def test_board_unauthenticated_is_401():
    """No Authorization header — auth fires before the flag gate → 401."""
    settings = _settings_with_crm_flag(True)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/crm/board")
    assert resp.status_code == 401


def test_board_user_without_company_is_400():
    settings = _settings_with_crm_flag(True)
    list_leads = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.list_leads", list_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: []),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/board",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 400
    list_leads.assert_not_called()


# ── overview + stage list ────────────────────────────────────────────


def test_overview_404_when_flag_off():
    settings = _settings(False)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/overview",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404


def test_overview_returns_stages_and_metrics():
    from helixis.db.repositories.leads import LeadMetrics

    settings = _settings(True)
    counts = {"new": 2, "contacted": 1, "scheduled": 0, "applied": 0, "leased": 1}
    recent = [_sample_lead(), _sample_lead()]  # both stage 'new'
    metrics = LeadMetrics(
        new_this_month=3, new_prev_month=2, replies_this_month=5, active=3, leased=1, total=4
    )

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch(
            "helixis.api.routes.crm.leads_repo.count_leads_by_stage", AsyncMock(return_value=counts)
        ),
        patch(
            "helixis.api.routes.crm.leads_repo.recent_leads_by_stage",
            AsyncMock(return_value=recent),
        ),
        patch("helixis.api.routes.crm.leads_repo.lead_metrics", AsyncMock(return_value=metrics)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/overview",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["key"] for s in body["stages"]] == [
        "new",
        "contacted",
        "scheduled",
        "applied",
        "leased",
    ]
    new_stage = body["stages"][0]
    assert new_stage["count"] == 2
    assert len(new_stage["recent"]) == 2  # both recent leads are stage 'new'
    assert body["metrics"]["total"] == 4
    assert body["metrics"]["replies_this_month"] == 5


def test_stage_leads_paginates_with_has_more():
    settings = _settings(True)
    # Route asks for limit+1 to detect has_more; return that many.
    rows = [_sample_lead() for _ in range(4)]
    list_leads = AsyncMock(return_value=rows)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.list_leads", list_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/leads?stage=new&limit=3",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is True  # 4 returned for limit 3
    assert len(body["leads"]) == 3
    # route passed stage + limit+1 to the repo
    assert list_leads.await_args.kwargs["stage"] == "new"
    assert list_leads.await_args.kwargs["limit"] == 4


def test_stage_leads_404_when_flag_off():
    settings = _settings(False)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/leads?stage=new",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404


# ── AI assist (draft) ────────────────────────────────────────────────


def test_draft_404_on_malformed_lead_id_never_reaches_db():
    """A non-UUID lead_id (e.g. a stale frontend demo-fixture id like
    ``demo-l-marcus``) must 404, not crash the repo's ``::uuid`` cast with an
    unhandled asyncpg.DataError (production incident PYTHON-FASTAPI-J/H)."""
    settings = _settings(True)
    get_lead = AsyncMock(return_value=_sample_lead())
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", get_lead),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/leads/demo-l-marcus/draft",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    get_lead.assert_not_called()


def test_draft_404_when_flag_off():
    settings = _settings(False)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/crm/leads/{LEAD_ID}/draft",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404


def test_draft_returns_cache_without_generating():
    settings = _settings(True)
    cached = LeadRow(
        id="lead-1",
        company_id=COMPANY_ID,
        name="Jordan",
        kind="renter",
        stage="new",
        source="sms",
        signals=["Wants 4B", "Tour this weekend"],
        draft_reply="Hi Jordan!",
    )
    generate = AsyncMock()

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=cached)),
        patch("helixis.api.routes.crm.leads_repo.list_messages", AsyncMock(return_value=[])),
        patch("helixis.api.routes.crm.docs_repo.default_template", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.generate_lead_draft", generate),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/crm/leads/{LEAD_ID}/draft",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == ["Wants 4B", "Tour this weekend"]
    assert body["draft_reply"] == "Hi Jordan!"
    generate.assert_not_called()  # no template + cached conversational draft → no LLM call


def test_draft_generates_and_caches_on_miss():
    from helixis.crm.draft import LeadDraft

    settings = _settings(True)
    uncached = _sample_lead()  # signals/draft default None
    generate = AsyncMock(return_value=LeadDraft(signals=["Noticed X"], draft_reply="Drafted reply"))
    set_draft = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=uncached)),
        patch("helixis.api.routes.crm.leads_repo.list_messages", AsyncMock(return_value=[])),
        patch("helixis.api.routes.crm.docs_repo.default_template", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.set_lead_draft", set_draft),
        patch("helixis.api.routes.crm.generate_lead_draft", generate),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/crm/leads/{LEAD_ID}/draft",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["signals"] == ["Noticed X"]
    assert body["draft_reply"] == "Drafted reply"
    generate.assert_awaited_once()
    set_draft.assert_awaited_once()  # non-empty result is cached


# ── reply (outbound SMS) ─────────────────────────────────────────────


def test_reply_sends_sms_records_and_moves_to_contacted():
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    get_lead = AsyncMock(return_value=_sample_lead())
    send_sms = AsyncMock(return_value="SM123")
    insert_message = AsyncMock(return_value="msg-1")
    update_stage = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", get_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.api.routes.crm.leads_repo.update_stage", update_stage),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "Thanks! Two slots open Saturday."},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["message_sid"] == "SM123"
    assert body["stage"] == "contacted"
    send_sms.assert_awaited_once()
    assert send_sms.await_args.kwargs["to"] == "+14155551234"
    update_stage.assert_awaited_once()
    assert update_stage.await_args.kwargs["stage"] == "contacted"


def test_reply_400_when_lead_has_no_phone():
    # An explicit sms reply to a lead with no phone → 400 (can't text).
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    lead = LeadRow(
        id="lead-1",
        company_id=COMPANY_ID,
        name="No Phone",
        kind="renter",
        stage="new",
        source="zillow",
        email="x@y.com",
        phone=None,
    )
    send_sms = AsyncMock(return_value="SM")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "hi", "channel": "sms"},
            )
    assert resp.status_code == 400
    send_sms.assert_not_called()


def test_reply_503_when_twilio_unconfigured():
    settings = _settings(True, {})  # crm on, twilio blank
    lead = _sample_lead()
    send_sms = AsyncMock(return_value="SM")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "hi"},
            )
    assert resp.status_code == 503
    send_sms.assert_not_called()


# ── inbound webhook (Twilio) ─────────────────────────────────────────


def test_the_real_inbound_route_has_no_signature_bypass():
    """``skip_signature_check`` used to reach this route.

    That meant a deployment carrying the TESTING flag accepted ANY unsigned
    POST here and created leads from it — and now that this route also mirrors
    STOP/START, an unsigned request could forge a consent revocation. That is
    exactly why ``api/routes/twilio.py`` has never had a bypass. The flag still
    gates ``/sms/simulate``, which remains the keyless testing path.
    """
    settings = _settings(True, {**_TWILIO_LIVE, "skip_signature_check": True})
    insert_lead = AsyncMock(return_value="new-lead-1")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={"From": "+14155551234", "Body": "Is it available?", "MessageSid": "SM9"},
            )
    assert resp.status_code == 403
    insert_lead.assert_not_awaited()


def test_inbound_creates_lead_on_a_signed_request():
    settings = _settings(True, _TWILIO_LIVE)
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="new-lead-1")
    insert_message = AsyncMock(return_value="msg-1")

    with (
        _signed_inbound(),
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={
                    "From": "+14155551234",
                    "To": "+18005551212",
                    "Body": "Is it available?",
                    "MessageSid": "SM9",
                },
            )
    assert resp.status_code == 200
    insert_lead.assert_awaited_once()
    insert_message.assert_awaited_once()
    assert insert_message.await_args.args[1].direction == "inbound"


def test_inbound_declines_when_no_company_owns_the_number():
    """The cross-tenant guarantee. ``default_company_id`` was ONE id for the
    whole deployment, so a second customer's inbound text filed into the first
    customer's pipeline. An unowned number now routes NOWHERE — a dropped
    inbound is recoverable by a human, one filed in the wrong company's
    account is not."""
    settings = _settings(True, _TWILIO_LIVE)
    insert_lead = AsyncMock(return_value="new-lead-1")

    with (
        patch("helixis.api.routes.crm.twilio_client.validate_signature", return_value=True),
        patch(
            "helixis.api.routes.crm.resolve_company_for_number",
            AsyncMock(return_value=None),
        ),
        patch("helixis.api.routes.crm.apply_consent_keywords", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={
                    "From": "+14155551234",
                    "To": "+19998887777",
                    "Body": "hi",
                    "MessageSid": "S1",
                },
            )
    # 200 so Twilio does not retry a request that will fail identically.
    assert resp.status_code == 200
    insert_lead.assert_not_awaited()


def test_inbound_honours_stop_before_touching_leads():
    """A Twilio number has ONE inbound URL. Whichever route the console names
    must honour opt-out, because a missed STOP is a TCPA violation while a
    missed lead is an inconvenience. Before this, pointing the number at the
    CRM route meant STOP was never mirrored at all."""
    settings = _settings(True, _TWILIO_LIVE)
    consent = AsyncMock(return_value="stop")

    with (
        _signed_inbound(),
        patch("helixis.api.routes.crm.apply_consent_keywords", consent),
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="l-1")),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={
                    "From": "+14155551234",
                    "To": "+18005551212",
                    "Body": "STOP",
                    "MessageSid": "S2",
                },
            )
    assert resp.status_code == 200
    consent.assert_awaited_once()
    assert consent.await_args.kwargs["body"] == "STOP"


def test_inbound_photo_triggers_maintenance_triage():
    """#533 — a texted photo (MMS media) runs the vision triage on top of the
    normal SMS ingest; a draft work order is recorded."""
    settings = _settings(True, _TWILIO_LIVE)
    triage = AsyncMock(return_value=True)

    with (
        _signed_inbound(),
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="lead-1")),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.webhooks.sms_photo_triage.triage_inbound_photo", triage),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={
                    "From": "+14155551234",
                    "To": "+18005551212",
                    "Body": "my sink is leaking",
                    "MessageSid": "SM9",
                    "NumMedia": "1",
                    "MediaUrl0": "https://api.twilio.com/m/ME0",
                    "MediaContentType0": "image/jpeg",
                },
            )
    assert resp.status_code == 200
    triage.assert_awaited_once()
    assert triage.await_args.kwargs["company_id"] == COMPANY_ID


def test_inbound_text_only_skips_triage():
    """A plain text (no media) must NOT invoke the vision triage."""
    settings = _settings(True, _TWILIO_LIVE)
    triage = AsyncMock(return_value=False)

    with (
        _signed_inbound(),
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="lead-1")),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.webhooks.sms_photo_triage.triage_inbound_photo", triage),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={
                    "From": "+14155551234",
                    "To": "+18005551212",
                    "Body": "is it available?",
                    "MessageSid": "SM9",
                },
            )
    assert resp.status_code == 200
    triage.assert_not_awaited()


def test_inbound_rejects_bad_signature():
    settings = _settings(True, _TWILIO_LIVE)  # skip_signature_check defaults False
    insert_lead = AsyncMock(return_value="x")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={"From": "+14155551234", "Body": "hi", "MessageSid": "SM9"},
                headers={"X-Twilio-Signature": "not-a-valid-signature"},
            )
    assert resp.status_code == 403
    insert_lead.assert_not_called()


def test_inbound_404_when_crm_flag_off():
    settings = _settings(False, {**_TWILIO_LIVE, "skip_signature_check": True})
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/inbound",
                data={"From": "+1", "Body": "hi"},
            )
    assert resp.status_code == 404


# ── simulate (keyless testing) ───────────────────────────────────────


def test_simulate_404_unless_skip_flag_on():
    settings = _settings(True, _TWILIO_LIVE)  # skip_signature_check False
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_number": "+1", "body": "hi"},
            )
    assert resp.status_code == 404


def _settings_email(ingest: bool) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        crm={"enabled": True, "email_ingest_enabled": ingest},
    )


def test_email_simulate_404_when_ingest_disabled():
    settings = _settings_email(False)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/email/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_email": "a@convo.zillow.com", "subject": "hi"},
            )
    assert resp.status_code == 404


def test_email_simulate_ingests_a_lead_shaped_email():
    settings = _settings_email(True)
    ingest = AsyncMock(return_value="email-lead-1")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.crm_lead_ingest.ingest_email_as_lead", ingest),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/email/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_email": "jordan@convo.zillow.com", "subject": "Is 4B available?"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_lead"] is True
    assert body["lead_id"] == "email-lead-1"
    ingest.assert_awaited_once()


def test_email_simulate_rejects_non_lead():
    settings = _settings_email(True)
    ingest = AsyncMock(return_value="should-not-run")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.crm_lead_ingest.ingest_email_as_lead", ingest),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/email/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_email": "newsletter@marketing.com", "subject": "Weekly digest"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_lead"] is False
    assert body["lead_id"] is None
    ingest.assert_not_called()


# ── going-cold (needs a nudge) ───────────────────────────────────────


def test_overview_includes_cold_count():
    from helixis.db.repositories.leads import LeadMetrics

    settings = _settings(True)
    counts = {"new": 1, "contacted": 1, "scheduled": 0, "applied": 0, "leased": 0}
    metrics = LeadMetrics(
        new_this_month=2, new_prev_month=1, replies_this_month=1, active=2, leased=0, total=2
    )

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch(
            "helixis.api.routes.crm.leads_repo.count_leads_by_stage", AsyncMock(return_value=counts)
        ),
        patch(
            "helixis.api.routes.crm.leads_repo.recent_leads_by_stage", AsyncMock(return_value=[])
        ),
        patch("helixis.api.routes.crm.leads_repo.lead_metrics", AsyncMock(return_value=metrics)),
        patch("helixis.api.routes.crm.leads_repo.count_cold_leads", AsyncMock(return_value=4)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/overview",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    assert resp.json()["cold_count"] == 4


def test_cold_leads_lists_going_cold():
    settings = _settings(True)
    cold = [_sample_lead(), _sample_lead()]
    cold_leads = AsyncMock(return_value=cold)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.cold_leads", cold_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/leads/cold",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["threshold_days"] == 3
    assert len(body["leads"]) == 2
    cold_leads.assert_awaited_once()
    assert cold_leads.await_args.kwargs["company_id"] == COMPANY_ID


def test_cold_leads_404_when_flag_off():
    settings = _settings(False)
    cold_leads = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.cold_leads", cold_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/crm/leads/cold",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404
    cold_leads.assert_not_called()


# ── manual controls (move stage / mark lost / add lead) ──────────────


def test_move_stage_updates_and_returns_lead():
    settings = _settings(True)
    lead = _sample_lead()
    get_lead = AsyncMock(return_value=lead)
    update_stage = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", get_lead),
        patch("helixis.api.routes.crm.leads_repo.update_stage", update_stage),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/stage",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"stage": "scheduled"},
            )
    assert resp.status_code == 200
    update_stage.assert_awaited_once()
    assert update_stage.await_args.kwargs["stage"] == "scheduled"


def test_move_stage_400_on_unknown_stage():
    settings = _settings(True)
    update_stage = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.update_stage", update_stage),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/stage",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"stage": "archived"},
            )
    assert resp.status_code == 400
    update_stage.assert_not_called()


def test_move_stage_404_when_lead_missing():
    settings = _settings(True)
    update_stage = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.update_stage", update_stage),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{MISSING_LEAD_ID}/stage",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"stage": "contacted"},
            )
    assert resp.status_code == 404
    update_stage.assert_not_called()


def test_mark_lost_updates_and_returns_lead():
    settings = _settings(True)
    lost = LeadRow(
        id="lead-1",
        company_id=COMPANY_ID,
        name="Gone",
        kind="renter",
        stage="new",
        source="sms",
        status="lost",
    )
    set_status = AsyncMock(return_value=True)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_lead_status", set_status),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lost)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/status",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"status": "lost"},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "lost"
    set_status.assert_awaited_once()
    assert set_status.await_args.kwargs["status"] == "lost"


def test_status_404_when_lead_missing():
    settings = _settings(True)
    set_status = AsyncMock(return_value=False)  # no row updated

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_lead_status", set_status),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{MISSING_LEAD_ID}/status",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"status": "lost"},
            )
    assert resp.status_code == 404


def test_status_400_on_unknown_status():
    settings = _settings(True)
    set_status = AsyncMock(return_value=True)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_lead_status", set_status),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/status",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"status": "archived"},
            )
    assert resp.status_code == 400
    set_status.assert_not_called()


def test_create_lead_inserts_manual_source():
    settings = _settings(True)
    insert_lead = AsyncMock(return_value="new-lead-1")
    created = LeadRow(
        id="new-lead-1",
        company_id=COMPANY_ID,
        name="Walk In",
        kind="renter",
        stage="new",
        source="manual",
        phone="+14155559999",
    )

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=created)),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/leads",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"name": "Walk In", "phone": "+14155559999", "unit": "Maple 4B"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "manual"
    assert body["id"] == "new-lead-1"
    insert_lead.assert_awaited_once()
    # the LeadRow handed to insert carries the manual source + company scope
    row = insert_lead.await_args.args[1]
    assert row.source == "manual"
    assert row.company_id == COMPANY_ID


def test_create_lead_400_when_empty():
    settings = _settings(True)
    insert_lead = AsyncMock(return_value="x")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/leads",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"name": "  ", "note": "no identity"},
            )
    assert resp.status_code == 400
    insert_lead.assert_not_called()


def test_create_lead_404_when_flag_off():
    settings = _settings(False)
    insert_lead = AsyncMock(return_value="x")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/leads",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"name": "Walk In"},
            )
    assert resp.status_code == 404
    insert_lead.assert_not_called()


def test_stage_list_defaults_to_active_status():
    """The board hides won't-pursue leads: the route passes status='active'
    to the repo by default, and status=all clears the filter."""
    settings = _settings(True)
    list_leads = AsyncMock(return_value=[])

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.list_leads", list_leads),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            c.get(
                "/api/v1/crm/leads?stage=new",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
            assert list_leads.await_args.kwargs["status"] == "active"
            c.get(
                "/api/v1/crm/leads?stage=new&status=all",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
            assert list_leads.await_args.kwargs["status"] is None


def test_simulate_creates_lead_when_enabled():
    settings = _settings(True, {**_TWILIO_LIVE, "skip_signature_check": True})
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="sim-lead-1")
    insert_message = AsyncMock(return_value="msg-1")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/sms/simulate",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"from_number": "+14155551234", "body": "Is the unit available?"},
            )
    assert resp.status_code == 200
    assert resp.json()["lead_id"] == "sim-lead-1"
    insert_lead.assert_awaited_once()


# ── click-to-call (Twilio bridge) ────────────────────────────────────


def test_call_lead_places_bridge_and_logs():
    settings = _settings(True, _TWILIO_VOICE)
    get_lead = AsyncMock(return_value=_sample_lead())
    create_call = AsyncMock(return_value="CA-bridge-1")
    insert_message = AsyncMock(return_value="msg-1")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", get_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.api.routes.crm.twilio_client.create_call", create_call),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    assert resp.json()["call_sid"] == "CA-bridge-1"
    # rings the PM's cell first; the lead's number is bridged in the TwiML
    assert create_call.await_args.kwargs["to"] == "+15125550100"
    assert "+14155551234" in create_call.await_args.kwargs["twiml"]
    insert_message.assert_awaited_once()
    row = insert_message.await_args.args[1]
    assert row.channel == "call" and row.direction == "outbound"


def test_call_lead_400_when_no_phone():
    settings = _settings(True, _TWILIO_VOICE)
    lead = LeadRow(
        id="lead-1",
        company_id=COMPANY_ID,
        name="No Phone",
        kind="renter",
        stage="new",
        source="zillow",
        phone=None,
    )
    create_call = AsyncMock(return_value="CA")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.twilio_client.create_call", create_call),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 400
    create_call.assert_not_called()


def test_call_lead_503_without_forward_number():
    settings = _settings(True, {**_TWILIO_LIVE})  # configured but no forward_to_number
    create_call = AsyncMock(return_value="CA")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_sample_lead())),
        patch("helixis.api.routes.crm.twilio_client.create_call", create_call),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 503
    create_call.assert_not_called()


def test_call_lead_404_when_flag_off():
    settings = _settings(False, _TWILIO_VOICE)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 404


# ── inbound voice (call → lead, forward, voicemail) ──────────────────


def test_voice_inbound_logs_lead_and_forwards(_signed_voice):
    """A call logs the lead by caller number and returns Dial TwiML that
    forwards to the PM's cell."""
    settings = _settings(True, _TWILIO_VOICE)
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="call-lead-1")
    insert_message = AsyncMock(return_value="msg-1")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+14155551234", "CallSid": "CA1", "To": "+18005551212"},
            )
    assert resp.status_code == 200
    assert "<Dial" in resp.text
    assert "+15125550100" in resp.text  # forwards to the PM's cell
    assert "/api/v1/crm/voice/status" in resp.text
    insert_lead.assert_awaited_once()
    insert_message.assert_awaited_once()
    assert insert_message.await_args.args[1].channel == "call"


def test_voice_inbound_no_forward_goes_to_voicemail(_signed_voice):
    settings = _settings(True, {**_TWILIO_VOICE, "forward_to_number": ""})
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="l1")),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m1")),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+1", "To": "+18005551212", "CallSid": "CA2"},
            )
    assert resp.status_code == 200
    assert "<Record" in resp.text  # no forward number → straight to voicemail


def test_voice_status_missed_returns_voicemail(_signed_voice):
    settings = _settings(True, _TWILIO_VOICE)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post("/api/v1/crm/voice/status", data={"DialCallStatus": "no-answer"})
    assert resp.status_code == 200
    assert "<Record" in resp.text
    assert "/api/v1/crm/voice/voicemail" in resp.text


def test_voice_status_completed_is_empty(_signed_voice):
    settings = _settings(True, _TWILIO_VOICE)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post("/api/v1/crm/voice/status", data={"DialCallStatus": "completed"})
    assert resp.status_code == 200
    assert "<Record" not in resp.text  # PM answered → no voicemail


def test_voice_voicemail_attaches_recording(_signed_voice):
    settings = _settings(True, _TWILIO_VOICE)
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="vm-lead-1")
    insert_message = AsyncMock(return_value="vm-msg-1")

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/voicemail",
                data={
                    "From": "+14155551234",
                    # ⚠ Load-bearing now — the callback resolves the owning
                    # company from the number that was CALLED.
                    "To": "+18005551212",
                    "CallSid": "CA3",
                    "RecordingSid": "RE9",
                    "RecordingUrl": "https://api.twilio.com/rec/RE9",
                    "RecordingDuration": "42",
                },
            )
    assert resp.status_code == 200
    insert_message.assert_awaited_once()
    row = insert_message.await_args.args[1]
    assert row.channel == "voicemail"
    assert "Voicemail" in row.body and "0:42" in row.body
    assert row.external_id == "RE9"
    assert row.raw["recording_url"] == "https://api.twilio.com/rec/RE9"


def test_voice_inbound_404_when_flag_off():
    settings = _settings(False, _TWILIO_VOICE)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+1", "To": "+18005551212", "CallSid": "CA4"},
            )
    assert resp.status_code == 404


def test_voice_inbound_rejects_bad_signature():
    settings = _settings(True, _TWILIO_LIVE)  # skip_signature_check defaults False
    insert_lead = AsyncMock(return_value="x")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+14155551234", "To": "+18005551212", "CallSid": "CA5"},
                headers={"X-Twilio-Signature": "not-valid"},
            )
    assert resp.status_code == 403
    insert_lead.assert_not_called()


def test_voice_simulate_creates_lead_when_enabled():
    settings = _settings(True, {**_TWILIO_VOICE, "skip_signature_check": True})
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="sim-call-1")
    insert_message = AsyncMock(return_value="m1")
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
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
    assert resp.status_code == 200
    assert resp.json()["lead_id"] == "sim-call-1"
    insert_lead.assert_awaited_once()
    # the manufactured lead carries the phone channel + caller number
    assert insert_lead.await_args.args[1].source == "sms"
    assert insert_lead.await_args.args[1].phone == "+14155551234"


# ── Team Activity logging (every CRM action lands on the audit feed) ──


def test_reply_logs_activity_to_team_feed():
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    write_activity = AsyncMock()
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_sample_lead())),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m")),
        patch("helixis.api.routes.crm.leads_repo.update_stage", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", AsyncMock(return_value="SM1")),
        _recipient_reachable(),
        patch("helixis.api.routes.crm.write_activity", write_activity),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "hi"},
            )
    assert resp.status_code == 200
    write_activity.assert_awaited_once()
    entry = write_activity.await_args.args[1]
    assert entry.action_type == "crm_reply_sent"
    assert "Texted" in entry.description
    assert entry.user_id == ME_USER_ID
    assert entry.source == "manual"


def test_create_lead_logs_activity():
    settings = _settings(True)
    write_activity = AsyncMock()
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", AsyncMock(return_value="new-1")),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_sample_lead())),
        patch("helixis.api.routes.crm.write_activity", write_activity),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/leads",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"name": "Walk-in Wanda", "kind": "renter", "source": "manual"},
            )
    assert resp.status_code == 200
    write_activity.assert_awaited_once()
    entry = write_activity.await_args.args[1]
    assert entry.action_type == "crm_lead_created"
    assert "Added lead: Walk-in Wanda" in entry.description


def test_move_stage_logs_activity_with_friendly_label():
    settings = _settings(True)
    write_activity = AsyncMock()
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_sample_lead())),
        patch("helixis.api.routes.crm.leads_repo.update_stage", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.write_activity", write_activity),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/stage",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"stage": "contacted"},
            )
    assert resp.status_code == 200
    entry = write_activity.await_args.args[1]
    assert entry.action_type == "crm_stage_moved"
    assert "to Contacted" in entry.description  # friendly label, not the raw key


def test_mark_lost_logs_activity():
    settings = _settings(True)
    write_activity = AsyncMock()
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_lead_status", AsyncMock(return_value=True)),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_sample_lead())),
        patch("helixis.api.routes.crm.write_activity", write_activity),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/status",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"status": "lost"},
            )
    assert resp.status_code == 200
    entry = write_activity.await_args.args[1]
    assert entry.action_type == "crm_status_changed"
    assert "lost" in entry.description


# ── voicemail transcription (roadmap CRM #3) ────────────────────────


def test_voice_transcription_attaches_by_recording_sid(_signed_voice):
    """Twilio transcribeCallback → attach the text to the voicemail message,
    matched on RecordingSid."""
    settings = _settings(True, _TWILIO_VOICE)
    set_transcription = AsyncMock(return_value=True)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_message_transcription", set_transcription),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/transcription",
                data={
                    # ⚠ ``To`` is now load-bearing: the callback resolves which
                    # company owns the number that was CALLED, instead of a
                    # deployment-global default. Real Twilio always sends it.
                    "To": "+18005551212",
                    "RecordingSid": "RE9",
                    "TranscriptionText": "Calling about the 2 bed on Maple. Call me back.",
                    "TranscriptionStatus": "completed",
                },
            )
    assert resp.status_code == 200
    set_transcription.assert_awaited_once()
    kwargs = set_transcription.await_args.kwargs
    assert kwargs["external_id"] == "RE9"
    assert "Maple" in kwargs["transcription"]


def test_voice_transcription_skips_when_not_completed(_signed_voice):
    """A failed/partial transcription is dropped, not attached."""
    settings = _settings(True, _TWILIO_VOICE)
    set_transcription = AsyncMock(return_value=True)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.set_message_transcription", set_transcription),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/transcription",
                data={
                    "RecordingSid": "RE9",
                    "TranscriptionText": "",
                    "TranscriptionStatus": "failed",
                },
            )
    assert resp.status_code == 200
    set_transcription.assert_not_called()


def test_simulate_voicemail_inserts_and_transcribes():
    """The keyless simulate path drops a voicemail + transcription on the lead."""
    settings = _settings(True, {**_TWILIO_VOICE, "skip_signature_check": True})
    find_by_phone = AsyncMock(return_value=None)
    insert_lead = AsyncMock(return_value="sim-vm-lead")
    insert_message = AsyncMock(return_value="m")
    set_transcription = AsyncMock(return_value=True)
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=_mock_pool()),
        patch("helixis.api.routes.crm.leads_repo.find_lead_by_phone", find_by_phone),
        patch("helixis.api.routes.crm.leads_repo.insert_lead", insert_lead),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.api.routes.crm.leads_repo.set_message_transcription", set_transcription),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/simulate-voicemail",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={
                    "from_number": "+14155551234",
                    "transcription": "Left a message about a tour.",
                },
            )
    assert resp.status_code == 200
    # A voicemail message was inserted (among the call-log + voicemail inserts).
    channels = [call.args[1].channel for call in insert_message.await_args_list]
    assert "voicemail" in channels
    set_transcription.assert_awaited_once()
    assert "tour" in set_transcription.await_args.kwargs["transcription"]


# ─────────────────────────────────────────────────────────────────────
# Per-company telephony at the ROUTE level (migrations 0082 / 0083).
#
# The module-level tests in test_inbound_number_routing.py prove the resolver.
# These prove the routes USE it — which is a different question, and the one a
# passing resolver cannot answer. Every value below is deliberately DIFFERENT
# from the deployment-global setting, so a route that quietly kept reading
# ``settings.twilio.*`` fails rather than coincidentally agreeing.
# ─────────────────────────────────────────────────────────────────────

_OTHER_COMPANY = "99999999-9999-9999-9999-999999999999"


def _pool_with_company_number(
    *,
    company_id: str = COMPANY_ID,
    phone: str = "+17775550001",
    forward_to: str | None = "+13105557777",
    greeting: str | None = "Blue Ridge, please hold",
) -> MagicMock:
    """A pool where ``helixis_twilio_numbers`` maps ONE number to a company."""
    pool = MagicMock()
    conn = pool.acquire.return_value.__aenter__.return_value
    default = conn.fetchrow

    async def _fetchrow(query: str = "", *args: object, **kw: object) -> object:
        q = str(query)
        if "helixis_a2p_registrations" in q:
            # This company registered and cleared — the state these tests
            # describe. Falling through would hand the gate a MagicMock whose
            # `["status"]` is not "approved", refusing every send as pending.
            return {"status": "approved"}
        if "helixis_twilio_numbers" not in q:
            return await default(query, *args, **kw)
        if "company_id = $1" in q:  # telephony for a company
            return (
                {
                    "phone_number": phone,
                    "forward_to_number": forward_to,
                    "voice_greeting": greeting,
                }
                if args and args[0] == company_id
                else None
            )
        if "WHERE phone_number = $1" in q:  # who owns this number
            return {"company_id": company_id} if args and args[0] == phone else None
        return {"?column?": 1}  # a mapping exists

    conn.fetchrow = _fetchrow
    pool.fetchrow = _fetchrow  # the A2P gate reads the pool directly
    return pool


def test_click_to_call_rings_the_company_s_own_cell_not_a_global_one():
    """A shared ``forward_to_number`` rings ONE phone for every company on the
    deployment — customer B's lead would open a live voice call to customer
    A's cell. The global value here is +15125550100; the company's is not."""
    settings = _settings(True, _TWILIO_VOICE)
    lead = _sample_lead()
    create_call = AsyncMock(return_value="CA-x")
    with (
        patch(
            "helixis.api.routes.crm.get_pool",
            return_value=_pool_with_company_number(),
        ),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.api.routes.crm.twilio_client.create_call", create_call),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{lead.id}/call",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200, resp.text
    kwargs = create_call.await_args.kwargs
    assert kwargs["to"] == "+13105557777", "rang the deployment-global cell, not the company's"
    assert kwargs["from_number"] == "+17775550001", "placed from the global number"


def test_inbound_call_files_under_whoever_owns_the_dialled_number(_signed_voice):
    """Not under a deployment-global default. With a second customer that
    default files every company's calls into one account."""
    settings = _settings(True, _TWILIO_VOICE)
    ingest = AsyncMock(return_value="lead-9")
    with (
        patch(
            "helixis.api.routes.crm.get_pool",
            return_value=_pool_with_company_number(company_id=_OTHER_COMPANY),
        ),
        patch("helixis.api.routes.crm._ingest_inbound_call", ingest),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+14155551234", "To": "+17775550001", "CallSid": "CA-own"},
            )
    assert resp.status_code == 200
    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["company_id"] == _OTHER_COMPANY, (
        "the call was filed under the global default_company_id instead of the "
        "company that owns the number that was dialled"
    )
    # And it forwards to THAT company's cell, not the global one.
    assert "+13105557777" in resp.text


def test_an_inbound_call_to_an_unowned_number_is_declined(_signed_voice):
    settings = _settings(True, _TWILIO_VOICE)
    ingest = AsyncMock(return_value=None)
    with (
        patch(
            "helixis.api.routes.crm.get_pool",
            return_value=_pool_with_company_number(phone="+17775550001"),
        ),
        patch("helixis.api.routes.crm._ingest_inbound_call", ingest),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/crm/voice/inbound",
                data={"From": "+14155551234", "To": "+16665550000", "CallSid": "CA-unowned"},
            )
    assert resp.status_code == 200  # 200 so Twilio does not retry
    ingest.assert_not_awaited()
    assert "<Dial" not in resp.text


def test_the_sms_reply_sends_from_the_company_s_own_number():
    """The bidirectional half. A shared sender puts our message into another
    company's conversation AND routes the lead's reply back to them, because
    the lead replies to whichever number they can see."""
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    lead = _sample_lead()
    send_sms = AsyncMock(return_value="SM-own")
    with (
        patch(
            "helixis.api.routes.crm.get_pool",
            return_value=_pool_with_company_number(),
        ),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.leads_repo.insert_message", AsyncMock(return_value="m-1")),
        patch("helixis.api.routes.crm.leads_repo.update_stage", AsyncMock(return_value=None)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{lead.id}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "Yes, 4B is available — want to tour it?"},
            )
    assert resp.status_code == 200, resp.text
    assert send_sms.await_args.kwargs["from_number"] == "+17775550001", (
        "the reply went out from the deployment-global number, so the lead's "
        "reply would come back to whichever company owns THAT number"
    )


def test_a_company_still_in_carrier_review_cannot_text_a_lead():
    """⚠ The per-company half of the A2P gate, at the ROUTE.

    ``twilio.enabled`` is one flag for the deployment and approval is per
    company, so once ANY customer clears, the flag is on for everyone —
    including customers whose own campaign is still with the carriers.
    Sending then is the violation carriers audit for.

    The AST guard in ``test_crm_a2p_gate.py`` proves the call site exists;
    this proves what the caller does with the answer.
    """
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    lead = _sample_lead()
    send_sms = AsyncMock(return_value="SM-nope")
    pool = _pool_with_company_number()
    approved = pool.fetchrow

    async def _pending(query: str = "", *args: object, **kw: object) -> object:
        if "helixis_a2p_registrations" in str(query):
            return {"status": "pending"}
        return await approved(query, *args, **kw)

    # Both, because the A2P gate reads the pool while everything else in the
    # route goes through acquire().
    pool.fetchrow = _pending
    pool.acquire.return_value.__aenter__.return_value.fetchrow = _pending
    with (
        patch("helixis.api.routes.crm.get_pool", return_value=pool),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{lead.id}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "Yes, 4B is available"},
            )
    assert resp.status_code == 503, resp.text
    send_sms.assert_not_awaited()
    # Says whose wait it is. "Finish setting up Twilio" would send them to fix
    # something that is not theirs to fix and is not the blocker.
    assert "carrier" in resp.json()["detail"].lower()


def test_a_company_with_no_number_cannot_text_a_lead():
    """Refusing beats borrowing. Sending from another company's number is a
    leak in both directions."""
    settings = _settings(True, _TWILIO_SMS_APPROVED)
    lead = _sample_lead()
    send_sms = AsyncMock(return_value="SM-nope")
    with (
        patch(
            "helixis.api.routes.crm.get_pool",
            # A number IS mapped on this deployment — just not to this company.
            return_value=_pool_with_company_number(company_id=_OTHER_COMPANY),
        ),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.twilio_client.send_sms", send_sms),
        _recipient_reachable(),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{lead.id}/reply",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json={"body": "Hello"},
            )
    assert resp.status_code == 503
    send_sms.assert_not_awaited()
