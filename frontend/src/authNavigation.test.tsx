/**
 * Every API call made while moving Chat → Inbox → Settings > Billing carries
 * the session, and a session that can no longer refresh ends on the sign-in
 * form instead of inside the shell.
 *
 * ⚠ THE DEFECT (founder report, 2026-09-29). After a token refresh failed
 * with 400, Inbox and Settings > Billing both said "You are not signed in.
 * Sign in and try again." inside a shell that still looked signed in, and
 * "Try again" sent nothing. Reproduced in Chromium against a fake Supabase:
 * supabase-js 2.112 keeps the stored session after a failed refresh and
 * caches the failure for 60 seconds, so `getSession()` returns no session
 * while nothing signs the app out. Every `apiFetch` then throws before a
 * request exists, and a retry meets the same cached failure.
 *
 * The fake client here returns what supabase-js returned in that repro:
 * `{ session: null, error: AuthApiError }` with the blob still stored.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  vi.stubEnv('VITE_SUPABASE_URL', 'https://abcdefghijklmnopqrst.supabase.co');
  vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'anon-key');
  vi.stubEnv('VITE_DEMO_MODE', 'false');
});

const STORAGE_KEY = 'sb-abcdefghijklmnopqrst-auth-token';
const USER = { id: 'user-1', email: 'pm@example.com', aud: 'authenticated', app_metadata: {}, user_metadata: {} };

const fake = vi.hoisted(() => {
  type Listener = (event: string, session: unknown) => void;
  const listeners = new Set<Listener>();
  return {
    listeners,
    /** What getSession answers once the stored token has aged out. */
    slowPath: { session: null as unknown, error: null as unknown },
    /** How many times the app fell through to getSession for a token. */
    lookups: 0,
  };
});

vi.mock('@supabase/supabase-js', () => ({
  createClient: () => ({
    auth: {
      getSession: vi.fn(async () => {
        fake.lookups += 1;
        return { data: { session: fake.slowPath.session }, error: fake.slowPath.error };
      }),
      onAuthStateChange: vi.fn((cb: (event: string, session: unknown) => void) => {
        fake.listeners.add(cb);
        return { data: { subscription: { unsubscribe: () => fake.listeners.delete(cb) } } };
      }),
      signOut: vi.fn(async () => {
        for (const cb of fake.listeners) cb('SIGNED_OUT', null);
        return { error: null };
      }),
      getUser: vi.fn(async () => ({ data: { user: USER }, error: null })),
      mfa: {
        getAuthenticatorAssuranceLevel: vi.fn(async () => ({
          data: { currentLevel: 'aal1', nextLevel: 'aal1' },
          error: null,
        })),
        listFactors: vi.fn(async () => ({ data: { all: [], totp: [] }, error: null })),
      },
    },
    from: vi.fn(),
  }),
}));

if (!Element.prototype.scrollTo) Element.prototype.scrollTo = () => {};

import { App } from './App';
import { BOOT_MIN_MS } from './components/BootScreen';
import { useStore } from './stores/store';

interface Sent {
  path: string;
  authorization: string | null;
}
let sent: Sent[] = [];

function seedSession(expiresInSec: number) {
  window.localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      access_token: 'tkn-live',
      refresh_token: 'rfk',
      token_type: 'bearer',
      expires_at: Math.floor(Date.now() / 1000) + expiresInSec,
      user: USER,
    }),
  );
}

function body(path: string): unknown {
  if (path.includes('/rest/v1/company_users')) return [{ company_id: 'c1', role: 'owner' }];
  if (path.includes('/billing/status')) {
    return {
      plan: 'trial',
      plan_label: 'Free trial',
      status: 'trialing',
      seats: 1,
      turns_used: 3,
      turns_allowed: 100,
      turns_remaining: 97,
      trial_ends_at: new Date(Date.now() + 5 * 86_400_000).toISOString(),
      period_end: null,
      past_due: false,
      block: null,
      block_message: null,
      enforced: false,
      plans: [],
    };
  }
  return {};
}

