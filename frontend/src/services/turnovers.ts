/**
 * Turnover checklist — property-page make-ready tracker.
 *
 * Mirrors `/api/v1/properties/{id}/turnovers/*`. Rows open automatically
 * when Buildium files a move-out; the PM works the three steps here.
 * Occupella-local state only — no Buildium writes on this surface.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type TurnoverStep = 'inspection' | 'work_orders' | 'listed';

export interface Turnover {
  id: string;
  lease_buildium_id: number;
  unit_id: number | null;
  unit_number: string | null;
  move_out_date: string | null;
  status: 'active' | 'done' | 'dismissed';
  inspection_done: boolean;
  work_orders_done: boolean;
  listed_done: boolean;
  open_work_orders: number;
}

const DEMO: Turnover[] = [
  {
    id: 'demo-turnover-1',
    lease_buildium_id: 9102,
    unit_id: 56,
    unit_number: '1A',
    move_out_date: '2026-08-15',
    status: 'active',
    inspection_done: true,
    work_orders_done: false,
    listed_done: false,
    open_work_orders: 2,
  },
];

function base(propertyId: number): string {
  return `/api/v1/properties/${propertyId}/turnovers`;
}

export async function fetchTurnovers(propertyId: number): Promise<Turnover[]> {
  if (demoMode) return DEMO;
  const res = await apiFetch(base(propertyId));
  const data = await res.json();
  return data.turnovers ?? [];
}

export async function setTurnoverStep(
  propertyId: number,
  turnoverId: string,
  step: TurnoverStep,
  done: boolean,
): Promise<Turnover> {
  if (demoMode) {
    const t = { ...DEMO[0] };
    if (step === 'inspection') t.inspection_done = done;
    else if (step === 'work_orders') t.work_orders_done = done;
    else t.listed_done = done;
    return t;
  }
  const res = await apiFetch(`${base(propertyId)}/${turnoverId}/steps/${step}`, {
    method: 'POST',
    body: JSON.stringify({ done }),
  });
  return res.json();
}

export async function dismissTurnover(propertyId: number, turnoverId: string): Promise<Turnover> {
  if (demoMode) return { ...DEMO[0], status: 'dismissed' };
  const res = await apiFetch(`${base(propertyId)}/${turnoverId}/dismiss`, { method: 'POST' });
  return res.json();
}
