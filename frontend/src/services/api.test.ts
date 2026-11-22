/**
 * Unit tests for the apiFetch helper and the normalizePath companion.
 *
 * These cover the three production bugs the helper is the front-line for:
 *   1. Bearer token must be attached on every authenticated request.
 *   2. Backend trailing-slash semantics (FastAPI redirect_slashes) must be
 *      satisfied client-side so we never hit Render's https→http rewrite.
 *   3. Errors come back as typed exceptions the UI can branch on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiAuthError,
  ApiError,
  apiFetch,
  normalizePath,
} from './api';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn(),
}));

import { getAccessToken } from './auth';

const mockedGetAccessToken = vi.mocked(getAccessToken);

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

describe('normalizePath', () => {
  it('adds a trailing slash to allowlisted collection routes', () => {
    expect(normalizePath('/api/v1/connectors')).toBe('/api/v1/connectors/');
  });

  it('keeps the trailing slash on collection routes that already have one', () => {
    expect(normalizePath('/api/v1/connectors/')).toBe('/api/v1/connectors/');
  });

  it('treats /api/v1/webhooks as a collection root', () => {
    expect(normalizePath('/api/v1/webhooks')).toBe('/api/v1/webhooks/');
  });

  it('leaves verb-tail agent paths alone (no trailing slash)', () => {
    expect(normalizePath('/api/v1/agent/run')).toBe('/api/v1/agent/run');
    expect(normalizePath('/api/v1/agent/confirm')).toBe('/api/v1/agent/confirm');
  });

  it('leaves uuid-tail paths alone', () => {
    expect(normalizePath('/api/v1/agent/cancel/abc-123-def')).toBe(
      '/api/v1/agent/cancel/abc-123-def',
    );
  });

  it('strips a stray trailing slash from non-collection paths', () => {
    // FastAPI registers /agent/run without a slash, so a stray trailing
    // slash from a caller would otherwise 307 on the backend.
    expect(normalizePath('/api/v1/agent/run/')).toBe('/api/v1/agent/run');
  });

  it('leaves connector item routes alone (id/connect, id/disconnect)', () => {
    expect(normalizePath('/api/v1/connectors/buildium/connect')).toBe(
      '/api/v1/connectors/buildium/connect',
    );
    expect(normalizePath('/api/v1/connectors/buildium/disconnect')).toBe(
      '/api/v1/connectors/buildium/disconnect',
    );
  });

  it('preserves query strings on collection routes', () => {
    expect(normalizePath('/api/v1/webhooks?company_id=xyz')).toBe(
      '/api/v1/webhooks/?company_id=xyz',
    );
    expect(normalizePath('/api/v1/connectors?foo=bar')).toBe(
      '/api/v1/connectors/?foo=bar',
    );
  });

  it('does not touch non-/api paths', () => {
    expect(normalizePath('/login')).toBe('/login');
  });
});

describe('apiFetch', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => {
    fetchSpy.mockReset();
    mockedGetAccessToken.mockReset();
  });

  afterEach(() => {
    fetchSpy.mockReset();
  });

  it('injects the Bearer token from getAccessToken', async () => {
    mockedGetAccessToken.mockResolvedValue('tkn-abc');
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/api/v1/connectors/');

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [, init] = fetchSpy.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get('Authorization')).toBe('Bearer tkn-abc');
  });

  it('throws ApiAuthError when there is no access token', async () => {
    mockedGetAccessToken.mockResolvedValue(null);

    await expect(apiFetch('/api/v1/connectors/')).rejects.toBeInstanceOf(
      ApiAuthError,
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('surfaces backend detail JSON as an ApiError on non-OK responses', async () => {
    mockedGetAccessToken.mockResolvedValue('tkn-abc');
    fetchSpy.mockResolvedValue(
      jsonResponse({ detail: 'company not found' }, { status: 404 }),
    );

    const promise = apiFetch('/api/v1/connectors/');
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    await expect(promise).rejects.toMatchObject({
      status: 404,
      message: 'company not found',
    });
  });

  it('honors the anonymous flag and skips the Authorization header', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/api/v1/health/', { anonymous: true });

    expect(mockedGetAccessToken).not.toHaveBeenCalled();
    const [, init] = fetchSpy.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.has('Authorization')).toBe(false);
  });

  it('normalizes /api/v1/connectors → /api/v1/connectors/ before fetching', async () => {
    mockedGetAccessToken.mockResolvedValue('tkn-abc');
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/api/v1/connectors');

    const [url] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/connectors/');
  });

  it('leaves /api/v1/agent/cancel/<uuid> alone (no trailing slash added)', async () => {
    mockedGetAccessToken.mockResolvedValue('tkn-abc');
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));

    const id = '7c1f8d2e-9a04-4b85-9e7b-3b6a40e9f1c3';
    await apiFetch(`/api/v1/agent/cancel/${id}`, { method: 'POST' });

    const [url] = fetchSpy.mock.calls[0];
    expect(url).toBe(`/api/v1/agent/cancel/${id}`);
  });

  it('leaves /api/v1/agent/run alone (verb tail, not a collection)', async () => {
    mockedGetAccessToken.mockResolvedValue('tkn-abc');
    fetchSpy.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/api/v1/agent/run', { method: 'POST', body: '{}' });

    const [url] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/agent/run');
  });
});

/**
 * What the person on the other side of a failed request actually reads.
 *
 * ⚠ Every message here used to be the backend's raw `detail`, and FastAPI
 * fills that in from HTTP itself when a route says nothing: an unmatched
 * route answers `{"detail": "Not Found"}` and an unhandled exception answers
 * `{"detail": "Internal Server Error"}`. Those went through `new Error(...)`
 * unchanged, so a property manager's screen said **"Not Found"** — no
 * subject, no cause, nothing to do next.
 *
 * ⚠ The 403/404 pair is the one that matters. "You cannot see this" and "this
 * is not here" send somebody to opposite next actions, and a shared
 * "Request failed" makes them guess.
 */
