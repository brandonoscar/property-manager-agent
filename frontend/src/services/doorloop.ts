/**
 * DoorLoop calls the web app makes, for the Settings → Connectors panel
 * (`DoorLoopApiDetail`).
 *
 * Mirrors the backend `/api/v1/doorloop/*` surface
 * (`src/helixis/api/routes/doorloop.py`). Reads return masked metadata only;
 * the key never comes back from the API.
 *
 * A passed test starts the first sync, and Sync now reads DoorLoop again
 * (design/doorloop-connector.md, slice A1). There are no demo-mode answers:
 * the demo workspace lists no DoorLoop card, so nothing in demo mode can reach
 * these.
 */

import { apiFetch } from './api';

/** The shortest key the server accepts; the form waits for it so a cut-off
 *  paste is caught before the save. The server's own bound is the authority,
 *  and a test keeps the two equal. */
export const MIN_API_KEY_LENGTH = 16;

/** Where a DoorLoop user makes a key, and the plan it needs. The server's
 *  refusals say the same (`_WHERE_THE_KEY_IS`). */
export const DOORLOOP_KEY_WHERE =
  "In DoorLoop, open Settings → Zapier & API Keys and create a key. API keys need DoorLoop's Premium plan.";

/** `PUT /api/v1/doorloop/credentials` (`DoorLoopCredentialsInput`). */
export interface DoorLoopCredentialsInput {
  api_key: string;
}

/** The masked read (`DoorLoopCredentialsStatus`). */
export interface DoorLoopStatus {
  configured: boolean;
  api_key_last4: string | null;
  /** Set only by a PASSED test; cleared whenever the key is saved. */
  last_tested_at: string | null;
  /** Set only on the disconnect response. */
  purged_rows?: number | null;
  /** Whether this deployment accepts a DoorLoop connection at all (GET only). */
  connector_enabled?: boolean | null;
}

/** `POST /api/v1/doorloop/test` (`DoorLoopTestResult`). */
export interface DoorLoopTestResult {
  ok: boolean;
  /** DoorLoop's own count of properties, when its answer states one. */
  properties_count: number | null;
  /** A sentence for the customer, written server-side per failure kind. */
  error: string | null;
}

/** `POST` and `GET /api/v1/doorloop/sync` (`DoorLoopSyncResult`). */
export interface DoorLoopSyncResult {
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

/** How often the page asks whether the sync has ended, and for how long. A
 *  pass is paced under DoorLoop's limits and can take about ten minutes. */
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
 * Sync now, and what Operations → Refresh calls for a DoorLoop workspace.
 * `POST /api/v1/doorloop/sync` starts the sync (or joins the one already
 * going) and answers at once; `GET /api/v1/doorloop/sync` is then asked until
 * THAT run ends, and its answer is returned. Never one request for the whole
 * sync: a sync takes minutes and `apiFetch` stops waiting at 45 s (gotcha
 * 199). Admin-only; a 403 or 503 on the start throws ApiError.
 */
export async function runDoorLoopSync({
  pollMs = SYNC_POLL_MS,
  maxWaitMs = SYNC_MAX_WAIT_MS,
}: { pollMs?: number; maxWaitMs?: number } = {}): Promise<DoorLoopSyncResult> {
  const startRes = await apiFetch('/api/v1/doorloop/sync', { method: 'POST' });
  const start = (await startRes.json()) as DoorLoopSyncResult;
  // Refused (no connection saved) or ended at once: that answer is the result.
  if (!start.running || !start.started_at) return start;

  const deadline = Date.now() + maxWaitMs;
  let failures = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    let now: DoorLoopSyncResult;
    try {
      now = (await (await apiFetch('/api/v1/doorloop/sync')).json()) as DoorLoopSyncResult;
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

/** GET /api/v1/doorloop/credentials. Admin-only; a 403 throws ApiError. */
export async function getDoorLoopStatus(): Promise<DoorLoopStatus> {
  const res = await apiFetch('/api/v1/doorloop/credentials');
  return res.json();
}

/**
 * PUT /api/v1/doorloop/credentials. Throws ApiError with the backend's own
 * sentence: 503 connector switched off, 409 workspace already on another
 * system, 422 a key that can't be used (it never repeats the key).
 */
export async function saveDoorLoopCreds(input: DoorLoopCredentialsInput): Promise<DoorLoopStatus> {
  const body: DoorLoopCredentialsInput = { api_key: input.api_key };
  const res = await apiFetch('/api/v1/doorloop/credentials', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return res.json();
}

/** POST /api/v1/doorloop/test. A refusal from DoorLoop is `ok: false` with a
 *  sentence, not a throw; a pass also starts the first sync. */
export async function testDoorLoopConnection(): Promise<DoorLoopTestResult> {
  const res = await apiFetch('/api/v1/doorloop/test', { method: 'POST' });
  return res.json();
}

/** DELETE /api/v1/doorloop/credentials: removes the key AND deletes any
 *  DoorLoop data in Occupella. It does not delete the key inside DoorLoop. */
export async function deleteDoorLoopCreds(): Promise<DoorLoopStatus> {
  const res = await apiFetch('/api/v1/doorloop/credentials', { method: 'DELETE' });
  return res.json();
}
