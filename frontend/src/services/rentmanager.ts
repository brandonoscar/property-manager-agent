/**
 * Rent Manager calls the web app makes: the Settings → Connectors panel
 * (`RentManagerApiDetail`). The onboarding wizard (helixis-onboarding)
 * connects through the same routes, with the same field names.
 *
 * Mirrors the backend `/api/v1/rentmanager/*` surface
 * (`api/routes/rentmanager.py`). `tests/unit/test_rentmanager_routes.py`
 * reads the interfaces below against the route's models, and
 * `rentmanager.test.ts` pins the names this file sends and reads. Reads return
 * masked metadata only; a secret never comes back through the API.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

/** The masked read (`RentManagerCredentialsStatus`). Never a secret. */
export interface RentManagerStatus {
  configured: boolean;
  /** The subdomain before `.api.rentmanager.com`. Not a secret. */
  corp_id: string | null;
  /** The one Rent Manager location this workspace reads. */
  location_id: number | null;
  /** The username's last four characters; null when that would be all of it. */
  username_last4: string | null;
  /** Whether a partner token is stored. The token itself never comes back. */
  partner_token_saved: boolean;
  /** Set only by a SUCCESSFUL test; cleared whenever the details change. */
  last_tested_at: string | null;
  /** Set only on the disconnect response. */
  purged_rows?: number | null;
  /** Whether this deployment accepts Rent Manager details at all (GET only).
   *  Only an explicit `false` means switched off. */
  connector_enabled?: boolean | null;
}

/** What the save sends (`RentManagerCredentialsInput`). */
export interface RentManagerCredentialsInput {
  corp_id: string;
  /** A whole number above 0. The backend refuses text, decimals and true. */
  location_id: number;
  username: string;
  password: string;
  /** Only if Rent Manager issued one; left out otherwise (blank means none). */
  partner_token?: string;
}

export interface RentManagerTestResult {
  ok: boolean;
  /** Rent Manager's own count of the location's properties, when it stated
   *  one. `null` is "not stated", never zero. */
  properties_count: number | null;
  /** A sentence for the customer, written server-side per failure kind. */
  error: string | null;
}

export interface RentManagerSyncResult {
  ok: boolean;
  /** Rows written per stage, e.g. `{ units: 37 }`. A stale stage is not here. */
  rows?: Record<string, number>;
  /** Stages whose read failed: the last good sync is on screen, not empty. */
  stale?: string[];
  error?: string | null;
  /** True while the sync this answer describes is still running. */
  running?: boolean;
  /** When that sync began (server clock). The poll waits for THIS run. */
  started_at?: string | null;
}

/** How often the page asks whether the sync has ended, and for how long. A
 *  Rent Manager sync reads at one request a second, so it is slow by design. */
export const SYNC_POLL_MS = 5_000;
export const SYNC_MAX_WAIT_MS = 15 * 60_000;
/** Checks in a row that may fail (a blip, a deploy) before the page stops asking. */
const SYNC_MAX_POLL_FAILURES = 3;

export const SYNC_LOST =
  'The sync stopped reporting back, most likely because Occupella restarted. Nothing ' +
  'you entered needs to change: reload in a few minutes to see the latest data, or click ' +
  'Sync now again.';
export const SYNC_UNREACHABLE =
  'Occupella stopped answering while the sync ran. It may still finish: reload in a few ' +
  'minutes to see the latest data.';
export const SYNC_STILL_RUNNING =
  'Still syncing after 15 minutes. It keeps going on our side: reload the page later to ' +
  'see the new counts.';

const DEMO_STATUS: RentManagerStatus = {
  configured: false,
  corp_id: null,
  location_id: null,
  username_last4: null,
  partner_token_saved: false,
  last_tested_at: null,
  connector_enabled: true,
};

