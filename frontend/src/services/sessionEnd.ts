/**
 * Telling a DELIBERATE sign-out apart from one that happened TO you.
 *
 * ⚠ CORRECTION to an earlier audit note, which claimed "the JWT dies around
 * an hour and the app just starts 401ing". Measured: it does not. The client
 * is built with `autoRefreshToken: true`, `getAccessToken` refreshes on its
 * slow path, and `App.tsx` already handles `TOKEN_REFRESHED`. So a countdown
 * clock ticking toward an expiry that never arrives would be theatre — and a
 * warning that cries wolf is one people learn to dismiss.
 *
 * What DOES happen, with no explanation at all:
 *
 *   · the refresh token is revoked from another device ("sign out everywhere",
 *     a password change, an admin action) — every open tab dies mid-sentence;
 *   · a refresh genuinely fails and Supabase gives up;
 *   · the session outlives whatever ceiling the project has configured.
 *
 * In all three the app's `SIGNED_OUT` handler silently cleared state and
 * rendered the login screen. From the user's side that is indistinguishable
 * from the product crashing and throwing away what they were doing — and it
 * lands mid-task, which is the worst moment to be told nothing.
 *
 * Two facts fix it, and both need to survive a component unmount, so they
 * live here rather than in React state:
 *   1. WAS IT ON PURPOSE — set immediately before an intentional sign-out.
 *   2. WHERE WERE THEY — so signing back in returns them to it.
 */

/** Where the return-view is kept. sessionStorage, deliberately: per TAB and
 *  gone when the tab closes. localStorage would send somebody who signed out
 *  on Settings back to Settings a week later, which is not a resumption. */
const RETURN_VIEW_KEY = 'occupella.returnView';

/**
 * True only between `markIntentionalSignOut()` and the `SIGNED_OUT` that
 * follows it.
 *
 * ⚠ Module scope, not React state. The sign-out unmounts the tree that would
 * hold the flag, so a `useState` here reads its initial value on the very
 * event it exists to describe — which classes EVERY sign-out as involuntary
 * and shows the "your session ended" message to somebody who just clicked
 * Sign out.
 */
let intentional = false;

/** Call immediately BEFORE any sign-out the user asked for. */
export function markIntentionalSignOut(): void {
  intentional = true;
}

/**
 * Was the sign-out now being handled deliberate? Reading CONSUMES the flag,
 * so a later involuntary sign-out in the same tab is reported honestly rather
 * than inheriting an earlier click's intent.
 */
export function consumeSignOutIntent(): boolean {
  const was = intentional;
  intentional = false;
  return was;
}

/**
 * Remember the view to come back to.
 *
 * ⚠ Not called on an intentional sign-out FROM A SIGN-OUT CONTROL. Somebody
 * who clicks Sign out means to leave; dropping them back where they were on
 * the next sign-in would undo a choice they made on purpose.
 *
 * ⚠ THERE IS A THIRD CASE and the rule above does not cover it, which is why
 * this says "not called" rather than "never called". `SessionBanner`'s *Sign
 * in again* button signs out deliberately — so `performSignOut` marks it
 * intentional, correctly, because the banner has already explained and a
 * second "your session ended" message would be noise. But that person is not
 * leaving, they are getting back in, and the whole promise the banner makes
 * is that they return to the screen they were on. So it calls this ITSELF,
 * before signing out. The discriminator is intent to LEAVE, not the value of
 * the intentional flag.
 */
export function rememberReturnView(view: string): void {
  try {
    window.sessionStorage.setItem(RETURN_VIEW_KEY, view);
  } catch {
    // Private windows and blocked site data both throw here. Losing the
    // return view is a smaller failure than breaking sign-out, so it is
    // swallowed rather than surfaced.
  }
}

/**
 * The remembered view, or null. Reading CLEARS it — a return view is spent
 * once, and a stale one would yank a later, deliberate sign-in away from
 * wherever they meant to land.
 */
export function takeReturnView(): string | null {
  try {
    const value = window.sessionStorage.getItem(RETURN_VIEW_KEY);
    if (value) window.sessionStorage.removeItem(RETURN_VIEW_KEY);
    return value || null;
  } catch {
    return null;
  }
}

/**
 * Set by `expireSession` (auth.ts) when the app ends a session whose refresh
 * token was rejected. The sign-in form reads it once and says why it is
 * showing. Module scope for the same reason as `intentional`: the sign-out
 * unmounts the tree, and LoginForm mounts after it.
 */
let expired = false;

export function markSessionExpired(): void {
  expired = true;
}

/** Was the last sign-out an expiry? */
export function sessionExpired(): boolean {
  return expired;
}

/** Called once the sign-in form has shown the notice, so it is not shown
 *  again on a later, deliberate visit. A separate call rather than a
 *  consuming read: React may run a state initializer twice. */
export function clearSessionExpired(): void {
  expired = false;
}
