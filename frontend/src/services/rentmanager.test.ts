/**
 * The Rent Manager service: the names it sends and reads, and Sync now's poll.
 *
 * The names are the onboarding wizard's contract too (ADR 0003): the same
 * lists are pinned server-side in tests/unit/test_rentmanager_routes.py
 * (`WIRE`), which also reads the interfaces in rentmanager.ts against the
 * route's models. A rename on one side only fails one of the two.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  SYNC_LOST,
  SYNC_STILL_RUNNING,
  SYNC_UNREACHABLE,
  deleteRentManagerCreds,
  getRentManagerStatus,
  runRentManagerSync,
  saveRentManagerCreds,
  testRentManagerConnection,
  type RentManagerStatus,
  type RentManagerSyncResult,
  type RentManagerTestResult,
} from './rentmanager';

const fetchMock = vi.mocked(apiFetch);

/** The wire names, exactly as the route's models declare them. */
const INPUT_NAMES = ['corp_id', 'location_id', 'partner_token', 'password', 'username'];
const STATUS_NAMES = [
  'configured',
  'connector_enabled',
  'corp_id',
  'last_tested_at',
  'location_id',
  'partner_token_saved',
  'purged_rows',
  'username_last4',
];
const TEST_NAMES = ['error', 'ok', 'properties_count'];
const SYNC_NAMES = ['error', 'ok', 'rows', 'running', 'stale', 'started_at'];

function answer(body: unknown): Response {
  return { json: async () => body } as Response;
}

/** The JSON body of the n-th call. */
function sent(n = 0): Record<string, unknown> {
  const init = fetchMock.mock.calls[n][1] as RequestInit;
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

describe('the names rentmanager.ts sends and reads', () => {
  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(answer({ configured: true }));
  });

  it('saves with exactly the contract names, the location as a number', async () => {
    await saveRentManagerCreds({
      corp_id: 'synthcorp',
      location_id: 1,
      username: 'rm-user',
      password: 'rm-password',
      partner_token: '  rm-partner  ',
    });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/rentmanager/credentials');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('PUT');
    const body = sent();
    expect(Object.keys(body).sort()).toEqual(INPUT_NAMES);
    expect(body.location_id).toBe(1);
    expect(body.partner_token).toBe('rm-partner');
  });

  it.each([undefined, '', '   '])('leaves a blank partner token out (%j): absent is none', async (token) => {
    await saveRentManagerCreds({
      corp_id: 'synthcorp',
      location_id: 1,
      username: 'rm-user',
      password: 'rm-password',
      partner_token: token,
    });
    expect(Object.keys(sent()).sort()).toEqual(INPUT_NAMES.filter((n) => n !== 'partner_token'));
  });

  it('declares the status, test and sync answers name for name', async () => {
    // `Required<>` makes tsc refuse a missing name; the literal refuses an extra one.
    const status: Required<RentManagerStatus> = {
      configured: true,
      corp_id: 'synthcorp',
      location_id: 1,
      username_last4: 'user',
      partner_token_saved: false,
      last_tested_at: null,
      purged_rows: null,
      connector_enabled: true,
    };
    const test: Required<RentManagerTestResult> = { ok: true, properties_count: null, error: null };
    const sync: Required<RentManagerSyncResult> = {
      ok: true,
      rows: {},
      stale: [],
      error: null,
      running: false,
      started_at: null,
    };
    expect(Object.keys(status).sort()).toEqual(STATUS_NAMES);
    expect(Object.keys(test).sort()).toEqual(TEST_NAMES);
    expect(Object.keys(sync).sort()).toEqual(SYNC_NAMES);

    fetchMock.mockResolvedValueOnce(answer(status));
    expect(await getRentManagerStatus()).toEqual(status);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/rentmanager/credentials');
  });

  it('tests and disconnects on the routes the backend serves', async () => {
    fetchMock.mockResolvedValueOnce(answer({ ok: true, properties_count: 2, error: null }));
    expect(await testRentManagerConnection()).toEqual({ ok: true, properties_count: 2, error: null });
    await deleteRentManagerCreds();
    expect(fetchMock.mock.calls.map((c) => [c[0], (c[1] as RequestInit).method])).toEqual([
      ['/api/v1/rentmanager/test', 'POST'],
      ['/api/v1/rentmanager/credentials', 'DELETE'],
    ]);
  });
});

// ── Sync now: start, then ask until THAT run ends (gotcha 199) ─────────

const STARTED = '2026-10-05T09:14:02+00:00';
const FAST = { pollMs: 1, maxWaitMs: 2_000 };
const running = { ok: true, running: true, started_at: STARTED };

/** The POST's answer, then one GET answer (or thrown error) per poll. */
function serve(start: RentManagerSyncResult, polls: Array<RentManagerSyncResult | Error>) {
  fetchMock.mockImplementation(async (_path, init) => {
    if (init?.method === 'POST') return answer(start);
    const next = polls.length > 1 ? polls.shift()! : polls[0];
    if (next instanceof Error) throw next;
    return answer(next);
  });
}

describe('runRentManagerSync', () => {
  // ⚠ A block body: `mockReset()` returns the mock, and a function returned
  // from beforeEach is run as the test's teardown.
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('starts the sync, then reports how THAT run ended', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 3 }, stale: [] };
    serve(running, [running, running, done]);
    expect(await runRentManagerSync(FAST)).toEqual(done);
    expect(fetchMock.mock.calls[0]).toEqual(['/api/v1/rentmanager/sync', { method: 'POST' }]);
    expect(fetchMock.mock.calls.slice(1).map((c) => c[0])).toEqual([
      '/api/v1/rentmanager/sync',
      '/api/v1/rentmanager/sync',
      '/api/v1/rentmanager/sync',
    ]);
  });

  it('waits between checks rather than asking back to back', async () => {
    vi.useFakeTimers();
    try {
      const done = { ok: true, running: false, started_at: STARTED, rows: {} };
      serve(running, [done]);
      const pending = runRentManagerSync({ pollMs: 5_000, maxWaitMs: 60_000 });
      await vi.advanceTimersByTimeAsync(4_999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(await pending).toEqual(done);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a refused start is the result, and nothing is polled', async () => {
    const refused = { ok: false, error: 'No Rent Manager connection is saved for this workspace.' };
    serve(refused, [running]);
    expect(await runRentManagerSync(FAST)).toEqual(refused);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a different run, or none, means ours is gone: it says so', async () => {
    serve(running, [{ ok: true, running: false, started_at: '2026-10-05T10:00:00+00:00' }]);
    expect(await runRentManagerSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });
    fetchMock.mockReset();
    serve(running, [{ ok: false, running: false, started_at: null, error: 'no record' }]);
    expect(await runRentManagerSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });
  });

  it('counts failed checks in a row, not in total', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 3 } };
    serve(running, [new Error('blip'), new Error('blip'), running, new Error('blip'), done]);
    expect(await runRentManagerSync(FAST)).toEqual(done);
  });

  it('stops after three failed checks in a row, without calling the sync failed', async () => {
    serve(running, [new Error('down')]);
    expect(await runRentManagerSync(FAST)).toEqual({ ok: false, error: SYNC_UNREACHABLE });
    expect(fetchMock).toHaveBeenCalledTimes(1 + 3);
  });

  it('gives up waiting, but says the sync is still running', async () => {
    serve(running, [running]);
    expect(await runRentManagerSync({ pollMs: 1, maxWaitMs: 30 })).toEqual({
      ok: false,
      running: true,
      started_at: STARTED,
      error: SYNC_STILL_RUNNING,
    });
  });
});
