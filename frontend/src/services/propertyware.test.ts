/**
 * The Propertyware calls, as they go over the wire.
 *
 * The field names are a contract with the server's models
 * (`src/helixis/api/routes/propertyware.py`) and with the onboarding wizard,
 * which is built against the same names (ADR 0003).
 * `tests/unit/test_propertyware_routes.py` reads this file's interfaces from
 * the other side (rule 15).
 *
 * Sync now starts the sync, then asks until THAT run ends: one sync takes
 * minutes and apiFetch stops waiting at 45 s (gotcha 199).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  SYNC_LOST,
  SYNC_STILL_RUNNING,
  SYNC_UNREACHABLE,
  deletePropertywareCreds,
  getPropertywareStatus,
  runPropertywareSync,
  savePropertywareCreds,
  testPropertywareConnection,
  type PropertywareCredentialsInput,
  type PropertywareSyncResult,
} from './propertyware';

const fetchMock = vi.mocked(apiFetch);
const STARTED = '2026-10-05T14:02:11+00:00';
const FAST = { pollMs: 1, maxWaitMs: 2_000 };

function answer(body: unknown): Response {
  return { json: async () => body } as Response;
}

describe('the credential calls', () => {
  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(answer({ configured: true }));
  });

  it('saves with exactly the field names the server reads, and nothing else', async () => {
    const input = {
      system_id: '990000001',
      client_id: 'pw-client-id-0001',
      client_secret: 'pw-secret-12345678',
      // A stray field a caller passed must not reach the server.
      account_code: 'not-ours',
    } as PropertywareCredentialsInput;
    await savePropertywareCreds(input);

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/v1/propertyware/credentials');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({
      system_id: '990000001',
      client_id: 'pw-client-id-0001',
      client_secret: 'pw-secret-12345678',
    });
  });

  it('reads, tests and disconnects on the routes the server serves', async () => {
    await getPropertywareStatus();
    await testPropertywareConnection();
    await deletePropertywareCreds();
    expect(fetchMock.mock.calls.map(([path, init]) => [path, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/propertyware/credentials', 'GET'],
      ['/api/v1/propertyware/test', 'POST'],
      ['/api/v1/propertyware/credentials', 'DELETE'],
    ]);
  });
});

/** The POST's answer, then one GET answer (or thrown error) per poll. */
function serve(start: PropertywareSyncResult, polls: Array<PropertywareSyncResult | Error>) {
  fetchMock.mockImplementation(async (_path, init) => {
    if (init?.method === 'POST') return answer(start);
    const next = polls.length > 1 ? polls.shift()! : polls[0];
    if (next instanceof Error) throw next;
    return answer(next);
  });
}

const running = { ok: true, running: true, started_at: STARTED };

describe('runPropertywareSync', () => {
  // ⚠ A block body: `mockReset()` returns the mock, and a function returned
  // from beforeEach is run as the test's teardown, calling the mock again.
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('starts the sync, then reports how THAT run ended', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 4 }, stale: [] };
    serve(running, [running, running, done]);

    const result = await runPropertywareSync(FAST);

    expect(result).toEqual(done);
    expect(fetchMock.mock.calls[0]).toEqual(['/api/v1/propertyware/sync', { method: 'POST' }]);
    expect(fetchMock.mock.calls.slice(1).map((c) => c[0])).toEqual([
      '/api/v1/propertyware/sync',
      '/api/v1/propertyware/sync',
      '/api/v1/propertyware/sync',
    ]);
  });

  it('waits between checks rather than asking back to back', async () => {
    vi.useFakeTimers();
    try {
      const done = { ok: true, running: false, started_at: STARTED, rows: {} };
      serve(running, [done]);
      const pending = runPropertywareSync({ pollMs: 5_000, maxWaitMs: 60_000 });
      await vi.advanceTimersByTimeAsync(4_999);
      expect(fetchMock).toHaveBeenCalledTimes(1); // the POST, nothing else yet
      await vi.advanceTimersByTimeAsync(1);
      expect(await pending).toEqual(done);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a refused start is the result, and nothing is polled', async () => {
    const refused = { ok: false, error: 'No Propertyware connection is saved for this workspace.' };
    serve(refused, [running]);
    expect(await runPropertywareSync(FAST)).toEqual(refused);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a stale table comes back with the server’s sentence, untouched', async () => {
    const stale = {
      ok: false,
      running: false,
      started_at: STARTED,
      rows: { units: 4 },
      stale: ['vendors'],
      error: "Some of Propertyware didn't sync this time. Vendors: Sync again in a few minutes.",
    };
    serve(running, [stale]);
    expect(await runPropertywareSync(FAST)).toEqual(stale);
  });

  it('a different run, or none, means ours is gone: it says so rather than reporting it', async () => {
    serve(running, [{ ok: true, running: false, started_at: '2026-10-05T15:00:00+00:00' }]);
    expect(await runPropertywareSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });

    fetchMock.mockReset();
    serve(running, [{ ok: false, running: false, started_at: null, error: 'no record' }]);
    expect(await runPropertywareSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });
  });

  it('counts failed checks in a row, not in total', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 4 } };
    serve(running, [new Error('blip'), new Error('blip'), running, new Error('blip'), done]);
    expect(await runPropertywareSync(FAST)).toEqual(done);
  });

  it('stops after three failed checks in a row, without calling the sync failed', async () => {
    serve(running, [new Error('down')]);
    expect(await runPropertywareSync(FAST)).toEqual({ ok: false, error: SYNC_UNREACHABLE });
    expect(fetchMock).toHaveBeenCalledTimes(1 + 3);
  });

  it('gives up waiting, but says the sync is still running', async () => {
    serve(running, [running]);
    expect(await runPropertywareSync({ pollMs: 1, maxWaitMs: 30 })).toEqual({
      ok: false,
      running: true,
      started_at: STARTED,
      error: SYNC_STILL_RUNNING,
    });
  });
});
