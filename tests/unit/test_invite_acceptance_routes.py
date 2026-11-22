"""The routes of an invite that waits for an existing account (migration 0112).

``test_invites_need_acceptance`` pins that no code path writes the membership;
``test_company_users_capture_db`` runs the SQL. This pins the HTTP side: the capture
is refused by the invite route, the invitee alone accepts or declines, an admin sees
and withdraws what waits, and each route refuses a caller who isn't allowed and bad
input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from helixis.db.repositories import member_invites
from helixis.db.repositories.members import Member
from helixis.settings import Settings
from tests.unit.test_company_routes import (
    COMPANY_ID,
    INVITEE_ID,
    USER_ID,
    _build_pool,
    _FakeGotrueResponse,
    _make_client,
    _token,
    test_settings,  # noqa: F401 — the fixture, used by name below
)

INVITE_ID = "11111111-2222-4333-8444-555555555555"
OTHER_COMPANY = "99999999-9999-9999-9999-999999999999"
_AUTH = {"Authorization": f"Bearer {_token()}"}
_INVITEE_AUTH = {"Authorization": f"Bearer {_token(sub=INVITEE_ID, email='victim@example.com')}"}


def _pending(**overrides: Any) -> member_invites.PendingInvite:
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    fields: dict[str, Any] = {
        "id": INVITE_ID,
        "company_id": COMPANY_ID,
        "company_name": "Acme Property Co",
        "user_id": INVITEE_ID,
        "email": "victim@example.com",
        "role": "member",
        "invited_by_email": "owner@example.com",
        "created_at": now,
        "expires_at": now + timedelta(days=14),
    }
    fields.update(overrides)
    return member_invites.PendingInvite(**fields)


def _membership_writes(captured: list[tuple[str, tuple[Any, ...]]]) -> list[str]:
    return [sql for sql, _ in captured if "INSERT INTO public.company_users" in sql]


# ── The capture, refused by the route ──────────────────────────────────────────


def test_the_capture_is_refused_by_the_invite_route(test_settings: Settings) -> None:  # noqa: F811
    """Any account holder admins their own sign-up company. Inviting a stranger's
    address that already has an account writes NO membership for them: nothing of
    theirs changes until they accept. GoTrue is never asked (None asserts on a call),
    so an account still unconfirmed is no different."""
    pool, captured = _build_pool(existing_user=True)
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    with patch("helixis.api.routes.company.sync_company_seats") as sync:
        try:
            resp = client.post(
                "/api/v1/company/invite",
                json={"email": "victim@example.com", "role": "admin"},
                headers=_AUTH,
            )
        finally:
            client.close()
    assert resp.status_code == 200
    assert resp.json()["awaiting_acceptance"] is True
    assert _membership_writes(captured) == []
    assert not any("DELETE FROM public.company_users" in sql for sql, _ in captured)
    assert sync.call_count == 0, "a pending invite takes no seat"


def test_a_gotrue_422_after_an_empty_lookup_is_an_existing_account(
    test_settings: Settings,  # noqa: F811
) -> None:
    """Someone confirms between our lookup and GoTrue's: still a pending invite."""
    pool, captured = _build_pool()
    lookups = iter([None, {"id": INVITEE_ID}])
    original = pool.fetchrow

    async def fetchrow(sql: str, *args: Any) -> Any:
        if "FROM auth.users" in sql:
            return next(lookups)
        return await original(sql, *args)

    pool.fetchrow = fetchrow
    client = _make_client(
        test_settings,
        pool=pool,
        gotrue_response=_FakeGotrueResponse(422, {"msg": "already registered"}),
    )
    try:
        resp = client.post(
            "/api/v1/company/invite",
            json={"email": "racer@example.com", "role": "member"},
            headers=_AUTH,
        )
    finally:
        client.close()
    assert resp.status_code == 200
    assert resp.json()["awaiting_acceptance"] is True
    assert _membership_writes(captured) == []


