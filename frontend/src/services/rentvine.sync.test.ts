/**
 * runRentvineSync — Sync now starts the sync, then asks until THAT run ends.
 *
 * Measured 2026-10-03: one sandbox sync took 87 s on the server, and apiFetch
 * stops waiting at 45 s. As one request, every sync that worked was reported
 * as "timed out… try again" (gotcha 199). The server side is
 * tests/unit/test_rentvine_routes.py.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  runRentvineSync,
  SYNC_LOST,
  SYNC_STILL_RUNNING,
  SYNC_UNREACHABLE,
  type RentvineSyncResult,
} from './rentvine';

const fetchMock = vi.mocked(apiFetch);
const STARTED = '2026-10-03T22:44:49+00:00';
const FAST = { pollMs: 1, maxWaitMs: 2_000 };

function answer(body: RentvineSyncResult): Response {
  return { json: async () => body } as Response;
}

/** The POST's answer, then one GET answer (or thrown error) per poll. */
function serve(start: RentvineSyncResult, polls: Array<RentvineSyncResult | Error>) {
  fetchMock.mockImplementation(async (_path, init) => {
    if (init?.method === 'POST') return answer(start);
    const next = polls.length > 1 ? polls.shift()! : polls[0];
    if (next instanceof Error) throw next;
    return answer(next);
  });
}

const running = { ok: true, running: true, started_at: STARTED };

describe('runRentvineSync', () => {
  // ⚠ A block body: `mockReset()` returns the mock, and a function returned
  // from beforeEach is run as the test's teardown, calling the mock again.
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('starts the sync, then reports how THAT run ended', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 157 }, stale: [] };
    serve(running, [running, running, done]);

    const result = await runRentvineSync(FAST);

    expect(result).toEqual(done);
    expect(fetchMock.mock.calls[0]).toEqual(['/api/v1/rentvine/sync', { method: 'POST' }]);
    // Three checks: two while running, one that saw it end.
    expect(fetchMock.mock.calls.slice(1).map((c) => c[0])).toEqual([
      '/api/v1/rentvine/sync',
      '/api/v1/rentvine/sync',
      '/api/v1/rentvine/sync',
    ]);
  });

  it('waits between checks rather than asking back to back', async () => {
    vi.useFakeTimers();
    try {
      const done = { ok: true, running: false, started_at: STARTED, rows: {} };
      serve(running, [done]);
      const pending = runRentvineSync({ pollMs: 5_000, maxWaitMs: 60_000 });
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
    const refused = { ok: false, error: 'No Rentvine connection is saved for this workspace.' };
    serve(refused, [running]);

    expect(await runRentvineSync(FAST)).toEqual(refused);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('a different run, or none, means ours is gone: it says so rather than reporting it', async () => {
    serve(running, [{ ok: true, running: false, started_at: '2026-10-03T23:00:00+00:00' }]);
    expect(await runRentvineSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });

    fetchMock.mockReset();
    serve(running, [{ ok: false, running: false, started_at: null, error: 'no record' }]);
    expect(await runRentvineSync(FAST)).toEqual({ ok: false, error: SYNC_LOST });
  });

  it('rides out a failed check or two', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 157 } };
    serve(running, [new Error('blip'), new Error('blip'), done]);
    expect(await runRentvineSync(FAST)).toEqual(done);
  });

  it('counts failed checks in a row, not in total', async () => {
    const done = { ok: true, running: false, started_at: STARTED, rows: { units: 157 } };
    serve(running, [new Error('blip'), new Error('blip'), running, new Error('blip'), done]);
    expect(await runRentvineSync(FAST)).toEqual(done);
  });

  it('stops after three failed checks in a row, without calling the sync failed', async () => {
    serve(running, [new Error('down')]);
    const result = await runRentvineSync(FAST);
    expect(result).toEqual({ ok: false, error: SYNC_UNREACHABLE });
    expect(fetchMock).toHaveBeenCalledTimes(1 + 3);
  });

  it('gives up waiting, but says the sync is still running', async () => {
    serve(running, [running]);
    const result = await runRentvineSync({ pollMs: 1, maxWaitMs: 30 });
    expect(result).toEqual({
      ok: false,
      running: true,
      started_at: STARTED,
      error: SYNC_STILL_RUNNING,
    });
  });
});
