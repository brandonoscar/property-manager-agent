/**
 * The page and the server disagree — and saying so must be rarer than it is
 * tempting.
 *
 * A stale bundle is real: Render and Vercel deploy from one branch
 * independently, so after every merge the page in front of a customer can be
 * a different build from the server answering it. When the contract moved,
 * their next request fails and the message says the record "may have been
 * deleted" or to "check the fields" — false, and retrying never fixes it.
 *
 * But a mismatch check that fires when it is not true is the worse bug. Every
 * developer runs a `-dev` bundle against a real backend; every response that
 * misses the middleware carries no header at all. Both must claim NOTHING,
 * and those are the tests that matter here.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BUILD_HEADER,
  UNKNOWN_BUILD,
  buildsDisagree,
  lastServerBuild,
  noteServerBuild,
  resetBuildVersion,
  skewAdvice,
} from './buildVersion';

beforeEach(() => {
  resetBuildVersion();
});

/**
 * `__APP_VERSION__` is a Vite `define`, i.e. a compile-time substitution —
 * there is no binding to reassign at runtime. `vi.stubGlobal` does not reach
 * it either. So the client half is exercised by re-importing the module with
 * the define replaced, which is also the only way the real thing ever varies.
 */
async function withClientBuild(value: string) {
  vi.resetModules();
  vi.stubGlobal('__APP_VERSION__', value);
  return import('./buildVersion');
}

describe('what this bundle thinks it is', () => {
  it('reports the baked-in commit when there is one', async () => {
    const m = await withClientBuild('abc123def456');
    expect(m.clientBuild()).toBe('abc123def456');
    vi.unstubAllGlobals();
  });

  it('claims NOTHING for a local -dev build', async () => {
    // ⚠ The false-positive that would have made this permanent noise. A local
    // build has no commit, so `vite.config.ts` falls back to `0.1.0-dev`,
    // while the backend it talks to resolves a real SHA from `git rev-parse`.
    // Compared naively those differ on every request, so every developer
    // would see the skew sentence on every 404 forever — and a control that
    // is always wrong is one nobody reads on the day it is right.
    const m = await withClientBuild('0.1.0-dev');
    expect(m.clientBuild()).toBe('');

    m.noteServerBuild('abc123def456');
    expect(m.buildsDisagree()).toBe(false);
    vi.unstubAllGlobals();
  });

  it('claims nothing when the define is missing entirely', async () => {
    const m = await withClientBuild('');
    expect(m.clientBuild()).toBe('');
    m.noteServerBuild('abc123def456');
    expect(m.buildsDisagree()).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe('what the server told us', () => {
  it('remembers the build that answered', () => {
    noteServerBuild('abc123def456');
    expect(lastServerBuild()).toBe('abc123def456');
  });

  it("treats the server's own unknown sentinel as unknown", () => {
    // The backend says `unknown` when it could not resolve its SHA. Storing
    // that verbatim would compare it against a real commit and report a
    // mismatch on every request.
    noteServerBuild(UNKNOWN_BUILD);
    expect(lastServerBuild()).toBe('');
    expect(buildsDisagree()).toBe(false);
  });

  it('an absent header does NOT forget a build we already know', () => {
    // ⚠ Not every response comes through the API middleware — a proxy error
    // page, an aborted request. Clearing on those would make the check
    // flicker between known and unknown, so the sentence would appear and
    // vanish for one underlying state.
    noteServerBuild('abc123def456');
    noteServerBuild(null);
    noteServerBuild('');
    noteServerBuild(undefined);
    expect(lastServerBuild()).toBe('abc123def456');
  });
});

describe('when it says the builds disagree', () => {
  it('stays silent until BOTH sides are known', async () => {
    const m = await withClientBuild('abc123def456');
    expect(m.buildsDisagree()).toBe(false); // server not heard from yet
    m.noteServerBuild('abc123def456');
    expect(m.buildsDisagree()).toBe(false); // heard, and the same
    vi.unstubAllGlobals();
  });

  it('says so when they really are different', async () => {
    // The other direction. A one-sided test passes against a function that
    // refuses everything, which is the shape this repo keeps re-learning.
    const m = await withClientBuild('abc123def456');
    m.noteServerBuild('999888777666');
    expect(m.buildsDisagree()).toBe(true);
    vi.unstubAllGlobals();
  });
});

describe('the sentence', () => {
  it('names the action and claims no direction', () => {
    // ⚠ Two opaque SHAs carry no ordering. During a deploy Vercel usually
    // lands first, so the common case is a FRESH page against an OLD server —
    // "you are out of date" would be a confident wrong claim, and "update
    // your page" would be advice that cannot work. Reloading in a moment is
    // true either way.
    const advice = skewAdvice().toLowerCase();
    expect(advice).toContain('reload');
    expect(advice).toContain('different versions');
    for (const direction of ['out of date', 'older version of occupella']) {
      expect(advice).not.toContain(direction);
    }
  });
});

describe('the contract with the server', () => {
  it('names the header the backend actually stamps', () => {
    // The Python side asserts the same equality from its end; this is the
    // half that fails if the TS constant is edited alone.
    expect(BUILD_HEADER).toBe('X-Occupella-Build');
  });
});
