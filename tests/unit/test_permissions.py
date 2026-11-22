"""Team roster + role-management endpoints (ADR 0005).

GET    /api/v1/company/members            — roster, manager+ visibility
POST   /api/v1/company/members/{id}/role  — change a role, admin only
DELETE /api/v1/company/members/{id}       — remove a member, admin only

Pins the role gating (member can't view the team, only admins mutate), the
last-admin guard (a company can never be left with zero admins), and the
legacy-role normalization on read (a stored 'owner' reads as admin, so the
deployed backend keeps working before migration 0034 reconciles the rows).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
ADMIN_ID = "00000000-0000-0000-0000-000000000001"
COMPANY_ID = ADMIN_ID  # user-as-personal-company convention
MEMBER_ID = "00000000-0000-0000-0000-000000000002"
MANAGER_ID = "00000000-0000-0000-0000-000000000003"
SECOND_ADMIN_ID = "00000000-0000-0000-0000-000000000004"


def _token(sub: str = ADMIN_ID, email: str = "admin@example.com") -> str:
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


def _row(user_id: str, role: str, email: str, *, invite_pending: bool = False) -> dict[str, Any]:
    """One roster row as ``list_members``' SELECT returns it.

    ⚠ ``invite_pending`` is not optional decoration. ``list_members`` reads it
    with ``row["invite_pending"]``, so a fixture missing the key raises
    KeyError INSIDE the route and the caller gets a 500 — which is how three
    tests here went red: the column was added to the query and to
    ``test_roster_invite_pending``'s fixture, and this file's was never
    updated (gotcha 25's "fix it at every layer", pointed at a test double).

    The failure shape is worth knowing: only the ADMIN and MANAGER cases
    broke, because they are the only ones that reach ``list_members`` at all.
    A member 403s before it and passed throughout, which reads like a
    permissions bug rather than a missing key.
    """
    return {
        "user_id": user_id,
        "role": role,
        "email": email,
        "display_name": None,
        "invite_pending": invite_pending,
    }


class _FakePool:
    """Answers the queries roles/members code issues, off an in-memory roster.

    ``caller_id``/``caller_role`` drive ``require_role``'s own-role lookup; the
    ``roster`` list (pre-sorted by join order) drives the member lookups and the
    admin count. ``execute`` calls are captured so tests can assert the write
    (and the audit row) happened."""

    def __init__(
        self,
        roster: list[dict[str, Any]],
        caller_id: str,
        caller_role: str | None,
        *,
        knowledge: dict[str, Any] | None = None,
    ):
        self.roster = roster
        self.caller_id = caller_id
        self.caller_role = caller_role
        self.knowledge = knowledge
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any):
        if "company_knowledge" in sql:  # company_knowledge.load
            return self.knowledge
        if "WHERE user_id = $1::uuid AND company_id" in sql:  # require_role own-role
            return {"role": self.caller_role} if self.caller_role else None
        if "WHERE company_id = $1::uuid AND user_id" in sql:  # get_member_role
            target = args[1]
            for m in self.roster:
                if m["user_id"] == target:
                    return {"role": m["role"]}
            return None
        return None

    async def fetch(self, sql: str, *args: Any):
        if "company_member_invites" in sql:  # member_invites.for_company: none waiting
            return []
        if "LEFT JOIN auth.users" in sql:  # list_members roster
            return [dict(m) for m in self.roster]
        if "SELECT role FROM public.company_users WHERE company_id" in sql:  # count_admins
            return [{"role": m["role"]} for m in self.roster]
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        stripped = sql.strip()
        if stripped.startswith("UPDATE"):
            return "UPDATE 1"
        if stripped.startswith("DELETE"):
            return "DELETE 1"
        return "INSERT 0 1"

    def acquire(self):  # noqa: ANN201 — write_activity's `async with pool.acquire()`
        pool = self

        @asynccontextmanager
        async def _cm():
            yield pool

        return _cm()


def _make_client(test_settings: Settings, *, pool: Any) -> TestClient:
    from helixis.api.rate_limit import connectors_limiter, invite_limiter

    connectors_limiter.counter._log.clear()
    invite_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    patches = [
        patch("helixis.api.routes.company.get_pool", return_value=pool),
        patch("helixis.api.routes.company.get_settings", return_value=test_settings),
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


def _headers(sub: str = ADMIN_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(sub)}"}


# ---- list members ----------------------------------------------------------


def test_members_requires_authentication(test_settings: Settings) -> None:
    pool = _FakePool([_row(ADMIN_ID, "admin", "admin@example.com")], ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/members")
        assert resp.status_code in (401, 403)
    finally:
        client.close()


def test_members_listed_for_admin_with_unknown_role_folded_down(test_settings: Settings) -> None:
    """The roster surfaces canonical roles + the primary-admin flag, and an
    unrecognised stored value folds DOWN to member.

    ⚠ Renamed 2026-08-29. This used to read "a stored legacy 'owner' caller
    reads as admin", which was a deploy->migrate shim from before migration
    0034 — and the shim is gone, because 'owner' in this product means a
    Buildium RENTAL owner with no Occupella login. The 'employee' row below
    still demonstrates the surviving rule, but for the opposite reason: it is
    no longer aliased, it is simply unrecognised, and normalize_role folds
    anything unrecognised to least privilege.
    """
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MANAGER_ID, "manager", "manager@example.com"),
        _row(MEMBER_ID, "employee", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/members", headers=_headers())
    finally:
        client.close()

    assert resp.status_code == 200
    body = resp.json()
    assert body["company_id"] == COMPANY_ID
    by_id = {m["user_id"]: m for m in body["members"]}
    assert by_id[ADMIN_ID]["role"] == "admin"
    assert by_id[ADMIN_ID]["is_primary_admin"] is True
    assert by_id[ADMIN_ID]["is_self"] is True
    assert by_id[MEMBER_ID]["role"] == "member"  # unrecognised -> least privilege
    assert by_id[MANAGER_ID]["role"] == "manager"
    # Admins first in the ordering.
    assert body["members"][0]["role"] == "admin"


def test_members_manager_can_view(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MANAGER_ID, "manager", "manager@example.com"),
    ]
    pool = _FakePool(roster, MANAGER_ID, "manager")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/members", headers=_headers(MANAGER_ID))
    finally:
        client.close()
    assert resp.status_code == 200


def test_members_hidden_from_member(test_settings: Settings) -> None:
    """Visibility gating: a plain member can't see the team-management screen."""
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, MEMBER_ID, "member")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/members", headers=_headers(MEMBER_ID))
    finally:
        client.close()
    assert resp.status_code == 403


