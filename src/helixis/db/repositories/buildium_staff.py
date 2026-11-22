"""Repository for the ``buildium_staff`` cache (PR #81).

Wraps the two operations the Properties API needs:

1. ``list_staff(company_id)`` — read-side, returns active staff for
   dropdown options.
2. ``upsert_staff(...)`` — write-side, called by the sync job after
   pulling ``/v1/users`` from Buildium. Idempotent on the unique
   ``(company_id, buildium_user_id)`` pair so multiple runs converge.

The sync flow itself lives in ``helixis.buildium.sync.staff`` — this
module is just the SQL layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildiumStaffRow:
    """One row from ``buildium_staff``."""

    buildium_user_id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    user_types: list[str]
    user_role_id: int | None
    user_role_name: str | None
    is_active: bool

    @property
    def display_name(self) -> str:
        """Best human label — name if present, else email, else
        ``"Unnamed user"`` as a last resort (never the raw id, per
        PR #75 HARD PRESENTATION RULES)."""
        full = f"{self.first_name or ''} {self.last_name or ''}".strip()
        if full:
            return full
        if self.email:
            return self.email
        return "Unnamed user"


async def list_active_staff(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    only_routing_eligible: bool = True,
) -> list[BuildiumStaffRow]:
    """Return active Buildium staff for the company.

    ``only_routing_eligible=True`` filters to users whose
    ``user_types`` contains ``"Staff"`` — these are the only users
    that should appear in the Properties × Roles dropdowns or the
    Inbox reassign list. Tenant-Center / Rental-Owner portal users
    are excluded by design.
    """
    try:
        if only_routing_eligible:
            rows = await pool.fetch(
                """
                SELECT buildium_user_id,
                       first_name, last_name, email,
                       user_types,
                       user_role_id, user_role_name,
                       is_active
                  FROM public.buildium_staff
                 WHERE company_id = $1::uuid
                   AND is_active = true
                   AND 'Staff' = ANY (user_types)
                 ORDER BY last_name NULLS LAST, first_name NULLS LAST, email
                """,
                company_id,
            )
        else:
            rows = await pool.fetch(
                """
                SELECT buildium_user_id,
                       first_name, last_name, email,
                       user_types,
                       user_role_id, user_role_name,
                       is_active
                  FROM public.buildium_staff
                 WHERE company_id = $1::uuid
                 ORDER BY last_name NULLS LAST, first_name NULLS LAST, email
                """,
                company_id,
            )
    except asyncpg.exceptions.UndefinedTableError:
        # Migration 0015 hasn't landed yet — return empty so the
        # Properties page renders cleanly during the migration window.
        logger.warning("buildium_staff table not provisioned; apply migration 0015.")
        return []

    return [
        BuildiumStaffRow(
            buildium_user_id=row["buildium_user_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            email=row["email"],
            user_types=list(row["user_types"] or []),
            user_role_id=row["user_role_id"],
            user_role_name=row["user_role_name"],
            is_active=row["is_active"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class StaffSyncSummary:
    """Counts returned by ``upsert_staff`` so the sync route can
    report ``"synced 23 staff (3 deactivated)"`` to the property
    manager."""

    upserted: int
    deactivated: int


async def upsert_staff(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    rows: list[dict[str, object]],
) -> StaffSyncSummary:
    """Upsert every row in ``rows`` and soft-delete missing ones.

    Each entry in ``rows`` must carry at least ``buildium_user_id``;
    the other fields default to ``NULL`` / ``[]`` if not present. A
    user who was in the cache before but isn't in this sync's
    ``rows`` is flipped to ``is_active=false`` (NOT deleted) so
    historical ``property_assignments`` rows still resolve a name.

    Returns the count of upserts + deactivations for the
    "Re-sync from Buildium" toast.
    """
    if not rows:
        return StaffSyncSummary(upserted=0, deactivated=0)

    upserted = 0
    seen_ids: list[int] = []
    for row in rows:
        bid = row.get("buildium_user_id")
        if not isinstance(bid, int):
            continue
        seen_ids.append(bid)
        await pool.execute(
            """
            INSERT INTO public.buildium_staff (
                company_id, buildium_user_id,
                first_name, last_name, email,
                user_types, user_role_id, user_role_name,
                is_active, last_synced_at
            ) VALUES (
                $1::uuid, $2,
                $3, $4, $5,
                $6, $7, $8,
                $9, now()
            )
            ON CONFLICT (company_id, buildium_user_id) DO UPDATE SET
                first_name = EXCLUDED.first_name,
                last_name  = EXCLUDED.last_name,
                email      = EXCLUDED.email,
                user_types = EXCLUDED.user_types,
                user_role_id   = EXCLUDED.user_role_id,
                user_role_name = EXCLUDED.user_role_name,
                is_active  = EXCLUDED.is_active,
                last_synced_at = now()
            """,
            company_id,
            bid,
            row.get("first_name"),
            row.get("last_name"),
            row.get("email"),
            list(cast("list[str]", row.get("user_types") or [])),
            row.get("user_role_id"),
            row.get("user_role_name"),
            bool(row.get("is_active", True)),
        )
        upserted += 1

    # Soft-delete users that disappeared from the latest sync window.
    # ``seen_ids`` is the new ground truth; anyone NOT in it is now
    # inactive on Buildium's side (or we lost permission to see
    # them — either way they shouldn't appear in dropdowns).
    deactivated = 0
    if seen_ids:
        result = await pool.execute(
            """
            UPDATE public.buildium_staff
               SET is_active = false,
                   last_synced_at = now()
             WHERE company_id = $1::uuid
               AND buildium_user_id <> ALL ($2::int[])
               AND is_active = true
            """,
            company_id,
            seen_ids,
        )
        # asyncpg's execute returns "UPDATE N" — pull the count off
        # the tail of the string.
        try:
            deactivated = int(result.split(" ")[-1])
        except (ValueError, IndexError):
            deactivated = 0

    return StaffSyncSummary(upserted=upserted, deactivated=deactivated)


__all__ = [
    "BuildiumStaffRow",
    "StaffSyncSummary",
    "list_active_staff",
    "load_staff_cache",
    "upsert_staff",
]


async def load_staff_cache(
    pool: asyncpg.Pool,
    company_id: str,
) -> dict[int, BuildiumStaffRow]:
    """Return ``{buildium_user_id: BuildiumStaffRow}`` for the company.

    PR #82 — thin wrapper over ``list_active_staff`` that re-keys the
    list by Buildium user id so the webhook receiver's surface
    builder can resolve assignees in O(1) without re-querying. Empty
    dict when the sync has never run; the surface builder degrades
    to ``"the admin"`` text gracefully.

    Includes BOTH active and inactive (soft-deleted) staff because
    a historical Inbox event may carry an assignee who was on
    Buildium when the event landed but has since been deactivated;
    we still want the display name. The Properties grid uses the
    routing-eligible default (active only) — this helper is the
    superset.
    """
    rows = await list_active_staff(
        pool,
        company_id=company_id,
        only_routing_eligible=False,
    )
    return {row.buildium_user_id: row for row in rows}