def test_before_0112_an_existing_account_is_refused_not_added(
    test_settings: Settings,  # noqa: F811
) -> None:
    """Deployed ahead of its migration, the route fails closed: 503, nothing written."""
    pool, captured = _build_pool(existing_user=True)
    client = _make_client(test_settings, pool=pool, gotrue_response=None)
    with patch.object(
        member_invites,
        "upsert",
        AsyncMock(side_effect=member_invites.InvitesUnavailableError("missing")),
    ):
        try:
            resp = client.post(
                "/api/v1/company/invite",
                json={"email": "victim@example.com", "role": "member"},
                headers=_AUTH,
            )
        finally:
            client.close()
    assert resp.status_code == 503
    assert "already has an Occupella account" in resp.json()["detail"]
    assert _membership_writes(captured) == []


def test_a_bulk_invite_reports_an_existing_account_as_pending(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, captured = _build_pool(existing_user=True)
    client = _make_client(test_settings, pool=pool, gotrue_responses=[])
    with patch("helixis.api.routes.company.sync_company_seats") as sync:
        try:
            resp = client.post(
                "/api/v1/company/invite/bulk",
                json={"invites": [{"email": "victim@example.com", "role": "member"}]},
                headers=_AUTH,
            )
        finally:
            client.close()
    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert (result["status"], result["email_sent"]) == ("pending", False)
    assert _membership_writes(captured) == []
    assert sync.call_count == 0


# ── The invitee's side ─────────────────────────────────────────────────────────


def test_my_invites_lists_what_waits_for_the_caller(test_settings: Settings) -> None:  # noqa: F811
    pool, _ = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    with patch.object(member_invites, "for_invitee", AsyncMock(return_value=[_pending()])) as m:
        try:
            resp = client.get("/api/v1/company/invites", headers=_INVITEE_AUTH)
            unauthenticated = client.get("/api/v1/company/invites")
        finally:
            client.close()
    assert resp.status_code == 200
    assert resp.json()["invites"] == [
        {
            "id": INVITE_ID,
            "company_id": COMPANY_ID,
            "company_name": "Acme Property Co",
            "role": "member",
            "invited_by_email": "owner@example.com",
            "expires_at": "2026-11-25T12:00:00+00:00",
        }
    ]
    assert m.await_args is not None
    assert m.await_args.kwargs["user_id"] == INVITEE_ID, "only the caller's own invites"
    assert unauthenticated.status_code in (401, 403)


def test_accepting_joins_the_callers_own_account_and_counts_the_seat(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, captured = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    accepted = member_invites.Accepted(
        company_id=COMPANY_ID, company_name="Acme Property Co", role="member", invited_by=USER_ID
    )
    with (
        patch.object(member_invites, "accept", AsyncMock(return_value=accepted)) as accept,
        patch("helixis.api.routes.company.sync_company_seats") as sync,
    ):
        try:
            resp = client.post(f"/api/v1/company/invites/{INVITE_ID}/accept", headers=_INVITEE_AUTH)
        finally:
            client.close()
    assert resp.status_code == 200
    assert resp.json() == {
        "company_id": COMPANY_ID,
        "company_name": "Acme Property Co",
        "role": "member",
    }
    assert accept.await_args is not None
    assert accept.await_args.kwargs == {"invite_id": INVITE_ID, "user_id": INVITEE_ID}
    assert sync.call_args.args[2] == COMPANY_ID
    assert any("helixis_activity_log" in sql for sql, _ in captured)


def test_accepting_an_invite_that_isnt_theirs_or_open_is_404(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, captured = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    with (
        patch.object(member_invites, "accept", AsyncMock(return_value=None)),
        patch("helixis.api.routes.company.sync_company_seats") as sync,
    ):
        try:
            resp = client.post(f"/api/v1/company/invites/{INVITE_ID}/accept", headers=_INVITEE_AUTH)
        finally:
            client.close()
    assert resp.status_code == 404
    assert "no longer open" in resp.json()["detail"]
    assert captured == [] and sync.call_count == 0


def test_accepting_while_in_another_company_is_409_naming_it(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, captured = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    refused = AsyncMock(side_effect=member_invites.AcceptRefusedError("Blue Ridge Properties"))
    with patch.object(member_invites, "accept", refused):
        try:
            resp = client.post(f"/api/v1/company/invites/{INVITE_ID}/accept", headers=_INVITEE_AUTH)
        finally:
            client.close()
    assert resp.status_code == 409
    assert "Blue Ridge Properties" in resp.json()["detail"]
    assert captured == []


def test_accept_and_decline_refuse_bad_input_and_strangers(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, _ = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    try:
        for action in ("accept", "decline"):
            assert (
                client.post(f"/api/v1/company/invites/not-a-uuid/{action}", headers=_AUTH)
            ).status_code == 422
            assert (client.post(f"/api/v1/company/invites/{INVITE_ID}/{action}")).status_code in (
                401,
                403,
            )
    finally:
        client.close()


def test_declining_is_the_invitees_own(test_settings: Settings) -> None:  # noqa: F811
    pool, captured = _build_pool(caller_role=None)
    client = _make_client(test_settings, pool=pool)
    with patch.object(member_invites, "decline", AsyncMock(side_effect=[COMPANY_ID, None])) as m:
        try:
            ok = client.post(f"/api/v1/company/invites/{INVITE_ID}/decline", headers=_INVITEE_AUTH)
            gone = client.post(f"/api/v1/company/invites/{INVITE_ID}/decline", headers=_AUTH)
        finally:
            client.close()
    assert ok.status_code == 200 and ok.json() == {"id": INVITE_ID, "status": "declined"}
    assert gone.status_code == 404
    assert [c.kwargs["user_id"] for c in m.await_args_list] == [INVITEE_ID, USER_ID]
    assert _membership_writes(captured) == []


# ── The admin's side ──────────────────────────────────────────────────────────


def test_the_roster_lists_pending_invites_apart_from_members(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, _ = _build_pool(caller_role="manager")
    client = _make_client(test_settings, pool=pool)
    me = Member(
        user_id=USER_ID,
        email="owner@example.com",
        display_name=None,
        role="admin",
        is_primary_admin=True,
    )
    with (
        patch("helixis.api.routes.company.members_repo.list_members", AsyncMock(return_value=[me])),
        patch.object(member_invites, "for_company", AsyncMock(return_value=[_pending()])) as m,
    ):
        try:
            resp = client.get("/api/v1/company/members", headers=_AUTH)
        finally:
            client.close()
    assert resp.status_code == 200
    body = resp.json()
    assert [x["user_id"] for x in body["members"]] == [USER_ID]
    assert body["pending_invites"] == [
        {
            "id": INVITE_ID,
            "email": "victim@example.com",
            "role": "member",
            "invited_at": "2026-11-11T12:00:00+00:00",
            "expires_at": "2026-11-25T12:00:00+00:00",
        }
    ]
    assert m.await_args is not None and m.await_args.kwargs == {"company_id": COMPANY_ID}


def test_an_admin_withdraws_an_invite_and_nobody_else_can(
    test_settings: Settings,  # noqa: F811
) -> None:
    for role, expected in (("admin", 200), ("manager", 403), ("member", 403)):
        pool, _ = _build_pool(caller_role=role)
        client = _make_client(test_settings, pool=pool)
        with patch.object(member_invites, "withdraw", AsyncMock(return_value=INVITEE_ID)) as m:
            try:
                resp = client.delete(f"/api/v1/company/invites/{INVITE_ID}", headers=_AUTH)
            finally:
                client.close()
        assert resp.status_code == expected, role
        if expected == 200:
            assert resp.json() == {"id": INVITE_ID, "status": "withdrawn"}
            assert m.await_args is not None
            assert m.await_args.kwargs == {"invite_id": INVITE_ID, "company_id": COMPANY_ID}
        else:
            assert m.await_count == 0


def test_withdrawing_needs_a_company_of_the_callers_and_a_real_invite(
    test_settings: Settings,  # noqa: F811
) -> None:
    pool, _ = _build_pool(caller_role="admin")
    client = _make_client(test_settings, pool=pool)
    with patch.object(member_invites, "withdraw", AsyncMock(return_value=None)):
        try:
            foreign = client.delete(
                f"/api/v1/company/invites/{INVITE_ID}?company_id={OTHER_COMPANY}", headers=_AUTH
            )
            missing = client.delete(f"/api/v1/company/invites/{INVITE_ID}", headers=_AUTH)
            bad = client.delete("/api/v1/company/invites/nope", headers=_AUTH)
        finally:
            client.close()
    assert foreign.status_code == 403
    assert missing.status_code == 404
    assert bad.status_code == 422
