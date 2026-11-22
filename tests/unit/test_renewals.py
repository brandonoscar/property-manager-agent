"""Renewals autopilot (migration 0044) — draft module, repo, and routes.

Pins: the deterministic fallback draft (LLM is an enhancement, never
load-bearing), the repo's draft-only edit guard + re-draft-resets-sent
semantics, the computed-renewed query shape, and the route guards around
send (no draft → 404, no tenant email → 400, Gmail not connected → 503 —
Helixis never auto-sends, so the send route IS the only outbound seam).
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.db.repositories import renewals as renewals_repo
from helixis.main import create_app
from helixis.renewals import draft as renewals_draft
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL


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


# ── draft module ──────────────────────────────────────────────────────


def test_build_facts_includes_terms_and_notes() -> None:
    facts = renewals_draft.build_facts(
        tenant_names="Priya Natarajan",
        unit_number="3C",
        property_name="Garden Row",
        current_rent=1850.0,
        lease_to_date=date(2026, 9, 30),
        proposed_rent=1895.0,
        term_months=12,
        pm_notes="mention the new laundry room",
    )
    assert "Priya Natarajan" in facts
    assert "Garden Row, Unit 3C" in facts
    assert "$1,850.00/month" in facts and "$1,895.00/month" in facts
    assert "2026-09-30" in facts
    assert "12 months" in facts
    assert "laundry room" in facts


def test_fallback_draft_is_complete_and_deterministic() -> None:
    fb = renewals_draft.fallback_draft(
        tenant_names="Priya Natarajan",
        unit_number="3C",
        property_name="Garden Row",
        current_rent=1850.0,
        lease_to_date=date(2026, 9, 30),
        proposed_rent=None,  # unchanged rent falls back to current
        term_months=None,
    )
    assert fb.generated is False
    assert "Garden Row, Unit 3C" in fb.subject
    assert "Priya Natarajan" in fb.body
    assert "$1,850.00/month" in fb.body  # unchanged → current rent quoted
    assert "12-month" in fb.body  # default term


@pytest.mark.asyncio
async def test_generate_falls_back_when_llm_fails() -> None:
    settings = MagicMock()
    settings.llm.models = {}
    settings.llm.default_model = "gemini/gemini-x"
    with (
        patch.object(renewals_draft.litellm, "acompletion", AsyncMock(side_effect=RuntimeError)),
        patch.object(renewals_draft, "_api_key_for_model", return_value=None),
    ):
        out = await renewals_draft.generate_renewal_draft(
            settings,
            tenant_names="Priya",
            unit_number="3C",
            property_name="Garden Row",
            current_rent=1850.0,
            lease_to_date=date(2026, 9, 30),
            proposed_rent=1895.0,
            term_months=12,
        )
    assert out.generated is False
    assert out.subject and out.body  # the PM always gets an editable draft


# ── repository ────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, fetch_rows: list | None = None, fetchrow_result: Any = None) -> None:
        self.fetch = AsyncMock(return_value=fetch_rows or [])
        self.fetchrow = AsyncMock(return_value=fetchrow_result)


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _offer_record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-00000000dead",
        "lease_buildium_id": 9101,
        "status": "draft",
        "proposed_rent": 1895.0,
        "term_months": 12,
        "subject": "s",
        "body": "b",
        "recipient_email": "t@example.com",
        "sent_at": None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_upsert_draft_resets_sent_state_on_conflict() -> None:
    conn = _Conn(fetchrow_result=_offer_record())
    await renewals_repo.upsert_draft(
        _pool(conn),
        company_id=COMPANY_ID,
        lease_buildium_id=9101,
        property_id=700,
        unit_id=55,
        proposed_rent=1895.0,
        term_months=12,
        subject="s",
        body="b",
        recipient_email="t@example.com",
        created_by=ME_USER_ID,
    )
    sql = conn.fetchrow.await_args.args[0]
    assert "ON CONFLICT (company_id, lease_buildium_id)" in sql
    # Re-drafting a sent offer revives it as a draft with a clean send slate.
    assert "status          = 'draft'" in sql
    assert "sent_at         = NULL" in sql


@pytest.mark.asyncio
async def test_update_draft_only_touches_drafts() -> None:
    conn = _Conn(fetchrow_result=None)
    out = await renewals_repo.update_draft(
        _pool(conn),
        company_id=COMPANY_ID,
        lease_buildium_id=9101,
        proposed_rent=None,
        term_months=None,
        subject="s",
        body="b",
        recipient_email=None,
    )
    assert out is None  # sent/dismissed offers aren't editable
    assert "status = 'draft'" in conn.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_renewed_is_computed_from_moved_lease_end_date() -> None:
    conn = _Conn(fetch_rows=[_offer_record(status="sent")])
    out = await renewals_repo.list_renewed_offers(
        _pool(conn), company_id=COMPANY_ID, property_id=700
    )
    sql = conn.fetch.await_args.args[0]
    # A sent offer whose lease end date left the window = the PM executed
    # the renewal in Buildium and the mirror caught it.
    assert "o.status = 'sent'" in sql
    assert "l.lease_to_date > now() + make_interval" in sql
    assert out[0].renewed is True


@pytest.mark.asyncio
async def test_set_status_rejects_unknown_transitions() -> None:
    with pytest.raises(ValueError):
        await renewals_repo.set_status(
            _pool(_Conn()), company_id=COMPANY_ID, lease_buildium_id=1, status="sent"
        )


# ── routes ────────────────────────────────────────────────────────────


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


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _client_env(settings: Settings):
    return (
        patch("helixis.api.routes.renewals.get_pool", return_value=MagicMock()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token()}"}


def test_list_renewals_returns_candidates_and_renewed() -> None:
    settings = _settings()
    candidate = renewals_repo.RenewalCandidate(
        lease_buildium_id=9101,
        lease_to_date=date(2026, 9, 30),
        current_rent=1850.0,
        unit_id=55,
        unit_number="3C",
        tenant_names="Priya Natarajan",
        tenant_email="priya@example.com",
        offer=None,
    )
    with ExitStack() as stack:
        for cm in _client_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.renewals.renewals_repo.list_candidates",
                AsyncMock(return_value=[candidate]),
            )
        )
        stack.enter_context(
            patch(
                "helixis.api.routes.renewals.renewals_repo.list_renewed_offers",
                AsyncMock(return_value=[]),
            )
        )
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/properties/700/renewals", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"][0]["tenant_names"] == "Priya Natarajan"
    assert body["candidates"][0]["lease_to_date"] == "2026-09-30"


def test_send_404s_without_a_draft_and_400s_without_email() -> None:
    settings = _settings()
    with ExitStack() as stack:
        for cm in _client_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.renewals.renewals_repo.get_offer",
                AsyncMock(return_value=None),
            )
        )
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post("/api/v1/properties/700/renewals/9101/send", headers=_auth())
    assert resp.status_code == 404

    no_email = renewals_repo.RenewalOffer(
        id="x",
        lease_buildium_id=9101,
        status="draft",
        proposed_rent=None,
        term_months=None,
        subject="s",
        body="b",
        recipient_email=None,
        sent_at=None,
    )
    with ExitStack() as stack:
        for cm in _client_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.renewals.renewals_repo.get_offer",
                AsyncMock(return_value=no_email),
            )
        )
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post("/api/v1/properties/700/renewals/9101/send", headers=_auth())
    assert resp.status_code == 400


def test_send_503s_when_gmail_not_connected() -> None:
    settings = _settings()
    sendable = renewals_repo.RenewalOffer(
        id="x",
        lease_buildium_id=9101,
        status="draft",
        proposed_rent=1895.0,
        term_months=12,
        subject="s",
        body="b",
        recipient_email="priya@example.com",
        sent_at=None,
    )
    send_fn = MagicMock()
    with ExitStack() as stack:
        for cm in _client_env(settings):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.api.routes.renewals.renewals_repo.get_offer",
                AsyncMock(return_value=sendable),
            )
        )
        stack.enter_context(patch("helixis.api.routes.renewals.send_gmail_email", send_fn))
        app = create_app(settings)
        with TestClient(app) as c:
            # Test-app composio client is absent/unavailable → 503, and the
            # send seam is NEVER reached (the never-auto-send invariant).
            app.state.composio_client = None
            resp = c.post("/api/v1/properties/700/renewals/9101/send", headers=_auth())
    assert resp.status_code == 503
    send_fn.assert_not_called()
