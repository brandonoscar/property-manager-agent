"""Developer-facing inspection endpoints.

Staff-only debug surfaces. Each route is gated by a
``settings.dev.*`` flag AND the same authentication every other
dashboard route uses. The flags default to ``False`` in production
(see ``config/production.yaml``) so the routes 404 on the public
deploy; they default to ``True`` in development.

Current contents:
  * ``GET /dev/runs``      — last 100 orchestrator runs for the
                             authenticated user's company (PR #141).
  * ``GET /dev/runs/{id}`` — single run detail.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.repositories import orchestrator_runs
from helixis.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["dev"])


def _settings_from(request: Request) -> Settings:
    """Pull settings off the app state. ``Depends`` would also work
    but the rest of this codebase reads from ``request.app.state``."""
    settings: Settings = request.app.state.settings
    return settings


def _require_dev_flag(settings: Settings, flag_value: bool) -> None:
    """Common gate for every route in this module. Raise 404 when the
    flag is off so the route's existence is not leaked to unauth'd
    probes — same shape as a route that doesn't exist."""
    if not flag_value:
        raise HTTPException(status_code=404)


def _primary_company_id(user: AuthenticatedUser) -> str:
    """Pick the company scope for a dev-route lookup. Mirrors the
    helper in ``connectors.py`` rather than importing it (the dev
    routes intentionally don't depend on product routes)."""
    if not user.company_ids:
        raise HTTPException(
            status_code=400,
            detail="User is not a member of any company",
        )
    return user.company_ids[0]


@router.get("/runs")
async def list_runs(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    """Return the last ``limit`` orchestrator runs for the caller's company.

    Always company-scoped — the repository's ``fetch_recent_runs``
    filters by ``company_id``. RLS on ``orchestrator_runs`` is the
    second line of defense for any raw-SQL caller.
    """
    settings = _settings_from(request)
    _require_dev_flag(settings, settings.dev.show_runs_page)
    company_id = _primary_company_id(user)
    pool = get_pool()
    rows = await orchestrator_runs.fetch_recent_runs(pool=pool, company_id=company_id, limit=limit)
    return {"runs": [r.to_dict() for r in rows], "count": len(rows)}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Return a single orchestrator run, or 404 if it doesn't exist or
    belongs to a different company.

    The 404 is deliberately identical to the missing-run case — we
    don't want a cross-tenant request to be distinguishable from a
    bad id by the response code or message.
    """
    settings = _settings_from(request)
    _require_dev_flag(settings, settings.dev.show_runs_page)
    company_id = _primary_company_id(user)
    pool = get_pool()
    row = await orchestrator_runs.fetch_run(pool=pool, company_id=company_id, run_id=run_id)
    if row is None:
        raise HTTPException(status_code=404)
    return row.to_dict()