/** GET /api/v1/rentmanager/credentials. Admin-only; a 403 throws ApiError. */
export async function getRentManagerStatus(): Promise<RentManagerStatus> {
  if (demoMode) return DEMO_STATUS;
  const res = await apiFetch('/api/v1/rentmanager/credentials');
  return res.json();
}

/**
 * PUT /api/v1/rentmanager/credentials. Throws ApiError with the backend's own
 * sentence: 503 switched off, 409 the workspace is on another system, 422 a
 * field that is not right (named, never echoed).
 *
 * The body carries exactly the contract's names. A blank partner token is left
 * out: the backend reads absent as "none", never as an empty stored value.
 */
export async function saveRentManagerCreds(
  input: RentManagerCredentialsInput,
): Promise<RentManagerStatus> {
  const body: RentManagerCredentialsInput = {
    corp_id: input.corp_id,
    location_id: input.location_id,
    username: input.username,
    password: input.password,
  };
  const partner = input.partner_token?.trim();
  if (partner) body.partner_token = partner;
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 500));
    return {
      ...DEMO_STATUS,
      configured: true,
      corp_id: body.corp_id,
      location_id: body.location_id,
      partner_token_saved: Boolean(partner),
    };
  }
  const res = await apiFetch('/api/v1/rentmanager/credentials', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return res.json();
}

/** POST /api/v1/rentmanager/test. A refusal from Rent Manager is `ok: false`
 *  with a sentence, not a throw; a success also starts the first sync. */
export async function testRentManagerConnection(): Promise<RentManagerTestResult> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 600));
    return { ok: true, properties_count: 12, error: null };
  }
  const res = await apiFetch('/api/v1/rentmanager/test', { method: 'POST' });
  return res.json();
}

/** DELETE /api/v1/rentmanager/credentials: removes the saved details AND
 *  deletes the synced Rent Manager data. Nothing changes inside Rent Manager. */
export async function deleteRentManagerCreds(): Promise<RentManagerStatus> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 400));
    return { ...DEMO_STATUS, purged_rows: 0 };
  }
  const res = await apiFetch('/api/v1/rentmanager/credentials', { method: 'DELETE' });
  return res.json();
}

/**
 * Sync now. `POST /api/v1/rentmanager/sync` starts the sync (or joins the one
 * already going) and answers at once; `GET /api/v1/rentmanager/sync` is then
 * asked until THAT run ends, and its answer is returned.
 *
 * ⚠ Never one request for the whole sync: `apiFetch` stops waiting long
 * before a sync at one request a second ends (gotcha 199).
 * Admin-only; a 403 on the start throws ApiError.
 */
export async function runRentManagerSync({
  pollMs = SYNC_POLL_MS,
  maxWaitMs = SYNC_MAX_WAIT_MS,
}: { pollMs?: number; maxWaitMs?: number } = {}): Promise<RentManagerSyncResult> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 900));
    return { ok: true, rows: { properties: 12, units: 37, leases: 8 }, stale: [] };
  }
  const startRes = await apiFetch('/api/v1/rentmanager/sync', { method: 'POST' });
  const start = (await startRes.json()) as RentManagerSyncResult;
  // Refused (nothing saved) or ended already: that answer is the result.
  if (!start.running || !start.started_at) return start;

  const deadline = Date.now() + maxWaitMs;
  let failures = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    let now: RentManagerSyncResult;
    try {
      now = (await (await apiFetch('/api/v1/rentmanager/sync')).json()) as RentManagerSyncResult;
    } catch {
      failures += 1;
      if (failures >= SYNC_MAX_POLL_FAILURES) return { ok: false, error: SYNC_UNREACHABLE };
      continue;
    }
    failures = 0;
    // A different run, or none at all: ours is gone (the server restarted).
    if (now.started_at !== start.started_at) return { ok: false, error: SYNC_LOST };
    if (!now.running) return now;
  }
  return { ok: false, running: true, started_at: start.started_at, error: SYNC_STILL_RUNNING };
}
