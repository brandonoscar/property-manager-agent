"""Applicant decision write — endpoint contract, tool guard rails, CRM route.

Spec-grounded 2026-07-06 (LeadSimple SDK): status lives on the APPLICATION —
``PUT /applicants/{id}/applications/{appId}`` with exactly
``{"ApplicationStatus": <Undecided|Approved|Rejected|Cancelled|Deferred>}``;
reads carry the same ``ApplicationStatus`` key. The API has NO delete
operation on applications, so "Helixis never deletes applications" is
structural — these tests pin the allowlist as the only reachable statuses.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.agents import buildium_tools as bt
from helixis.buildium.endpoints.applicants import Applicants
from helixis.db.repositories.leads import LeadRow
from helixis.main import create_app
from helixis.memory.context import memory_context
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

# ── endpoint ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_application_status_put_path_and_body() -> None:
    request = AsyncMock(return_value={"Id": 77, "ApplicationStatus": "Approved"})
    client = MagicMock()
    client.request = request

    await Applicants(client).update_application_status(5045, 77, "Approved")
    method, path = request.await_args.args[:2]
    assert (method, path) == ("PUT", "/applicants/5045/applications/77")
    assert request.await_args.kwargs["body"] == {"ApplicationStatus": "Approved"}


@pytest.mark.asyncio
async def test_update_application_status_rejects_unknown_status() -> None:
    client = MagicMock()
    client.request = AsyncMock()
    with pytest.raises(ValueError):
        await Applicants(client).update_application_status(5045, 77, "Deleted")
    client.request.assert_not_awaited()


# ── chat tool guard rails ────────────────────────────────────────


class _ClientCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _ctx() -> Any:
    return memory_context(company_id="co-1", user_id="u1", session_id="s1")


def _api_with_applications(applications: list[dict[str, Any]]) -> MagicMock:
    api = MagicMock()
    api.applicants.list_applications = AsyncMock(return_value=applications)
    api.applicants.update_application_status = AsyncMock(return_value={})
    return api


def _patched_open_api(api: MagicMock):
    return patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None)))


@pytest.mark.asyncio
async def test_tool_rejects_bad_status_before_any_api_call() -> None:
    open_api = AsyncMock()
    with _ctx(), patch.object(bt, "_open_api", open_api):
        out = await bt._update_applicant_status_impl(5045, "Deleted", "Priya")
    assert "must be one of" in out.lower() or "Status must be" in out
    open_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_asks_when_multiple_applications() -> None:
    api = _api_with_applications(
        [
            {"Id": 71, "ApplicationStatus": "Undecided"},
            {"Id": 72, "ApplicationStatus": "Deferred"},
        ]
    )
    run_guarded = AsyncMock()
    with _ctx(), _patched_open_api(api), patch.object(bt, "run_guarded", run_guarded):
        out = await bt._update_applicant_status_impl(5045, "Approved", "Priya")
    assert "#71" in out and "#72" in out and "application_id" in out
    run_guarded.assert_not_awaited()  # never a write without a chosen target


@pytest.mark.asyncio
async def test_tool_short_circuits_when_already_at_status() -> None:
    api = _api_with_applications([{"Id": 71, "ApplicationStatus": "Approved"}])
    run_guarded = AsyncMock()
    with _ctx(), _patched_open_api(api), patch.object(bt, "run_guarded", run_guarded):
        out = await bt._update_applicant_status_impl(5045, "Approved", "Priya")
    assert "already" in out.lower()
    run_guarded.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_confirm_card_and_execute_hit_the_spec_put() -> None:
    api = _api_with_applications([{"Id": 71, "ApplicationStatus": "Undecided"}])
    captured: dict[str, Any] = {}

    async def _fake_run_guarded(**kwargs: Any) -> Any:
        captured.update(kwargs)
        result = await kwargs["execute"]()
        return MagicMock(outcome="executed", result=result)

    with _ctx(), _patched_open_api(api), patch.object(bt, "run_guarded", _fake_run_guarded):
        out = await bt._update_applicant_status_impl(5045, "Approved", "Priya Natarajan")

    # ⚠ REWRITTEN 2026-08-24. This asserted the model-SUPPLIED label appears —
    # it pinned the vulnerability as a requirement. Applicants have no getter,
    # so the card shows the checkable ID form rather than echoing a name on the
    # model's say-so. See test_confirm_label_provenance.
    assert 'Set applicant 5045\'s application status to "Approved"' in out
    assert "Priya Natarajan" not in out
    confirm = captured["confirm"]
    assert confirm["action"] == "buildium_update_applicant_status"
    # The card reads like a human decision: name + old → new status.
    assert any("applicant 5045" in item for item in confirm["items"])
    assert not any("Priya Natarajan" in item for item in confirm["items"]), (
        "the confirm card rendered the model-supplied applicant label"
    )
    assert any("Undecided → Approved" in item for item in confirm["items"])
    api.applicants.update_application_status.assert_awaited_once_with(5045, 71, "Approved")


# ── CRM route ────────────────────────────────────────────────────

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
# Must be a real UUID — the route 404s a malformed lead_id before the repo
# (see crm._require_valid_lead_id). Matches the id _lead() returns.
LEAD_ID = "00000000-0000-0000-0000-0000000000aa"


def _make_token() -> str:
    return jwt.encode(
        {
            "sub": ME_USER_ID,
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
        crm={"enabled": True},
    )


def _lead(applicant_id: int | None) -> LeadRow:
    return LeadRow(
        id="00000000-0000-0000-0000-0000000000aa",
        company_id=COMPANY_ID,
        name="Jordan Reyes",
        kind="renter",
        stage="applied",
        source="zillow",
        email="jordan@example.com",
        phone=None,
        unit_label=None,
        note=None,
        raw={},
        buildium_applicant_id=applicant_id,
    )


@asynccontextmanager
async def _yield_api(api):
    yield api


def _route_env(settings: Settings, lead: LeadRow | None):
    return (
        patch("helixis.api.routes.crm.get_pool", return_value=MagicMock()),
        patch("helixis.api.routes.crm.leads_repo.get_lead", AsyncMock(return_value=lead)),
        patch("helixis.api.routes.crm.write_activity", AsyncMock()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    )


def _post(app: Any, lead_id: str, body: dict[str, Any]) -> Any:
    with TestClient(app) as c:
        return c.post(
            f"/api/v1/crm/leads/{lead_id}/applicant-status",
            json=body,
            headers={"Authorization": f"Bearer {_make_token()}"},
        )


def test_route_rejects_bad_status_and_unlinked_lead() -> None:
    settings = _settings()
    with ExitStack() as stack:
        for cm in _route_env(settings, _lead(applicant_id=5045)):
            stack.enter_context(cm)
        app = create_app(settings)
        resp = _post(app, LEAD_ID, {"status": "Deleted"})
    assert resp.status_code == 400  # unknown status never reaches Buildium

    with ExitStack() as stack:
        for cm in _route_env(settings, _lead(applicant_id=None)):
            stack.enter_context(cm)
        app = create_app(settings)
        resp = _post(app, LEAD_ID, {"status": "Approved"})
    assert resp.status_code == 400
    assert "isn't linked" in resp.json()["detail"]


def test_route_puts_status_for_linked_lead() -> None:
    settings = _settings()
    api = MagicMock()
    api.applicants.list_applications = AsyncMock(
        return_value=[{"Id": 71, "ApplicationStatus": "Undecided"}]
    )
    api.applicants.update_application_status = AsyncMock(return_value={})
    with ExitStack() as stack:
        for cm in _route_env(settings, _lead(applicant_id=5045)):
            stack.enter_context(cm)
        stack.enter_context(
            patch(
                "helixis.webhooks.handlers._helpers.open_api_for_company",
                return_value=_yield_api(api),
            )
        )
        app = create_app(settings)
        resp = _post(app, LEAD_ID, {"status": "Approved"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "status": "Approved", "application_id": 71}
    api.applicants.update_application_status.assert_awaited_once_with(5045, 71, "Approved")
