/**
 * Rentvine calls the web app makes: the Settings → Connectors panel
 * (`RentvineApiDetail`), the Operations page's Re-sync (the Rentvine twin of
 * `runBuildiumBackfill`) and the Getting started checklist. The onboarding
 * wizard (helixis-onboarding) connects through the same routes.
 *
 * Mirrors the backend `/api/v1/rentvine/*` surface. Reads return masked
 * metadata only; a secret never round-trips through the API.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';
import { backendOrigin } from './backendOrigin';

export interface RentvineSyncResult {
  ok: boolean;
  /** Rows written per record type, e.g. `{ units: 37 }`. */
  rows?: Record<string, number>;
  /** Record types whose fetch failed: last good sync on screen, not empty. */
  stale?: string[];
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
 * Sync now. `POST /api/v1/rentvine/sync` starts the sync (or joins the one
 * already going) and answers at once; `GET /api/v1/rentvine/sync` is then asked
 * until THAT run ends, and its answer is returned.
 *
 * ⚠ Never one request for the whole sync. Measured 2026-10-03: one sandbox
 * sync took 87 s on the server, and `apiFetch` stops waiting at 45 s, so every
 * sync that worked was reported as "timed out… try again" (gotcha 199).
 * Admin-only; a 403 on the start throws ApiError.
 */
export async function runRentvineSync({
  pollMs = SYNC_POLL_MS,
  maxWaitMs = SYNC_MAX_WAIT_MS,
}: { pollMs?: number; maxWaitMs?: number } = {}): Promise<RentvineSyncResult> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 900));
    return { ok: true, rows: { properties: 12, units: 37, leases: 8 }, stale: [] };
  }
  const startRes = await apiFetch('/api/v1/rentvine/sync', { method: 'POST' });
  const start = (await startRes.json()) as RentvineSyncResult;
  // Refused (no connection saved, connector off): that answer is the result.
  if (!start.running || !start.started_at) return start;

  const deadline = Date.now() + maxWaitMs;
  let failures = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    let now: RentvineSyncResult;
    try {
      now = (await (await apiFetch('/api/v1/rentvine/sync')).json()) as RentvineSyncResult;
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

export interface RentvineCredentialStatus {
  configured: boolean;
  /** When the saved keys last passed a test call, or null if never. */
  lastTestedAt: string | null;
}

/**
 * GET /api/v1/rentvine/credentials, reduced to what the Getting started
 * checklist reads. Admin-only on the backend; a member's 403 throws, and the
 * checklist treats a failed probe as "not done yet".
 */
export async function fetchRentvineCredentialStatus(): Promise<RentvineCredentialStatus> {
  const res = await apiFetch('/api/v1/rentvine/credentials');
  const body = (await res.json()) as { configured?: boolean; last_tested_at?: string | null };
  return { configured: Boolean(body.configured), lastTestedAt: body.last_tested_at ?? null };
}

/** The masked read the backend returns (`RentvineCredentialsStatus`). */
export interface RentvineStatus {
  configured: boolean;
  /** The subdomain before `.rentvine.com`. Not a secret. */
  account_code: string | null;
  api_key_last4: string | null;
  /** Set only by a SUCCESSFUL test; cleared whenever the keys change. */
  last_tested_at: string | null;
  /** Whether this deployment accepts a Rentvine key at all (GET only). */
  connector_enabled?: boolean | null;
  /** Set only on the disconnect response. */
  purged_rows?: number | null;
  /** Whether a webhook signing key is stored. The key itself never comes back. */
  webhook_secret_saved?: boolean;
  /** Whether this deployment can check a Rentvine signature at all (GET only).
   *  Until it can, every delivery is refused, so only `true` means ready. */
  webhook_scheme_ready?: boolean | null;
}

export interface RentvineCredentialsInput {
  api_key: string;
  api_secret: string;
  /** Optional: the backend works it out from the keys, and answers 422
   *  asking for it only when it cannot. */
  account_code?: string;
}

export interface RentvineTestResult {
  ok: boolean;
  /** The account's property count, when Rentvine states one. */
  properties_count: number | null;
  /** A sentence for the customer, written server-side per failure kind. */
  error: string | null;
}

const DEMO_STATUS: RentvineStatus = {
  configured: true,
  account_code: 'occupella-demo',
  api_key_last4: '7f3a',
  last_tested_at: new Date(Date.now() - 6 * 60_000).toISOString(),
  connector_enabled: true,
  webhook_secret_saved: false,
  webhook_scheme_ready: true,
};

/**
 * The eight events the webhook receiver handles (`rentvine/events.py`: four
 * record types, created and updated), named as Rentvine's event menu shows
 * them. Rentvine takes one event per webhook.
 */
export const RENTVINE_WEBHOOK_EVENTS = [
  'Property Created',
  'Property Updated',
  'Unit Created',
  'Unit Updated',
  'Lease Created',
  'Lease Updated',
  'Work Order Created',
  'Work Order Updated',
] as const;

/** The URL to paste into each Rentvine webhook. The account code is in the
 *  path because a delivery carries no company context of its own. */
export function rentvineWebhookUrl(accountCode: string): string {
  return `${backendOrigin()}/webhooks/rentvine/${accountCode}`;
}

/**
 * PUT /api/v1/rentvine/webhook-secret. Throws ApiError with the backend's own
 * sentence: 422 not a whole key, 409 no connection saved, 503 connector off.
 */
export async function saveRentvineWebhookSecret(secret: string): Promise<RentvineStatus> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 400));
    return { ...DEMO_STATUS, webhook_secret_saved: true };
  }
  const res = await apiFetch('/api/v1/rentvine/webhook-secret', {
    method: 'PUT',
    body: JSON.stringify({ secret }),
  });
  return res.json();
}

