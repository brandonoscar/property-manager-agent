/**
 * Properties × Roles routing grid API client (PR #81).
 *
 * Mirrors the FastAPI routes in ``helixis.api.routes.properties`` —
 * keep these in sync. Every call goes through ``apiFetch`` so the
 * Supabase bearer token + JSON envelope handling stays uniform with
 * the rest of the app.
 */

import { apiFetch, ApiError } from './api';
import { demoMode } from './auth';

export const CATEGORIES = [
  'maintenance',
  'leasing',
  'accounting',
  'tenant_relations',
  'other',
] as const;

export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABELS: Record<Category, string> = {
  maintenance: 'Maintenance',
  leasing: 'Leasing',
  accounting: 'Accounting',
  tenant_relations: 'Tenant Relations',
  other: 'Other',
};

export interface CellAssignment {
  action_type: string;
  buildium_user_id: number | null;
}

export interface PropertyRow {
  buildium_property_id: number;
  name: string;
  address: string | null;
  assignments: Partial<Record<Category, CellAssignment | null>>;
}

export interface DefaultRow {
  assignments: Partial<Record<Category, CellAssignment | null>>;
}

export interface PropertiesGridResponse {
  default_row: DefaultRow;
  properties: PropertyRow[];
  page: number;
  page_size: number;
  total: number;
}

export interface StaffOption {
  buildium_user_id: number;
  display_name: string;
  email: string | null;
  role: string | null;
}

export interface StaffListResponse {
  options: StaffOption[];
}

export interface SyncResponse {
  ok: boolean;
  upserted?: number;
  deactivated?: number;
  error?: string | null;
}

export interface AssignmentMutationResponse {
  ok: boolean;
  cleared: boolean;
  assignment: CellAssignment | null;
}

async function readJson<T>(resp: Response): Promise<T> {
  // ``apiFetch`` already throws ApiError on non-2xx, so any response
  // we get here is good to parse.
  return (await resp.json()) as T;
}

export async function fetchPropertiesGrid(params: {
  page?: number;
  pageSize?: number;
} = {}): Promise<PropertiesGridResponse> {
  const query = new URLSearchParams();
  if (params.page) query.set('page', String(params.page));
  if (params.pageSize) query.set('page_size', String(params.pageSize));
  const qs = query.toString();
  const path = qs ? `/api/v1/properties?${qs}` : '/api/v1/properties';
  const resp = await apiFetch(path);
  return readJson<PropertiesGridResponse>(resp);
}

export async function fetchStaffOptions(): Promise<StaffListResponse> {
  const resp = await apiFetch('/api/v1/properties/staff');
  return readJson<StaffListResponse>(resp);
}

export async function setPropertyAssignment(
  propertyId: number,
  category: Category,
  buildiumUserId: number | null,
): Promise<AssignmentMutationResponse> {
  if (demoMode) {
    // Demo isolation: reflect the change locally, never PUT.
    return {
      ok: true,
      cleared: buildiumUserId === null,
      assignment: buildiumUserId === null ? null : { action_type: 'assign_to_user', buildium_user_id: buildiumUserId },
    };
  }
  const resp = await apiFetch(
    `/api/v1/properties/${propertyId}/assignment/${category}`,
    {
      method: 'PUT',
      body: JSON.stringify({ buildium_user_id: buildiumUserId }),
    },
  );
  return readJson<AssignmentMutationResponse>(resp);
}

export async function setDefaultAssignment(
  category: Category,
  buildiumUserId: number | null,
): Promise<AssignmentMutationResponse> {
  if (demoMode) {
    // Demo isolation: reflect the change locally, never PUT.
    return {
      ok: true,
      cleared: buildiumUserId === null,
      assignment: buildiumUserId === null ? null : { action_type: 'assign_to_user', buildium_user_id: buildiumUserId },
    };
  }
  const resp = await apiFetch(
    `/api/v1/properties/default-assignment/${category}`,
    {
      method: 'PUT',
      body: JSON.stringify({ buildium_user_id: buildiumUserId }),
    },
  );
  return readJson<AssignmentMutationResponse>(resp);
}

