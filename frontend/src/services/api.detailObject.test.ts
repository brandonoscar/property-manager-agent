/**
 * A `detail` that is a JSON OBJECT still reaches the person reading the screen.
 *
 * Six backend routes answer with one: the four inbox-execute raises
 * (`{"error": …, "tool_id": …}`), A2P submit (`{"problems": [...]}`), and the
 * CRM "which application?" conflict (`{"message": …, "applications": [...]}`).
 * The object exists because each carries a machine-readable half beside the
 * sentence. `apiFetch` kept `detail` only when `typeof raw === 'string'`, so
 * all six sentences were dropped and the generic status fallback took their
 * place.
 *
 * The worst of them, measured end to end below: the execute route's
 * `blocked_needs_review` — "a previous attempt did not complete cleanly; this
 * action needs review before it can be retried" — reached the screen as
 * "Somebody changed this while you were working on it. Reload and try again."
 * Nobody changed anything, and reloading cannot clear that state. A fabricated
 * cause and an instruction that does nothing, on the one inbox outcome where a
 * write may already have fired (gotcha 70's 200-but-lie).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch, DETAIL_MESSAGE_KEYS, writtenDetail } from './api';

vi.mock('./auth', () => ({ getAccessToken: vi.fn() }));

import { getAccessToken } from './auth';

const mockedGetAccessToken = vi.mocked(getAccessToken);

/**
 * The error `apiFetch` raised.
 *
 * ⚠ Asserts it actually THREW. `apiFetch` resolves to a `Response`, so a bare
 * `.catch(e => e)` types as `Response | Error` and a version that stopped
 * raising would hand back a Response whose `message` is undefined — every
 * `not.toContain` here would pass vacuously.
 */
async function errorFrom(p: Promise<unknown>): Promise<ApiError> {
  const outcome = await p.then(
    (value) => ({ threw: false as const, value }),
    (err: unknown) => ({ threw: true as const, err }),
  );
  expect(outcome.threw).toBe(true);
  const err = (outcome as { threw: true; err: unknown }).err;
  expect(err).toBeInstanceOf(ApiError);
  return err as ApiError;
}

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  mockedGetAccessToken.mockResolvedValue('token');
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('writtenDetail', () => {
  it('reads the sentence out of an object detail', () => {
    expect(writtenDetail({ error: 'the vendor is not on this property', tool_id: 'x' })).toBe(
      'the vendor is not on this property',
    );
    expect(writtenDetail({ message: 'Applicant has multiple applications — pick one.' })).toBe(
      'Applicant has multiple applications — pick one.',
    );
  });

  it('joins a list of written problems', () => {
    // A2P's shape. Each entry is a complete sentence from `a2p.validate`.
    expect(
      writtenDetail({
        problems: ['EIN should be your federal tax ID number.', 'Website should start with http.'],
      }),
    ).toBe('EIN should be your federal tax ID number. Website should start with http.');
  });

  it('leaves a plain string exactly as it was', () => {
    // The other direction (gotchas 38/51): a version that only handled objects
    // would break every route that answers with a sentence, which is most of
    // them, and a one-sided test would not notice.
    expect(writtenDetail('company has no Buildium credentials configured')).toBe(
      'company has no Buildium credentials configured',
    );
  });

  it('refuses an ARRAY, so the 422 list stays with validationFields', () => {
    // ⚠ The direction that would leak. FastAPI's validation body is a list of
    // {loc, msg, type}; pydantic's `msg` embeds the constraint verbatim
    // ("String should match pattern '^[^@\\s]+…'"). Swallowing it here would
    // put a regex on a property manager's screen.
    expect(writtenDetail([{ loc: ['body', 'email'], msg: "String should match pattern '^x$'" }])).toBe(
      undefined,
    );
  });

  it('refuses an object with no written key', () => {
    // Never "[object Object]" and never raw JSON — the caller falls back to the
    // status sentence, which is at least true.
    expect(writtenDetail({ tool_id: 'send_tenant_message', retry_after: 30 })).toBe(undefined);
    expect(writtenDetail({ error: '   ' })).toBe(undefined);
    expect(writtenDetail(null)).toBe(undefined);
  });

  it('prefers the keys in the declared order', () => {
    expect(writtenDetail({ error: 'second', message: 'first' })).toBe('first');
    expect(DETAIL_MESSAGE_KEYS[0]).toBe('message');
  });
});

describe('apiFetch surfaces an object detail', () => {
  it('shows the execute route’s needs-review sentence, not the 409 fallback', async () => {
    const backend =
      'a previous attempt did not complete cleanly; this action needs review before it can be retried';
    vi.mocked(fetch).mockResolvedValue(
      respond(409, { detail: { error: backend, tool_id: 'send_tenant_message' } }),
    );

    const err = await errorFrom(apiFetch('/api/v1/buildium/events/e/actions/0/execute'));
    expect(err.message).toContain('needs review');
    // ⚠ Both directions. The generic 409 invents a cause and gives an
    // instruction that cannot clear this state.
    expect(err.message).not.toContain('Somebody changed this');
    expect(err.message).not.toContain('[object Object]');
  });

  it('keeps the machine-readable half on the error for diagnosis', async () => {
    // `message` is the screen's copy and `detail` is the engineer's — the split
    // is why an object detail can be handled at all. Losing `tool_id` would
    // make a support conversation start from nothing.
    vi.mocked(fetch).mockResolvedValue(
      respond(502, { detail: { error: 'handler blew up', tool_id: 'update_buildium_task' } }),
    );

    const err = await errorFrom(apiFetch('/api/v1/buildium/events/e/actions/0/execute'));
    expect(err.detail).toContain('update_buildium_task');
    // ...and the JSON must NOT be what the person reads.
    expect(err.message).toBe('handler blew up');
  });

  it('names the A2P problems instead of "check the fields"', async () => {
    vi.mocked(fetch).mockResolvedValue(
      respond(422, { detail: { problems: ['Business name is required.'] } }),
    );

    const err = await errorFrom(apiFetch('/api/v1/a2p/registration/submit'));
    expect(err.message).toContain('Business name is required.');
  });

  it('still falls back when the object says nothing usable', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(404, { detail: { tool_id: 'x' } }));

    const err = await errorFrom(apiFetch('/api/v1/nope'));
    expect(err.message).toContain('not here');
    expect(err.message).not.toContain('tool_id');
  });

  it('still replaces an HTTP reason phrase carried inside an object', async () => {
    // The written-vs-reason-phrase rule has to survive the new shape: "Not
    // Found" on its own tells a property manager nothing, wherever it arrives.
    vi.mocked(fetch).mockResolvedValue(respond(404, { detail: { error: 'Not Found' } }));

    const err = await errorFrom(apiFetch('/api/v1/nope'));
    expect(err.message).toContain('not here');
  });

  it('still names the fields on a real FastAPI 422', async () => {
    // The pre-existing behaviour this change must not disturb.
    vi.mocked(fetch).mockResolvedValue(
      respond(422, { detail: [{ loc: ['body', 'email'], msg: 'x', type: 'y' }] }),
    );

    const err = await errorFrom(apiFetch('/api/v1/company/invite'));
    expect(err.message).toContain('email');
    expect(err.fields).toEqual(['email']);
  });
});
