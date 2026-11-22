/**
 * The DoorLoop calls, as they go over the wire.
 *
 * The field names are a contract with the server's models
 * (`src/helixis/api/routes/doorloop.py`), and `tests/unit/test_doorloop_routes.py`
 * reads this file's interfaces and constants from the other side (rule 15).
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
  deleteDoorLoopCreds,
  getDoorLoopStatus,
  runDoorLoopSync,
  saveDoorLoopCreds,
  testDoorLoopConnection,
  type DoorLoopCredentialsInput,
  type DoorLoopSyncResult,
} from './doorloop';

const fetchMock = vi.mocked(apiFetch);
const STARTED = '2026-10-06T14:02:11+00:00';
const FAST = { pollMs: 1, maxWaitMs: 2_000 };

function answer(body: unknown): Response {
  return { json: async () => body } as Response;
}

describe('the credential calls', () => {
  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(answer({ configured: true }));
  });

  it('saves with exactly the field name the server reads, and nothing else', async () => {
    const input = {
      api_key: 'dl-synthetic-key-0000000000000001',
      // A stray field a caller passed must not reach the server.
      client_secret: 'not-ours',
    } as DoorLoopCredentialsInput;
    await saveDoorLoopCreds(input);

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/v1/doorloop/credentials');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({ api_key: 'dl-synthetic-key-0000000000000001' });
  });

  it('reads, tests and disconnects on the server’s routes', async () => {
    await getDoorLoopStatus();
    await testDoorLoopConnection();
    await deleteDoorLoopCreds();
    expect(fetchMock.mock.calls.map(([path, init]) => [path, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/doorloop/credentials', 'GET'],
      ['/api/v1/doorloop/test', 'POST'],
      ['/api/v1/doorloop/credentials', 'DELETE'],
    ]);
  });
});

/** The POST's answer, then one GET answer (or thrown error) per poll. */
function serve(start: DoorLoopSyncResult, polls: Array<DoorLoopSyncResult | Error>) {
  fetchMock.mockImplementation(async (_path, init) => {
    if (init?.method === 'POST') return answer(start);
    const next = polls.length > 1 ? polls.shift()! : polls[0];
    if (next instanceof Error) throw next;
    return answer(next);
  });
}

const running = { ok: true, running: true, started_at: STARTED };

// Re-pointed from "Refresh for a DoorLoop workspace says why there is nothing
// to refresh, and sends no request": since slice A1 there is a sync, and
// Refresh and Sync now both run it.
describe('runDoorLoopSync', () => {
  // ⚠ A block body: `mockReset()` returns the mock, and a function returned
  // from beforeEach is run as the test's teardown, calling the mock again.
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('starts the sync, then reports how THAT run ended', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 4 }, stale: [] };
    serve(running, [running, running, done]);

    const result = await runDoorLoopSync(FAST);

    expect(result).toEqual(done);
    expect(fetchMock.mock.calls[0]).toEqual(['/api/v1/doorloop/sync', { method: 'POST' }]);
    expect(fetchMock.mock.calls.slice(1).map((c) => c[0])).toEqual([
      '/api/v1/doorloop/sync',
      '/api/v1/doorloop/sync',
      '/api/v1/doorloop/sync',
    ]);
  });

  it('waits between checks rather than asking back to back', async () => {
    vi.useFakeTimers();
    try {
      const done = { ok: true, running: false, started_at: STARTED, rows: {} };
      serve(running, [done]);
      const pending = runDoorLoopSync({ pollMs: 5_000, maxWaitMs: 60_000 });
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
    const refused = { ok: false, error: 'No DoorLoop connection is saved for this workspace.' };
    serve(refused, [running]);
    expect(await runDoorLoopSync(FAST)).toEqual(refused);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a stale table comes back with the server’s sentence, untouched', async () => {
    const stale = {
      ok: false,
      running: false,
      started_at: STARTED,
      rows: { units: 4 },
      stale: ['vendors'],
      error: "Some of DoorLoop didn't sync this time. Vendors: Sync again in a few minutes.",
    };
    serve(running, [stale]);
    expect(await runDoorLoopSync(FAST)).toEqual(stale);
  });

  it('a different run, or none, means ours is gone: it says so rather than reporting it', async () => {
    serve(running, [{ ok: true, running: false, started_at: '2026-10-06T15:00:00+00:00' }]);
    expect(await runDoorLoopSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });

    fetchMock.mockReset();
    serve(running, [{ ok: false, running: false, started_at: null, error: 'no record' }]);
    expect(await runDoorLoopSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });
  });

  it('counts failed checks in a row, not in total', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 4 } };
    serve(running, [new Error('blip'), new Error('blip'), running, new Error('blip'), done]);
    expect(await runDoorLoopSync(FAST)).toEqual(done);
  });

  it('stops after three failed checks in a row, without calling the sync failed', async () => {
    serve(running, [new Error('down')]);
    expect(await runDoorLoopSync(FAST)).toEqual({ ok: false, error: SYNC_UNREACHABLE });
    expect(fetchMock).toHaveBeenCalledTimes(1 + 3);
  });

  it('gives up waiting, but says the sync is still running', async () => {
    serve(running, [running]);
    expect(await runDoorLoopSync({ pollMs: 1, maxWaitMs: 30 })).toEqual({
      ok: false,
      running: true,
      started_at: STARTED,
      error: SYNC_STILL_RUNNING,
    });
  });
});
