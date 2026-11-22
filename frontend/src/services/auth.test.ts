/**
 * Coverage for the lock-contention-free auth helpers.
 *
 * History: PR #55 coalesced concurrent ``getAccessToken`` callers but
 * Supabase's own ``_startAutoRefresh`` and our ``fetchCompanyIds``
 * still raced for the ``lock:sb-<ref>-auth-token`` Web Lock, leaving
 * the /connectors page blank when the lock got stolen mid-flight.
 *
 * The current implementation eliminates that contention class three
 * ways: (a) a process-local lock replaces the Web Lock, so steals
 * can't happen; (b) ``getAccessToken`` fast-paths from the
 * localStorage blob Supabase already persists; (c) ``fetchCompanyIds``
 * issues a direct REST request rather than going through
 * ``supabase.from(…)`` which would itself call ``auth.getSession()``.
 *
 * These tests pin each path so the contention can't quietly come back.
 */

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// Minimal browser-globals polyfill so the auth module — which reads
// localStorage and (in non-test code paths) window.location — runs in
// the default Node test environment without pulling jsdom in.
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(k: string): string | null {
    return this.store.get(k) ?? null;
  }
  setItem(k: string, v: string): void {
    this.store.set(k, String(v));
  }
  removeItem(k: string): void {
    this.store.delete(k);
  }
  clear(): void {
    this.store.clear();
  }
  key(i: number): string | null {
    return [...this.store.keys()][i] ?? null;
  }
  get length(): number {
    return this.store.size;
  }
}

const memoryStorage = new MemoryStorage();
const fakeWindow = {
  localStorage: memoryStorage,
  location: { search: '', origin: 'http://localhost' },
};
// `cookie` because getAccessToken can now reach the store (to end a dead
// session), and the store reads its sidebar preference from a cookie.
const fakeDocument = Object.assign(new EventTarget(), { cookie: '' });

beforeAll(() => {
  Object.defineProperty(globalThis, 'window', { value: fakeWindow, writable: true });
  Object.defineProperty(globalThis, 'localStorage', {
    value: memoryStorage,
    writable: true,
  });
  Object.defineProperty(globalThis, 'document', { value: fakeDocument, writable: true });
});

const getSession = vi.fn();
const onAuthStateChange = vi.fn().mockReturnValue({
  data: { subscription: { unsubscribe: () => {} } },
});
const signInWithPassword = vi.fn();
const signUp = vi.fn();
const signOut = vi.fn();

vi.mock('@supabase/supabase-js', () => ({
  createClient: () => ({
    auth: {
      getSession,
      onAuthStateChange,
      signInWithPassword,
      signUp,
      signOut,
    },
    from: vi.fn(),
  }),
}));

// Ending a dead session resets analytics and error reporting; neither SDK
// loads under this file's hand-made window.
vi.mock('./analytics', () => ({ resetIdentity: vi.fn() }));
vi.mock('./sentry', () => ({ clearSentryUser: vi.fn() }));

const STORAGE_KEY = 'sb-abcdefghijklmnopqrst-auth-token';

function seedStoredSession(opts: { expiresInSec: number; accessToken?: string }) {
  const blob = {
    access_token: opts.accessToken ?? 'tkn-stored',
    refresh_token: 'rfk',
    token_type: 'bearer',
    expires_at: Math.floor(Date.now() / 1000) + opts.expiresInSec,
    user: { id: 'user-1', email: 'brandon@example.com' },
  };
  memoryStorage.setItem(STORAGE_KEY, JSON.stringify(blob));
  return blob;
}

beforeEach(() => {
  vi.unstubAllEnvs();
  vi.stubEnv('VITE_SUPABASE_URL', 'https://abcdefghijklmnopqrst.supabase.co');
  vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'anon-key');
  memoryStorage.clear();
  getSession.mockReset();
  onAuthStateChange.mockClear();
  vi.resetModules();
});

afterEach(() => {
  getSession.mockReset();
  memoryStorage.clear();
});

