"""Lead lands in draft + one-click send-by-email.

Covers the "default message first, conversational after" rule for
``GET /crm/leads/{id}/draft`` and the channel-aware ``POST /crm/leads/{id}/reply``
(email via Composio Gmail), plus the pure ``crm.email_send`` helpers.

The route tests mock the repo + the Composio send seam so the phase logic and
the 200/503 branches are pinned without a live LLM or Gmail.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.crm.draft import LeadDraft
from helixis.crm.email_send import (
    GMAIL_SEND_SLUG,
    build_send_arguments,
    extract_message_id,
    send_gmail_email,
)
from helixis.db.repositories.docs_links import TemplateRow
from helixis.db.repositories.leads import LeadRow, MessageRow
from helixis.main import create_app
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
LEAD_ID = "00000000-0000-0000-0000-0000000000aa"


# ── pure email-send helpers ─────────────────────────────────────────


def test_build_send_arguments_shape_and_caps() -> None:
    args = build_send_arguments(recipient_email="a@b.co", subject="  Hi  ", body="hello")
    assert args["recipient_email"] == "a@b.co"
    assert args["subject"] == "Hi"
    assert args["body"] == "hello"
    assert args["is_html"] is False


def test_extract_message_id_variants() -> None:
    assert extract_message_id({"id": "m1"}) == "m1"
    assert extract_message_id({"message_id": "m2"}) == "m2"
    assert extract_message_id({"data": {"thread_id": "t3"}}) == "t3"
    assert extract_message_id({"nope": 1}) is None
    assert extract_message_id(None) is None


def test_send_gmail_email_unavailable_success_and_failure() -> None:
    ok, mid = send_gmail_email(None, user_id="u", recipient_email="a@b.co", subject="s", body="b")
    assert ok is False and mid is None

    composio = MagicMock(available=True)
    composio.execute_tool.return_value = {"id": "sent_1"}
    ok, mid = send_gmail_email(
        composio, user_id="u", recipient_email="a@b.co", subject="s", body="b"
    )
    assert ok is True and mid == "sent_1"
    _, kwargs = composio.execute_tool.call_args
    assert kwargs["slug"] == GMAIL_SEND_SLUG

    composio.execute_tool.return_value = None
    ok, mid = send_gmail_email(
        composio, user_id="u", recipient_email="a@b.co", subject="s", body="b"
    )
    assert ok is False and mid is None

    composio.execute_tool.side_effect = RuntimeError("boom")
    ok, mid = send_gmail_email(
        composio, user_id="u", recipient_email="a@b.co", subject="s", body="b"
    )
    assert ok is False and mid is None


# ── shared route scaffolding ────────────────────────────────────────


def _token(sub: str = ME_USER_ID) -> str:
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


def _settings(twilio: dict | None = None) -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        crm={"enabled": True},
        twilio=twilio or {},
    )


def _lead(*, phone: str | None = "+14155551234", email: str | None = None) -> LeadRow:
    return LeadRow(
        id=LEAD_ID,
        company_id=COMPANY_ID,
        name="Jordan Reyes",
        kind="renter",
        stage="new",
        source="zillow",
        email=email,
        phone=phone,
        unit_label="Maple Court 4B",
        note="Is 4B still available?",
        raw={},
    )


# ── draft: default template first, conversational after ─────────────


def test_first_contact_draft_uses_default_template() -> None:
    """No outbound yet → the drafted reply is the default template, merge-filled;
    the LLM is used only for the 'noticed' signals (cached signals-only)."""
    settings = _settings()
    template = TemplateRow(
        id="tmpl-1", name="Welcome", body="Hi {first_name}, {property} is still available!"
    )
    generate = AsyncMock(return_value=LeadDraft(signals=["Wants 4B"], draft_reply="conversational"))
    set_signals = AsyncMock(return_value=None)
    set_draft = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=_lead())),
        patch("helixis.api.routes.crm.leads_repo.list_messages", AsyncMock(return_value=[])),
        patch(
            "helixis.api.routes.crm.docs_repo.default_template", AsyncMock(return_value=template)
        ),
        patch("helixis.api.routes.crm.leads_repo.set_lead_signals", set_signals),
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
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_reply"] == "Hi Jordan, Maple Court 4B is still available!"
    assert body["signals"] == ["Wants 4B"]
    set_signals.assert_awaited_once()  # signals cached, not the template reply
    set_draft.assert_not_called()  # the template draft is never persisted as the conv draft


def test_conversational_draft_after_first_outbound() -> None:
    """Once a reply has gone out, the drafted reply is the cached conversational
    one — the default template is not consulted."""
    settings = _settings()
    cached = _lead()
    cached = LeadRow(**{**cached.__dict__, "draft_reply": "Great to hear from you again!"})
    outbound = [
        MessageRow(
            company_id=COMPANY_ID, lead_id=LEAD_ID, direction="outbound", channel="sms", body="hi"
        )
    ]
    default_template = AsyncMock(
        return_value=TemplateRow(id="t", name="n", body="unused {first_name}")
    )
    generate = AsyncMock()

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=cached)),
        patch("helixis.api.routes.crm.leads_repo.list_messages", AsyncMock(return_value=outbound)),
        patch("helixis.api.routes.crm.docs_repo.default_template", default_template),
        patch("helixis.api.routes.crm.generate_lead_draft", generate),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/crm/leads/{LEAD_ID}/draft",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    assert resp.json()["draft_reply"] == "Great to hear from you again!"
    default_template.assert_not_awaited()  # has outbound → template phase skipped
    generate.assert_not_called()  # conversational draft was cached


# ── reply: email channel (one-click send) ───────────────────────────


def test_reply_sends_email_when_lead_has_no_phone() -> None:
    settings = _settings()
    lead = _lead(phone=None, email="jordan@example.com")
    send_email = MagicMock(return_value=(True, "gmail-1"))
    insert_message = AsyncMock(return_value="msg-1")
    update_stage = AsyncMock(return_value=None)

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.leads_repo.insert_message", insert_message),
        patch("helixis.api.routes.crm.leads_repo.update_stage", update_stage),
        patch("helixis.api.routes.crm.send_gmail_email", send_email),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            # Set after startup so the lifespan doesn't overwrite it.
            app.state.composio_client = MagicMock(available=True)
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"body": "Thanks for reaching out — happy to help!"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "email"
    assert body["message_sid"] == "gmail-1"
    assert body["stage"] == "contacted"
    send_email.assert_called_once()
    assert insert_message.await_args.args[1].channel == "email"
    assert update_stage.await_args.kwargs["stage"] == "contacted"


def test_reply_email_503_when_gmail_not_connected() -> None:
    settings = _settings()
    lead = _lead(phone=None, email="jordan@example.com")
    send_email = MagicMock(return_value=(True, "x"))

    with (
        patch("helixis.api.routes.crm.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.send_gmail_email", send_email),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        # No composio_client on app.state → Gmail unavailable.
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/crm/leads/{LEAD_ID}/reply",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"body": "hi"},
            )
    assert resp.status_code == 503
    send_email.assert_not_called()
