"""Properties × Roles routing-grid API (PR #81).

Endpoints:

- ``GET /api/v1/properties`` — paginated property list with the
  per-property assignments grid pre-joined. The default ``(no
  property)`` row is included in the response under ``default_row``.
- ``GET /api/v1/properties/staff`` — active Buildium staff (the
  dropdown options).
- ``PUT /api/v1/properties/{property_id}/assignment/{category}`` —
  set one cell for one property. Body: ``{"buildium_user_id": int}``
  or ``{"buildium_user_id": null}`` to clear (falls back to admin).
- ``PUT /api/v1/properties/default-assignment/{category}`` — same
  for the (no property) default row.
- ``POST /api/v1/properties/sync`` — pull ``/v1/users`` + refresh
  the staff cache. The Properties page's "Re-sync from Buildium"
  button.

All routes are gated by ``get_current_user``; the active company is
the requester's first ``company_users`` membership (single-company
v1 — same pattern as the Buildium routes).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.roles import MANAGER
from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.sync import sync_buildium_staff
from helixis.common.support import contact_support
from helixis.db.engine import get_pool
from helixis.db.pms_coverage import Coverage, coverage_for
from helixis.db.repositories import cockpit
from helixis.db.repositories.buildium_staff import list_active_staff
from helixis.db.repositories.property_assignments import (
    delete_assignment,
    list_assignments,
    upsert_assignment,
)
from helixis.pms import registry, sync_cycle
from helixis.pms.registry import BUILDIUM
from helixis.webhooks.categorization import CATEGORIES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/properties", tags=["properties"])


# ── Request / response shapes ──────────────────────────────────────


class StaffOption(BaseModel):
    """One dropdown option in the Properties grid."""

    buildium_user_id: int
    display_name: str
    email: str | None = None
    role: str | None = None


class CellAssignment(BaseModel):
    """The contents of one (property × category) cell."""

    action_type: str
    buildium_user_id: int | None = None


class PropertyRow(BaseModel):
    """One row in the grid — a Buildium property + its 5 cells."""

    buildium_property_id: int
    name: str
    address: str | None = None
    assignments: dict[str, CellAssignment | None]


class DefaultRow(BaseModel):
    """The (no property) default row at the top of the grid."""

    assignments: dict[str, CellAssignment | None]


class PropertiesGridResponse(BaseModel):
    """``GET /properties`` response."""

    default_row: DefaultRow
    properties: list[PropertyRow]
    page: int
    page_size: int
    total: int


class StaffListResponse(BaseModel):
    options: list[StaffOption]


class SetAssignmentRequest(BaseModel):
    """PUT body for setting one cell.

    ``buildium_user_id=null`` (or the field absent) clears the cell —
    the cell is deleted, the router falls back to the default row /
    admin chain.
    """

    buildium_user_id: int | None = None


class SyncResponse(BaseModel):
    ok: bool
    upserted: int = 0
    deactivated: int = 0
    error: str | None = None


class AssignmentMutationResponse(BaseModel):
    ok: bool
    cleared: bool = False
    assignment: CellAssignment | None = None


# ── Helpers ────────────────────────────────────────────────────────


def _require_company(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company associated with this account.",
        )
    return user.company_ids[0]


def _empty_cells() -> dict[str, CellAssignment | None]:
    return {cat: None for cat in CATEGORIES}


def _to_cell(action_type: str, action_config: dict[str, Any]) -> CellAssignment:
    """Project a ``property_assignments`` row's action_config into the
    grid cell shape. v1 only knows ``assign_to_user``; future
    action_types serialize whatever sub-shape the Properties UI's
    CellConfigurator needs."""
    if action_type == "assign_to_user":
        bid = action_config.get("buildium_user_id")
        if isinstance(bid, int):
            return CellAssignment(action_type=action_type, buildium_user_id=bid)
        return CellAssignment(action_type=action_type, buildium_user_id=None)
    return CellAssignment(action_type=action_type, buildium_user_id=None)


def _validate_category(category: str) -> str:
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown category '{category}'. Valid: {', '.join(CATEGORIES)}.",
        )
    return category


async def _fetch_properties_from_buildium(
    company_id: str,
    *,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Pull the property list from Buildium for the grid.

    Returns ``(slice_for_page, total_count)``. v1 reads live on every
    render — small N, cheap call — so there's no properties cache
    table to maintain. PR #82's cron can warm a cache; this function
    is the seam.
    """
    settings = await load_buildium_settings_for_company(company_id)
    if settings is None:
        return [], 0

    async with BuildiumClient(settings) as client:
        api = BuildiumApi(client)
        try:
            properties = await api.rentals.list_properties()
        except BuildiumApiError as exc:
            logger.warning(
                "Couldn't pull properties for grid (company=%s): %s",
                company_id,
                exc,
            )
            return [], 0

    total = len(properties)
    start = (page - 1) * page_size
    sliced = properties[start : start + page_size]

    formatted: list[dict[str, Any]] = []
    for p in sliced:
        addr = getattr(p, "address", None)
        addr_str: str | None = None
        if addr is not None:
            bits = [
                getattr(addr, "address_line1", None),
                getattr(addr, "city", None),
                getattr(addr, "state", None),
            ]
            addr_str = ", ".join(b for b in bits if b) or None
        formatted.append(
            {
                "buildium_property_id": p.id,
                "name": getattr(p, "name", None) or "Unnamed property",
                "address": addr_str,
            }
        )
    return formatted, total


