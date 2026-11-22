/**
 * `apiFetch` is the only place that sees every backend call, so it is the
 * only place that can tell whether the backend is there.
 *
 * ⚠ These tests exist because `serviceHealth.test.ts` structurally cannot
 * see this. Those tests drive `reportServiceFailure` directly and would pass
 * forever against an `apiFetch` that never calls it — the "complete, tested,
 * and wired to nothing" failure this repo has shipped before. What is
 * asserted here is the WIRING: a real call, a real outcome, the health
 * state that results.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './api';
import {
  CONSECUTIVE_FAILURES_FOR_OUTAGE,
  getServiceHealth,
  resetServiceHealth,
} from './serviceHealth';

vi.mock('./auth', () => ({ getAccessToken: vi.fn() }));

import { getAccessToken } from './auth';

const mockedGetAccessToken = vi.mocked(getAccessToken);

/** Drive `apiFetch` to the bar, swallowing the errors it raises. */
async function callUntilOutageBar(): Promise<void> {
  for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
    await apiFetch('/api/v1/health').catch(() => undefined);
  }
}

beforeEach(() => {
  resetServiceHealth();
  mockedGetAccessToken.mockResolvedValue('token');
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('apiFetch reports what it saw', () => {
  it('a failed connection, repeated, marks the service down', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'));
    await callUntilOutageBar();
    expect(getServiceHealth().down).toBe(true);
  });

  it('a 503, repeated, marks the service down', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('', { status: 503 }));
    await callUntilOutageBar();
    expect(getServiceHealth().down).toBe(true);
  });

  it('a 404 does NOT — the backend answered', async () => {
    // The whole distinction the banner rests on. A served 4xx is proof a
    // process was there; treating it as an outage would put the banner up
    // during ordinary use.
    vi.mocked(fetch).mockResolvedValue(new Response('', { status: 404 }));
    await callUntilOutageBar();
    expect(getServiceHealth().down).toBe(false);
  });

  it('a 500 does NOT — one endpoint throwing is not the product being down', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('', { status: 500 }));
    await callUntilOutageBar();
    expect(getServiceHealth().down).toBe(false);
  });

  it('one successful call clears an outage', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'));
    await callUntilOutageBar();
    expect(getServiceHealth().down).toBe(true);

    vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }));
    await apiFetch('/api/v1/health');
    expect(getServiceHealth().down).toBe(false);
  });

  it('a request the CALLER aborted is not counted', async () => {
    // ⚠ Streams are aborted on every navigation. Counting those would put
    // the banner up every time somebody changed page — the exact
    // cry-wolf failure that gets a warning switched off.
    vi.mocked(fetch).mockImplementation(async () => {
      throw new DOMException('The operation was aborted.', 'AbortError');
    });

    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      const controller = new AbortController();
      controller.abort();
      await apiFetch('/api/v1/health', { signal: controller.signal }).catch(
        () => undefined,
      );
    }
    expect(getServiceHealth().down).toBe(false);
  });
});
