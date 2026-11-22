/**
 * How many things are waiting in the Inbox, for people who are not looking at
 * the Inbox.
 *
 * The Inbox is this product's notification surface — that part of the design
 * is right and a separate bell would be a second answer to the same question.
 * What was missing is that the surface only spoke when you were standing on
 * it: the page says "4 new items need your attention" and the sidebar said
 * nothing, so somebody working in Chat or Operations had no idea. `NavItem`
 * has carried a `badge` prop through both of its render branches and its
 * collapsed tooltip the whole time, and nothing ever passed one.
 *
 * ⚠ `null` means UNKNOWN and must never render as zero. Zero is a claim —
 * "your inbox is clear" — and a failed fetch has not earned it. The badge only
 * draws above 0, so an unknown count silently draws nothing, which is the
 * right shape: a signal that cannot be trusted should be absent, not
 * reassuring (the same decline-to-assert the mirror rollups take).
 *
 * ⚠ Deliberately SLOWER than the Inbox's own 15s poll. This runs for every
 * signed-in person on every page, forever, and it exists to answer "is there
 * anything" rather than to be live — the page that needs live already polls
 * fast while it is open, and writes its number here as it goes.
 */

import { useEffect } from 'react';

import { demoMode } from '../services/auth';
import { demoInboxRows } from '../services/demoData';
import { listInboxRows } from '../services/inbox';
import { useStore } from '../stores/store';

/** Quiet enough to be free, often enough that a badge is not stale for long. */
export const INBOX_COUNT_POLL_MS = 90_000;

export function useInboxCount(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) {
      // Signed out, or in the invite / password-recovery flow. Clear rather
      // than leave the previous person's number on screen.
      useStore.getState().setInboxCount(null);
      return;
    }
    // ⚠ Demo mode has no backend, so the poll below correctly fails and the
    // badge stays silent — which reproduces, in the demo, exactly the defect
    // this fixes: the Inbox announcing four items while the rail says
    // nothing. `InboxPage` already branches here for the same reason.
    if (demoMode) {
      useStore.getState().setInboxCount(demoInboxRows().length);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        // ⚠ Unfiltered, always. `listInboxRows(true)` is the "mine" view, and
        // a badge fed by it would quietly under-report for anybody who had
        // ever used that filter — a wrong number that looks like a right one.
        const rows = await listInboxRows(false);
        if (!cancelled) useStore.getState().setInboxCount(rows.length);
      } catch {
        // Leave whatever was last known. Replacing a real count with 0 on a
        // network blip tells somebody their inbox emptied itself.
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), INBOX_COUNT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [enabled]);
}
