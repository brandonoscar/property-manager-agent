/**
 * Supabase auth service — manages authentication and company resolution.
 *
 * Token retrieval and company lookup deliberately bypass the
 * ``@supabase/supabase-js`` data path. Supabase's ``auth.getSession()``
 * and ``supabase.from(…).select(…)`` both acquire a ``navigator.locks``
 * Web Lock named ``lock:sb-<ref>-auth-token`` to serialise reads against
 * its internal auto-refresh loop. Under contention — multiple fetches
 * firing on /connectors mount while a visibility change triggers the
 * refresh loop — callers steal the lock from each other and throw
 * ``Lock "…" was released because another request stole it``, which
 * left the page blank.
 *
 * We replace the Web Lock with an in-memory process lock (safe for a
 * single-tab single-client app) so concurrent calls serialise within
 * the tab without involving the cross-tab Web Lock at all. Reads then
 * use the localStorage blob Supabase already persists, so they never
 * even contend with the process lock.
 */

import { createClient, type Session, type User } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

export const supabaseConfigured = Boolean(supabaseUrl && supabaseAnonKey);

/**
 * Resolve whether demo mode is active for this page load.
 *
 * The build-time ``VITE_DEMO_MODE`` env flag is the security gate: when
 * the build did not opt in, the function returns ``false`` regardless of
 * the URL — so a prod visitor cannot drop into the synthetic-data store
 * via ``?demo=1``. Inside a demo build, ``?demo=0`` is honored as a
 * dev-only escape hatch for iterating on the non-demo path without
 * having to rebuild with ``VITE_DEMO_MODE=false``.
 *
 * Exported so the unit tests can pin both axes (env on/off × query
 * variations) without having to stub ``import.meta.env`` and
 * ``window.location.search`` per case.
 */
export function resolveDemoMode(
  envValue: string | undefined,
  search: string,
): boolean {
  const envOn = String(envValue ?? '').toLowerCase() === 'true';
  if (!envOn) return false;
  const queryOff = new URLSearchParams(search).get('demo') === '0';
  return !queryOff;
}

export const demoMode = resolveDemoMode(
  import.meta.env.VITE_DEMO_MODE as string | undefined,
  typeof window !== 'undefined' ? window.location.search : '',
);

export const DEMO_USER = {
  id: 'demo-user',
  email: 'demo@occupella.local',
  app_metadata: {},
  user_metadata: { name: 'Demo User' },
  aud: 'authenticated',
  created_at: new Date().toISOString(),
} as unknown as User;

export const DEMO_SESSION = {
  access_token: 'demo-token',
  refresh_token: 'demo-refresh',
  expires_in: 3600,
  expires_at: Math.floor(Date.now() / 1000) + 3600,
  token_type: 'bearer',
  user: DEMO_USER,
} as unknown as Session;

export const DEMO_COMPANY_ID = 'demo-company';

if (!supabaseConfigured && !demoMode) {
  console.error(
    'Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY. ' +
      'Copy frontend/.env.example to frontend/.env and fill in your values.',
  );
}

// In-memory mutex chained through a single promise. Each acquisition
// awaits the previous one before running ``fn`` and then resolves the
// next slot. Concurrent calls serialise without touching the cross-tab
// ``navigator.locks`` API — so the "another request stole it" rejection
// class can't surface. Single-tab applications don't need cross-tab
// coordination; multi-tab would have to revisit this.
let processLockChain: Promise<unknown> = Promise.resolve();
const processLock = <R>(
  _name: string,
  _acquireTimeout: number,
  fn: () => Promise<R>,
): Promise<R> => {
  const run = processLockChain.then(() => fn());
  // Swallow rejection on the chain itself so one failed lock-holder
  // doesn't poison every subsequent acquisition.
  processLockChain = run.catch(() => undefined);
  return run;
};