# ── Routes ─────────────────────────────────────────────────────────


@router.get("", response_model=PropertiesGridResponse)
async def list_grid(
    user: AuthenticatedUser = Depends(get_current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=200),
) -> PropertiesGridResponse:
    """Render the Properties × Roles grid in one shot.

    Joins the live Buildium property list (paginated) with the
    persisted ``property_assignments`` rows for this company so the
    UI doesn't have to per-cell fetch.
    """
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()

    properties, total = await _fetch_properties_from_buildium(
        company_id, page=page, page_size=page_size
    )
    assignments = await list_assignments(pool, company_id=company_id)

    # Index assignments by (property_id_or_None, category) for O(1)
    # cell lookup.
    by_key: dict[tuple[int | None, str], CellAssignment] = {}
    for a in assignments:
        by_key[(a.buildium_property_id, a.category)] = _to_cell(a.action_type, a.action_config)

    default_cells = _empty_cells()
    for cat in CATEGORIES:
        default_cells[cat] = by_key.get((None, cat))
    default_row = DefaultRow(assignments=default_cells)

    property_rows: list[PropertyRow] = []
    for p in properties:
        cells = _empty_cells()
        for cat in CATEGORIES:
            cells[cat] = by_key.get((p["buildium_property_id"], cat))
        property_rows.append(
            PropertyRow(
                buildium_property_id=p["buildium_property_id"],
                name=p["name"],
                address=p["address"],
                assignments=cells,
            )
        )

    return PropertiesGridResponse(
        default_row=default_row,
        properties=property_rows,
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/staff", response_model=StaffListResponse)
async def list_staff_options(
    user: AuthenticatedUser = Depends(get_current_user),
) -> StaffListResponse:
    """List active Buildium staff for the dropdown options.

    Reads from the ``buildium_staff`` cache (populated by
    ``POST /properties/sync``). If the cache is empty (first visit,
    no sync yet) returns an empty list — the UI prompts the user to
    click "Re-sync from Buildium".
    """
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    staff = await list_active_staff(pool, company_id=company_id)
    return StaffListResponse(
        options=[
            StaffOption(
                buildium_user_id=row.buildium_user_id,
                display_name=row.display_name,
                email=row.email,
                role=row.user_role_name,
            )
            for row in staff
        ]
    )


async def _set_or_clear_cell(
    *,
    company_id: str,
    user_id: str,
    buildium_property_id: int | None,
    category: str,
    body: SetAssignmentRequest,
) -> AssignmentMutationResponse:
    """Shared implementation for the two PUT cell endpoints."""
    _validate_category(category)
    pool: asyncpg.Pool = get_pool()

    # Role gate (ADR 0005): the routing grid decides WHO on the team gets
    # auto-assigned each category of incoming work — an operational settings
    # surface, not a per-user preference. Without this, any member could
    # silently reroute the whole team's work to themselves or away from a
    # colleague. Manager-or-admin only, matching the CRM send floor. Deferred
    # import to avoid a routes↔routes import cycle (same pattern as crm.py).
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user_id,
        company_id,
        MANAGER,
        detail="Only managers and admins can change work routing.",
    )

    if body.buildium_user_id is None:
        cleared = await delete_assignment(
            pool,
            company_id=company_id,
            buildium_property_id=buildium_property_id,
            category=category,
        )
        return AssignmentMutationResponse(ok=True, cleared=cleared, assignment=None)

    saved = await upsert_assignment(
        pool,
        company_id=company_id,
        buildium_property_id=buildium_property_id,
        category=category,
        action_type="assign_to_user",
        action_config={"buildium_user_id": body.buildium_user_id},
        updated_by=user_id,
    )
    return AssignmentMutationResponse(
        ok=True,
        cleared=False,
        assignment=_to_cell(saved.action_type, saved.action_config),
    )