beforeEach(() => {
  sent = [];
  fake.listeners.clear();
  fake.slowPath = { session: null, error: null };
  fake.lookups = 0;
  window.localStorage.clear();
  useStore.setState({ view: 'chat' });
  window.matchMedia = ((query: string) =>
    ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
      const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
      sent.push({ path, authorization: headers.get('Authorization') });
      return new Response(JSON.stringify(body(path)), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  useStore.getState().clearAuth();
});

async function renderSignedIn() {
  seedSession(3600);
  const view = render(<App />);
  await waitFor(() => expect(document.getElementById('main')).toBeTruthy(), {
    timeout: BOOT_MIN_MS + 2000,
  });
  return view;
}

/** A nav place is a link; an action (Refresh) is a button. */
async function go(label: RegExp) {
  await waitFor(() =>
    expect(
      screen.queryAllByRole('link', { name: label }).length +
        screen.queryAllByRole('button', { name: label }).length,
    ).toBeGreaterThan(0),
  );
  const links = screen.queryAllByRole('link', { name: label });
  const nav = links.length ? links : screen.getAllByRole('button', { name: label });
  await act(async () => {
    fireEvent.click(nav[0]);
  });
}

const apiCalls = () => sent.filter((s) => s.path.startsWith('/api/'));

describe('moving through the app keeps the session on every call', () => {
  it('Chat, Inbox and Settings > Billing each send the token', async () => {
    await renderSignedIn();
    const inboxBefore = apiCalls().filter((c) => c.path.includes('/recent-events')).length;
    await go(/^Inbox/);
    await waitFor(() =>
      expect(apiCalls().filter((c) => c.path.includes('/recent-events')).length).toBeGreaterThan(inboxBefore),
    );
    await act(async () => {
      useStore.getState().openSettings('billing');
    });
    await waitFor(() =>
      expect(apiCalls().filter((c) => c.path.includes('/billing/status')).length).toBeGreaterThan(1),
    );

    expect(apiCalls().length).toBeGreaterThan(3);
    expect(apiCalls().filter((c) => c.authorization !== 'Bearer tkn-live')).toEqual([]);
    expect(screen.queryByText(/not signed in/i)).toBeNull();
  }, 15_000);

  it('a refresh token that was rejected ends on sign-in, with the reason', async () => {
    await renderSignedIn();
    const before = apiCalls().length;

    // The access token ages out and the refresh has failed: supabase-js's
    // own answer in the Chromium repro. The blob is still stored.
    seedSession(-10);
    fake.slowPath = {
      session: null,
      error: { name: 'AuthApiError', status: 400, message: 'Invalid Refresh Token: Already Used' },
    };
    await go(/^Inbox/);

    expect(await screen.findByTestId('login-session-expired', {}, { timeout: 3000 })).toHaveTextContent(
      'Your session expired. Sign in again.',
    );
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(/not signed in/i)).toBeNull();
    // Nothing went out without the token, before or after.
    expect(apiCalls().filter((c) => c.authorization !== 'Bearer tkn-live')).toEqual([]);
    expect(apiCalls().length).toBe(before);
  }, 15_000);

  it('a network failure on refresh does not sign anybody out', async () => {
    await renderSignedIn();
    seedSession(-10);
    fake.slowPath = { session: null, error: { name: 'AuthRetryableFetchError', status: 0, message: 'Failed to fetch' } };
    const before = apiCalls().length;
    await go(/^Inbox/);
    // Refresh always refetches, so the app has to ask for a token; wait for
    // that lookup, or this test proves nothing.
    await go(/^Refresh/);
    await waitFor(() => expect(fake.lookups).toBeGreaterThan(0), { timeout: 3000 });
    await new Promise((r) => setTimeout(r, 300));

    // The session may still be good once the network is back, so it is kept.
    expect(apiCalls().length).toBe(before);
    expect(screen.queryByTestId('login-session-expired')).toBeNull();
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull();
    expect(useStore.getState().user).not.toBeNull();
  }, 15_000);
});