export const supabase = createClient(
  supabaseUrl || 'https://placeholder.supabase.co',
  supabaseAnonKey || 'placeholder',
  {
    auth: {
      lock: processLock,
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  },
);

// Storage key Supabase uses for its session blob. The default format is
// ``sb-<project-ref>-auth-token`` where ``project-ref`` is the first DNS
// label of the project URL (e.g. ``abcdefghijklmnopqrst`` for
// ``https://abcdefghijklmnopqrst.supabase.co``).
const STORAGE_KEY = supabaseConfigured
  ? `sb-${new URL(supabaseUrl).hostname.split('.')[0]}-auth-token`
  : null;

interface StoredSession {
  access_token?: string;
  refresh_token?: string;
  token_type?: string;
  expires_at?: number;
  expires_in?: number;
  user?: User;
}

/**
 * Read the session blob Supabase persists in localStorage, without
 * going through ``supabase.auth.getSession()`` (which acquires the
 * Web Lock and can be stolen by the auto-refresh loop). Returns
 * ``null`` if there is no stored session or the blob is malformed.
 */
export function readStoredSession(): StoredSession | null {
  if (!STORAGE_KEY) return null;
  if (typeof window === 'undefined') return null;
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as StoredSession;
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

export type { Session, User };

export async function signIn(email: string, password: string) {
  const { data, error } = await supabase.auth.signInWithPassword({
    email,
    password,
  });
  if (error) throw error;
  return data;
}

/**
 * Create a new Occupella user account.
 *
 * Wraps Supabase's ``signUp`` with the ``emailRedirectTo`` set to the
 * current origin so the magic-link returns the user back to the app.
 * The DB-side trigger (``handle_new_user``, see migration 0003) auto-
 * creates a personal company + membership, so the user lands signed-in
 * with a working tenancy boundary on first request.
 *
 * Throws if Supabase rejects the credentials. Returns the
 * ``{user, session}`` object — when email confirmation is required
 * (Supabase project default) ``session`` is ``null`` and the caller
 * should show an "check your inbox" message.
 */
/**
 * The current Terms of Service + Privacy Policy version. Bump this string
 * whenever the terms change so acceptance is re-collected. Recorded against
 * each account at signup for a versioned audit trail.
 */
export const TERMS_VERSION = '2026-07-05';

export async function signUp(email: string, password: string, termsVersion?: string) {
  const redirectTo =
    typeof window !== 'undefined' ? window.location.origin : undefined;
  // Record the accepted terms version + timestamp on the user's metadata at
  // signup — a versioned acceptance trail. (Client-set user_metadata is fine
  // for the pre-launch wiring; a server-verified record via app_metadata / a
  // dedicated table is the hardening step before real customers.)
  const data_meta = termsVersion
    ? { tos_version: termsVersion, tos_accepted_at: new Date().toISOString() }
    : undefined;
  const options =
    redirectTo || data_meta
      ? { ...(redirectTo ? { emailRedirectTo: redirectTo } : {}), ...(data_meta ? { data: data_meta } : {}) }
      : undefined;
  const { data, error } = await supabase.auth.signUp({ email, password, options });
  if (error) throw error;
  return data;
}

/**
 * Re-send the signup confirmation email.
 *
 * The user's only self-service recovery when the first confirmation
 * email is slow or lost (found on the first real provisioning walk,
 * 2026-08-20: the project's built-in SMTP silently dropped it).
 * Supabase rate-limits resends (~60s) — callers gate the button behind
 * a cooldown; a rate-limit rejection here still throws so the form can
 * say "wait a minute".
 */
export async function resendConfirmation(email: string) {
  const redirectTo =
    typeof window !== 'undefined' ? window.location.origin : undefined;
  const { error } = await supabase.auth.resend({
    type: 'signup',
    email,
    ...(redirectTo ? { options: { emailRedirectTo: redirectTo } } : {}),
  });
  if (error) throw error;
}

/**
 * Send the password-reset email.
 *
 * ⚠ This function is why the recovery flow works at all. Every OTHER half
 * of it already existed and had been unreachable since it landed:
 * `authFlow.parsePasswordFlow` recognises `type=recovery`,
 * `authFlow.test.ts` asserts it, `SetPasswordForm` renders the
 * set-a-new-password screen, and `App.tsx` routes to it — but nothing
 * anywhere called `resetPasswordForEmail`, so no recovery link was ever
 * sent and none of that code could run. Same shape as `comms_triage`,
 * which was complete, unit-tested, and called by nothing.
 *
 * Until this shipped, a customer who forgot their password had NO way back
 * in; an admin had to reset it by hand in the Supabase dashboard.
 *
 * ⚠ LINK, not a 6-digit code — deliberately, and it is the one door that
 * differs. `verifySignupCode`'s note records the founder's call that "every
 * door confirms by CODE"; that was about SIGNUP, and signup's code lands the
 * user in a product they already have a password for. Recovery has to end on
 * a set-a-password screen, and the link flow already delivers exactly that
 * via the recovery hash. Moving recovery to a code would mean a second
 * set-password entry point beside a working one.
 *
 * ⚠ `redirectTo` must be an allowed Redirect URL in Supabase (Auth → URL
 * Configuration) or GoTrue silently sends the user to the Site URL instead —
 * the mail arrives, the link "works", and the user lands signed-in on the
 * app with no password prompt and no idea the reset didn't finish.
 *
 * ⚠ Never rejects for an unknown address, and that is not an oversight:
 * Supabase deliberately returns success either way so the form cannot be
 * used to enumerate who has an account. Callers must therefore show the same
 * message for a hit and a miss.
 */
export async function requestPasswordReset(email: string) {
  const redirectTo =
    typeof window !== 'undefined' ? window.location.origin : undefined;
  const { error } = await supabase.auth.resetPasswordForEmail(
    email,
    redirectTo ? { redirectTo } : undefined,
  );
  if (error) throw error;
}

/** The current password was wrong — distinguishable from the update failing. */
export class CurrentPasswordWrong extends Error {
  constructor() {
    super('That current password is incorrect.');
    this.name = 'CurrentPasswordWrong';
  }
}

/**
 * Change the signed-in user's password, after proving they know the old one.
 *
 * ⚠ `supabase.auth.updateUser({ password })` does NOT ask for the current
 * password — a live session is the only thing it requires. So on its own it
 * means anyone who reaches an unlocked laptop can change the password and lock
 * the real owner out of their own company's books. Verifying first is the
 * whole point of this function; calling `updateUser` directly from a settings
 * form would be the weaker thing that merely looks the same.
 *
 * The verification is a re-sign-in with the CURRENT password, which is
 * Supabase's own recommended check — there is no client-side "is this password
 * correct" call that avoids minting a session.
 *
 * ⚠ It fires `onAuthStateChange('SIGNED_IN')`, so `App.tsx` re-runs
 * `ensureCompanyProvisioned` + identify. That is idempotent for a user who is
 * already signed in — same person, same companies — and costs one backend
 * round trip on a rare action. Known and accepted, not overlooked.
 *
 * ⚠ A WRONG current password does not disturb the existing session: Supabase
 * returns an error and leaves the stored session alone. So a mistyped attempt
 * costs the attempt and nothing else — which is what makes this safe to put
 * behind a settings form at all.
 */
export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const email = (await supabase.auth.getUser()).data.user?.email;
  if (!email) throw new Error('You are not signed in.');

  const { error: verifyError } = await supabase.auth.signInWithPassword({
    email,
    password: currentPassword,
  });
  if (verifyError) throw new CurrentPasswordWrong();

  const { error } = await supabase.auth.updateUser({ password: newPassword });
  if (error) throw error;
}

/**
 * Change the signed-in user's email, after proving they know the password.
 *
 * ⚠ The re-auth is not optional here, and this is the STRONGER case of the
 * two. `changePassword` protects the password; this protects the ADDRESS every
 * future reset link is sent to — so an unlocked laptop plus one unguarded
 * `updateUser({ email })` is a complete account takeover: change the address,
 * sign out, "forgot password", and the real owner is locked out of their own
 * company's books with no way back. Same verification, higher stakes.
 *
 * ⚠ Supabase sends a confirmation to BOTH addresses when secure email change
 * is on (its default), and the change lands only when BOTH are clicked. That
 * is a feature — the old inbox gets a chance to refuse — but it means the UI
 * must NOT say "done", and the caller's copy says what actually has to happen
 * next. A form that reports success on a change still waiting on two clicks
 * teaches people the product is broken when the address does not move.
 */
export async function changeEmail(
  currentPassword: string,
  newEmail: string,
): Promise<void> {
  const current = (await supabase.auth.getUser()).data.user?.email;
  if (!current) throw new Error('You are not signed in.');
  if (current.toLowerCase() === newEmail.trim().toLowerCase()) {
    throw new Error('That is already your email address.');
  }

  const { error: verifyError } = await supabase.auth.signInWithPassword({
    email: current,
    password: currentPassword,
  });
  if (verifyError) throw new CurrentPasswordWrong();

  const { error } = await supabase.auth.updateUser({ email: newEmail.trim() });
  if (error) throw error;
}

/**
 * Revoke every session for this user, on every device, everywhere.
 *
 * The one control that answers "I signed in on a machine I no longer have."
 * Supabase has supported it the whole time — `scope: 'global'` — and nothing
 * called it, so a lost laptop meant changing the password and hoping that was
 * enough. It is not: a password change does not by itself invalidate refresh
 * tokens issued to other devices.
 *
 * ⚠ It signs THIS device out too, necessarily — "everywhere" includes here —
 * so the caller must treat it as a sign-out and run the same local cleanup
 * (`performSignOut`), or the app is left rendering a signed-in shell over a
 * dead session and every request 401s with no explanation.
 */
export async function signOutEverywhere(): Promise<void> {
  if (demoMode) return;
  const { error } = await supabase.auth.signOut({ scope: 'global' });
  if (error) throw error;
}

/**
 * Verify the 6-digit signup confirmation code from the email.
 *
 * The founder's call (2026-08-20 walk): every door confirms by CODE,
 * matching the onboarding wizard's flow — no link-clicking. The
 * "Confirm signup" email template renders ``{{ .Token }}``; this
 * verifies it. On success Supabase returns a live session, so the
 * user lands signed in immediately — no second sign-in step. Throws
 * on a wrong/expired code (callers show the message + keep the form).
 */
/**
 * "Email me a code": a one-time sign-in code for an EXISTING account.
 *
 * ⚠ `shouldCreateUser: false`. This screen signs people in; accounts are made
 * at occupella.com/start, which also creates the company. Without the flag an
 * unknown address would get a bare auth user with no company behind it.
 *
 * ⚠ The email Supabase sends is its "Magic Link" template. It shows a code
 * only if that template includes `{{ .Token }}`; otherwise it carries just the
 * link. `emailRedirectTo` is this origin so the link signs in here too
 * (`detectSessionInUrl`). NOT VERIFIED which the project's template sends.
 */
export async function sendSignInCode(email: string) {
  const redirectTo =
    typeof window !== 'undefined' ? window.location.origin : undefined;
  const { error } = await supabase.auth.signInWithOtp({
    email,
    options: { shouldCreateUser: false, ...(redirectTo ? { emailRedirectTo: redirectTo } : {}) },
  });
  if (error) throw error;
}

/** Finish "Email me a code". A live session fires SIGNED_IN in App.tsx. */
export async function verifySignInCode(email: string, code: string) {
  const { data, error } = await supabase.auth.verifyOtp({ email, token: code, type: 'email' });
  if (error) throw error;
  return data;
}

export async function verifySignupCode(email: string, code: string) {
  const { data, error } = await supabase.auth.verifyOtp({
    email,
    token: code,
    type: 'signup',
  });
  if (error) throw error;
  return data;
}

/**
 * Start the "Continue with Google" flow.
 *
 * Redirects to Google's consent screen; on return Supabase parses the
 * session out of the URL (the client is created with
 * ``detectSessionInUrl: true``) and the ``onAuthStateChange`` listener in
 * App.tsx transitions the app — so there's nothing to await here beyond
 * kicking off the redirect. ``redirectTo`` is the current origin so it
 * works in dev (localhost) and prod (Vercel) without hardcoding.
 *
 * This is distinct from the Composio Google connectors (Gmail/Calendar/
 * Drive) — that's the agent's data access; this is Supabase Auth sign-in.
 * Requires the Google provider enabled in the Supabase dashboard
 * (Authentication → Providers → Google) with this origin allowlisted as a
 * redirect URL; until then the redirect returns an error.
 */
export async function signInWithGoogle() {
  const redirectTo =
    typeof window !== 'undefined' ? window.location.origin : undefined;
  const { error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: redirectTo ? { redirectTo } : undefined,
  });
  if (error) throw error;
}