describe('apiFetch error messages', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => {
    fetchSpy.mockReset();
    mockedGetAccessToken.mockReset();
    mockedGetAccessToken.mockResolvedValue('tkn');
  });

  afterEach(() => fetchSpy.mockReset());

  async function failWith(status: number, body: unknown): Promise<ApiError> {
    fetchSpy.mockResolvedValue(jsonResponse(body, { status }));
    try {
      await apiFetch('/api/v1/anything');
    } catch (e) {
      return e as ApiError;
    }
    throw new Error('expected apiFetch to throw');
  }

  it('keeps a message a route actually wrote', async () => {
    // The role gate writes a real sentence. Replacing it would be a
    // regression — it names the role, which the generic text cannot.
    const err = await failWith(403, { detail: 'This action requires the manager role.' });
    expect(err.message).toBe('This action requires the manager role.');
  });

  it('replaces the HTTP reason phrase on a 404', async () => {
    const err = await failWith(404, { detail: 'Not Found' });
    expect(err.message).not.toBe('Not Found');
    expect(err.message).toMatch(/deleted|out of date/i);
    // The raw words survive for a log line or a support report.
    expect(err.detail).toBe('Not Found');
  });

  it('says something DIFFERENT for a bare 403 than for a 404', async () => {
    const forbidden = await failWith(403, { detail: 'Forbidden' });
    const missing = await failWith(404, { detail: 'Not Found' });
    expect(forbidden.message).not.toBe(missing.message);
    expect(forbidden.message).toMatch(/permission/i);
    // And it says who can fix it, which is the whole point of the message.
    expect(forbidden.message).toMatch(/admin/i);
  });

  it('does not blame the user for a server fault', async () => {
    const err = await failWith(500, { detail: 'Internal Server Error' });
    expect(err.message).toMatch(/our side/i);
    expect(err.status).toBe(500);
  });

  it('survives a non-string detail rather than printing an object', async () => {
    // FastAPI's request-validation handler answers 422 with a LIST of
    // {loc, msg, type}. The old `as string` cast sent that into new Error()
    // and it reached the screen as "[object Object]".
    //
    // ⚠ The expected wording CHANGED on 2026-09-09 and the assertion moved
    // with it rather than being relaxed. The list used to be dropped whole, so
    // this read the generic "check the fields"; it now NAMES the field, which
    // is what the person actually needed. `msg` is still never shown — see
    // api.validation.test.ts.
    const err = await failWith(422, {
      detail: [{ loc: ['body', 'rent'], msg: 'not a valid number', type: 'float' }],
    });
    expect(err.message).not.toMatch(/\[object Object\]/);
    expect(err.message).toMatch(/rent/);
    expect(err.message).not.toMatch(/not a valid number/);
  });

  it('falls back on a body that is not JSON at all', async () => {
    fetchSpy.mockResolvedValue(new Response('<html>502</html>', { status: 502 }));
    await expect(apiFetch('/api/v1/anything')).rejects.toThrow(/our side/i);
  });

  it('leaves 401 to the auth path', async () => {
    // A dead session is handled elsewhere (the app signs you out); rewriting
    // its message here would break that branch.
    const err = await failWith(401, { detail: 'Unauthorized' });
    expect(err.name).toBe('ApiAuthError');
  });
});
