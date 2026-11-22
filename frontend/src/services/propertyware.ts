/**
 * Propertyware calls the web app makes, for the Settings → Connectors panel
 * (`PropertywareApiDetail`). The onboarding wizard (helixis-onboarding)
 * connects through the same routes, with the same field names.
 *
 * Mirrors the backend `/api/v1/propertyware/*` surface
 * (`src/helixis/api/routes/propertyware.py`). Reads return masked metadata
 * only; a secret never comes back from the API.
 *
 * There are no demo-mode answers: the demo workspace lists no Propertyware
 * card, so nothing in demo mode can reach these.
 */

import { apiFetch } from './api';

/** The permissions the key needs, one per list the sync reads, as Propertyware
 *  names them on its API key form. `tests/unit/test_propertyware_routes.py`
 *  compares this with the read list and the spec's index. */
export const PROPERTYWARE_READ_PERMISSIONS = [
  'BUILDINGS - Read',
  'UNITS - Read',
  'PORTFOLIOS - Read',
  'CONTACTS - Read',
  'VENDORS - Read',
  'LEASES - Read',
  'WORK ORDERS - Read',
  'BILLS - Read',
  'GL ACCOUNTS - Read',
] as const;

/** The shortest client ID and secret the server accepts; the form waits for
 *  them so a cut-off paste is caught before the save. The server's own bounds
 *  are the authority, and a test keeps these equal to them. */
export const MIN_CLIENT_ID_LENGTH = 4;
export const MIN_CLIENT_SECRET_LENGTH = 8;

/** `PUT /api/v1/propertyware/credentials` (`PropertywareCredentialsInput`). */
export interface PropertywareCredentialsInput {
  /** The organization ID: it identifies the Propertyware account. Not a secret. */
  system_id: string;
  client_id: string;
  client_secret: string;
}

/** The masked read (`PropertywareCredentialsStatus`). */
export interface PropertywareStatus {
  configured: boolean;
  system_id: string | null;
  client_id_last4: string | null;
  /** Set only by a PASSED test; cleared whenever the keys are saved. */
  last_tested_at: string | null;
  /** Set only on the disconnect response. */
  purged_rows?: number | null;
  /** Whether this deployment accepts a Propertyware connection at all (GET only). */
  connector_enabled?: boolean | null;
}

/** `POST /api/v1/propertyware/test` (`PropertywareTestResult`). */
export interface PropertywareTestResult {
  ok: boolean;
  /** Propertyware's own count of buildings, when it states one. */
  properties_count: number | null;
  /** A sentence for the customer, written server-side per failure kind. */
  error: string | null;
}

/** `POST` and `GET /api/v1/propertyware/sync` (`PropertywareSyncResult`). */
export interface PropertywareSyncResult {
  ok: boolean;
  /** Rows written per table, e.g. `{ units: 37 }`. A stale table is left out. */
  rows?: Record<string, number>;
  /** Tables that did not sync: the last good sync is on screen, not nothing. */
  stale?: string[];
  /** Names each stale table and why, in words for the customer. */
  error?: string | null;
  /** True while the sync this answer describes is still running. */
  running?: boolean;
  /** When that sync began (server clock). The poll waits for THIS run. */
  started_at?: string | null;
}

/** How often the page asks whether the sync has ended, and for how long. */
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

/**
 * Sync now. `POST /api/v1/propertyware/sync` starts the sync (or joins the one
 * already going) and answers at once; `GET /api/v1/propertyware/sync` is then
 * asked until THAT run ends, and its answer is returned. Never one request for
 * the whole sync: a sync takes minutes and `apiFetch` stops waiting at 45 s
 * (gotcha 199). Admin-only; a 403 or 503 on the start throws ApiError.
 */
export async function runPropertywareSync({
  pollMs = SYNC_POLL_MS,
  maxWaitMs = SYNC_MAX_WAIT_MS,
}: { pollMs?: number; maxWaitMs?: number } = {}): Promise<PropertywareSyncResult> {
  const startRes = await apiFetch('/api/v1/propertyware/sync', { method: 'POST' });
  const start = (await startRes.json()) as PropertywareSyncResult;
  // Refused (no connection saved) or ended at once: that answer is the result.
  if (!start.running || !start.started_at) return start;

  const deadline = Date.now() + maxWaitMs;
  let failures = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    let now: PropertywareSyncResult;
    try {
      now = (await (await apiFetch('/api/v1/propertyware/sync')).json()) as PropertywareSyncResult;
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

/** GET /api/v1/propertyware/credentials. Admin-only; a 403 throws ApiError. */
export async function getPropertywareStatus(): Promise<PropertywareStatus> {
  const res = await apiFetch('/api/v1/propertyware/credentials');
  return res.json();
}

/**
 * PUT /api/v1/propertyware/credentials. Throws ApiError with the backend's own
 * sentence: 503 connector switched off, 409 workspace already on another
 * system, 422 a value that can't be used (it never repeats the value).
 */
export async function savePropertywareCreds(
  input: PropertywareCredentialsInput,
): Promise<PropertywareStatus> {
  const body: PropertywareCredentialsInput = {
    system_id: input.system_id,
    client_id: input.client_id,
    client_secret: input.client_secret,
  };
  const res = await apiFetch('/api/v1/propertyware/credentials', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return res.json();
}

/** POST /api/v1/propertyware/test. A refusal from Propertyware is `ok: false`
 *  with a sentence, not a throw; a pass also starts the first sync. */
export async function testPropertywareConnection(): Promise<PropertywareTestResult> {
  const res = await apiFetch('/api/v1/propertyware/test', { method: 'POST' });
  return res.json();
}

/** DELETE /api/v1/propertyware/credentials: removes the keys AND deletes the
 *  synced Propertyware data. It does not delete the key inside Propertyware. */
export async function deletePropertywareCreds(): Promise<PropertywareStatus> {
  const res = await apiFetch('/api/v1/propertyware/credentials', { method: 'DELETE' });
  return res.json();
}