export async function signOut() {
  if (demoMode) return;
  const { error } = await supabase.auth.signOut();
  if (error) throw error;
}

/**
 * Full sign-out flow — call this from any UI exposing a Sign-out
 * affordance. Single source of truth for the sequence so the palette,
 * TitleBar avatar, Settings panel, and sidebar footer don't drift.
 *
 * Steps (each wrapped — failure in one doesn't block local cleanup):
 *   1. Best-effort Supabase signOut (no-op in demo mode).
 *   2. Reset PostHog identity so the next session is anonymous.
 *   3. Clear Sentry user so future reports aren't tagged with the old id.
 *   4. Clear the in-app store so the LoginForm re-renders.
 */
export async function performSignOut(options?: { everywhere?: boolean }): Promise<void> {
  // Imports are inlined to avoid a circular dep through stores/services.
  const [{ resetIdentity }, { clearSentryUser }, { useStore }, { markIntentionalSignOut }] =
    await Promise.all([
      import('./analytics'),
      import('./sentry'),
      import('../stores/store'),
      import('./sessionEnd'),
    ]);
  // BEFORE the network call, not after: Supabase fires SIGNED_OUT from inside
  // signOut(), so a flag set afterwards is still false when the handler that
  // reads it runs, and a deliberate sign-out gets reported to the user as
  // "your session ended".
  markIntentionalSignOut();
  try {
    await (options?.everywhere ? signOutEverywhere() : signOut());
  } catch {
    /* surface a toast at the call site if the network signout matters */
  }
  try {
    resetIdentity();
  } catch {
    /* analytics is best-effort */
  }
  try {
    clearSentryUser();
  } catch {
    /* sentry is best-effort */
  }
  useStore.getState().clearAuth();
}