/** DELETE /api/v1/rentvine/webhook-secret: forgets the signing key and keeps
 *  the connection. Every delivery is refused until a key is saved again. */
export async function deleteRentvineWebhookSecret(): Promise<RentvineStatus> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 300));
    return { ...DEMO_STATUS, webhook_secret_saved: false };
  }
  const res = await apiFetch('/api/v1/rentvine/webhook-secret', { method: 'DELETE' });
  return res.json();
}

/** GET /api/v1/rentvine/credentials. Admin-only; a 403 throws ApiError. */
export async function getRentvineStatus(): Promise<RentvineStatus> {
  if (demoMode) return DEMO_STATUS;
  const res = await apiFetch('/api/v1/rentvine/credentials');
  return res.json();
}

/**
 * PUT /api/v1/rentvine/credentials. Throws ApiError with the backend's own
 * sentence: 503 connector off, 409 workspace already on Buildium, 422 the
 * account code is needed.
 */
export async function saveRentvineCreds(input: RentvineCredentialsInput): Promise<RentvineStatus> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 500));
    return { ...DEMO_STATUS, last_tested_at: null };
  }
  const body: RentvineCredentialsInput = { api_key: input.api_key, api_secret: input.api_secret };
  if (input.account_code) body.account_code = input.account_code;
  const res = await apiFetch('/api/v1/rentvine/credentials', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
  return res.json();
}

/** POST /api/v1/rentvine/test. A refusal from Rentvine is `ok: false` with
 *  a sentence, not a throw; a success also starts the first sync. */
export async function testRentvineConnection(): Promise<RentvineTestResult> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 600));
    return { ok: true, properties_count: 12, error: null };
  }
  const res = await apiFetch('/api/v1/rentvine/test', { method: 'POST' });
  return res.json();
}

/** DELETE /api/v1/rentvine/credentials: removes the keys AND deletes the
 *  synced Rentvine data. It does not revoke the key inside Rentvine. */
export async function deleteRentvineCreds(): Promise<RentvineStatus> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 400));
    return { configured: false, account_code: null, api_key_last4: null, last_tested_at: null };
  }
  const res = await apiFetch('/api/v1/rentvine/credentials', { method: 'DELETE' });
  return res.json();
}
