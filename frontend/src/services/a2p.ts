/**
 * A2P 10DLC registration API client — the white-label application.
 *
 * Mirrors ``helixis.api.routes.a2p``; keep these in sync. The customer never
 * opens a carrier account and never sees a vendor name — they fill this in and
 * we register on their behalf against our own messaging account.
 *
 * ⚠ **"Submit" means QUEUED.** The backend moves the row draft -> submitted
 * and a separate runner talks to the carriers. Nothing in this client or its
 * callers may say the carriers have received anything, because at the moment
 * of that response they have not.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

/** Ordered. Each value means everything before it is done. */
export const A2P_STATUSES = [
  'draft',
  'submitted',
  'brand_pending',
  'brand_approved',
  'campaign_pending',
  'approved',
] as const;
export type A2pStatus = (typeof A2P_STATUSES)[number] | 'failed';

export interface A2pDetails {
  legal_business_name: string | null;
  /** ⚠ null and '' are DIFFERENT: no EIN registers a Sole Proprietor brand,
   *  an EIN registers Standard, and the carriers reject the wrong kind. */
  ein: string | null;
  business_type: string | null;
  business_vertical: string | null;
  website: string | null;
  street: string | null;
  city: string | null;
  region: string | null;
  postal_code: string | null;
  iso_country: string | null;
  rep_first_name: string | null;
  rep_last_name: string | null;
  rep_email: string | null;
  rep_phone: string | null;
  rep_job_position: string | null;
  use_case: string | null;
  campaign_description: string | null;
  opt_in_description: string | null;
}

export interface A2pRegistration {
  status: A2pStatus;
  details: A2pDetails;
  sample_messages: string[];
  /** What still blocks a submission, in the customer's words. Derived server
   *  side from the SAVED row on every read — never cached, so it cannot go
   *  stale against a field the user has just filled in. */
  problems: string[];
  editable: boolean;
  /** The carrier's own words on a rejection. Shown verbatim. */
  failure_reason: string | null;
  submitted_at: string | null;
  brand_approved_at: string | null;
  campaign_approved_at: string | null;
}

/** Business structures the carriers accept, in the customer's language. */
export const BUSINESS_TYPES: { value: string; label: string }[] = [
  { value: 'Sole Proprietorship', label: 'Sole proprietorship' },
  { value: 'Partnership', label: 'Partnership' },
  { value: 'Limited Liability Corporation', label: 'LLC' },
  { value: 'Corporation', label: 'Corporation' },
  { value: 'Co-operative', label: 'Co-operative' },
  { value: 'Non-profit Corporation', label: 'Non-profit' },
];

/** Job titles the carrier accepts for the authorized representative.
 *
 * ⚠ This is an ENUM, not free text, and the field used to be a plain input
 * whose placeholder said "Owner" — a value the carrier REJECTS ("Job Level is
 * invalid."). The list is not published by the API; each value was measured
 * against the policy evaluation on 2026-09-04. "Owner", "Manager",
 * "President" and "General Manager" are all rejected, which is why anything
 * unlisted maps to "Other" server-side rather than failing the registration.
 * ⚠ Since this is a select, `business_title` now carries the same selected
 * value rather than the customer's own words — harmless (the carrier does not
 * validate that field) but do not describe it as free text.
 */
export const JOB_POSITIONS: { value: string; label: string }[] = [
  { value: 'Other', label: 'Owner / other' },
  { value: 'Director', label: 'Director' },
  { value: 'GM', label: 'General manager' },
  { value: 'VP', label: 'Vice president' },
  { value: 'CEO', label: 'CEO' },
  { value: 'CFO', label: 'CFO' },
  { value: 'General Counsel', label: 'General counsel' },
];

/** Industry, as the carriers classify it. Property management is REAL_ESTATE. */
export const BUSINESS_VERTICALS: { value: string; label: string }[] = [
  { value: 'REAL_ESTATE', label: 'Real estate / property management' },
  { value: 'PROFESSIONAL', label: 'Professional services' },
  { value: 'CONSTRUCTION', label: 'Construction' },
  { value: 'NGO', label: 'Non-profit' },
];

const EMPTY_DETAILS: A2pDetails = {
  legal_business_name: null,
  ein: null,
  business_type: null,
  business_vertical: null,
  website: null,
  street: null,
  city: null,
  region: null,
  postal_code: null,
  iso_country: 'US',
  rep_first_name: null,
  rep_last_name: null,
  rep_email: null,
  rep_phone: null,
  rep_job_position: null,
  use_case: 'MIXED',
  campaign_description: null,
  opt_in_description: null,
};

async function readJson<T>(resp: Response): Promise<T> {
  return (await resp.json()) as T;
}

export async function fetchRegistration(): Promise<A2pRegistration> {
  if (demoMode) {
    return {
      status: 'draft',
      details: EMPTY_DETAILS,
      sample_messages: [],
      problems: [],
      editable: true,
      failure_reason: null,
      submitted_at: null,
      brand_approved_at: null,
      campaign_approved_at: null,
    };
  }
  const resp = await apiFetch('/api/v1/a2p/registration');
  return readJson<A2pRegistration>(resp);
}

export interface A2pSaveBody extends Partial<Record<keyof A2pDetails, string | null>> {
  sample_messages?: string[];
  /** The one consent fact the customer attests to; see comms/a2p_defaults. */
  consent_attested?: boolean;
}

export async function saveRegistration(body: A2pSaveBody): Promise<A2pRegistration> {
  const resp = await apiFetch('/api/v1/a2p/registration', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return readJson<A2pRegistration>(resp);
}

/**
 * Queue the registration.
 *
 * ⚠ Resolving does NOT mean a carrier has it — it means the row is queued and
 * the runner will submit it. Callers must phrase their success copy that way.
 */
export async function submitRegistration(): Promise<A2pRegistration> {
  const resp = await apiFetch('/api/v1/a2p/registration/submit', { method: 'POST' });
  return readJson<A2pRegistration>(resp);
}
