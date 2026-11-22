/**
 * Initial-sync progress — the "your Buildium data is still loading" card.
 *
 * `GET /buildium/sync-status` shipped with a docstring claiming "the frontend's
 * banner polls this" and had ZERO callers in all four repos. So a new customer
 * whose first crawl was still running asked a question and got told the mirror
 * was empty — a true statement about the database and a false one about their
 * portfolio, which is this product's worst failure shape (gotchas 26/42/55).
 *
 * Deliberately the same restraint as GettingStartedCard: a card above the chat
 * hero, token palette, 1px var(--line) border, no overlay, no spinner theatrics.
 * It retires itself the moment the sync finishes.
 *
 * ⚠ Three states, not two. `syncing` and `syncing-but-stale` need OPPOSITE
 * copy: one says "wait", the other says "this stopped". Collapsing them is how
 * the banner ends up promising progress that is never coming — a deploy
 * mid-crawl strands the row at 'syncing' permanently, and launch week is deploy
 * week.
 */

import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

import { fetchSyncStatus, type SyncStatus } from '../services/buildium';

/** Poll cadence while a sync is genuinely running. */
const POLL_MS = 15_000;

/** Stages worth naming; anything else is summed into the total. */
const STAGE_LABELS: Record<string, string> = {
  properties: 'properties',
  units: 'units',
  leases: 'leases',
  tenants: 'tenants',
  work_orders: 'work orders',
  bills: 'bills',
};

function summarise(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([, n]) => typeof n === 'number' && n > 0)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 3)
    .map(([k, n]) => `${n.toLocaleString()} ${STAGE_LABELS[k] ?? k.replace(/_/g, ' ')}`);
  return parts.join(' · ');
}

export function SyncProgressBanner() {
  const [state, setState] = useState<SyncStatus | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const next = await fetchSyncStatus();
        if (cancelled) return;
        setState(next);
        // Stop polling once there is nothing left to watch. A stale row is
        // terminal for our purposes: nothing is going to move it, so
        // re-asking every 15s is pure noise.
        if (next.status === 'syncing' && !next.stale) {
          timer.current = setTimeout(poll, POLL_MS);
        }
      } catch {
        // Progress is UX, never load-bearing. A failed poll hides the card
        // rather than showing an error the user cannot act on.
        if (!cancelled) setState(null);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  if (!state) return null;
  // 'done' | 'unknown' | 'pending' are all "nothing to say". `unknown` means no
  // sync was ever kicked (usually Buildium isn't connected) — the first-run
  // checklist already covers that, and two cards saying different things about
  // the same setup step is worse than one.
  if (state.status !== 'syncing' && state.status !== 'failed') return null;

  const stalled = state.status === 'failed' || state.stale;
  const progress = summarise(state.counts);

  return (
    <div
      className="mx-auto mb-3 w-full max-w-[720px] rounded-[var(--r-md)] border px-4 py-3"
      style={{
        borderColor: stalled ? 'var(--caution)' : 'var(--card-edge)',
        background: 'var(--canvas)',
      }}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-start gap-2.5">
        {stalled ? (
          <AlertTriangle
            className="mt-0.5 h-4 w-4 shrink-0"
            style={{ color: 'var(--caution)' }}
            aria-hidden
          />
        ) : (
          <RefreshCw
            className="mt-0.5 h-4 w-4 shrink-0 motion-safe:animate-spin"
            style={{ color: 'var(--ink-muted)' }}
            aria-hidden
          />
        )}
        <div className="min-w-0">
          <p className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
            {stalled
              ? 'Your Buildium import stopped before it finished'
              : 'Still importing your Buildium data'}
          </p>
          <p className="mt-1 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
            {stalled ? (
              <>
                Answers may be missing records until it finishes. You can re-run it from
                Settings → Connectors → Buildium.
                {progress && <> Imported so far: {progress}.</>}
              </>
            ) : (
              <>
                Answers may be incomplete until this finishes.
                {progress ? <> So far: {progress}.</> : <> This usually takes a few minutes.</>}
              </>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}
