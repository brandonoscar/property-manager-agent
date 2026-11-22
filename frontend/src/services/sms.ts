/**
 * SMS consent management API client (Stage 3).
 *
 * Mirrors the FastAPI routes in ``helixis.api.routes.sms`` — keep these
 * in sync. Every call goes through ``apiFetch`` so the Supabase bearer
 * token + JSON envelope handling stays uniform with the rest of the app.
 *
 * The consent ledger is the TCPA record: rows are never hard-deleted —
 * "delete" REVOKES (the backend keeps the row, marked revoked), and the
 * server always records ``source='app_settings'`` for rows added here.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export const RECIPIENT_KINDS = ['manager', 'tenant', 'owner', 'vendor'] as const;
export type RecipientKind = (typeof RECIPIENT_KINDS)[number];

export interface SmsConsent {
  id: string;
  phone: string;
  display_name: string | null;
  recipient_kind: string;
  source: string;
  timezone: string;
  consented_at: string | null;
  revoked_at: string | null;
  /**
   * The account that CLAIMED this number as theirs, or null when nobody has.
   *
   * Only a claimed number can be revoked when its owner leaves the company —
   * so an unattributed staff number goes on receiving the manager digest no
   * matter who departs. The panel shows that rather than hiding it: a gap the
   * product does not surface is one nobody closes.
   *
   * Optional because a backend older than migration 0096 answers 200 without
   * the key.
   */
  owner_user_id?: string | null;
  /**
   * How this number's MOST RECENT outbound message actually ended.
   *
   * `helixis_sms_log` recorded the whole delivery lifecycle from day one and
   * nothing ever read it: a text to a landline came back `undelivered` with
   * error 30006, was written down, and the number stayed in this table
   * looking healthy.
   *
   * ⚠ `null` means NO MESSAGES SENT, never "fine". A number nobody has texted
   * and a number whose texts all vanished must not render the same way.
   *
   * ⚠ `no_receipt` is NOT `undelivered`. A carrier that filters a message as
   * spam sends no receipt at all, so Twilio reported nothing — showing that as
   * a failure claims something Twilio never said, and hiding it claims
   * delivery nobody confirmed.
   */
  last_outcome?: SmsOutcome | null;
  /** Twilio's ErrorCode on that last message. 30006 is the landline case. */
  last_error_code?: string | null;
}

export type SmsOutcome = 'delivered' | 'undelivered' | 'no_receipt' | 'pending';

/** What to show a manager for each outcome, and whether it needs action. */
export const OUTCOME_LABEL: Record<SmsOutcome, string> = {
  delivered: 'Delivered',
  undelivered: 'Not delivered',
  no_receipt: 'Never confirmed',
  pending: 'In flight',
};

export interface SmsStatus {
  /**
   * May THIS COMPANY send.
   *
   * ⚠ Not the deployment flag. A2P approval is per company while
   * `twilio.enabled` is one switch for the install, so on a white-label
   * deployment the first customer's approval used to turn this true for
   * everyone — four of five saw a green Leasing gate and a 503 on their first
   * reply.
   */
  enabled: boolean;
  /** Twilio creds + a sender are present on the deployment. */
  configured: boolean;
  /**
   * WHICH blocker fired, straight from the backend predicate that also
   * refuses the send. Never re-derive this from `enabled` + `configured`:
   * that is how the panel's explanation came to disagree with the refusal it
   * was explaining.
   */
  block?: '' | 'disabled' | 'not_configured' | 'campaign_pending';
  /** This company's own A2P registration status; '' if never registered. */
  campaign_status?: string;
  consent_count: number;
  revoked_count: number;
  /**
   * Outbound delivery over `delivery_window_days`.
   *
   * ⚠ `null` (or absent, from an older backend) means the log could not be
   * read — NOT that nothing failed. Rendering an unread log as a clean bill of
   * health is the exact silent degradation these counts exist to end.
   */
  delivered_count?: number | null;
  undelivered_count?: number | null;
  /** Accepted, never confirmed, past the window — the carrier-filter shape. */
  no_receipt_count?: number | null;
  pending_count?: number | null;
  delivery_window_days?: number;
}

/** Common US IANA zones for the add-consent form's timezone selector. */
export const US_TIMEZONES: { value: string; label: string }[] = [
  { value: 'America/New_York', label: 'Eastern (New York)' },
  { value: 'America/Chicago', label: 'Central (Chicago)' },
  { value: 'America/Denver', label: 'Mountain (Denver)' },
  { value: 'America/Phoenix', label: 'Arizona (Phoenix)' },
  { value: 'America/Los_Angeles', label: 'Pacific (Los Angeles)' },
  { value: 'America/Anchorage', label: 'Alaska (Anchorage)' },
  { value: 'Pacific/Honolulu', label: 'Hawaii (Honolulu)' },
];

