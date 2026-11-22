"""Invites that wait for the invitee: ``public.company_member_invites`` (migration 0112).

An admin who invites an address that already has an Occupella account no longer adds
that account to their company. The invite waits here until the person signs in and
accepts or declines it (``routes/company.py``). A row here is not a membership: nothing
that grants access reads this table. The RLS helpers, every backend query, the web
app's company list and the books' sign-in all read ``company_users``, which only
:func:`accept` writes for an existing account.

This module is the only code that reads or writes the table
(``tests/unit/test_invites_need_acceptance.py`` pins that), and :func:`accept` is the
only place an existing account's membership is written.

Before migration 0112 is applied the table is missing. Reads then answer "no
invites"; writes raise :class:`InvitesUnavailableError`, which the route answers with
a 503 so an existing account is never added without its say.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

#: How long an invite waits, in days. The migration's column default says the same;
#: the upsert below restates it so a re-invite starts the clock again.
INVITE_DAYS = 14


class InvitesUnavailableError(RuntimeError):
    """``company_member_invites`` isn't there yet: migration 0112 isn't applied."""


class AcceptRefusedError(RuntimeError):
    """The caller already belongs to another company besides their own sign-up one."""

    def __init__(self, company_name: str) -> None:
        super().__init__(company_name)
        self.company_name = company_name


@dataclass(frozen=True)
class PendingInvite:
    """One invite waiting for its invitee."""

    id: str
    company_id: str
    company_name: str
    user_id: str
    email: str | None
    role: str
    invited_by_email: str | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Accepted:
    """What accepting joined."""

    company_id: str
    company_name: str
    role: str
    invited_by: str | None


def _pending(row: Any) -> PendingInvite:
    return PendingInvite(
        id=str(row["id"]),
        company_id=str(row["company_id"]),
        company_name=str(row["company_name"] or ""),
        user_id=str(row["user_id"]),
        email=row["email"],
        role=str(row["role"]),
        invited_by_email=row["invited_by_email"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


_SELECT_PENDING = """
    SELECT i.id, i.company_id, c.name AS company_name, i.user_id, u.email AS email,
           i.role, inviter.email AS invited_by_email, i.created_at, i.expires_at
      FROM public.company_member_invites i
      JOIN public.companies c ON c.id = i.company_id
      LEFT JOIN auth.users u ON u.id = i.user_id
      LEFT JOIN auth.users inviter ON inviter.id = i.invited_by
"""


async def upsert(
    pool: asyncpg.Pool, *, company_id: str, user_id: str, role: str, invited_by: str
) -> None:
    """Invite ``user_id`` into ``company_id`` as ``role``, or renew the invite waiting.

    ⚠ Writes ``company_member_invites`` and nothing else. The membership is written by
    :func:`accept`, on the invitee's own request, never here.
    """
    try:
        await pool.execute(
            "INSERT INTO public.company_member_invites "
            "(company_id, user_id, role, invited_by, expires_at) "
            "VALUES ($1::uuid, $2::uuid, $3, $4::uuid, now() + make_interval(days => $5)) "
            "ON CONFLICT (company_id, user_id) DO UPDATE SET "
            "role = EXCLUDED.role, invited_by = EXCLUDED.invited_by, "
            "created_at = now(), expires_at = EXCLUDED.expires_at",
            company_id,
            user_id,
            role,
            invited_by,
            INVITE_DAYS,
        )
    except asyncpg.UndefinedTableError as exc:
        raise InvitesUnavailableError("company_member_invites is missing") from exc


async def for_invitee(pool: asyncpg.Pool, *, user_id: str) -> list[PendingInvite]:
    """The open invites waiting for ``user_id``, newest first. Empty before 0112."""
    try:
        rows = await pool.fetch(
            _SELECT_PENDING + " WHERE i.user_id = $1::uuid AND i.expires_at > now()"
            " ORDER BY i.created_at DESC",
            user_id,
        )
    except asyncpg.UndefinedTableError:
        return []
    return [_pending(r) for r in rows]


async def for_company(pool: asyncpg.Pool, *, company_id: str) -> list[PendingInvite]:
    """The open invites ``company_id`` sent, oldest first. Empty before 0112."""
    try:
        rows = await pool.fetch(
            _SELECT_PENDING + " WHERE i.company_id = $1::uuid AND i.expires_at > now()"
            " ORDER BY i.created_at ASC",
            company_id,
        )
    except asyncpg.UndefinedTableError:
        return []
    return [_pending(r) for r in rows]


async def withdraw(pool: asyncpg.Pool, *, invite_id: str, company_id: str) -> str | None:
    """An admin takes back an invite their company sent. The invitee's id, or None."""
    try:
        row = await pool.fetchrow(
            "DELETE FROM public.company_member_invites "
            "WHERE id = $1::uuid AND company_id = $2::uuid RETURNING user_id",
            invite_id,
            company_id,
        )
    except asyncpg.UndefinedTableError:
        return None
    return str(row["user_id"]) if row else None