// Treat tokens with less than this many seconds remaining as expired
// so we refresh before the access token actually ages out — covers
// clock skew + a short network round-trip.
const TOKEN_FRESHNESS_BUFFER_SEC = 30;

export async function getAccessToken(): Promise<string | null> {
  if (demoMode) return DEMO_SESSION.access_token;

  // Fast path: read the persisted session blob directly. No lock
  // acquisition, no race with Supabase's auto-refresh loop. The blob
  // is the source of truth Supabase itself reads from on init.
  const stored = readStoredSession();
  if (stored?.access_token && stored.expires_at) {
    const nowSec = Math.floor(Date.now() / 1000);
    if (stored.expires_at - nowSec > TOKEN_FRESHNESS_BUFFER_SEC) {
      return stored.access_token;
    }
  }

  // Slow path: blob missing, malformed, or expired. Coalesce concurrent
  // refresh attempts into one ``getSession`` call so we don't queue N
  // refreshes against the process lock.
  if (inflightAccessToken) return inflightAccessToken;
  inflightAccessToken = supabase.auth
    .getSession()
    .then(async ({ data: { session }, error }) => {
      if (session?.access_token) return session.access_token;
      // No token and it is not a network blip: the session is over. Say so
      // and show sign-in, rather than leave a signed-in shell whose every
      // call fails without a request.
      if (!isRetryableAuthError(error)) await expireSession();
      return null;
    })
    .finally(() => {
      inflightAccessToken = null;
    });
  return inflightAccessToken;
}

