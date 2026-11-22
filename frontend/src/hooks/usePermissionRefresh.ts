/**
 * Re-run a loader when the backend says the caller's permissions changed.
 *
 * WHAT THIS IS FOR
 * ----------------
 * Two screens cache the caller's role from `GET /company/me` on mount:
 * `SettingsPage` and `LeasingComplianceGate` (a third, `InviteTeamBanner`,
 * was removed on 2026-09-30 when the invite nudge moved into the Getting
 * started checklist). An admin
 * demoted to member went on seeing admin affordances until something remounted
 * that screen — which, on a tab a property manager leaves open all day, is
 * "never".
 *
 * The backend was never wrong. `require_role` reads `company_users` on every
 * request, so the button returns a 403 with a real sentence when clicked. The
 * only defect is that the button was still there. This closes that.
 *
 * WHY A SUBSCRIPTION AND NOT A SHARED `useMyRole`
 * -----------------------------------------------
 * ⚠ The obvious move — one hook that owns the role and hands it to every caller —
 * was written and then deleted, because the call sites are not the same
 * shape and folding them together would have changed the gate:
 *
 *   * `SettingsPage` just stores the role.
 *   * `LeasingComplianceGate` distinguishes a FAILED probe from a low role,
 *     and shows a different panel for each. A shared hook that swallows the
 *     error to keep the last good value would erase that distinction.
 *
 * So each screen keeps its own loader and this only says WHEN to run it again.
 * That is also why it takes a callback rather than returning data: the thing
 * they disagree about is the handling, not the fetching.
 *
 * ⚠ **It can only narrow.** Re-running a loader replaces the cached role with
 * whatever the server now says. This is not a permission check — the server's
 * `require_role` is, on every call — so a bug here costs a stale button, never
 * an unauthorised action.
 */

import { useEffect, useRef } from 'react';

import { onPermissionDenied } from '../services/api';

/**
 * Call `reload` whenever any API request comes back 403.
 *
 * `reload` is held in a ref, so a caller may pass an inline arrow without
 * re-subscribing on every render — the alternative is every consumer having to
 * remember `useCallback`, and forgetting it re-subscribes in a loop.
 */
export function usePermissionRefresh(reload: () => void): void {
  const latest = useRef(reload);
  latest.current = reload;

  useEffect(() => {
    return onPermissionDenied(() => latest.current());
  }, []);
}
