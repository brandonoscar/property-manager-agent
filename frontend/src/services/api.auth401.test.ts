/**
 * A dead session says so in a sentence, and never in the backend's words.
 *
 * THE DEFECT
 * ----------
 * `apiFetch`'s 401 branch sat ABOVE the `HTTP_REASON_PHRASES` filter — the
 * rule written precisely to stop status lines reaching a property manager. So
 * the one status where the backend NEVER writes a usable sentence was also the
 * one that skipped the check, and a person whose session expired mid-form read
 * the single word **"Unauthorized"**.
 *
 * Nearly every 401 this API produces is written for an engineer:
 *
 *   * `"Token has expired"`
 *   * `"Missing Authorization header"`
 *   * `f"Invalid token: {exc}"`  ← a JWT library's exception, on screen
 *
 * The last one is the reason this is a blanket rule rather than a filter: no
 * list of forbidden phrases can anticipate what a library puts in `{exc}`.
 *
 * ⚠ "Every" was the word here and it was measurably false —
 * `auth.py::_REVOKED_DETAIL` is "This device was signed out. Sign in again.",
 * a sentence for a person. The blanket rule survives that correction for a
 * better reason than the count: whichever cause fired, the remedy is
 * identical, so one written sentence serves all of them and there is no
 * vocabulary to keep in step with the backend.
 *
 * ⚠ **The detail is kept, not discarded.** `ApiError.detail` is the engineer's
 * copy and `message` is the screen's; a support report still carries the cause.
 * A fix that dropped the detail would trade one blind spot for another.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiAuthError, apiFetch, httpFallbackMessage } from './api';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn(async () => 'test-token'),
}));

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Every 401 detail string this backend is known to send. */
const BACKEND_401_DETAILS = [
  'Token has expired',
  'Missing Authorization header',
  'Invalid token: Signature verification failed',
  'Unauthorized',
  'Not authenticated',
];

describe('a 401 reaching the screen', () => {
  it.each(BACKEND_401_DETAILS)('does not show the backend detail %j', async (detail) => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail }));

    const err = await apiFetch('/api/v1/anything').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiAuthError);
    const api = err as ApiAuthError;
    expect(api.message).toBe(httpFallbackMessage(401));
    expect(api.message).not.toContain(detail);
  });

  it('keeps the backend detail for a bug report', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(401, { detail: 'Invalid token: Signature verification failed' }),
    );

    const err = (await apiFetch('/api/v1/anything').catch((e: unknown) => e)) as ApiAuthError;

    // ⚠ Both halves. Showing the detail is the defect; DISCARDING it is a
    // different one — the cause has to survive somewhere.
    expect(err.detail).toBe('Invalid token: Signature verification failed');
    expect(err.message).not.toBe(err.detail);
  });

  it('never leaks an exception string, whatever the library puts in it', async () => {
    // The `f"Invalid token: {exc}"` shape, with something that looks like a
    // stack. No phrase list could have anticipated this, which is why the rule
    // is "never show a 401 detail" rather than "filter known-bad ones".
    const nasty = 'Invalid token: JWSError JWSInvalidSignature at /usr/lib/python3.11/jose.py:214';
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: nasty }));

    const err = (await apiFetch('/api/v1/anything').catch((e: unknown) => e)) as ApiAuthError;

    expect(err.message).not.toContain('JWSError');
    expect(err.message).not.toContain('.py');
  });

  it('tells the person their typed work is still there', async () => {
    // The whole point for a form: a session that dies mid-submit does NOT
    // unmount anything, so the values are still on screen. Saying so is what
    // stops someone reloading the page and losing them for real.
    expect(httpFallbackMessage(401)).toMatch(/still on screen/i);
  });

  it('names an act the reader can actually perform', async () => {
    // ⚠ RE-POINTED, not deleted. This assertion read `/sign in again/i` and
    // was right about the property — the message must name a remedy — while
    // pinned to a sentence that named one the reader CANNOT carry out:
    // "Sign in again in another tab". A second tab finds the stored Supabase
    // session, sets `user`, and renders the app; there is no sign-in form
    // there to reach without signing out first.
    //
    // Both directions, because a message that merely APPENDED the right
    // advice to the wrong advice would pass a one-sided check while still
    // sending somebody to open a tab that cannot help them.
    const message = httpFallbackMessage(401);
    expect(message).toMatch(/sign out/i);
    expect(message).not.toMatch(/another tab/i);
  });

  it('says something different when there was no session at all', async () => {
    // No credential is not an expired one: nothing was lost and there is
    // nothing to retry. Reusing the expiry sentence would tell a signed-out
    // visitor their work is safe, which is a claim about work that never
    // existed.
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, {}));
    const auth = await import('./auth');
    vi.mocked(auth.getAccessToken).mockResolvedValueOnce(null as unknown as string);

    const err = (await apiFetch('/api/v1/anything').catch((e: unknown) => e)) as ApiAuthError;

    expect(err).toBeInstanceOf(ApiAuthError);
    expect(err.message).not.toBe(httpFallbackMessage(401));
    expect(err.message).toMatch(/not signed in/i);
  });
});

describe('the other statuses still behave', () => {
  it('a route that wrote a real sentence keeps it', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(403, { detail: 'This action requires the manager role.' }),
    );

    const err = (await apiFetch('/api/v1/anything').catch((e: unknown) => e)) as Error;

    // ⚠ The 401 rule must not have widened. A 403 from a route that bothered
    // to explain itself is the case `HTTP_REASON_PHRASES` exists to preserve.
    expect(err.message).toBe('This action requires the manager role.');
  });

  it('a bare HTTP reason phrase is still replaced', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(404, { detail: 'Not Found' }));

    const err = (await apiFetch('/api/v1/anything').catch((e: unknown) => e)) as Error;

    expect(err.message).toBe(httpFallbackMessage(404));
  });
});
