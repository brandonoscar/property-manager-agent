/**
 * A rejected field is NAMED, and pydantic's own words never reach the screen.
 *
 * THE DEFECT
 * ----------
 * FastAPI answers a validation failure with a LIST:
 *
 *     {"detail": [{"loc": ["body", "email"], "msg": "...", "type": "..."}]}
 *
 * `apiFetch` kept only string details, so the list was dropped whole and the
 * person read "Some of that was not accepted. Check the fields and try again"
 * — without being told which. On the A2P registration form that is twenty
 * fields to re-read to find one.
 *
 * ⚠ **The field name is taken and the `msg` is not**, and that asymmetry is
 * the point. Pydantic writes for a developer: `"String should match pattern
 * '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'"` puts a regex in front of a property
 * manager. It is the same leak the 401 branch exists to prevent, arriving from
 * a different direction — and no allowlist of readable messages could
 * anticipate the next one, which is why the rule is structural.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch, httpFallbackMessage, validationFields, validationMessage } from './api';

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

describe('reading the field names out of a 422', () => {
  it('drops the source segment, which is not a field anybody filled in', () => {
    expect(validationFields([{ loc: ['body', 'email'], msg: 'x', type: 'y' }])).toEqual(['email']);
    expect(validationFields([{ loc: ['query', 'limit'], msg: 'x', type: 'y' }])).toEqual(['limit']);
  });

  it('drops list indices so a bulk entry reads as a path', () => {
    // The real shape from POST /company/invite/bulk.
    expect(validationFields([{ loc: ['body', 'invites', 0, 'email'], msg: 'x', type: 'y' }])).toEqual(
      ['invites.email'],
    );
  });

  it('reports each field once, in the order the server gave them', () => {
    expect(
      validationFields([
        { loc: ['body', 'role'], msg: 'x', type: 'y' },
        { loc: ['body', 'email'], msg: 'x', type: 'y' },
        { loc: ['body', 'role'], msg: 'z', type: 'y' },
      ]),
    ).toEqual(['role', 'email']);
  });

  it('returns nothing for anything that is not a FastAPI validation list', () => {
    // ⚠ Both directions. A string detail must keep the existing behaviour, so
    // this cannot start hijacking ordinary errors.
    expect(validationFields('Not Found')).toEqual([]);
    expect(validationFields(null)).toEqual([]);
    expect(validationFields([{ nope: true }])).toEqual([]);
    expect(validationFields([{ loc: ['body'] }])).toEqual([]);
    expect(validationFields([{ loc: 'body.email' }])).toEqual([]);
  });
});

describe('what reaches the screen', () => {
  it('names the one field that was rejected', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, {
        detail: [
          {
            loc: ['body', 'email'],
            msg: "String should match pattern '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'",
            type: 'string_pattern_mismatch',
          },
        ],
      }),
    );

    const err = (await apiFetch('/api/v1/company/invite').catch((e: unknown) => e)) as ApiError;

    expect(err.message).toContain('email');
    expect(err.fields).toEqual(['email']);
  });

  it('never puts pydantic’s constraint text on screen', async () => {
    const leaky = "String should match pattern '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'";
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, {
        detail: [{ loc: ['body', 'email'], msg: leaky, type: 'string_pattern_mismatch' }],
      }),
    );

    const err = (await apiFetch('/api/v1/company/invite').catch((e: unknown) => e)) as ApiError;

    expect(err.message).not.toContain('pattern');
    expect(err.message).not.toContain('^[^@');
    expect(err.message).not.toContain(leaky);
  });

  it('keeps the whole payload on `detail` for a bug report', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, {
        detail: [{ loc: ['body', 'email'], msg: 'Field required', type: 'missing' }],
      }),
    );

    const err = (await apiFetch('/api/v1/company/invite').catch((e: unknown) => e)) as ApiError;

    // Showing it is the defect; DISCARDING it is a different one.
    expect(err.detail).toContain('Field required');
    expect(err.message).not.toContain('Field required');
  });

  it('lists several fields rather than picking one', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, {
        detail: [
          { loc: ['body', 'email'], msg: 'a', type: 'x' },
          { loc: ['body', 'role'], msg: 'b', type: 'x' },
        ],
      }),
    );

    const err = (await apiFetch('/api/v1/x').catch((e: unknown) => e)) as ApiError;

    expect(err.fields).toEqual(['email', 'role']);
    expect(err.message).toContain('email');
    expect(err.message).toContain('role');
  });

  it('falls back to the written sentence when the server named no field', () => {
    expect(validationMessage([])).toBe(httpFallbackMessage(422));
  });

  it('leaves a 422 that sent a plain string alone', async () => {
    // A route that raised its own HTTPException(422, "…") wrote a real
    // sentence. The new path must not swallow it.
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, { detail: 'That timezone is not a real IANA name.' }),
    );

    const err = (await apiFetch('/api/v1/x').catch((e: unknown) => e)) as ApiError;

    expect(err.message).toBe('That timezone is not a real IANA name.');
    expect(err.fields).toEqual([]);
  });

  it('leaves other statuses completely alone', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(404, { detail: 'Not Found' }));
    const err = (await apiFetch('/api/v1/x').catch((e: unknown) => e)) as ApiError;
    expect(err.message).toBe(httpFallbackMessage(404));
    expect(err.fields).toEqual([]);
  });
});
