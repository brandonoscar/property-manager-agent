/**
 * Operations page API client — the Tenants / Vendors / Team tabs.
 *
 * Mirrors GET /operations/{tenants,vendors,team} (api/routes/operations.py).
 * The Properties tab rides fetchCockpitOverview (services/properties.ts).
 * Demo mode returns fixtures shaped like a real small portfolio so the
 * tables sort meaningfully in screenshots and dogfooding.
 */

import { apiFetch } from './api';
import { NotSyncedRows } from '../lib/notSynced';
import { demoMode } from './auth';

async function readJson<T>(resp: Response): Promise<T> {
  // ``apiFetch`` already throws ApiError on non-2xx, so any response
  // we get here is good to parse.
  return (await resp.json()) as T;
}

export interface VendorOps {
  buildium_id: number;
  name: string | null;
  category_name: string | null;
  is_active: boolean | null;
  phone: string | null;
  email: string | null;
  /** `null`: work orders can't be attributed to vendors for this PMS. */
  open_work_orders: number | null;
  total_work_orders: number | null;
  jobs_90d: number | null;
  avg_days_to_complete: number | null;
  /** `null`: this PMS sends no bills. Unknown, not $0. */
  spend_12m: number | null;
  bill_count_12m: number | null;
  last_bill_date: string | null;
  /** Copied from the response onto each row, for "doesn't sync X from Rentvine". */
  pms_name?: string | null;
}

export interface TenantOps {
  tenant_buildium_id: number;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  property_buildium_id: number | null;
  property_name: string | null;
  unit_number: string | null;
  lease_buildium_id: number;
  lease_to_date: string | null;
  rent: number | null;
  /** `null`: this PMS sends no balances. Unknown, not $0. */
  balance: number | null;
  oldest_bucket: string | null;
}

export interface TeamOps {
  buildium_user_id: number;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  user_role_name: string | null;
  is_active: boolean | null;
  open_work_orders: number;
  open_tasks: number;
  completed_tasks_7d: number;
}

export async function fetchVendorOps(): Promise<VendorOps[]> {
  if (demoMode) return DEMO_VENDORS;
  const resp = await apiFetch('/api/v1/operations/vendors');
  const body = await readJson<{ vendors: VendorOps[]; pms_name?: string | null }>(resp);
  return (body.vendors ?? []).map((v) => ({ ...v, pms_name: body.pms_name ?? null }));
}

export async function fetchTenantOps(): Promise<TenantOps[] | NotSyncedRows> {
  if (demoMode) return DEMO_TENANTS;
  const resp = await apiFetch('/api/v1/operations/tenants');
  const body = await readJson<{ tenants: TenantOps[] | null; pms_name?: string | null }>(resp);
  return body.tenants === null ? new NotSyncedRows(body.pms_name ?? null) : (body.tenants ?? []);
}

export async function fetchTeamOps(): Promise<TeamOps[] | NotSyncedRows> {
  if (demoMode) return DEMO_TEAM;
  const resp = await apiFetch('/api/v1/operations/team');
  const body = await readJson<{ members: TeamOps[] | null; pms_name?: string | null }>(resp);
  return body.members === null ? new NotSyncedRows(body.pms_name ?? null) : (body.members ?? []);
}

// ── Demo fixtures ───────────────────────────────────────────────────

