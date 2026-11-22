/**
 * "This device's sign-in is no longer accepted" — and the one click that
 * fixes it.
 *
 * ⚠ **This strip exists because the instruction it replaces could not be
 * carried out.** A rejected credential used to surface as *"Your session has
 * expired. Sign in again in another tab"* on every failing action — and a
 * second tab finds the stored Supabase session, sets `user`, and renders the
 * app. There is no sign-in form to reach there without signing out first, so
 * the only person who could follow that advice was one who already knew to
 * ignore it.
 *
 * ⚠ **A strip with a BUTTON, where `ServiceBanner` is a strip with a
 * sentence — and the difference is not decoration.** An outage ends by
 * itself; this one does not. Supabase's client has no idea anything is
 * wrong (it holds a session that looks valid and will keep holding it until
 * the access token ages out — up to an hour, measured in
 * `api/session_liveness.py`), so nothing in the product will clear this
 * state unless somebody acts. Naming the act without offering it would leave
 * the user to find Sign out in the sidebar and work out why that helps.
 *
 * ⚠ **It does NOT sign anybody out on its own.** The message promises that
 * what they typed is still on screen, and clearing the session would break
 * that promise on evidence that could be two unlucky requests. The click is
 * the user's; the banner only makes it one click.
 */

import { toast } from 'sonner';

import { performSignOut } from '../services/auth';
import { rememberReturnView } from '../services/sessionEnd';
import { isSessionRejected, onSessionRejectedChange } from '../services/sessionRejected';
import { useStore } from '../stores/store';
import { useEffect, useState } from 'react';

/**
 * ⚠ NAMES NO CAUSE. The client cannot tell a revoked session from an expired
 * one from a signature it could not verify — the backend's 401 details say
 * so and are deliberately never shown (`api.ts`). Guessing "you were signed
 * out from another device" would be right often and wrong sometimes, on a
 * strip whose whole job is to be believed. The second sentence is the only
 * claim made, and it is one this component keeps: `rememberReturnView` runs
 * before the sign-out.
 */
export const SESSION_REJECTED_MESSAGE =
  'Occupella no longer accepts this device’s sign-in. Signing in again takes a moment and brings you back to this screen.';

export function SessionBanner() {
  const [rejected, setRejected] = useState<boolean>(() => isSessionRejected());
  const [working, setWorking] = useState(false);

  useEffect(() => {
    // Read again after commit as well as during render: listeners are told
    // about CHANGES only, so a banner that never reads the current value
    // stays silent through a rejection that began before it mounted.
    //
    // ⚠ The two reads are belt-and-braces for the render→commit window — the
    // initializer runs during render, this runs after commit, and a request
    // in flight can be rejected in between with no subscription attached yet.
    // Removing EITHER one alone changes nothing a test can observe (verified
    // by mutation); removing both fails. Recorded rather than pinned, for
    // `ServiceBanner`'s stated reason: a test asserting a distinction that
    // does not exist would only teach the next reader that it does.
    setRejected(isSessionRejected());
    return onSessionRejectedChange(setRejected);
  }, []);

  if (!rejected) return null;

  const signInAgain = async () => {
    setWorking(true);
    // ⚠ BEFORE `performSignOut`, which marks the sign-out intentional and so
    // suppresses the involuntary-sign-out path that would otherwise have
    // remembered this for us. The user did click a button, so "intentional"
    // is the right classification — but they are not leaving, they are
    // getting back in, and landing them on the default view would lose the
    // screen they were working on.
    try {
      rememberReturnView(useStore.getState().view);
    } catch {
      /* the return view is a convenience; never block the sign-out for it */
    }
    try {
      await performSignOut();
    } catch {
      // `performSignOut` already swallows its own network failure and clears
      // local state regardless, so reaching here means something further out
      // broke. Say so rather than leaving a button that looks stuck.
      setWorking(false);
      toast.error('Could not sign out. Reload the page and sign in again.');
    }
  };

  return (
    <div
      role="status"
      data-testid="session-banner"
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1.5 px-4 py-2 text-center"
      style={{
        background: 'var(--caution-bg)',
        borderBottom: '1px solid var(--line)',
        color: 'var(--ink)',
        fontSize: 'var(--text-body)',
      }}
    >
      <span>{SESSION_REJECTED_MESSAGE}</span>
      <button
        type="button"
        onClick={() => void signInAgain()}
        disabled={working}
        className="h-7 px-2.5 shrink-0 rounded-[var(--r-sm)] border text-[13px] transition-colors disabled:opacity-60"
        style={{
          borderColor: 'var(--line-strong)',
          background: 'var(--canvas)',
          color: 'var(--ink)',
        }}
      >
        {working ? 'Signing out…' : 'Sign in again'}
      </button>
    </div>
  );
}
