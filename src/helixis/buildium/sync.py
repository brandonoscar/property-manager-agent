"""Buildium staff + properties sync (PR #81).

Two callable entry points:

- ``sync_buildium_staff(company_id)`` — pull ``/v1/users`` (filtered
  to active Staff) and upsert into ``buildium_staff``. Used by the
  on-demand "Re-sync from Buildium" button on the Properties page.
- ``sync_buildium_properties(company_id)`` — pull ``/v1/rentals`` to
  warm whatever properties cache the Properties page reads from. v1
  reads properties live from the API on every render of the
  Properties page (small N, cheap call), so this is a no-op for now;
  the function exists so PR #82 can flip it on without touching the
  routes.

Both functions return a small ``SyncResult`` so the route can echo
counts back to the property manager. Errors from Buildium are
caught + sanitized via ``_sanitize_buildium_error`` (PR #76) so the
toast never leaks raw API jargon.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories.buildium_staff import upsert_staff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaffSyncResult:
    """Outcome of a staff sync run — surfaced to the UI toast."""

    ok: bool
    upserted: int
    deactivated: int
    error: str | None = None


async def sync_buildium_staff(company_id: str) -> StaffSyncResult:
    """Pull active Staff users from ``/v1/users`` and refresh the cache.

    Filters server-side: ``usertypes=Staff`` + ``status=Active`` so
    we only carry routing-eligible users. Pagination is handled
    inside ``Administration.list_users`` via ``client.list_all``.

    Permission failures (the customer's Buildium API key doesn't
    have ``Administration > Users — View``) are reported back as a
    friendly error string the Properties page can display in the
    toast — we DO NOT raise, because a missing permission isn't a
    code bug.
    """
    if not company_id:
        return StaffSyncResult(ok=False, upserted=0, deactivated=0, error="No company")

    settings = await load_buildium_settings_for_company(company_id)
    if settings is None:
        return StaffSyncResult(
            ok=False,
            upserted=0,
            deactivated=0,
            error="Buildium is not connected for this company.",
        )

    try:
        async with BuildiumClient(settings, company_id=company_id) as client:
            api = BuildiumApi(client)
            users = await api.administration.list_users(
                usertypes=["Staff"],
                status="Active",
            )
    except BuildiumApiError as exc:
        # 403 is the headline error we expect — the API key doesn't
        # carry the ``Administration > Users — View`` scope. Surface
        # a permission-specific message so the property manager
        # knows which Buildium toggle to flip.
        if exc.status in (401, 403):
            error = (
                "Buildium denied access to the staff list. Make sure the "
                "Buildium API key has the 'Administration > Users — View' "
                "permission enabled."
            )
        else:
            error = "Couldn't reach Buildium to sync staff. (logged for review)"
        logger.warning(
            "Buildium staff sync failed for company=%s status=%s: %s",
            company_id,
            exc.status,
            exc,
        )
        return StaffSyncResult(ok=False, upserted=0, deactivated=0, error=error)

    rows: list[dict[str, object]] = []
    for u in users:
        rows.append(
            {
                "buildium_user_id": u.id,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "user_types": list(u.user_types or []),
                "user_role_id": u.user_role.id if u.user_role else None,
                "user_role_name": u.user_role.name if u.user_role else None,
                "is_active": bool(u.is_active) if u.is_active is not None else True,
            }
        )

    pool = get_pool()
    summary = await upsert_staff(pool, company_id=company_id, rows=rows)
    logger.info(
        "Buildium staff sync ok company=%s upserted=%s deactivated=%s",
        company_id,
        summary.upserted,
        summary.deactivated,
    )
    return StaffSyncResult(
        ok=True,
        upserted=summary.upserted,
        deactivated=summary.deactivated,
    )


__all__ = ["StaffSyncResult", "sync_buildium_staff"]
