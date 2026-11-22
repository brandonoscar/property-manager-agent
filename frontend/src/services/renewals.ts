/**
 * Renewals autopilot — property-page renewal offers.
 *
 * Mirrors `/api/v1/properties/{id}/renewals/*`. Drafts are LLM-generated
 * server-side, edited here, and sent via the PM's connected Gmail — the
 * send fires ONLY from the explicit Send button. Demo mode serves canned
 * data and never sends.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export interface RenewalOffer {
  id: string;
  lease_buildium_id: number;
  status: 'draft' | 'sent' | 'dismissed';
  proposed_rent: number | null;
  term_months: number | null;
  subject: string | null;
  body: string | null;
  recipient_email: string | null;
  sent_at: string | null;
  renewed: boolean;
}

export interface RenewalCandidate {
  lease_buildium_id: number;
  lease_to_date: string | null;
  current_rent: number | null;
  unit_id: number | null;
  unit_number: string | null;
  tenant_names: string | null;
  tenant_email: string | null;
  offer: RenewalOffer | null;
}

export interface RenewalsResponse {
  candidates: RenewalCandidate[];
  renewed: RenewalOffer[];
}

const DEMO_OFFER: RenewalOffer = {
  id: 'demo-offer-1',
  lease_buildium_id: 9101,
  status: 'draft',
  proposed_rent: 1895,
  term_months: 12,
  subject: 'Lease renewal offer — Garden Row, Unit 3C',
  body: 'Hi Priya,\n\nYour lease at Garden Row, Unit 3C ends on September 30 and we would love to have you stay. We are offering a 12-month renewal at $1,895/month.\n\nPlease reply within the next two weeks to accept or discuss.\n\nThank you for being a valued resident.',
  recipient_email: 'priya@example.com',
  sent_at: null,
  renewed: false,
};

const DEMO: RenewalsResponse = {
  candidates: [
    {
      lease_buildium_id: 9101,
      lease_to_date: '2026-09-30',
      current_rent: 1850,
      unit_id: 55,
      unit_number: '3C',
      tenant_names: 'Priya Natarajan',
      tenant_email: 'priya@example.com',
      offer: DEMO_OFFER,
    },
    {
      lease_buildium_id: 9102,
      lease_to_date: '2026-08-15',
      current_rent: 1600,
      unit_id: 56,
      unit_number: '1A',
      tenant_names: 'Marcus Webb',
      tenant_email: 'marcus@example.com',
      offer: null,
    },
  ],
  renewed: [],
};

function base(propertyId: number): string {
  return `/api/v1/properties/${propertyId}/renewals`;
}

export async function fetchRenewals(propertyId: number): Promise<RenewalsResponse> {
  if (demoMode) return DEMO;
  const res = await apiFetch(base(propertyId));
  return res.json();
}

export async function draftRenewal(
  propertyId: number,
  leaseId: number,
  input: { proposed_rent?: number | null; term_months?: number | null; notes?: string },
): Promise<RenewalOffer> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 600));
    return { ...DEMO_OFFER, lease_buildium_id: leaseId };
  }
  const res = await apiFetch(`${base(propertyId)}/${leaseId}/draft`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function saveRenewalDraft(
  propertyId: number,
  leaseId: number,
  input: {
    subject: string;
    body: string;
    proposed_rent?: number | null;
    term_months?: number | null;
    recipient_email?: string | null;
  },
): Promise<RenewalOffer> {
  if (demoMode) {
    return { ...DEMO_OFFER, lease_buildium_id: leaseId, subject: input.subject, body: input.body };
  }
  const res = await apiFetch(`${base(propertyId)}/${leaseId}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function sendRenewal(propertyId: number, leaseId: number): Promise<RenewalOffer> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 500));
    return {
      ...DEMO_OFFER,
      lease_buildium_id: leaseId,
      status: 'sent',
      sent_at: new Date().toISOString(),
    };
  }
  const res = await apiFetch(`${base(propertyId)}/${leaseId}/send`, { method: 'POST' });
  return res.json();
}

export async function dismissRenewal(propertyId: number, leaseId: number): Promise<RenewalOffer> {
  if (demoMode) {
    return { ...DEMO_OFFER, lease_buildium_id: leaseId, status: 'dismissed' };
  }
  const res = await apiFetch(`${base(propertyId)}/${leaseId}/dismiss`, { method: 'POST' });
  return res.json();
}
