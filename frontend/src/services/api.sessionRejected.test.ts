/**
 * The wiring: a real `apiFetch` against a real response decides whether this
 * device's sign-in is still accepted.
 *
 * ⚠ `sessionRejected.test.ts` structurally cannot see any of this. Those
 * tests call the reporters by hand and would pass forever against an
 * `apiFetch` that never called either — which is exactly the state the
 * product was in before this file existed: the fact was knowable on every
 * response and nobody was recording it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './api';
import {
  CONSECUTIVE_REJECTIONS,
  isSessionRejected,
  resetSessionRejected,
} from './sessionRejected';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn(async () => 'test-token'),
}));

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Swallow the throw — `apiFetch` raises on every non-2xx. */
async function call(options?: Parameters<typeof apiFetch>[1]) {
  await apiFetch('/api/v1/anything', options).catch(() => undefined);
}

beforeEach(() => {
  resetSessionRejected();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('a credentialed request', () => {
  it('announces the rejection once the bar is met', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: 'Token has expired' }));

    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) await call();

    expect(isSessionRejected()).toBe(true);
  });

  it('is cleared by any other served status', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: 'Token has expired' }));
    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) await call();
    expect(isSessionRejected()).toBe(true);

    // ⚠ A 404 and not a 200, deliberately. The claim is that a served
    // response OTHER than 401 proves the credential was accepted — a 404
    // reached a route that had already authenticated the caller — so the
    // weaker "only a success clears it" reading would leave this banner up
    // through an ordinary missing record.
    vi.mocked(fetch).mockResolvedValue(jsonResponse(404, { detail: 'No such property' }));
    await call();

    expect(isSessionRejected()).toBe(false);
  });
});

describe('an anonymous request', () => {
  it('never counts, however many times it 401s', async () => {
    // ⚠ The distinguishing case. It carried no `Authorization` header, so
    // its 401 is not evidence about the credential we hold — counting it
    // would announce a dead session to somebody whose session is fine.
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: 'Not authenticated' }));

    for (let i = 0; i < CONSECUTIVE_REJECTIONS + 2; i += 1) await call({ anonymous: true });

    expect(isSessionRejected()).toBe(false);
  });

  it('does not clear a real rejection either', async () => {
    // The other direction of the same rule: an anonymous 200 says nothing
    // about a credential it never sent, so it must not withdraw a warning
    // that credentialed requests earned.
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: 'Token has expired' }));
    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) await call();
    expect(isSessionRejected()).toBe(true);

    vi.mocked(fetch).mockResolvedValue(jsonResponse(200));
    await call({ anonymous: true });

    expect(isSessionRejected()).toBe(true);
  });
});

describe('a request that never reached a server', () => {
  it('is not a rejection', async () => {
    // A transport failure proves nothing about the credential — it proves
    // nothing was there to judge it. `serviceHealth` owns that question.
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'));

    for (let i = 0; i < CONSECUTIVE_REJECTIONS + 2; i += 1) await call();

    expect(isSessionRejected()).toBe(false);
  });
});
