"""Operations page API — the tenants / vendors / team tabs.

Three JWT-authed, company-scoped list endpoints, each a single mirror
query (``db/repositories/operations.py``). The Properties tab reuses
``GET /properties/cockpit`` — no duplicate surface here.

Everything is read-only: the Operations page compares and ranks; acting
on what it shows stays with the agent (confirm-gated) and the cockpit.

⚠ ``null`` means "this company's PMS doesn't send it", never zero or empty
(``db/repositories/operations.py``). ``pms_name`` names the system.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.pms_coverage import coverage_for
from helixis.db.repositories import operations as ops_repo

router = APIRouter(prefix="/operations", tags=["operations"])


def _require_company(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company associated with this account.",
        )
    return user.company_ids[0]


class VendorOpsOut(BaseModel):
    buildium_id: int
    name: str | None = None
    category_name: str | None = None
    is_active: bool | None = None
    phone: str | None = None
    email: str | None = None
    open_work_orders: int | None = 0
    total_work_orders: int | None = 0
    jobs_90d: int | None = 0
    avg_days_to_complete: float | None = None
    spend_12m: float | None = 0.0
    bill_count_12m: int | None = 0
    last_bill_date: date | None = None


class TenantOpsOut(BaseModel):
    tenant_buildium_id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    property_buildium_id: int | None = None
    property_name: str | None = None
    unit_number: str | None = None
    lease_buildium_id: int
    lease_to_date: date | None = None
    rent: float | None = None
    balance: float | None = 0.0
    oldest_bucket: str | None = None


class TeamOpsOut(BaseModel):
    buildium_user_id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    user_role_name: str | None = None
    is_active: bool | None = None
    open_work_orders: int = 0
    open_tasks: int = 0
    completed_tasks_7d: int = 0


class VendorOpsResponse(BaseModel):
    vendors: list[VendorOpsOut]
    pms_name: str | None = None


class TenantOpsResponse(BaseModel):
    #: ``None``: the PMS sends no lease-tenant links, so "no tenants" is unknown.
    tenants: list[TenantOpsOut] | None
    pms_name: str | None = None


class TeamOpsResponse(BaseModel):
    #: ``None``: the PMS sends no staff list.
    members: list[TeamOpsOut] | None
    pms_name: str | None = None


@router.get("/vendors", response_model=VendorOpsResponse)
async def vendor_ops(
    user: AuthenticatedUser = Depends(get_current_user),
) -> VendorOpsResponse:
    """Every mirrored vendor with the comparison numbers: open/total work
    orders, jobs in 90d, avg days to complete (mirror-sighting proxy —
    Buildium exposes no WO created date), 12-month spend from bills."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    rows = await ops_repo.list_vendor_ops(pool, company_id)
    return VendorOpsResponse(
        vendors=[VendorOpsOut(**asdict(r)) for r in rows],
        pms_name=(await coverage_for(pool, company_id)).name,
    )


@router.get("/tenants", response_model=TenantOpsResponse)
async def tenant_ops(
    user: AuthenticatedUser = Depends(get_current_user),
) -> TenantOpsResponse:
    """Every tenant on an ACTIVE lease with balance, aging bucket, rent,
    lease end, and property/unit. A tenant on two leases appears twice —
    the operational view, one row per obligation."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    rows = await ops_repo.list_tenant_ops(pool, company_id)
    return TenantOpsResponse(
        tenants=None if rows is None else [TenantOpsOut(**asdict(r)) for r in rows],
        pms_name=(await coverage_for(pool, company_id)).name,
    )


@router.get("/team", response_model=TeamOpsResponse)
async def team_ops(
    user: AuthenticatedUser = Depends(get_current_user),
) -> TeamOpsResponse:
    """Buildium staff with open workload (assigned open WOs + tasks) and
    tasks completed in the last 7 days (mirror-updated_at proxy)."""
    company_id = _require_company(user)
    pool: asyncpg.Pool = get_pool()
    rows = await ops_repo.list_team_ops(pool, company_id)
    return TeamOpsResponse(
        members=None if rows is None else [TeamOpsOut(**asdict(r)) for r in rows],
        pms_name=(await coverage_for(pool, company_id)).name,
    )