async function readJson<T>(resp: Response): Promise<T> {
  // ``apiFetch`` already throws ApiError on non-2xx, so any response
  // we get here is good to parse.
  return (await resp.json()) as T;
}

export async function fetchSmsStatus(): Promise<SmsStatus> {
  if (demoMode) {
    return { enabled: false, configured: true, consent_count: 0, revoked_count: 0 };
  }
  const resp = await apiFetch('/api/v1/sms/status');
  return readJson<SmsStatus>(resp);
}

export async function listSmsConsent(): Promise<SmsConsent[]> {
  if (demoMode) return [];
  const resp = await apiFetch('/api/v1/sms/consent');
  const body = await readJson<{ consents?: unknown }>(resp);
  // Wrong-shape guard (the fetchReminders lesson): a 200 whose body isn't
  // {consents: [...]} must degrade to [], never poison component state.
  return Array.isArray(body?.consents) ? (body.consents as SmsConsent[]) : [];
}

export async function addSmsConsent(input: {
  phone: string;
  display_name?: string;
  recipient_kind?: RecipientKind;
  timezone?: string;
  /**
   * "This is my own phone." Records the signed-in user as the row's owner,
   * which is the only thing that lets their departure revoke it later.
   * Never inferred server-side — see the route's own note.
   */
  is_own_number?: boolean;
}): Promise<SmsConsent> {
  if (demoMode) {
    // Demo isolation: reflect the add locally, never POST.
    return {
      id: `demo-${Date.now()}`,
      phone: input.phone,
      display_name: input.display_name ?? null,
      recipient_kind: input.recipient_kind ?? 'manager',
      source: 'app_settings',
      timezone: input.timezone ?? 'America/New_York',
      consented_at: new Date().toISOString(),
      revoked_at: null,
      owner_user_id: input.is_own_number ? 'demo-user' : null,
    };
  }
  const resp = await apiFetch('/api/v1/sms/consent', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return readJson<SmsConsent>(resp);
}

export async function revokeSmsConsent(id: string): Promise<SmsConsent> {
  if (demoMode) {
    return {
      id,
      phone: '',
      display_name: null,
      recipient_kind: 'manager',
      source: 'app_settings',
      timezone: 'America/New_York',
      consented_at: null,
      revoked_at: new Date().toISOString(),
    };
  }
  const resp = await apiFetch(`/api/v1/sms/consent/${id}`, { method: 'DELETE' });
  return readJson<SmsConsent>(resp);
}

// ── number provisioning (in-app, never in onboarding) ────────────────

export interface TwilioNumber {
  id: string;
  phone_number: string;
  label: string | null;
  /**
   * Where an inbound CALL rings. Null/empty means voicemail.
   *
   * ⚠ A number provisioned by the A2P sweep arrives with this unset — the
   * sweep buys on carrier approval with nobody present to ask — so a company
   * can finish setup, see its number, and never hear it ring. That is what
   * the forwarding prompt in LeasingComplianceGate exists to catch.
   */
  forward_to_number?: string | null;
  voice_greeting?: string | null;
  created_at?: string | null;
}

export interface ProvisionResult {
  number: TwilioNumber;
  /** True when this call BOUGHT a number; false when one already existed. */
  purchased: boolean;
}

export async function listTwilioNumbers(): Promise<TwilioNumber[]> {
  if (demoMode) return [];
  const resp = await apiFetch('/api/v1/sms/numbers');
  const body = await readJson<{ numbers: TwilioNumber[] }>(resp);
  return body.numbers ?? [];
}

/**
 * Buy this company a number and wire its webhooks. ADMIN ONLY, and it spends
 * money — a Twilio number is a recurring monthly charge, so the caller must
 * confirm before invoking this.
 *
 * Safe to retry: the backend returns the existing number (purchased: false)
 * rather than buying a second one.
 */
export async function provisionTwilioNumber(
  areaCode = '',
  forwardToNumber = '',
): Promise<ProvisionResult> {
  const resp = await apiFetch('/api/v1/sms/numbers/provision', {
    method: 'POST',
    body: JSON.stringify({
      area_code: areaCode.trim(),
      forward_to_number: forwardToNumber.trim(),
    }),
  });
  return readJson<ProvisionResult>(resp);
}

/**
 * Set where a number's calls ring. ADMIN ONLY.
 *
 * ⚠ Omitting a field LEAVES the stored value; passing `''` clears it back to
 * voicemail. The backend upserts with COALESCE for exactly this reason — an
 * absent field must not wipe forwarding, and "stop ringing my cell" must stay
 * expressible. Don't pass `undefined` expecting a clear.
 */
export async function saveTwilioNumber(input: {
  phone_number: string;
  label?: string;
  forward_to_number?: string;
  voice_greeting?: string;
}): Promise<TwilioNumber> {
  const resp = await apiFetch('/api/v1/sms/numbers', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return readJson<TwilioNumber>(resp);
}
