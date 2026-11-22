/**
 * Guards the Sentry noise filters (IGNORE_ERRORS / DENY_URLS).
 *
 * The concrete trigger: the MetaMask browser extension injects
 * ``inpage.js`` into every page and throws "Failed to connect to MetaMask",
 * which our global handler was reporting as a production error. These assert
 * the filters that keep that (and other extension / benign browser noise) out
 * of Sentry so real Occupella errors aren't buried.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { DENY_URLS, IGNORE_ERRORS, REPLAY_PRIVACY, sentryEnvironment } from './sentry';

const sdk = vi.hoisted(() => ({ init: vi.fn(), setUser: vi.fn() }));
vi.mock('@sentry/react', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@sentry/react')>()),
  init: sdk.init,
  setUser: sdk.setUser,
}));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  sdk.init.mockClear();
  sdk.setUser.mockClear();
});

/** Re-import with a DSN, the way a deployed bundle is configured. */
async function configured() {
  vi.stubEnv('VITE_SENTRY_DSN', 'https://key@example.ingest.sentry.io/1');
  vi.resetModules();
  return import('./sentry');
}

describe('who Sentry is told about', () => {
  it('sends the user id and never the email', async () => {
    const m = await configured();
    m.setSentryUser('user-1');
    expect(sdk.setUser).toHaveBeenCalledWith({ id: 'user-1' });
    // The signature itself no longer takes an email, so a caller can't pass one.
    expect(m.setSentryUser.length).toBe(1);
  });
});

describe('which build and environment an event names', () => {
  it('reports a Vercel preview as a preview, whatever the env var says', () => {
    expect(sentryEnvironment('preview', 'production')).toBe('preview');
    expect(sentryEnvironment('development', undefined)).toBe('development');
  });

  it('uses the configured name for production, and production when unset', () => {
    expect(sentryEnvironment('production', 'staging')).toBe('staging');
    expect(sentryEnvironment('', undefined)).toBe('production');
  });

  it('tags the release with the full commit the bundle was built from', async () => {
    vi.stubGlobal('__APP_RELEASE__', '7a00f5a6e2a1c0ffee0000000000000000000000');
    vi.stubGlobal('__VERCEL_ENV__', 'preview');
    const m = await configured();
    m.initSentry();
    expect(sdk.init).toHaveBeenCalledWith(
      expect.objectContaining({
        release: '7a00f5a6e2a1c0ffee0000000000000000000000',
        environment: 'preview',
      }),
    );
  });

  it('claims no release for a local build', async () => {
    vi.stubGlobal('__APP_RELEASE__', '');
    const m = await configured();
    m.initSentry();
    expect(sdk.init.mock.calls[0][0].release).toBeUndefined();
  });
});

describe('Sentry noise filters', () => {
  it('ignores MetaMask / wallet-extension messages', () => {
    expect(IGNORE_ERRORS).toContain('MetaMask');
    expect(IGNORE_ERRORS).toContain('Failed to connect to MetaMask');
  });

  it('denies stack frames originating in a browser extension', () => {
    const metamaskFrame =
      'chrome-extension://nkbihfbeogaeaoehlefnkodbefgpgknn/scripts/inpage.js:7:84179';
    expect(DENY_URLS.some((re) => re.test(metamaskFrame))).toBe(true);
    // Firefox + Safari extension schemes too.
    expect(DENY_URLS.some((re) => re.test('moz-extension://abc/inject.js'))).toBe(true);
    expect(DENY_URLS.some((re) => re.test('safari-web-extension://abc/inject.js'))).toBe(true);
  });

  it('does NOT deny a normal same-origin app URL', () => {
    const appFrame = 'https://your-app.vercel.app/assets/index-abc123.js:1:2345';
    expect(DENY_URLS.some((re) => re.test(appFrame))).toBe(false);
  });
});

describe('Sentry replay privacy', () => {
  // Regression guard: replays must never ship rendered tenant/owner PII to
  // Sentry. This flipped to `false` once (unmasked DOM + email to Sentry on
  // every errored session); the export makes the safe posture a tested contract.
  it('masks all text and blocks all media in replays', () => {
    expect(REPLAY_PRIVACY.maskAllText).toBe(true);
    expect(REPLAY_PRIVACY.blockAllMedia).toBe(true);
  });
});