let inflightAccessToken: Promise<string | null> | null = null;

/** supabase-js's name for a refresh that failed on the network or a 5xx,
 *  where the session may still be good. Compared by name so this module
 *  does not need the class at runtime. */
function isRetryableAuthError(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === 'AuthRetryableFetchError';
}

/**
 * The session this tab is holding can no longer produce a token: the
 * refresh token was rejected, or the stored session is gone.
 *
 * ⚠ WHY THIS EXISTS. Measured against supabase-js 2.112 with a refresh
 * endpoint answering 400: after the first failed refresh, the client keeps
 * the stored session and caches the failure for 60 seconds. For that minute
 * `getSession()` returns no session and NO error-driven sign-out, so the
 * store still has `user`, the shell stays up, and every `apiFetch` throws
 * "You are not signed in" without sending a request. Retrying cannot help:
 * it hits the cached failure. The fix is to end the session here.
 *
 * ⚠ NOT `supabase.auth.signOut()`. In this state its own session read fails
 * first and it returns that error WITHOUT clearing anything (checked in
 * GoTrueClient `_signOut`). So the stored blob is removed directly.
 *
 * Only acts when the store has a signed-in user. A token lookup on the
 * sign-in screen must not announce an expiry to somebody who was never in.
 */
let expiring: Promise<void> | null = null;
export function expireSession(): Promise<void> {
  // Demo builds and unconfigured builds hold no Supabase session to end.
  if (demoMode || !supabaseConfigured) return Promise.resolve();
  expiring ??= (async () => {
    const { useStore } = await import('../stores/store');
    const state = useStore.getState();
    if (!state.user) return;
    const [sessionEnd, { resetIdentity }, { clearSentryUser }] = await Promise.all([
      import('./sessionEnd'),
      import('./analytics'),
      import('./sentry'),
    ]);
    sessionEnd.rememberReturnView(state.view);
    sessionEnd.markSessionExpired();
    // signOut below emits SIGNED_OUT when it can; the handler in App.tsx
    // must not add a toast on top of the sign-in notice.
    sessionEnd.markIntentionalSignOut();
    if (STORAGE_KEY) {
      try {
        window.localStorage.removeItem(STORAGE_KEY);
        window.localStorage.removeItem(`${STORAGE_KEY}-user`);
      } catch {
        /* blocked storage: clearing the store below still shows sign-in */
      }
    }
    try {
      await supabase.auth.signOut({ scope: 'local' });
    } catch {
      /* the local cleanup above is what matters */
    }
    // If signOut emitted nothing, the flag set above is still up and would
    // swallow the message for a later sign-out that nobody asked for.
    sessionEnd.consumeSignOutIntent();
    try {
      resetIdentity();
      clearSentryUser();
    } catch {
      /* analytics and error reporting are best-effort */
    }
    useStore.getState().clearAuth();
  })().finally(() => {
    expiring = null;
  });
  return expiring;
}


