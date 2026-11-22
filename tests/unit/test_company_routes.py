"""PATCH /api/v1/company + POST /api/v1/company/invite.

Backend counterpart for the onboarding wizard's workspace-name and
team-invite steps (ADR 0003 gaps). Tests pin:

1. Auth required on both endpoints.
2. Rename: scoped to the caller's company, admin/manager gated,
   blank names rejected.
3. Invite: 'owner' role refused (privilege-escalation guard — and since
   2026-08-29 'owner' is not a team role at all: it means a Buildium
   RENTAL owner, who has no Occupella login);
   unconfigured Supabase → 503; fresh invite goes through GoTrue and
   upserts the membership; already-registered (GoTrue 422) resolves
   the id from auth.users; non-admin callers are refused.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
USER_ID = "00000000-0000-0000-0000-000000000001"
COMPANY_ID = USER_ID  # user-as-personal-company convention
INVITEE_ID = "00000000-0000-0000-0000-000000000002"


def _token(sub: str = USER_ID, email: str = "owner@example.com") -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        supabase={"url": "https://test.supabase.co", "service_key": "service-key"},
    )


def _build_pool(
    *,
    caller_role: str | None = "admin",
    existing_user: bool = False,
    membership_inserted: bool = True,
    invitee_role: str | None = None,
):
    """Fake asyncpg pool. Captures execute() calls; fetchrow() answers
    the role lookups and (optionally) the auth.users email lookup.

    A company_users lookup naming ``INVITEE_ID`` answers ``invitee_role`` (None:
    not a member), any other the caller's role, so the invite's already-a-member
    check can't be answered by the caller's own row.

    ``membership_inserted=False`` makes the company_users upsert report
    ``INSERT 0 0`` (ON CONFLICT DO NOTHING hit)."""
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(sql: str, *args: Any):
        if "FROM public.company_users" in sql:
            if INVITEE_ID in args:
                return {"role": invitee_role} if invitee_role else None
            return {"role": caller_role} if caller_role else None
        if "FROM auth.users" in sql:
            return {"id": INVITEE_ID} if existing_user else None
        return None

    async def execute(sql: str, *args: Any) -> str:
        captured.append((sql, args))
        if "INSERT INTO public.company_users" in sql:
            return "INSERT 0 1" if membership_inserted else "INSERT 0 0"
        return "INSERT 0 1" if sql.lstrip().startswith("INSERT") else "UPDATE 1"

    pool = MagicMock()
    pool.fetchrow = fetchrow
    pool.execute = execute

    @asynccontextmanager
    async def _acquire():
        yield pool

    pool.acquire = _acquire
    return pool, captured


class _FakeGotrueResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


def _make_client(
    test_settings: Settings,
    *,
    pool: Any,
    gotrue_response: _FakeGotrueResponse | None = None,
    gotrue_responses: list[_FakeGotrueResponse] | None = None,
) -> TestClient:
    """``gotrue_responses`` answers a BATCH — one response per address, in
    order — so a bulk invite can mix a fresh account with an existing one and
    a hard failure. ``gotrue_response`` stays the single-invite form and is
    used for every call when given."""
    from helixis.api.rate_limit import connectors_limiter, invite_limiter

    connectors_limiter.counter._log.clear()
    invite_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> _FakeGotrueResponse:
            if gotrue_responses is not None:
                assert gotrue_responses, "more GoTrue calls than scripted responses"
                return gotrue_responses.pop(0)
            assert gotrue_response is not None, "unexpected GoTrue call"
            return gotrue_response

    patches = [
        patch("helixis.api.routes.company.get_pool", return_value=pool),
        patch("helixis.api.routes.company.get_settings", return_value=test_settings),
        patch("helixis.api.routes.company.httpx.AsyncClient", _FakeAsyncClient),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ]
    for p in patches:
        p.start()
    app = create_app(test_settings)
    client = TestClient(app)
    original_close = client.close

    def close_with_patches() -> None:
        for p in reversed(patches):
            p.stop()
        original_close()

    client.close = close_with_patches  # type: ignore[method-assign]
    return client


# ---- auth gating -----------------------------------------------------------


def test_rename_requires_authentication(test_settings: Settings) -> None:
    pool, _ = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.patch("/api/v1/company", json={"name": "Acme"})
        assert resp.status_code in (401, 403)
    finally:
        client.close()


def test_invite_requires_authentication(test_settings: Settings) -> None:
    pool, _ = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post("/api/v1/company/invite", json={"email": "a@b.co"})
        assert resp.status_code in (401, 403)
    finally:
        client.close()


# ---- rename ----------------------------------------------------------------


def test_rename_updates_scoped_to_callers_company(test_settings: Settings) -> None:
    pool, captured = _build_pool(caller_role="admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.patch(
            "/api/v1/company",
            json={"name": "  Acme Property Co  "},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    assert resp.json() == {"company_id": COMPANY_ID, "name": "Acme Property Co"}
    assert len(captured) == 1
    sql, args = captured[0]
    assert "UPDATE public.companies" in sql
    assert args == (COMPANY_ID, "Acme Property Co")


def test_rename_refuses_non_admin(test_settings: Settings) -> None:
    pool, captured = _build_pool(caller_role="member")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.patch(
            "/api/v1/company",
            json={"name": "Acme"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 403
    assert captured == []


def test_rename_rejects_blank_name(test_settings: Settings) -> None:
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.patch(
            "/api/v1/company",
            json={"name": "   "},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 422
    assert captured == []


# ---- invite ----------------------------------------------------------------


def test_invite_never_grants_owner(test_settings: Settings) -> None:
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "owner"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 422
    assert captured == []


def test_invite_503_when_supabase_unconfigured(test_settings: Settings) -> None:
    test_settings.supabase.service_key = ""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 503
    assert captured == []


def test_invite_fresh_user_invited_and_membership_upserted(test_settings: Settings) -> None:
    pool, captured = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "New@Example.com", "role": "manager"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    body = resp.json()
    # Response is uniform — no account-existence signal, no user_id leaked.
    assert body["status"] == "ok"
    assert body["email"] == "new@example.com"  # lowercased
    assert body["role"] == "manager"
    assert "user_id" not in body

    # The real behaviour (membership upsert) is verified via the captured
    # SQL, not the response. A best-effort audit-log insert also lands
    # (finding #2), so locate the membership insert rather than assume index.
    membership = [c for c in captured if "INSERT INTO public.company_users" in c[0]]
    assert len(membership) == 1
    sql, args = membership[0]
    assert "ON CONFLICT" in sql
    assert args == (INVITEE_ID, COMPANY_ID, "manager")
    assert any("helixis_activity_log" in c[0] for c in captured)  # audit entry written


def test_invite_existing_account_resolved_from_auth_users(test_settings: Settings) -> None:
    """An existing account is found in auth.users BEFORE GoTrue is asked (GoTrue
    answers 200 for an unconfirmed one), and gets an invite that waits for them.

    ⚠ Re-pointed 2026-11-11. This asserted the membership INSERT for an existing
    account, which was the hole: any account holder could make their company a
    stranger's active one. Now nothing is written to company_users until the
    invitee accepts (migration 0112)."""
    pool, captured = _build_pool(existing_user=True)
    # gotrue_response=None: a GoTrue call would fail the test.
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "existing@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "user_id" not in body
    assert body["email_sent"] is False
    assert body["awaiting_acceptance"] is True
    # No membership: the invite waits, plus the audit entry (finding #2).
    assert not any("INSERT INTO public.company_users" in c[0] for c in captured)
    invites = [c for c in captured if "INSERT INTO public.company_member_invites" in c[0]]
    assert len(invites) == 1
    assert invites[0][1][:4] == (COMPANY_ID, INVITEE_ID, "member", USER_ID)
    assert any("helixis_activity_log" in c[0] for c in captured)


# ---- explicit company targeting (finding #3) -------------------------------

_OTHER_COMPANY = "99999999-9999-9999-9999-999999999999"


def test_invite_rejects_company_the_caller_is_not_in(test_settings: Settings) -> None:
    """An explicit company_id the caller doesn't belong to is 403'd before
    any GoTrue call or DB write."""
    pool, captured = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "member", "company_id": _OTHER_COMPANY},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 403
    assert captured == []


def test_invite_accepts_explicit_member_company(test_settings: Settings) -> None:
    """An explicit company_id the caller IS a member of proceeds normally."""
    pool, captured = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "member", "company_id": COMPANY_ID},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    sql_seen = [c[0] for c in captured]
    assert any("INSERT INTO public.company_users" in s for s in sql_seen)


def test_rename_rejects_company_the_caller_is_not_in(test_settings: Settings) -> None:
    pool, captured = _build_pool(caller_role="admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.patch(
            "/api/v1/company",
            json={"name": "Acme", "company_id": _OTHER_COMPANY},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 403
    assert captured == []


def test_invite_refuses_non_admin(test_settings: Settings) -> None:
    pool, captured = _build_pool(caller_role="employee")
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 403
    assert captured == []


# ---- edge cases (close the remaining gaps so the flow can be trusted
#      without creating real accounts) ---------------------------------------


def test_invite_idempotent_already_member_is_noop(test_settings: Settings) -> None:
    """Re-inviting someone already in the company is a clean no-op: 200, nothing
    written, and NO activity row (the audit entry is only for an actual change).

    ⚠ Re-pointed 2026-11-11: the no-op used to be the membership upsert hitting
    ON CONFLICT; now the membership is read first and nothing is upserted."""
    pool, captured = _build_pool(existing_user=True, invitee_role="member")
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "existing@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["awaiting_acceptance"] is False
    # Nothing written at all, so no audit row either.
    assert captured == []


def test_invite_rejects_unknown_role(test_settings: Settings) -> None:
    """A role outside the invitable set (e.g. a typo or a made-up tier)
    is 422'd — and never reaches GoTrue (gotrue_response=None would assert
    if it were called)."""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "superadmin"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 422
    assert captured == []


def test_invite_rejects_malformed_email(test_settings: Settings) -> None:
    """A non-email string is rejected by request validation (422) before
    any GoTrue call or DB write."""
    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "not-an-email", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 422
    assert captured == []


def test_invite_gotrue_failure_returns_502(test_settings: Settings) -> None:
    """A GoTrue 5xx (mail service down) surfaces as a 502 the client can
    act on, not a silent success — and no membership row is written."""
    pool, captured = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(500, {"msg": "smtp down"}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "new@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 502
    assert not any("INSERT INTO public.company_users" in c[0] for c in captured)


def test_invite_response_is_uniform_fresh_vs_existing(test_settings: Settings) -> None:
    """Enumeration guard, narrowed 2026-09-03 to everything EXCEPT ``email_sent``.

    This asserted byte-for-byte identical bodies. That guarantee cost a real
    invite: on 2026-08-31 a teammate account was added to My Company, Supabase never
    attempted mail (it refuses to invite an address that already has a login),
    and the admin was shown "Invitation sent" — so they waited for a delivery
    that was never coming.

    ⚠ The tradeoff is deliberate and narrow. Learning that an address is
    registered requires ALREADY being an admin of the company you are inviting
    into, is rate-limited, and lands the invitee in that admin's own team list
    where they are visible anyway. Everything genuinely worth hiding —
    ``user_id``, the invitee's other companies, which path ran — stays hidden,
    and this test still pins that: the ONLY permitted difference is the one
    bit the admin has to act on.
    """
    fresh_pool, _ = _build_pool()
    fresh_client = _make_client(
        test_settings,
        pool=fresh_pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    try:
        fresh = fresh_client.post(
            "/api/v1/company/invite",
            json={"email": "fresh@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        fresh_client.close()

    existing_pool, _ = _build_pool(existing_user=True)
    existing_client = _make_client(
        test_settings,
        pool=existing_pool,
        gotrue_response=_FakeGotrueResponse(422, {"msg": "already registered"}),
    )
    try:
        existing = existing_client.post(
            "/api/v1/company/invite",
            json={"email": "fresh@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        existing_client.close()

    assert fresh.status_code == existing.status_code == 200

    fresh_body, existing_body = fresh.json(), existing.json()
    assert fresh_body["email_sent"] is True, "a fresh invite really does send mail"
    assert existing_body["email_sent"] is False, (
        "an address that already has an account gets NO mail — saying otherwise "
        "is what sent an admin waiting for a delivery that was never attempted"
    )

    # ⚠ `awaiting_acceptance` (2026-11-11, migration 0112) is the SAME bit, not a
    # second disclosure: an existing account is exactly the case where no mail goes
    # and the invite waits for them. Pinned as the negation of `email_sent` on both
    # paths, so it can never carry anything `email_sent` doesn't already say.
    for body in (fresh_body, existing_body):
        assert body["awaiting_acceptance"] is (not body["email_sent"])

    # Everything else must still be indistinguishable.
    assert fresh_body.keys() == existing_body.keys(), "a whole FIELD appears on only one path"
    differing = {k for k in fresh_body if fresh_body[k] != existing_body[k]}
    assert differing == {"email_sent", "awaiting_acceptance"}, (
        f"the two paths now differ in {sorted(differing)}. Only 'email_sent' (and "
        "its negation, 'awaiting_acceptance') is a deliberate disclosure; anything "
        "else here is an enumeration leak that tells an admin something they could "
        "not otherwise see."
    )


def test_invite_is_rate_limited(test_settings: Settings) -> None:
    """The invite endpoint is throttled (anti-spam): once the per-user cap
    is hit, further invites get 429 instead of sending mail. Tighten the
    limiter to 1/window for the test, then restore the default."""
    from helixis.api.rate_limit import invite_limiter

    pool, _ = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(200, {"id": INVITEE_ID}),
    )
    invite_limiter.reconfigure(1, 60)  # _make_client already cleared the counter
    try:
        first = client.post(
            "/api/v1/company/invite",
            json={"email": "one@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
        second = client.post(
            "/api/v1/company/invite",
            json={"email": "two@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        invite_limiter.reconfigure(30, 600)  # restore module default
        client.close()

    assert first.status_code == 200
    assert second.status_code == 429


# ---- invite redirect_to (broken-email-link fix) ----------------------------


def test_invite_redirect_prefers_explicit_public_app_url() -> None:
    from helixis.api.routes.company import _invite_redirect_url

    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        api={
            "public_app_url": "https://app.occupella.com/",
            "cors_origins": ["https://other.vercel.app"],
        },
    )
    assert _invite_redirect_url(s) == "https://app.occupella.com"  # trailing slash trimmed


def test_invite_redirect_falls_back_to_first_https_origin() -> None:
    from helixis.api.routes.company import _invite_redirect_url

    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        api={"cors_origins": ["http://localhost:5173", "https://your-app.vercel.app"]},
    )
    assert _invite_redirect_url(s) == "https://your-app.vercel.app"


def test_invite_redirect_none_when_no_https_origin() -> None:
    from helixis.api.routes.company import _invite_redirect_url

    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        api={"cors_origins": ["http://localhost:5173"]},
    )
    assert _invite_redirect_url(s) is None


@pytest.mark.asyncio
async def test_gotrue_invite_passes_redirect_to() -> None:
    """The GoTrue invite call carries ``redirect_to`` (the app URL) so the email
    link returns to the app, not the drifted dashboard Site URL."""
    from helixis.api.routes import company as company_mod

    captured: dict[str, Any] = {}

    class _CapturingResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"id": INVITEE_ID}

    class _CapturingClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _CapturingClient:
            return self

        async def __aexit__(self, *e: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _CapturingResp:
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _CapturingResp()

    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        supabase={"url": "https://test.supabase.co", "service_key": "k"},
        api={"public_app_url": "https://app.occupella.com"},
    )
    with (
        patch.object(company_mod, "get_settings", return_value=s),
        patch.object(company_mod.httpx, "AsyncClient", _CapturingClient),
    ):
        uid = await company_mod._gotrue_invite("new@teammate.com")

    assert uid == INVITEE_ID
    assert captured["params"] == {"redirect_to": "https://app.occupella.com"}


@pytest.mark.asyncio
async def test_gotrue_invite_omits_redirect_when_unresolvable() -> None:
    """No https app URL → no redirect_to param (preserve GoTrue Site-URL default)."""
    from helixis.api.routes import company as company_mod

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"id": INVITEE_ID}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *e: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _Resp:
            captured["params"] = kwargs.get("params")
            return _Resp()

    s = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        supabase={"url": "https://test.supabase.co", "service_key": "k"},
        api={"cors_origins": ["http://localhost:5173"]},
    )
    with (
        patch.object(company_mod, "get_settings", return_value=s),
        patch.object(company_mod.httpx, "AsyncClient", _Client),
    ):
        await company_mod._gotrue_invite("new@teammate.com")

    assert captured["params"] is None


# ---- bulk invite -----------------------------------------------------------
#
# ⚠ WHY A BATCH ENDPOINT EXISTS AT ALL, since a client could obviously loop.
# Two reasons, and neither is convenience:
#
#   * `invite_limiter` is 30/600s and deliberately not tunable by config. A
#     client looping five invites spends five of that budget, so an admin
#     adding a team watches the last two fail for a reason that is about our
#     rate limiter rather than about their people.
#   * every single invite calls `sync_company_seats`, which pushes a quantity
#     to STRIPE. Looping N pushes N quantities — 2, 3, 4, 5, 6 — and Stripe
#     prorates each, so one "add four teammates" lands four proration lines on
#     the next invoice. The batch syncs ONCE, from the count the whole batch
#     produced.
#
# Both of those are behaviours, so both are asserted below rather than left as
# prose in a docstring.


def test_bulk_invite_requires_authentication(test_settings: Settings) -> None:
    pool, _ = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post("/api/v1/company/invite/bulk", json={"invites": [{"email": "a@b.co"}]})
        assert resp.status_code in (401, 403)
    finally:
        client.close()


def test_bulk_invite_refuses_a_non_admin(test_settings: Settings) -> None:
    """The same ADMIN floor as the single invite — a batch must not be a way
    round the role check."""
    pool, captured = _build_pool(caller_role="manager")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/company/invite/bulk",
            json={"invites": [{"email": "a@b.co", "role": "member"}]},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()
    assert resp.status_code == 403
    assert captured == [], "a refused batch wrote to the database"


def test_bulk_invite_reports_on_every_address(test_settings: Settings) -> None:
    pool, _ = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_responses=[
            _FakeGotrueResponse(200, {"id": INVITEE_ID}),
            _FakeGotrueResponse(200, {"id": INVITEE_ID}),
        ],
    )
    try:
        resp = client.post(
            "/api/v1/company/invite/bulk",
            json={
                "invites": [
                    {"email": "One@Example.com", "role": "manager"},
                    {"email": "two@example.com", "role": "member"},
                ]
            },
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["email"] for r in results] == ["one@example.com", "two@example.com"]  # lowercased
    assert [r["role"] for r in results] == ["manager", "member"]
    assert all(r["status"] == "invited" and r["email_sent"] for r in results)


def test_one_bad_address_does_not_lose_the_batch(test_settings: Settings) -> None:
    """⚠ THE POINT OF THE ENDPOINT'S ERROR SHAPE.

    Four good invites plus one typo has to land the four and name the one. A
    batch that 4xx'd as a unit would make the admin re-type addresses they
    already got right, with no way to tell which of the five was the problem —
    and the memberships that DID land would still be real, so the retry would
    also double-invite them.
    """
    pool, _ = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_responses=[
            _FakeGotrueResponse(200, {"id": INVITEE_ID}),
            # 422 with no auth.users row → the route's honest 502 for one address.
            _FakeGotrueResponse(422, {"msg": "already registered"}),
        ],
    )
    try:
        resp = client.post(
            "/api/v1/company/invite/bulk",
            json={
                "invites": [
                    {"email": "good@example.com", "role": "member"},
                    {"email": "bad@example.com", "role": "member"},
                ]
            },
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 200, "one bad address failed the whole batch"
    results = resp.json()["results"]
    assert results[0]["status"] == "invited"
    assert results[1]["status"] == "failed"
    # ⚠ THE SERVER'S OWN WORDS for this address, not a generic apology. A
    # mutation battery caught this assertion at `assert results[1]["error"]`,
    # which survived deleting the `except HTTPException` branch entirely: the
    # exception simply fell through to the catch-all beside it, which also
    # produces *an* error — just one that says nothing the admin can act on.
    # Truthiness could not tell "could not resolve an account for that email"
    # from "something went wrong" (gotcha 65's shape: the fixture carried two
    # signals and the weaker assertion could not separate them).
    assert results[1]["error"] == "Could not resolve an account for that email."


def test_a_duplicate_address_in_one_batch_is_invited_once(test_settings: Settings) -> None:
    """The same address twice is a paste artefact, not an instruction.

    Inviting twice would land the second as `already_member`, which reads to
    the admin like the first one failed.
    """
    pool, _ = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_responses=[_FakeGotrueResponse(200, {"id": INVITEE_ID})],
    )
    try:
        resp = client.post(
            "/api/v1/company/invite/bulk",
            json={
                "invites": [
                    {"email": "dupe@example.com", "role": "member"},
                    {"email": "DUPE@example.com", "role": "admin"},
                ]
            },
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    results = resp.json()["results"]
    assert len(results) == 1
    # The FIRST role given wins — the later duplicate does not silently
    # promote somebody to admin.
    assert results[0]["role"] == "member"


def test_the_batch_syncs_stripe_once_not_once_per_address(test_settings: Settings) -> None:
    """⚠ The reason this endpoint exists rather than a client loop.

    Verified by counting CALLS to `sync_company_seats`, not by reading the
    route: the loop-shaped version is a one-line change that looks correct and
    silently prorates every intermediate quantity.
    """
    pool, _ = _build_pool()
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_responses=[_FakeGotrueResponse(200, {"id": INVITEE_ID}) for _ in range(3)],
    )
    with patch("helixis.api.routes.company.sync_company_seats") as sync:
        try:
            client.post(
                "/api/v1/company/invite/bulk",
                json={
                    "invites": [{"email": f"p{i}@example.com", "role": "member"} for i in range(3)]
                },
                headers={"Authorization": f"Bearer {_token()}"},
            )
        finally:
            client.close()
    assert sync.call_count == 1, (
        f"seats synced {sync.call_count} times for one batch — each call is a "
        "Stripe quantity write, and Stripe prorates every one of them."
    )


def test_a_batch_that_changed_nothing_does_not_touch_stripe(test_settings: Settings) -> None:
    """Everybody already a member → no membership written → no quantity to push.

    A sync here would be a no-op API call on every re-invite, and
    `sync_company_seats` is best-effort, so a failed no-op would log an error
    about a company whose seats are perfectly correct.
    """
    pool, _ = _build_pool(existing_user=True, invitee_role="member")
    client = _make_client(test_settings, pool=pool, gotrue_responses=[])
    with patch("helixis.api.routes.company.sync_company_seats") as sync:
        try:
            resp = client.post(
                "/api/v1/company/invite/bulk",
                json={"invites": [{"email": "member@example.com", "role": "member"}]},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        finally:
            client.close()
    assert resp.json()["results"][0]["status"] == "already_member"
    assert sync.call_count == 0


def test_bulk_invite_refuses_a_list_longer_than_the_cap(test_settings: Settings) -> None:
    """A blast radius, not a product cap: this is ONE request that can send
    arbitrary mail, and an unbounded list is a spam cannon pointed at our own
    sending reputation."""
    from helixis.api.routes.company import MAX_BULK_INVITES

    pool, captured = _build_pool()
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            "/api/v1/company/invite/bulk",
            json={
                "invites": [
                    {"email": f"p{i}@example.com", "role": "member"}
                    for i in range(MAX_BULK_INVITES + 1)
                ]
            },
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()
    assert resp.status_code == 422
    assert captured == [], "an over-long batch reached the database"


def test_the_client_and_server_agree_on_the_batch_cap() -> None:
    """⚠ Reads BOTH sides (gotcha 28). `MAX_BULK_INVITES` is duplicated in
    `frontend/src/services/company.ts` so the dialog can refuse a 26th row with
    a sentence instead of letting the whole batch 422 after the admin has typed
    all of them. A second copy of a server constant drifts silently — that is
    exactly what `PaidPlanKey` did — so the two are pinned against each other.
    """
    import re
    from pathlib import Path

    from helixis.api.routes.company import MAX_BULK_INVITES

    ts = (Path(__file__).resolve().parents[2] / "frontend/src/services/company.ts").read_text(
        encoding="utf-8"
    )
    found = re.search(r"export const MAX_BULK_INVITES = (\d+)", ts)
    assert found, "the client no longer declares MAX_BULK_INVITES"
    assert int(found.group(1)) == MAX_BULK_INVITES, (
        f"client says {found.group(1)}, server says {MAX_BULK_INVITES}. The "
        "client's number only decides when to refuse locally; a client that "
        "thinks the cap is higher lets an admin type a list the server rejects."
    )


def test_a_bad_role_is_422_even_when_invites_are_unconfigured(test_settings: Settings) -> None:
    """⚠ ORDER, not behaviour-in-isolation — and a DEPLOYED probe reads it.

    `scripts/role_matrix.py` sends a VALID email with an INVALID role against
    the live backend and interprets the status: 403 means the admin floor
    fired, 422 means the floor let the caller past and `INVITABLE_ROLES`
    stopped them. That only works while the role check sits AFTER the floor and
    BEFORE the deployment-config check.

    This broke once, silently, in the refactor that extracted `_invite_one`:
    the role validation moved into the shared helper, which runs after
    `_require_invites_configured`, so an unconfigured deployment started
    answering 503 where the probe expects 422 — and every existing test still
    passed, because none of them set up both conditions at once.
    """
    # Re-pointed 2026-11-11: the URL was blank too, and a blank URL now refuses
    # every token (no issuer to check), so the caller never reached the route.
    # The missing SERVICE KEY alone makes invites unconfigured
    # (`_require_invites_configured` refuses on either), so both conditions
    # still hold at once.
    unconfigured = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        supabase={"url": TEST_SUPABASE_URL, "service_key": ""},
    )
    pool, captured = _build_pool(caller_role="admin")
    client = _make_client(unconfigured, pool=pool)
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "valid@example.com", "role": "owner"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    finally:
        client.close()

    assert resp.status_code == 422, (
        f"got {resp.status_code}. The role check has moved behind the "
        "deployment-config check — role_matrix.py's admin row now reads 503 "
        "and stops testing the floor."
    )
    assert captured == [], "a refused invite wrote to the database"