const DEMO_VENDORS: VendorOps[] = [
  {
    buildium_id: 41,
    name: 'Rapid Plumbing Co',
    category_name: 'Plumbing',
    is_active: true,
    phone: '(216) 555-0141',
    email: 'dispatch@rapidplumbing.example',
    open_work_orders: 2,
    total_work_orders: 19,
    jobs_90d: 6,
    avg_days_to_complete: 2.4,
    spend_12m: 18240,
    bill_count_12m: 17,
    last_bill_date: '2026-08-02',
  },
  {
    buildium_id: 42,
    name: 'Northline HVAC',
    category_name: 'HVAC',
    is_active: true,
    phone: '(216) 555-0142',
    email: 'service@northlinehvac.example',
    open_work_orders: 1,
    total_work_orders: 11,
    jobs_90d: 4,
    avg_days_to_complete: 5.8,
    spend_12m: 12960,
    bill_count_12m: 9,
    last_bill_date: '2026-07-28',
  },
  {
    buildium_id: 43,
    name: 'Cuyahoga Electric',
    category_name: 'Electrical',
    is_active: true,
    phone: '(216) 555-0143',
    email: null,
    open_work_orders: 0,
    total_work_orders: 7,
    jobs_90d: 1,
    avg_days_to_complete: 1.6,
    spend_12m: 4310,
    bill_count_12m: 5,
    last_bill_date: '2026-06-14',
  },
  {
    buildium_id: 44,
    name: 'Lakeside Landscaping',
    category_name: 'Grounds',
    is_active: true,
    phone: null,
    email: 'crew@lakesidegrounds.example',
    open_work_orders: 0,
    total_work_orders: 24,
    jobs_90d: 12,
    avg_days_to_complete: 0.9,
    spend_12m: 8640,
    bill_count_12m: 12,
    last_bill_date: '2026-08-11',
  },
  {
    buildium_id: 45,
    name: 'Summit Roofing',
    category_name: 'Roofing',
    is_active: false,
    phone: '(330) 555-0145',
    email: null,
    open_work_orders: 1,
    total_work_orders: 3,
    jobs_90d: 0,
    avg_days_to_complete: 14.2,
    spend_12m: 21150,
    bill_count_12m: 2,
    last_bill_date: '2026-03-19',
  },
];

const DEMO_TENANTS: TenantOps[] = [
  {
    tenant_buildium_id: 9003,
    first_name: 'Priya',
    last_name: 'Natarajan',
    email: 'priya.n@example.com',
    phone: '(216) 555-0193',
    property_buildium_id: 100,
    property_name: '150 Main Ave',
    unit_number: '3C',
    lease_buildium_id: 553,
    lease_to_date: '2026-09-30',
    rent: 1850,
    balance: 850,
    oldest_bucket: '31-60',
  },
  {
    tenant_buildium_id: 9007,
    first_name: 'Marcus',
    last_name: 'Webb',
    email: 'marcus.webb@example.com',
    phone: '(216) 555-0197',
    property_buildium_id: 101,
    property_name: 'Lexington Court',
    unit_number: '1A',
    lease_buildium_id: 557,
    lease_to_date: '2026-08-31',
    rent: 1600,
    balance: 1490,
    oldest_bucket: '61-90',
  },
  {
    tenant_buildium_id: 9001,
    first_name: 'Jordan',
    last_name: 'Reyes',
    email: 'jordan.reyes@example.com',
    phone: '(216) 555-0191',
    property_buildium_id: 100,
    property_name: '150 Main Ave',
    unit_number: '1A',
    lease_buildium_id: 551,
    lease_to_date: '2027-01-31',
    rent: 1450,
    balance: 0,
    oldest_bucket: null,
  },
  {
    tenant_buildium_id: 9002,
    first_name: 'Sam',
    last_name: 'Okafor',
    email: 'sam.okafor@example.com',
    phone: null,
    property_buildium_id: 100,
    property_name: '150 Main Ave',
    unit_number: '2B',
    lease_buildium_id: 552,
    lease_to_date: '2026-08-31',
    rent: 1450,
    balance: 0,
    oldest_bucket: null,
  },
  {
    tenant_buildium_id: 9010,
    first_name: 'Elena',
    last_name: 'Duarte',
    email: 'elena.duarte@example.com',
    phone: '(440) 555-0110',
    property_buildium_id: 102,
    property_name: 'Harbor View',
    unit_number: '2',
    lease_buildium_id: 560,
    lease_to_date: '2026-12-31',
    rent: 2100,
    balance: 0,
    oldest_bucket: null,
  },
];

const DEMO_TEAM: TeamOps[] = [
  {
    buildium_user_id: 77,
    first_name: 'Dana',
    last_name: 'Whitfield',
    email: 'dana@company.example',
    user_role_name: 'Property Manager',
    is_active: true,
    open_work_orders: 4,
    open_tasks: 7,
    completed_tasks_7d: 12,
  },
  {
    buildium_user_id: 78,
    first_name: 'Chris',
    last_name: 'Gale',
    email: 'chris@company.example',
    user_role_name: 'Maintenance Coordinator',
    is_active: true,
    open_work_orders: 6,
    open_tasks: 3,
    completed_tasks_7d: 9,
  },
  {
    buildium_user_id: 79,
    first_name: 'Renee',
    last_name: 'Bishop',
    email: 'renee@company.example',
    user_role_name: 'Leasing Agent',
    is_active: true,
    open_work_orders: 0,
    open_tasks: 5,
    completed_tasks_7d: 6,
  },
];