# ---- my role ---------------------------------------------------------------


def test_my_role_returns_normalized_role_and_primary_flag(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/me", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "admin"  # 'owner' normalized
    assert body["is_primary_admin"] is True
    assert body["company_id"] == COMPANY_ID


def test_my_role_member_allowed(test_settings: Settings) -> None:
    """Any member may read their own role (unlike the roster, which is manager+)."""
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, MEMBER_ID, "member")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/me", headers=_headers(MEMBER_ID))
    finally:
        client.close()
    assert resp.status_code == 200
    assert resp.json()["role"] == "member"
    assert resp.json()["is_primary_admin"] is False


def test_my_role_non_member_403(test_settings: Settings) -> None:
    pool = _FakePool([_row(ADMIN_ID, "admin", "admin@example.com")], MEMBER_ID, None)
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/me", headers=_headers(MEMBER_ID))
    finally:
        client.close()
    assert resp.status_code == 403


# ---- change role -----------------------------------------------------------


def test_role_change_promotes_member_and_audits(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{MEMBER_ID}/role",
            json={"role": "manager"},
            headers=_headers(),
        )
    finally:
        client.close()

    assert resp.status_code == 200
    assert resp.json()["role"] == "manager"
    assert any("UPDATE public.company_users SET role" in c[0] for c in pool.executed)
    assert any("helixis_activity_log" in c[0] for c in pool.executed)


def test_role_change_requires_admin(test_settings: Settings) -> None:
    """A manager cannot change roles — admin only."""
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MANAGER_ID, "manager", "manager@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, MANAGER_ID, "manager")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{MEMBER_ID}/role",
            json={"role": "manager"},
            headers=_headers(MANAGER_ID),
        )
    finally:
        client.close()
    assert resp.status_code == 403
    assert not any("UPDATE public.company_users" in c[0] for c in pool.executed)


def test_role_change_unknown_role_422(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{MEMBER_ID}/role",
            json={"role": "superadmin"},
            headers=_headers(),
        )
    finally:
        client.close()
    assert resp.status_code == 422


def test_role_change_legacy_owner_input_rejected(test_settings: Settings) -> None:
    """'owner' is not a canonical role — an admin can't re-introduce it by hand
    (input is validated literally, not normalized)."""
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{MEMBER_ID}/role",
            json={"role": "owner"},
            headers=_headers(),
        )
    finally:
        client.close()
    assert resp.status_code == 422


def test_role_change_non_member_404(test_settings: Settings) -> None:
    roster = [_row(ADMIN_ID, "admin", "admin@example.com")]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{MEMBER_ID}/role",
            json={"role": "manager"},
            headers=_headers(),
        )
    finally:
        client.close()
    assert resp.status_code == 404


