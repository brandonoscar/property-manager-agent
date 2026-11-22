/**
 * The backend does not accept the credential this tab is holding.
 *
 * ⚠ This is NOT "the session expired", and the difference is the reason the
 * module exists. Supabase's client refreshes on its own and fires
 * `SIGNED_OUT` when a refresh finally fails, so a genuinely expired session
 * already has a path: `App.tsx` clears state, `sessionEnd` explains, the
 * login form renders. What had no path at all is the state where **Supabase
 * still holds a perfectly valid-looking session and the API rejects every
 * request made with it.**
 *
 * That state is not hypothetical — this product creates it deliberately.
 * `api/session_liveness.py` shipped so that "End" on Settings → Sessions
 * takes effect on the NEXT request instead of up to an hour later, and its
 * own header measures that hour: 3,481s between refreshes on a live session.
 * So the moment somebody ends another device's session, that device starts
 * 401ing immediately while its Supabase client knows nothing, renders the
 * app, and keeps rendering it until the access token expires on its own.
 *
 * What the person on that device saw before this module: every action
 * failing with *"Your session has expired. Sign in again in another tab"* —
 * a cause that is usually wrong (it was revoked, not expired) and an
 * instruction that cannot be carried out, because a new tab finds the stored
 * session, sets `user`, and renders the app rather than a sign-in form.
 *
 * ⚠ **Kept here rather than in `serviceHealth`, on purpose.** That module's
 * whole design is "is the backend there, and whose outage is it", and its own
 * header says a 401 *proves the backend is up*. Folding a credential fact
 * into a reachability fact would give this answer that module's guarantees
 * instead of its own.
 *
 * ⚠ **This module never signs anybody out.** It records the fact and offers
 * the act; the click is the user's. Clearing the session automatically would
 * break the one promise the message makes — that what they typed is still on
 * screen — on evidence that can be a single unlucky request.
 */

/**
 * Consecutive rejected requests before we say so.
 *
 * ⚠ Not 1, for `serviceHealth`'s stated reason: a banner that fires on a
 * blip is one people learn to scroll past. A token can expire in the gap
 * between `getAccessToken()` reading it and the server verifying it, and the
 * very next request refreshes and succeeds — so one 401 is a race, not a
 * verdict.
 *
 * It costs nothing in the case this exists for: a revoked or unverifiable
 * credential fails EVERY request, so the bar is met on the first screen that
 * loads more than one thing.
 */
export const CONSECUTIVE_REJECTIONS = 2;

type Listener = (rejected: boolean) => void;

let rejections = 0;
let rejected = false;
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) {
    // A throwing subscriber must not stop the others, and must never
    // propagate into the request that produced the signal — this is an
    // enhancement and is not allowed to break the call it describes.
    try {
      listener(rejected);
    } catch {
      /* a broken listener is not the request's problem */
    }
  }
}

/**
 * The backend answered 401 to a request that carried our credential.
 *
 * ⚠ Never call this for an ANONYMOUS request. A call that sent no
 * `Authorization` header says nothing about the credential we hold, and
 * counting it would announce a dead session to somebody whose session is
 * fine.
 */
export function reportCredentialRejected(): void {
  rejections += 1;
  const now = rejections >= CONSECUTIVE_REJECTIONS;
  if (now === rejected) return;
  rejected = now;
  emit();
}

/**
 * The backend answered something OTHER than 401 to a credentialed request.
 *
 * That is positive proof the credential is accepted — a 404 or a 422 reached
 * a route that had already authenticated the caller. So this clears the
 * count outright rather than decrementing it: the question is "is the
 * credential good right now", and one accepted request answers it.
 */
export function reportCredentialAccepted(): void {
  rejections = 0;
  // Only a CHANGE is emitted. A count reset from below the bar announced
  // nothing, so there is nothing to withdraw.
  if (!rejected) return;
  rejected = false;
  emit();
}

export function isSessionRejected(): boolean {
  return rejected;
}

/** Subscribe; returns the unsubscribe. The current value is NOT replayed —
 *  callers read `isSessionRejected()` for the initial render. */
export function onSessionRejectedChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Test hook. Never called by the product. */
export function resetSessionRejected(): void {
  rejections = 0;
  rejected = false;
  listeners.clear();
}