async def decline(pool: asyncpg.Pool, *, invite_id: str, user_id: str) -> str | None:
    """The invitee turns an invite down. The inviting company's id, or None."""
    try:
        row = await pool.fetchrow(
            "DELETE FROM public.company_member_invites "
            "WHERE id = $1::uuid AND user_id = $2::uuid RETURNING company_id",
            invite_id,
            user_id,
        )
    except asyncpg.UndefinedTableError:
        return None
    return str(row["company_id"]) if row else None


async def accept(pool: asyncpg.Pool, *, invite_id: str, user_id: str) -> Accepted | None:
    """The invitee accepts: the one place an existing account's membership is written.

    In one transaction: take the invite (only the invitee's own, only while it is
    open), refuse if the caller already belongs to a company other than their own
    sign-up one (one account, one company: :class:`AcceptRefusedError`, the invite
    kept), then write the membership. None when there is no such open invite.

    ⚠ The caller's own sign-up company is kept, never dropped. Emptiness in this
    database no longer proves a company is unused: its books live in the books'
    own database, which this one cannot see, and a membership removed here would
    lock its admin out of them. ``_resolve_company_ids`` orders the sign-up company
    last, so the company accepted becomes the active one, and leaving it returns
    them to their own.
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            invite = await conn.fetchrow(
                "DELETE FROM public.company_member_invites "
                "WHERE id = $1::uuid AND user_id = $2::uuid AND expires_at > now() "
                "RETURNING company_id, role, invited_by",
                invite_id,
                user_id,
            )
            if invite is None:
                return None
            company_id = str(invite["company_id"])
            other = await conn.fetchrow(
                "SELECT c.name FROM public.company_users cu "
                "JOIN public.companies c ON c.id = cu.company_id "
                "WHERE cu.user_id = $1::uuid AND cu.company_id <> $1::uuid "
                "AND cu.company_id <> $2::uuid "
                "ORDER BY cu.created_at ASC LIMIT 1",
                user_id,
                company_id,
            )
            if other is not None:
                # Raising rolls the transaction back, so the invite stays open.
                raise AcceptRefusedError(str(other["name"] or ""))
            await conn.execute(
                "INSERT INTO public.company_users (user_id, company_id, role) "
                "VALUES ($1::uuid, $2::uuid, $3) "
                "ON CONFLICT (company_id, user_id) DO NOTHING",
                user_id,
                company_id,
                str(invite["role"]),
            )
            name = await conn.fetchval(
                "SELECT name FROM public.companies WHERE id = $1::uuid", company_id
            )
    except asyncpg.UndefinedTableError:
        return None
    return Accepted(
        company_id=company_id,
        company_name=str(name or ""),
        role=str(invite["role"]),
        invited_by=str(invite["invited_by"]) if invite["invited_by"] else None,
    )


__all__ = [
    "INVITE_DAYS",
    "AcceptRefusedError",
    "Accepted",
    "InvitesUnavailableError",
    "PendingInvite",
    "accept",
    "decline",
    "for_company",
    "for_invitee",
    "upsert",
    "withdraw",
]
