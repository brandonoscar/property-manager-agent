/**
 * The wiring: `apiFetch` reads the build header, and a stale page's failure
 * says so.
 *
 * ⚠ `buildVersion.test.ts` structurally cannot see any of this. Those tests
 * drive `noteServerBuild` / `buildsDisagree` by hand and would pass forever
 * against an `apiFetch` that never called either — the "complete, tested and
 * wired to nothing" failure this repo keeps shipping (gotchas 62/66/69/103).
 * What is asserted here is a real call, a real response, and the sentence the
 * user ends up reading.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './api';
import {
  BUILD_HEADER,
  buildsDisagree,
  lastServerBuild,
  noteServerBuild,
  resetBuildVersion,
} from './buildVersion';

vi.mock('./auth', () => ({ getAccessToken: vi.fn() }));

import { getAccessToken } from './auth';

const mockedGetAccessToken = vi.mocked(getAccessToken);

/**
 * ⚠ The client build is a compile-time `define`, so under vitest the bundle
 * always reports `0.1.0-dev` — which `clientBuild()` correctly reads as
 * unknown, so a real disagreement can never arise in this environment.
 *
 * An earlier cut of this file recorded that as an honest limitation and moved
 * on. It is not a limitation, it is a hole: with `buildsDisagree()` permanently false,
 * `explainIfStale` could be DELETED from `api.ts` and every test here would
 * still pass — the positive direction untested, which is the half that
 * matters. So the predicate is overridden per test (`importActual` + one
 * override, never a blanket mock — gotcha 88), and what these tests assert is
 * what `api.ts` DOES with the answer.
 */
vi.mock('./buildVersion', async (importActual) => ({
  ...(await importActual<typeof import('./buildVersion')>()),
  buildsDisagree: vi.fn(() => false),
}));

/**
 * The message `apiFetch` raised.
 *
 * ⚠ Asserts it actually THREW. `apiFetch` resolves to a `Response`, so a
 * bare `.catch(e => e)` types as `Response | Error` and a version that
 * stopped raising on a 404 would hand back a Response whose `message` is
 * undefined — every `not.toContain` below would pass vacuously.
 */
async function messageFrom(p: Promise<unknown>): Promise<string> {
  const outcome = await p.then(
    (value) => ({ threw: false as const, value }),
    (err: unknown) => ({ threw: true as const, err }),
  );
  expect(outcome.threw).toBe(true);
  const err = (outcome as { threw: true; err: unknown }).err;
  expect(err).toBeInstanceOf(Error);
  return (err as Error).message;
}

function respond(status: number, build: string | null, body: unknown = {}) {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (build !== null) headers.set(BUILD_HEADER, build);
  return new Response(JSON.stringify(body), { status, headers });
}

const disagree = vi.mocked(buildsDisagree);

beforeEach(() => {
  resetBuildVersion();
  disagree.mockReturnValue(false);
  mockedGetAccessToken.mockResolvedValue('token');
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('apiFetch reads which build answered', () => {
  it('records it from a successful response', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(200, 'abc123def456'));
    await apiFetch('/api/v1/health');
    expect(lastServerBuild()).toBe('abc123def456');
  });

  it('records it from a FAILING response too', async () => {
    // ⚠ The whole reason this rides a header rather than a probe endpoint.
    // The request that breaks under a stale bundle is the one that proves the
    // bundle is stale — a separate probe would need an extra round trip and
    // could answer about a different moment.
    vi.mocked(fetch).mockResolvedValue(respond(404, 'abc123def456'));
    await apiFetch('/api/v1/nope').catch(() => undefined);
    expect(lastServerBuild()).toBe('abc123def456');
  });

  it('a response with no header does not erase what we knew', async () => {
    noteServerBuild('abc123def456');
    vi.mocked(fetch).mockResolvedValue(respond(200, null));
    await apiFetch('/api/v1/health');
    expect(lastServerBuild()).toBe('abc123def456');
  });
});

describe('the advice is scoped to failures a stale page explains', () => {
  it('stays silent while the builds agree', async () => {
    // The default state, and the one that must never be noisy: nearly every
    // 404 in this product is a genuinely deleted record.
    vi.mocked(fetch).mockResolvedValue(respond(404, 'abc123def456'));
    const message = await messageFrom(apiFetch('/api/v1/nope'));
    expect(message.toLowerCase()).not.toContain('reload');
  });

  it('EXPLAINS a 404 once the builds really differ', async () => {
    // The direction the whole change exists for. Without it a stale tab's
    // 404 reads as "that record may have been deleted" — false, and retrying
    // fails identically forever because nothing suggests the one act that
    // works.
    disagree.mockReturnValue(true);
    vi.mocked(fetch).mockResolvedValue(respond(404, '999888777666'));
    const message = await messageFrom(apiFetch('/api/v1/nope'));
    expect(message.toLowerCase()).toContain('reload');
    // ⚠ APPENDED, not substituted: a 404 really can be a deleted record and
    // we cannot tell which from two SHAs, so the original reading survives.
    expect(message).toContain('may have been deleted');
  });

  it('EXPLAINS a 422 too — a moved contract is what that looks like', async () => {
    disagree.mockReturnValue(true);
    vi.mocked(fetch).mockResolvedValue(respond(422, '999888777666'));
    const message = await messageFrom(apiFetch('/api/v1/anything'));
    expect(message.toLowerCase()).toContain('reload');
  });

  it('says nothing about reloading on a 500, whatever the builds say', async () => {
    // ⚠ THE STATUS THAT ACTUALLY EXERCISES THE SCOPE. A server error has
    // nothing to do with which build is asking, and telling somebody to
    // reload about it is a useless instruction attached to a real problem —
    // this change's own failure mode, pointed the other way.
    //
    // A 500 is the honest test because it reaches `explainIfStale` and is
    // refused there. The 401 below looks like the same assertion and is not.
    disagree.mockReturnValue(true);
    vi.mocked(fetch).mockResolvedValue(respond(500, '999888777666'));
    const message = await messageFrom(apiFetch('/api/v1/boom'));
    expect(message.toLowerCase()).not.toContain('different versions');
  });

  it('says nothing about reloading on a 401 either — for its own reason', async () => {
    // ⚠ TRUE, AND NOT BECAUSE OF `SKEW_EXPLAINS`. A 401 throws `ApiAuthError`
    // several lines earlier and never reaches `explainIfStale` at all, so
    // adding 401 to that set changes nothing — the mutation proving it
    // survived, which is how this comment came to be written (gotcha 58: a
    // mutation that cannot change any output is telling you something).
    // Kept because the user-facing property is worth pinning; corrected so
    // the next reader does not mistake it for coverage of the scope.
    disagree.mockReturnValue(true);
    vi.mocked(fetch).mockResolvedValue(respond(401, '999888777666'));
    const message = await messageFrom(apiFetch('/api/v1/me'));
    expect(message.toLowerCase()).not.toContain('different versions');
  });
});