// Auth state transitions invalidate any in-flight lookup: a token
// refresh / sign-in / sign-out fired during the call means the
// resolved token is already stale by the time it returns. Drop the
// cache so the next caller refetches. The subscription is module-
// level — it lives for the page lifetime, same as the supabase
// client itself.
supabase.auth.onAuthStateChange(() => {
  inflightAccessToken = null;
});

/**
 * Resolve the user's company memberships.
 *
 * Uses a direct ``fetch`` to Supabase REST rather than
 * ``supabase.from(…).select(…)``. The latter internally calls
 * ``auth.getSession()`` to attach the JWT, which acquires the same
 * Web Lock that gets stolen on /connectors mount. A bare ``fetch``
 * with the token we already have on hand sidesteps that entire path.
 */
export async function fetchCompanyIds(): Promise<string[]> {
  if (demoMode) return [DEMO_COMPANY_ID];
  if (!supabaseConfigured) return [];

  const token = await getAccessToken();
  if (!token) return [];

  try {
    const res = await fetch(
      `${supabaseUrl}/rest/v1/company_users?select=company_id`,
      {
        headers: {
          apikey: supabaseAnonKey,
          Authorization: `Bearer ${token}`,
        },
      },
    );
    if (!res.ok) {
      console.error('Failed to fetch companies: HTTP', res.status);
      return [];
    }
    const rows = (await res.json()) as Array<{ company_id: string }>;
    return rows.map((row) => row.company_id);
  } catch (err) {
    console.error('Failed to fetch companies:', err);
    return [];
  }
}

/**
 * Ensure the calling user has at least one company membership.
 *
 * PR #74 Bug 3 — Brandon's second sandbox user signed up cleanly but
 * landed at the chat with no ``company_users`` row, and typing tripped
 * the ``"No company selected."`` toast. The trigger that normally
 * creates a personal company on signup
 * (``handle_new_user`` from migration 0012) didn't fire for that user
 * — either an attachment gap, a race, or a pre-0012 broken signup.
 *
 * This helper is the self-heal path. ``fetchCompanyIds()`` first; if
 * the list is empty, POST to ``/api/v1/auth/bootstrap`` (idempotent
 * upsert on the backend) and re-query. The second call returns the
 * just-upserted personal-company id, so the chat surface immediately
 * has a company to scope against.
 *
 * Returns the same shape as ``fetchCompanyIds`` so callers can use it
 * as a drop-in replacement. Demo mode skips the bootstrap step (the
 * demo company id is already wired through).
 */
export async function ensureCompanyProvisioned(): Promise<string[]> {
  const ids = await fetchCompanyIds();
  if (ids.length > 0) return ids;
  if (demoMode) return ids; // demoMode short-circuit handles itself
  if (!supabaseConfigured) return ids;

  const token = await getAccessToken();
  if (!token) return ids;

  try {
    const res = await fetch('/api/v1/auth/bootstrap', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    });
    if (!res.ok) {
      console.error('Bootstrap company provision failed: HTTP', res.status);
      return ids;
    }
  } catch (err) {
    console.error('Bootstrap company provision failed:', err);
    return ids;
  }
  // Re-query so callers see the just-upserted personal company id.
  return await fetchCompanyIds();
}