export async function syncBuildiumStaff(): Promise<SyncResponse> {
  if (demoMode) return { ok: true, upserted: 0, deactivated: 0 }; // demo isolation: no POST
  try {
    const resp = await apiFetch('/api/v1/properties/sync', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    return readJson<SyncResponse>(resp);
  } catch (err) {
    // ``apiFetch`` raises ApiError on non-2xx, but the sync endpoint
    // returns 200 + ``{ok: false, error: …}`` for permission issues —
    // so this catch is genuinely network / auth failures.
    if (err instanceof ApiError) {
      return { ok: false, error: err.message };
    }
    return { ok: false, error: 'Sync failed (network)' };
  }
}

// ── Property cockpit (Workstream A) ────────────────────────────────
// Mirrors GET /properties/cockpit + /properties/cockpit/{id}. Reads the
// backend's Buildium entity mirror — empty until a backfill has run.
//
// ⚠ A `null` figure or list means "this company's PMS doesn't send it"
// (Rentvine: no balances, payments, tasks or lease-tenant links), never zero
// or empty. Render it with `lib/notSynced.ts`, naming `pms_name`.

export interface CockpitSummary {
  buildium_id: number;
  name: string | null;
  address_line1: string | null;
  city: string | null;
  state: string | null;
  unit_count: number;
  occupied_count: number;
  /** Units neither occupied nor known vacant; never count them as vacant
   * (`lib/occupancy.ts`). 0 when the PMS states every unit's occupancy. */
  occupancy_unknown_count: number;
  open_work_orders: number;
  expiring_leases_90d: number;
  delinquent_leases: number | null;
  delinquent_total: number | null;
  /** Copied from the list response onto each row by `fetchCockpitOverview`. */
  pms_name?: string | null;
}

export interface CockpitDetail {
  summary: CockpitSummary;
  /** null when Active leases exist and none has a known rent: unknown, never $0. */
  rent_scheduled: number | null;
  /** Active leases whose rent is unknown. Absent from older responses. */
  rent_unknown_leases?: number;
  rent_collected_mtd: number | null;
  /** Which system the mirror came from, for "doesn't sync X from Rentvine". */
  pms_name?: string | null;
  tenants: Array<{
    buildium_id: number;
    first_name: string | null;
    last_name: string | null;
    unit_id: number | null;
    unit_number: string | null;
  }> | null;
  open_work_order_list: Array<{
    buildium_id: number;
    title: string | null;
    status: string | null;
    priority: string | null;
    unit_id: number | null;
  }>;
  expiring_lease_list: Array<{
    buildium_id: number;
    unit_number: string | null;
    lease_to_date: string | null;
    rent: number | null;
  }>;
  delinquency_list: Array<{
    lease_id: number;
    unit_id: number | null;
    unit_number: string | null;
    total_balance: number | null;
  }> | null;
}

const DEMO_COCKPIT: CockpitDetail = {
  summary: {
    buildium_id: 100,
    name: '150 Main Ave',
    address_line1: '150 Main Ave',
    city: 'Cleveland',
    state: 'OH',
    unit_count: 8,
    occupied_count: 7,
    occupancy_unknown_count: 0,
    open_work_orders: 2,
    expiring_leases_90d: 1,
    delinquent_leases: 1,
    delinquent_total: 850,
  },
  rent_scheduled: 11200,
  rent_collected_mtd: 9800,
  tenants: [
    { buildium_id: 1, first_name: 'Jordan', last_name: 'Reyes', unit_id: 401, unit_number: '1A' },
    { buildium_id: 2, first_name: 'Sam', last_name: 'Okafor', unit_id: 402, unit_number: '2B' },
    { buildium_id: 3, first_name: 'Priya', last_name: 'Natarajan', unit_id: 403, unit_number: '3C' },
  ],
  open_work_order_list: [
    { buildium_id: 11, title: 'Water heater pilot out', status: 'New', priority: 'High', unit_id: 401 },
    { buildium_id: 12, title: 'Hallway light flickering', status: 'InProgress', priority: 'Low', unit_id: null },
  ],
  expiring_lease_list: [
    { buildium_id: 21, unit_number: '2B', lease_to_date: '2026-08-31', rent: 1450 },
  ],
  delinquency_list: [{ lease_id: 31, unit_id: 403, unit_number: '3C', total_balance: 850 }],
};

export async function fetchCockpitDetail(propertyId: number): Promise<CockpitDetail> {
  if (demoMode) {
    return { ...DEMO_COCKPIT, summary: { ...DEMO_COCKPIT.summary, buildium_id: propertyId } };
  }
  const resp = await apiFetch(`/api/v1/properties/cockpit/${propertyId}`);
  return readJson<CockpitDetail>(resp);
}

export async function fetchCockpitOverview(): Promise<CockpitSummary[]> {
  if (demoMode) return demoCockpitOverview();
  // The API wraps the list: { properties: [...] }. This used to be typed as
  // a bare array, so the onboarding checklist's `.length > 0` was always
  // `undefined > 0` — "portfolio synced" never ticked in production.
  const resp = await apiFetch('/api/v1/properties/cockpit');
  const body = await readJson<{ properties: CockpitSummary[]; pms_name?: string | null }>(resp);
  return (body.properties ?? []).map((p) => ({ ...p, pms_name: body.pms_name ?? null }));
}

/** Demo cockpit rows — three properties so the Operations table has
 *  something to sort. The detail fetch keeps returning DEMO_COCKPIT for
 *  whichever property is opened. */
function demoCockpitOverview(): CockpitSummary[] {
  return [
    { ...DEMO_COCKPIT.summary },
    {
      buildium_id: 101,
      name: 'Lexington Court',
      address_line1: '77 Lexington Ave',
      city: 'Cleveland',
      state: 'OH',
      unit_count: 12,
      occupied_count: 11,
      occupancy_unknown_count: 0,
      open_work_orders: 4,
      expiring_leases_90d: 2,
      delinquent_leases: 2,
      delinquent_total: 2340,
    },
    {
      buildium_id: 102,
      name: 'Harbor View',
      address_line1: '9 Harbor View Dr',
      city: 'Lakewood',
      state: 'OH',
      unit_count: 6,
      occupied_count: 6,
      occupancy_unknown_count: 0,
      open_work_orders: 0,
      expiring_leases_90d: 1,
      delinquent_leases: 0,
      delinquent_total: 0,
    },
  ];
}

// ── Unit + tenant detail (drill-downs from the cockpit) ────────────
// Mirrors GET /properties/cockpit/units/{id} + /cockpit/tenants/{id}.

export interface UnitDetail {
  unit: {
    buildium_id: number;
    unit_number: string | null;
    market_rent: number | null;
    is_occupied: boolean | null;
    property_id: number | null;
    property_name: string | null;
  };
  current_lease: {
    buildium_id: number;
    status: string | null;
    lease_from_date: string | null;
    lease_to_date: string | null;
    rent: number | null;
  } | null;
  tenants: Array<{
    buildium_id: number;
    first_name: string | null;
    last_name: string | null;
    email: string | null;
    phone: string | null;
    move_in_date: string | null;
  }> | null;
  /** `false`: a null `balance` is unknown, not "owes nothing". */
  balances_sent?: boolean;
  pms_name?: string | null;
  balance: {
    total_balance: number | null;
    balance_0_30: number | null;
    balance_31_60: number | null;
    balance_61_90: number | null;
    balance_over_90: number | null;
    snapshot_at: string | null;
  } | null;
  open_work_orders: Array<{
    buildium_id: number;
    title: string | null;
    status: string | null;
    priority: string | null;
    updated_at: string | null;
  }>;
  recent_work_orders: Array<{
    buildium_id: number;
    title: string | null;
    status: string | null;
    priority: string | null;
    updated_at: string | null;
  }>;
  open_tasks: Array<{
    buildium_id: number;
    title: string | null;
    status: string | null;
    priority: string | null;
    due_date: string | null;
    category_name: string | null;
  }> | null;
}

export interface TenantDetail {
  tenant: {
    buildium_id: number;
    first_name: string | null;
    last_name: string | null;
    email: string | null;
    phone: string | null;
  };
  leases: Array<{
    buildium_id: number;
    status: string | null;
    lease_from_date: string | null;
    lease_to_date: string | null;
    rent: number | null;
    unit_id: number | null;
    unit_number: string | null;
    property_name: string | null;
    move_in_date: string | null;
    total_balance: number | null;
  }> | null;
  recent_transactions: Array<{
    transaction_date: string | null;
    transaction_type: string | null;
    total_amount: number | null;
    lease_id: number | null;
  }> | null;
  open_work_orders: Array<{
    buildium_id: number;
    title: string | null;
    status: string | null;
    priority: string | null;
    updated_at: string | null;
    unit_id: number | null;
  }> | null;
  /** `false`: each lease's null `total_balance` is unknown, not zero. */
  balances_sent?: boolean;
  pms_name?: string | null;
}

const DEMO_UNIT_DETAIL: UnitDetail = {
  unit: {
    buildium_id: 402,
    unit_number: '2B',
    market_rent: 1450,
    is_occupied: true,
    property_id: 100,
    property_name: '150 Main Ave',
  },
  current_lease: {
    buildium_id: 21,
    status: 'Active',
    lease_from_date: '2025-09-01',
    lease_to_date: '2026-08-31',
    rent: 1450,
  },
  tenants: [
    {
      buildium_id: 2,
      first_name: 'Sam',
      last_name: 'Okafor',
      email: 'sam.okafor@example.com',
      phone: '(216) 555-0142',
      move_in_date: '2025-09-01',
    },
  ],
  balance: null,
  open_work_orders: [
    {
      buildium_id: 11,
      title: 'Water heater pilot out',
      status: 'New',
      priority: 'High',
      updated_at: '2026-07-10',
    },
  ],
  recent_work_orders: [
    {
      buildium_id: 11,
      title: 'Water heater pilot out',
      status: 'New',
      priority: 'High',
      updated_at: '2026-07-10',
    },
    {
      buildium_id: 9,
      title: 'Garbage disposal jammed',
      status: 'Completed',
      priority: 'Normal',
      updated_at: '2026-05-22',
    },
  ],
  open_tasks: [
    {
      buildium_id: 41,
      title: 'Schedule renewal conversation',
      status: 'New',
      priority: 'Normal',
      due_date: '2026-07-25',
      category_name: 'Leasing',
    },
  ],
};

const DEMO_TENANT_DETAIL: TenantDetail = {
  tenant: {
    buildium_id: 2,
    first_name: 'Sam',
    last_name: 'Okafor',
    email: 'sam.okafor@example.com',
    phone: '(216) 555-0142',
  },
  leases: [
    {
      buildium_id: 21,
      status: 'Active',
      lease_from_date: '2025-09-01',
      lease_to_date: '2026-08-31',
      rent: 1450,
      unit_id: 402,
      unit_number: '2B',
      property_name: '150 Main Ave',
      move_in_date: '2025-09-01',
      total_balance: null,
    },
  ],
  recent_transactions: [
    { transaction_date: '2026-07-01', transaction_type: 'Payment', total_amount: 1450, lease_id: 21 },
    { transaction_date: '2026-07-01', transaction_type: 'Charge', total_amount: 1450, lease_id: 21 },
    { transaction_date: '2026-06-01', transaction_type: 'Payment', total_amount: 1450, lease_id: 21 },
  ],
  open_work_orders: [
    {
      buildium_id: 11,
      title: 'Water heater pilot out',
      status: 'New',
      priority: 'High',
      updated_at: '2026-07-10',
      unit_id: 402,
    },
  ],
};

export async function fetchUnitDetail(unitId: number): Promise<UnitDetail> {
  if (demoMode) {
    return { ...DEMO_UNIT_DETAIL, unit: { ...DEMO_UNIT_DETAIL.unit, buildium_id: unitId } };
  }
  const resp = await apiFetch(`/api/v1/properties/cockpit/units/${unitId}`);
  return readJson<UnitDetail>(resp);
}

export async function fetchTenantDetail(tenantId: number): Promise<TenantDetail> {
  if (demoMode) {
    return {
      ...DEMO_TENANT_DETAIL,
      tenant: { ...DEMO_TENANT_DETAIL.tenant, buildium_id: tenantId },
    };
  }
  const resp = await apiFetch(`/api/v1/properties/cockpit/tenants/${tenantId}`);
  return readJson<TenantDetail>(resp);
}
