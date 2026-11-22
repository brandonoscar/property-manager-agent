"""Repository for the team roster — ``company_users`` + member identities.

Backs the Team & Permissions settings screen (ADR 0005): list the members of a
company with their reconciled role, change a member's role, and remove a member.
The role vocabulary + normalization live in :mod:`helixis.api.roles`; this module
speaks to Postgres and returns already-normalized roles so callers never see a
legacy 'owner'/'employee' string.

Email/display-name come from ``auth.users`` (the service-key connection can read
it — the same connection ``api.auth._resolve_company_ids`` uses). "Primary
admin" is not a stored column: it is computed as the earliest-joined admin
(``company_users.created_at``), which is the account that bootstrapped the
company via ``handle_new_user``. That member cannot be demoted/removed below the
last-admin guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from helixis.api.roles import ADMIN, normalize_role

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Member:
    """One member of a company, as the roster UI shows them."""

    user_id: str
    email: str | None
    display_name: str | None
    role: str  # already normalized to admin/manager/member
    is_primary_admin: bool
    #: Invited, and has never signed in. ⚠ The membership row is created UP
    #: FRONT at invite time (``routes/company._invite_one``), so somebody who
    #: never opened the email is an ordinary-looking row on the team screen —
    #: indistinguishable from a colleague who set up their account weeks ago.
    #: The admin cannot answer "did Sam ever get in?" from the product, and a
    #: seat is billed from the invite either way.
    invite_pending: bool = False


async def list_members(pool: asyncpg.Pool, *, company_id: str) -> list[Member]:
    """Every member of ``company_id``, admins first then by join order.

    Roles are normalized on the way out. ``is_primary_admin`` marks the
    earliest-joined admin (the bootstrap account) — exactly one per company as
    long as any admin exists.
    """
    rows = await pool.fetch(
        """
        SELECT cu.user_id::text AS user_id,
               u.email           AS email,
               u.raw_user_meta_data ->> 'full_name' AS display_name,
               cu.role           AS role,
               cu.created_at     AS created_at,
               -- ⚠ BOTH columns, not either one. `invited_at` alone stays set
               -- forever after somebody accepts, so it marks long-standing
               -- colleagues as pending; `last_sign_in_at IS NULL` alone would
               -- also catch an account created some other way. The pair means
               -- exactly "was sent an invitation and has never used it".
               (u.invited_at IS NOT NULL AND u.last_sign_in_at IS NULL)
                                 AS invite_pending
        FROM public.company_users cu
        LEFT JOIN auth.users u ON u.id = cu.user_id
        WHERE cu.company_id = $1::uuid
        ORDER BY cu.created_at ASC, cu.user_id ASC
        """,
        company_id,
    )

    # The primary admin is the first row (created_at ASC) whose normalized role
    # is admin — computed here rather than stored, so no is_primary column drifts.
    primary_admin_id: str | None = None
    for row in rows:
        if normalize_role(row["role"]) == ADMIN:
            primary_admin_id = row["user_id"]
            break

    members = [
        Member(
            user_id=row["user_id"],
            email=row["email"],
            display_name=row["display_name"],
            role=normalize_role(row["role"]),
            is_primary_admin=row["user_id"] == primary_admin_id,
            # A missing auth.users row (LEFT JOIN) reads as NOT pending: an
            # absent identity is a different problem, and labelling it
            # "invitation pending" would send an admin to resend an email to
            # somebody who has no address on file.
            invite_pending=bool(row["invite_pending"]),
        )
        for row in rows
    ]
    # Admins first, then managers, then members; stable within a rank by join order.
    rank = {ADMIN: 0, "manager": 1, "member": 2}
    members.sort(key=lambda m: rank.get(m.role, 3))
    return members


async def get_member_role(pool: asyncpg.Pool, *, company_id: str, user_id: str) -> str | None:
    """The normalized role of ``user_id`` in ``company_id``, or ``None`` if not
    a member."""
    row = await pool.fetchrow(
        "SELECT role FROM public.company_users WHERE company_id = $1::uuid AND user_id = $2::uuid",
        company_id,
        user_id,
    )
    return normalize_role(row["role"]) if row else None


async def count_admins(pool: asyncpg.Pool, *, company_id: str) -> int:
    """How many admins the company has (legacy 'owner' counts as admin).

    Used by the last-admin guard: a company must never be left with zero admins,
    so a demote/remove that would drop this to 0 is rejected.
    """
    rows = await pool.fetch(
        "SELECT role FROM public.company_users WHERE company_id = $1::uuid",
        company_id,
    )
    return sum(1 for row in rows if normalize_role(row["role"]) == ADMIN)


async def set_member_role(pool: asyncpg.Pool, *, company_id: str, user_id: str, role: str) -> bool:
    """Set ``user_id``'s role in ``company_id``. Returns False if not a member.

    ``role`` must already be a canonical value (admin/manager/member) — the route
    validates before calling. Writes the canonical string so the row stops being
    a legacy alias.
    """
    result = await pool.execute(
        "UPDATE public.company_users SET role = $3 "
        "WHERE company_id = $1::uuid AND user_id = $2::uuid",
        company_id,
        user_id,
        role,
    )
    return bool(result == "UPDATE 1")


async def remove_member(pool: asyncpg.Pool, *, company_id: str, user_id: str) -> bool:
    """Remove ``user_id`` from ``company_id``. Returns False if not a member.

    Only removes the membership row — it does not delete the user's account or
    their personal company (the user-as-company model keeps those).
    """
    result = await pool.execute(
        "DELETE FROM public.company_users WHERE company_id = $1::uuid AND user_id = $2::uuid",
        company_id,
        user_id,
    )
    return bool(result == "DELETE 1")


__all__ = [
    "Member",
    "list_members",
    "get_member_role",
    "count_admins",
    "set_member_role",
    "remove_member",
]