def test_role_change_last_admin_demote_blocked(test_settings: Settings) -> None:
    """Demoting the only admin would orphan the company — 409, no write."""
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{ADMIN_ID}/role",
            json={"role": "member"},
            headers=_headers(),
        )
    finally:
        client.close()
    assert resp.status_code == 409
    assert not any("UPDATE public.company_users" in c[0] for c in pool.executed)


def test_role_change_admin_demote_allowed_when_another_admin_exists(
    test_settings: Settings,
) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(SECOND_ADMIN_ID, "admin", "admin2@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.post(
            f"/api/v1/company/members/{SECOND_ADMIN_ID}/role",
            json={"role": "manager"},
            headers=_headers(),
        )
    finally:
        client.close()
    assert resp.status_code == 200
    assert any("UPDATE public.company_users SET role" in c[0] for c in pool.executed)


# ---- remove member ---------------------------------------------------------


def test_remove_member_by_admin(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.delete(f"/api/v1/company/members/{MEMBER_ID}", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"
    assert any("DELETE FROM public.company_users" in c[0] for c in pool.executed)
    assert any("helixis_activity_log" in c[0] for c in pool.executed)


def test_remove_requires_admin(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MANAGER_ID, "manager", "manager@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, MANAGER_ID, "manager")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.delete(f"/api/v1/company/members/{MEMBER_ID}", headers=_headers(MANAGER_ID))
    finally:
        client.close()
    assert resp.status_code == 403
    assert not any("DELETE FROM public.company_users" in c[0] for c in pool.executed)


def test_remove_last_admin_blocked(test_settings: Settings) -> None:
    roster = [
        _row(ADMIN_ID, "admin", "admin@example.com"),
        _row(MEMBER_ID, "member", "member@example.com"),
    ]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.delete(f"/api/v1/company/members/{ADMIN_ID}", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 409
    assert not any("DELETE FROM public.company_users" in c[0] for c in pool.executed)


def test_remove_non_member_404(test_settings: Settings) -> None:
    roster = [_row(ADMIN_ID, "admin", "admin@example.com")]
    pool = _FakePool(roster, ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.delete(f"/api/v1/company/members/{MEMBER_ID}", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 404


# ---- company knowledge (ADR 0006) ------------------------------------------


def test_company_knowledge_empty_when_no_row(test_settings: Settings) -> None:
    """No profile row yet → 200 with empty content (the UI shows 'still learning')."""
    pool = _FakePool([_row(ADMIN_ID, "admin", "admin@example.com")], ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/knowledge", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_md"] == ""
    assert body["generated_on"] is None


def test_company_knowledge_returns_profile(test_settings: Settings) -> None:
    import datetime

    knowledge = {
        "company_id": COMPANY_ID,
        "content_md": "## Portfolio\n\n40 leases.",
        "generated_on": datetime.date(2026, 7, 4),
        "updated_at": datetime.datetime(2026, 7, 4, 9, 0, tzinfo=datetime.UTC),
    }
    pool = _FakePool(
        [_row(ADMIN_ID, "admin", "admin@example.com")], ADMIN_ID, "admin", knowledge=knowledge
    )
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/knowledge", headers=_headers())
    finally:
        client.close()
    assert resp.status_code == 200
    body = resp.json()
    assert "40 leases" in body["content_md"]
    assert body["generated_on"] == "2026-07-04"


def test_company_knowledge_requires_auth(test_settings: Settings) -> None:
    pool = _FakePool([_row(ADMIN_ID, "admin", "admin@example.com")], ADMIN_ID, "admin")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.get("/api/v1/company/knowledge")
        assert resp.status_code in (401, 403)
    finally:
        client.close()


# ---- company knowledge is READ-ONLY (no hand-typed layer, #67) --------------


def test_company_knowledge_surface_is_read_only(test_settings: Settings) -> None:
    """There is no write endpoint on /company/knowledge — Helixis LEARNS the
    company (daily distiller + Drive/docs); nobody hand-types context into it
    (founder directive). A PUT must not exist, and no company_knowledge write
    can be triggered through this surface."""
    pool = _FakePool([_row(MANAGER_ID, "manager", "manager@example.com")], MANAGER_ID, "manager")
    client = _make_client(test_settings, pool=pool)
    try:
        resp = client.put(
            "/api/v1/company/knowledge",
            headers=_headers(MANAGER_ID),
            json={"authored_md": "Late fee is $50."},
        )
    finally:
        client.close()
    # No PUT route registered → 405 Method Not Allowed (never 200).
    assert resp.status_code == 405
    assert not any("company_knowledge" in sql for sql, _ in pool.executed)