describe('resolveDemoMode', () => {
  it('returns true when the env flag is on and no query param is present', async () => {
    const { resolveDemoMode } = await import('./auth');
    expect(resolveDemoMode('true', '')).toBe(true);
  });

  it('honors ?demo=0 as a dev escape hatch inside a demo build', async () => {
    const { resolveDemoMode } = await import('./auth');
    expect(resolveDemoMode('true', '?demo=0')).toBe(false);
  });

  it('returns false when the env flag is off, regardless of query', async () => {
    const { resolveDemoMode } = await import('./auth');
    expect(resolveDemoMode('false', '')).toBe(false);
  });

  // Regression: pre-fix, `?demo=1` flipped demoMode true on any build,
  // including prod. The build-time env flag is now the security gate.
  it('does NOT allow ?demo=1 to opt into demo mode on a prod build', async () => {
    const { resolveDemoMode } = await import('./auth');
    expect(resolveDemoMode('false', '?demo=1')).toBe(false);
  });

  it('returns false when VITE_DEMO_MODE is unset, even with ?demo=1', async () => {
    const { resolveDemoMode } = await import('./auth');
    expect(resolveDemoMode(undefined, '?demo=1')).toBe(false);
  });
});

describe('getAccessToken', () => {
  it('reads a fresh token from localStorage without calling getSession', async () => {
    seedStoredSession({ expiresInSec: 3600, accessToken: 'tkn-fresh' });

    const { getAccessToken } = await import('./auth');
    expect(await getAccessToken()).toBe('tkn-fresh');
    expect(getSession).not.toHaveBeenCalled();
  });

  it('falls through to getSession when no localStorage blob exists', async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: 'tkn-refreshed' } } });

    const { getAccessToken } = await import('./auth');
    expect(await getAccessToken()).toBe('tkn-refreshed');
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('falls through to getSession when the stored token has expired', async () => {
    // expires_at 10s ago — below the 30s freshness buffer.
    seedStoredSession({ expiresInSec: -10 });
    getSession.mockResolvedValue({ data: { session: { access_token: 'tkn-rotated' } } });

    const { getAccessToken } = await import('./auth');
    expect(await getAccessToken()).toBe('tkn-rotated');
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('falls through to getSession when the stored token is within the freshness buffer', async () => {
    // expires_at 15s from now — inside the 30s buffer, treat as expired.
    seedStoredSession({ expiresInSec: 15 });
    getSession.mockResolvedValue({ data: { session: { access_token: 'tkn-soon' } } });

    const { getAccessToken } = await import('./auth');
    expect(await getAccessToken()).toBe('tkn-soon');
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('coalesces concurrent slow-path callers into one getSession lookup', async () => {
    let resolveSession: (v: { data: { session: { access_token: string } | null } }) => void;
    const pending = new Promise<{ data: { session: { access_token: string } | null } }>(
      (r) => {
        resolveSession = r;
      },
    );
    getSession.mockReturnValue(pending);

    const { getAccessToken } = await import('./auth');
    const calls = [getAccessToken(), getAccessToken(), getAccessToken()];
    expect(getSession).toHaveBeenCalledTimes(1);

    resolveSession!({ data: { session: { access_token: 'tkn-coalesced' } } });
    expect(await Promise.all(calls)).toEqual([
      'tkn-coalesced',
      'tkn-coalesced',
      'tkn-coalesced',
    ]);
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('clears the in-flight cache on rejection so the next call retries', async () => {
    getSession
      .mockRejectedValueOnce(new Error('network blip'))
      .mockResolvedValueOnce({ data: { session: { access_token: 'tkn-recovered' } } });

    const { getAccessToken } = await import('./auth');
    await expect(getAccessToken()).rejects.toThrow('network blip');
    expect(await getAccessToken()).toBe('tkn-recovered');
  });

  it('subscribes to onAuthStateChange to invalidate the slow-path cache', async () => {
    onAuthStateChange.mockClear();
    await import('./auth');
    expect(onAuthStateChange).toHaveBeenCalled();
  });

  // Acceptance criterion #5 from the PR brief: two parallel callers
  // hit the slow path while a ``visibilitychange`` fires mid-flight.
  // Pre-fix, Supabase's auto-refresh loop would have stolen the Web
  // Lock here and one caller would have rejected with
  // ``Lock '…-auth-token' was released``. With the in-memory process
  // lock + localStorage fast path, both callers settle cleanly.
  it('survives a visibilitychange firing mid-flight: both parallel callers resolve', async () => {
    let resolveSession: (v: { data: { session: { access_token: string } | null } }) => void;
    const pending = new Promise<{ data: { session: { access_token: string } | null } }>(
      (r) => {
        resolveSession = r;
      },
    );
    getSession.mockReturnValueOnce(pending);

    const { getAccessToken } = await import('./auth');

    const a = getAccessToken();
    const b = getAccessToken();

    // Fire visibilitychange mid-flight — Supabase's _onVisibilityChanged
    // would have triggered a contending refresh here in the broken
    // implementation. Dispatch the event; nothing in our code should
    // throw or hang.
    (globalThis as { document: EventTarget }).document.dispatchEvent(
      new Event('visibilitychange'),
    );

    resolveSession!({ data: { session: { access_token: 'tkn-after-vis' } } });

    await expect(Promise.all([a, b])).resolves.toEqual([
      'tkn-after-vis',
      'tkn-after-vis',
    ]);
    expect(getSession).toHaveBeenCalledTimes(1);
  });
});

describe('getAccessToken when the refresh was rejected', () => {
  // supabase-js's answer after a refresh 400, measured in Chromium: no
  // session, an AuthApiError, and the blob left in storage.
  const rejected = {
    data: { session: null },
    error: { name: 'AuthApiError', status: 400, message: 'Invalid Refresh Token: Already Used' },
  };

  it('ends a signed-in session and flags the expiry for the sign-in form', async () => {
    seedStoredSession({ expiresInSec: -10 });
    getSession.mockResolvedValue(rejected);
    const { useStore } = await import('../stores/store');
    useStore.getState().setAuth({ id: 'user-1' } as never, null);

    const { getAccessToken } = await import('./auth');
    const { sessionExpired } = await import('./sessionEnd');
    expect(await getAccessToken()).toBeNull();

    expect(memoryStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(useStore.getState().user).toBeNull();
    expect(sessionExpired()).toBe(true);
  });

  it('announces nothing when nobody was signed in', async () => {
    getSession.mockResolvedValue(rejected);
    const { useStore } = await import('../stores/store');
    useStore.getState().clearAuth();

    const { getAccessToken } = await import('./auth');
    const { sessionExpired } = await import('./sessionEnd');
    expect(await getAccessToken()).toBeNull();
    expect(sessionExpired()).toBe(false);
  });

  it('keeps the session through a network failure', async () => {
    seedStoredSession({ expiresInSec: -10 });
    getSession.mockResolvedValue({
      data: { session: null },
      error: { name: 'AuthRetryableFetchError', status: 0, message: 'Failed to fetch' },
    });
    const { useStore } = await import('../stores/store');
    useStore.getState().setAuth({ id: 'user-1' } as never, null);

    const { getAccessToken } = await import('./auth');
    expect(await getAccessToken()).toBeNull();
    expect(memoryStorage.getItem(STORAGE_KEY)).not.toBeNull();
    expect(useStore.getState().user).not.toBeNull();
  });
});

describe('readStoredSession', () => {
  it('returns the parsed blob when localStorage has a session', async () => {
    seedStoredSession({ expiresInSec: 3600, accessToken: 'tkn-stored' });
    const { readStoredSession } = await import('./auth');
    const stored = readStoredSession();
    expect(stored?.access_token).toBe('tkn-stored');
    expect(stored?.user?.id).toBe('user-1');
  });

  it('returns null when no blob is present', async () => {
    const { readStoredSession } = await import('./auth');
    expect(readStoredSession()).toBeNull();
  });

  it('returns null when the blob is malformed', async () => {
    memoryStorage.setItem(STORAGE_KEY, 'not-json{');
    const { readStoredSession } = await import('./auth');
    expect(readStoredSession()).toBeNull();
  });
});

describe('fetchCompanyIds', () => {
  it('bypasses supabase.from and hits REST directly with the stored token', async () => {
    seedStoredSession({ expiresInSec: 3600, accessToken: 'tkn-rest' });

    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        new Response(JSON.stringify([{ company_id: 'company-A' }]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

    const { fetchCompanyIds } = await import('./auth');
    const ids = await fetchCompanyIds();
    expect(ids).toEqual(['company-A']);

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe(
      'https://abcdefghijklmnopqrst.supabase.co/rest/v1/company_users?select=company_id',
    );
    const headers = new Headers(init?.headers);
    expect(headers.get('apikey')).toBe('anon-key');
    expect(headers.get('Authorization')).toBe('Bearer tkn-rest');

    fetchSpy.mockRestore();
  });

  it('returns [] when there is no access token (signed out)', async () => {
    getSession.mockResolvedValue({ data: { session: null } });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const { fetchCompanyIds } = await import('./auth');
    expect(await fetchCompanyIds()).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();

    fetchSpy.mockRestore();
  });

  it('returns [] and logs when REST returns non-2xx', async () => {
    seedStoredSession({ expiresInSec: 3600 });
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('boom', { status: 500 }));
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { fetchCompanyIds } = await import('./auth');
    expect(await fetchCompanyIds()).toEqual([]);
    expect(errorSpy).toHaveBeenCalled();

    fetchSpy.mockRestore();
    errorSpy.mockRestore();
  });
});

describe('ensureCompanyProvisioned (PR #74 Bug 3)', () => {
  it('short-circuits when fetchCompanyIds already returns a company', async () => {
    seedStoredSession({ expiresInSec: 3600 });
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        new Response(JSON.stringify([{ company_id: 'company-A' }]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

    const { ensureCompanyProvisioned } = await import('./auth');
    const ids = await ensureCompanyProvisioned();
    expect(ids).toEqual(['company-A']);
    // Only the supabase REST fetch should have fired — the bootstrap
    // endpoint must NOT be called when the company is already there.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const firstCallUrl = String(fetchSpy.mock.calls[0][0]);
    expect(firstCallUrl).toContain('/rest/v1/company_users');

    fetchSpy.mockRestore();
  });

  it('calls the bootstrap endpoint then re-queries when the list is empty', async () => {
    seedStoredSession({ expiresInSec: 3600 });
    let call = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      call += 1;
      const u = String(url);
      if (u.includes('/api/v1/auth/bootstrap')) {
        return new Response(
          JSON.stringify({ company_id: 'company-just-bootstrapped' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      // First REST query → empty. Second REST query (after bootstrap)
      // → the just-upserted company.
      if (call === 1) {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(
        JSON.stringify([{ company_id: 'company-just-bootstrapped' }]),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    });

    const { ensureCompanyProvisioned } = await import('./auth');
    const ids = await ensureCompanyProvisioned();
    expect(ids).toEqual(['company-just-bootstrapped']);

    // Three calls: REST empty → bootstrap → REST again.
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/api/v1/auth/bootstrap'))).toBe(true);
    expect(urls.filter((u) => u.includes('/rest/v1/company_users'))).toHaveLength(2);

    fetchSpy.mockRestore();
  });

  it('logs and returns empty when bootstrap fails', async () => {
    seedStoredSession({ expiresInSec: 3600 });
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes('/api/v1/auth/bootstrap')) {
        return new Response('boom', { status: 500 });
      }
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { ensureCompanyProvisioned } = await import('./auth');
    const ids = await ensureCompanyProvisioned();
    expect(ids).toEqual([]);
    expect(errorSpy).toHaveBeenCalled();

    fetchSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it('returns [] without calling bootstrap when there is no access token', async () => {
    // No seedStoredSession — no token will be resolved.
    getSession.mockResolvedValue({ data: { session: null } });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const { ensureCompanyProvisioned } = await import('./auth');
    expect(await ensureCompanyProvisioned()).toEqual([]);
    // Bootstrap must NOT be hit anonymously.
    const urls = fetchSpy.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/api/v1/auth/bootstrap'))).toBe(false);

    fetchSpy.mockRestore();
  });
});
