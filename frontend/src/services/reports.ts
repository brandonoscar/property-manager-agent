/**
 * Reports service — the morning brief (roadmap §7 rank 2).
 *
 * GET /api/v1/reports/morning-brief returns today's severity-ordered
 * "things that need you" items (max five, zero-count items omitted by
 * the backend). Dismissal is per-day in localStorage so the brief
 * returns tomorrow without any backend state.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type BriefView = 'chat' | 'inbox' | 'properties' | 'crm';

export interface MorningBriefItem {
  key: string;
  count: number;
  amount: number | null;
  label: string;
  detail: string;
  view: BriefView;
}

const DISMISS_KEY = 'helixis.morningBrief.dismissed';

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export function isBriefDismissedToday(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === today();
  } catch {
    return false;
  }
}

export function dismissBriefForToday(): void {
  try {
    localStorage.setItem(DISMISS_KEY, today());
  } catch {
    // private mode — the brief just reappears on reload; harmless
  }
}

const DEMO_ITEMS: MorningBriefItem[] = [
  {
    key: 'promises_due',
    count: 2,
    amount: null,
    label: '2 payment promises due or passed',
    detail: "Tenants who said they'd pay — today is the day to follow up.",
    view: 'inbox',
  },
  {
    key: 'delinquency',
    count: 3,
    amount: 4020,
    label: '$4,020 owed across 3 leases',
    detail: 'Ask “who’s behind on rent?” to see who actually needs the nudge.',
    view: 'chat',
  },
  {
    key: 'stalled_work_orders',
    count: 4,
    amount: null,
    label: '4 work orders stalled over 7 days',
    detail: 'No update from anyone — residents are waiting.',
    view: 'inbox',
  },
  {
    key: 'cold_leads',
    count: 3,
    amount: null,
    label: '3 leads going cold',
    detail: 'Quiet 3+ days — a nudge today keeps them in play.',
    view: 'crm',
  },
];

/**
 * Has the mirror got any portfolio rows yet? GET /api/v1/reports/first-scan
 * sets `synced` once properties, units or active leases exist, for Buildium
 * and Rentvine alike (db/repositories/first_scan.py).
 */
export async function fetchFirstScanSynced(): Promise<boolean> {
  const resp = await apiFetch('/api/v1/reports/first-scan');
  const body = (await resp.json()) as { synced?: boolean };
  return body.synced === true;
}

export async function fetchMorningBrief(): Promise<MorningBriefItem[]> {
  if (demoMode) return DEMO_ITEMS;
  const resp = await apiFetch('/api/v1/reports/morning-brief');
  const body = (await resp.json()) as { items?: MorningBriefItem[] };
  return body.items ?? [];
}

/**
 * "Occupella noticed" — the attention-budgeted findings digest
 * (GET /api/v1/reports/noticed). The detector framework writes findings into
 * one ledger; this endpoint governs the volume so week-three noise doesn't
 * bury the signal. `urgent` is delivered uncapped (critical only); `noticed`
 * is today's capped digest; `overflowCount` is what didn't make the cut (still
 * queryable via /reports/risk-flags). `miscalibrated` lists noisy detectors.
 */
export interface NoticedFinding {
  id: string;
  rule_key: string;
  severity: 'info' | 'warning' | 'critical';
  title: string;
  detail: string;
  amount: number | null;
  property_id: number | null;
  entity_type: string | null;
  entity_id: number | null;
}

export interface NoticedItem extends NoticedFinding {
  related: NoticedFinding[];
}

export interface MiscalibratedDetector {
  rule_key: string;
  total: number;
  dismissed: number;
  dismissal_rate: number;
}

export interface NoticedDigest {
  generated_at: string;
  urgent: NoticedItem[];
  noticed: NoticedItem[];
  overflow_count: number;
  delivered_count: number;
  miscalibrated: MiscalibratedDetector[];
}

const EMPTY_NOTICED: NoticedDigest = {
  generated_at: '',
  urgent: [],
  noticed: [],
  overflow_count: 0,
  delivered_count: 0,
  miscalibrated: [],
};

const DEMO_NOTICED: NoticedDigest = {
  ...EMPTY_NOTICED,
  generated_at: '2026-08-20T12:00:00Z',
  noticed: [
    {
      id: 'demo-flag-1',
      rule_key: 'rent_below_lease',
      severity: 'warning',
      title: 'Rent charged below the lease amount',
      detail: 'Lease 24215 for unit 3C bills $1,700/mo against a $1,850 lease.',
      amount: 150,
      property_id: 100,
      entity_type: 'lease',
      entity_id: 24215,
      related: [
        {
          id: 'demo-flag-2',
          rule_key: 'rent_below_lease',
          severity: 'warning',
          title: 'Rent charged below the lease amount',
          detail: 'Lease 24219 for unit 2 bills $1,400/mo against a $1,450 lease.',
          amount: 50,
          property_id: 101,
          entity_type: 'lease',
          entity_id: 24219,
        },
      ],
    },
    {
      id: 'demo-flag-3',
      rule_key: 'duplicate_bill',
      severity: 'info',
      title: 'Possible duplicate vendor bill',
      detail: 'Two $220 bills from Rapid Plumbing Co dated three days apart.',
      amount: 220,
      property_id: 100,
      entity_type: 'bill',
      entity_id: 88101,
      related: [],
    },
  ],
};

export async function fetchNoticed(): Promise<NoticedDigest> {
  if (demoMode) return DEMO_NOTICED;
  const resp = await apiFetch('/api/v1/reports/noticed');
  const body = (await resp.json()) as Partial<NoticedDigest>;
  return { ...EMPTY_NOTICED, ...body };
}

/**
 * Dismiss one noticed finding ("this is fine") — PATCHes the risk-flag
 * ledger, so the engine suppresses it from re-surfacing. The flag stays
 * queryable via /reports/risk-flags; dismissal drops it from the digest,
 * it does not delete it.
 */
export async function dismissRiskFlag(id: string): Promise<void> {
  if (demoMode) return;
  await apiFetch(`/api/v1/reports/risk-flags/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify({ status: 'dismissed' }),
  });
}