@router.put(
    "/{property_id}/assignment/{category}",
    response_model=AssignmentMutationResponse,
)
async def set_assignment(
    property_id: int,
    category: str,
    body: SetAssignmentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AssignmentMutationResponse:
    """Set one (property × category) cell."""
    company_id = _require_company(user)
    return await _set_or_clear_cell(
        company_id=company_id,
        user_id=user.user_id,
        buildium_property_id=property_id,
        category=category,
        body=body,
    )


@router.put(
    "/default-assignment/{category}",
    response_model=AssignmentMutationResponse,
)
async def set_default_assignment(
    category: str,
    body: SetAssignmentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AssignmentMutationResponse:
    """Set one cell of the (no property) default row."""
    company_id = _require_company(user)
    return await _set_or_clear_cell(
        company_id=company_id,
        user_id=user.user_id,
        buildium_property_id=None,
        category=category,
        body=body,
    )


class _SyncRequest(BaseModel):
    """``POST /properties/sync`` request body. v1 has no fields — the
    button is fire-and-forget — but we accept a body so the route
    has a stable shape for PR #82's cron-trigger metadata."""

    triggered_by: str | None = Field(default=None, max_length=64)


@router.post("/sync", response_model=SyncResponse)
async def sync_now(
    _body: _SyncRequest | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SyncResponse:
    """Trigger an on-demand Buildium staff sync.

    Returns counts (or a friendly error) the UI can show in a toast.
    """
    company_id = _require_company(user)
    result = await sync_buildium_staff(company_id)
    return SyncResponse(
        ok=result.ok,
        upserted=result.upserted,
        deactivated=result.deactivated,
        error=result.error,
    )


# ── Property cockpit (Workstream A, master #5) ─────────────────────
# Reads the 0038 entity mirror — no live Buildium calls on this path.
#
# ⚠ A ``null`` field means "this company's PMS doesn't send it" (Rentvine sends
# no balances, ledger, tasks or lease-tenant links), never zero or empty.
# ``pms_name`` names the system so the page can say where to check.
# ``db/repositories/cockpit.py`` decides; these models only carry it.


class CockpitSummary(BaseModel):
    """One property row on the cockpit list."""

    buildium_id: int
    name: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    unit_count: int = 0
    occupied_count: int = 0
    #: Units neither occupied nor known vacant (``PropertySummary``). The web
    #: app shows them on their own and never counts them as vacant.
    occupancy_unknown_count: int = 0
    open_work_orders: int = 0
    expiring_leases_90d: int = 0
    delinquent_leases: int | None = 0
    delinquent_total: float | None = 0.0


class CockpitListResponse(BaseModel):
    properties: list[CockpitSummary]
    pms_name: str | None = None


class CockpitDetailResponse(BaseModel):
    """The starter-six picture for one property."""

    summary: CockpitSummary
    rent_scheduled: float | None = 0.0
    rent_unknown_leases: int = 0
    rent_collected_mtd: float | None = 0.0
    tenants: list[dict[str, Any]] | None = Field(default_factory=list)
    open_work_order_list: list[dict[str, Any]] = Field(default_factory=list)
    expiring_lease_list: list[dict[str, Any]] = Field(default_factory=list)
    delinquency_list: list[dict[str, Any]] | None = Field(default_factory=list)
    pms_name: str | None = None


class UnitDetailResponse(BaseModel):
    """One unit's operational picture (``GET /cockpit/units/{id}``)."""

    unit: dict[str, Any]
    current_lease: dict[str, Any] | None = None
    tenants: list[dict[str, Any]] | None = Field(default_factory=list)
    balance: dict[str, Any] | None = None
    #: ``False``: a null ``balance`` is unknown, not "owes nothing".
    balances_sent: bool = True
    open_work_orders: list[dict[str, Any]] = Field(default_factory=list)
    recent_work_orders: list[dict[str, Any]] = Field(default_factory=list)
    open_tasks: list[dict[str, Any]] | None = Field(default_factory=list)
    pms_name: str | None = None


class TenantDetailResponse(BaseModel):
    """One tenant's picture (``GET /cockpit/tenants/{id}``)."""

    tenant: dict[str, Any]
    leases: list[dict[str, Any]] | None = Field(default_factory=list)
    recent_transactions: list[dict[str, Any]] | None = Field(default_factory=list)
    open_work_orders: list[dict[str, Any]] | None = Field(default_factory=list)
    #: ``False``: each lease's null ``total_balance`` is unknown, not zero.
    balances_sent: bool = True
    pms_name: str | None = None


def _summary_out(s: cockpit.PropertySummary) -> CockpitSummary:
    return CockpitSummary(
        buildium_id=s.buildium_id,
        name=s.name,
        address_line1=s.address_line1,
        city=s.city,
        state=s.state,
        unit_count=s.unit_count,
        occupied_count=s.occupied_count,
        occupancy_unknown_count=s.occupancy_unknown_count,
        open_work_orders=s.open_work_orders,
        expiring_leases_90d=s.expiring_leases_90d,
        delinquent_leases=s.delinquent_leases,
        delinquent_total=s.delinquent_total,
    )


@router.get("/cockpit", response_model=CockpitListResponse)
async def cockpit_list(
    user: AuthenticatedUser = Depends(get_current_user),
) -> CockpitListResponse:
    """Every mirrored property with its metric rollups. Empty until a
    backfill (``POST /buildium/backfill``) has populated the mirror."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    summaries = await cockpit.list_property_summaries(pool, company_id)
    cov = await coverage_for(pool, company_id)
    return CockpitListResponse(properties=[_summary_out(s) for s in summaries], pms_name=cov.name)


def not_synced_yet(record: str, cov: Coverage) -> str:
    """The 404 for a record Occupella's copy does not hold yet, naming the
    sync button that system's card actually has (rule 16): Buildium's says
    "Sync data", every other system's "Sync now" (``frontend/src/components/
    connectors/``). The cockpit panes show this sentence as it is.

    ⚠ It said "run a Buildium backfill" to every workspace, and the panes
    replaced it with "Run a Buildium backfill from Settings → Connectors": a
    Rentvine company was sent to a system it does not use.
    """
    if cov.name is None:
        return (
            f"This {record} isn't in Occupella's copy of your property management data "
            "yet. An admin can sync it from Settings → Connectors; then reload."
        )
    if cov.pms in sync_cycle.systems() and (
        registry.connector_attr(cov.pms, "sync", "sync_company") is None
    ):
        # A connector built before its sync (as DoorLoop was in slice A0): its card has
        # no sync button to name, and nothing will fill the copy until it does.
        return (
            f"This {record} isn't in Occupella's copy of your {cov.name} data: Occupella "
            f"doesn't sync {cov.name} yet. To hear when it will, " + contact_support() + "."
        )
    button = "Sync data" if cov.pms == BUILDIUM else "Sync now"
    return (
        f"This {record} isn't in Occupella's copy of your {cov.name} data yet. An admin "
        f"can run {button} on the {cov.name} card in Settings → Connectors; then reload."
    )


@router.get("/cockpit/units/{unit_buildium_id}", response_model=UnitDetailResponse)
async def cockpit_unit_detail(
    unit_buildium_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UnitDetailResponse:
    """One unit's detail: lease, tenants, balance, maintenance, tasks."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    detail = await cockpit.get_unit_detail(pool, company_id, unit_buildium_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_synced_yet("unit", await coverage_for(pool, company_id)),
        )
    return UnitDetailResponse(
        unit=detail.unit,
        current_lease=detail.current_lease,
        tenants=detail.tenants,
        balance=detail.balance,
        balances_sent=detail.balances_sent,
        open_work_orders=detail.open_work_orders,
        recent_work_orders=detail.recent_work_orders,
        open_tasks=detail.open_tasks,
        pms_name=detail.pms_name,
    )


@router.get("/cockpit/tenants/{tenant_buildium_id}", response_model=TenantDetailResponse)
async def cockpit_tenant_detail(
    tenant_buildium_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TenantDetailResponse:
    """One tenant's detail: leases, payments, open maintenance."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    detail = await cockpit.get_tenant_detail(pool, company_id, tenant_buildium_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_synced_yet("tenant", await coverage_for(pool, company_id)),
        )
    return TenantDetailResponse(
        tenant=detail.tenant,
        leases=detail.leases,
        recent_transactions=detail.recent_transactions,
        open_work_orders=detail.open_work_orders,
        balances_sent=detail.balances_sent,
        pms_name=detail.pms_name,
    )


@router.get("/cockpit/{buildium_property_id}", response_model=CockpitDetailResponse)
async def cockpit_detail(
    buildium_property_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CockpitDetailResponse:
    """The starter-six detail for one property."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    detail = await cockpit.get_property_detail(pool, company_id, buildium_property_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_synced_yet("property", await coverage_for(pool, company_id)),
        )
    return CockpitDetailResponse(
        summary=_summary_out(detail.summary),
        rent_scheduled=detail.rent_scheduled,
        rent_unknown_leases=detail.rent_unknown_leases,
        rent_collected_mtd=detail.rent_collected_mtd,
        tenants=detail.tenants,
        open_work_order_list=detail.open_work_order_list,
        expiring_lease_list=detail.expiring_lease_list,
        delinquency_list=detail.delinquency_list,
        pms_name=detail.pms_name,
    )


__all__ = ["router"]
