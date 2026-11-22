/**
 * How current the mirrored Buildium data on this screen is.
 *
 * Chat has said "as of <date>" since migration 0057; every PAGE showing the
 * same rows said nothing. A property manager reading the Operations grid had
 * no way to know it is a cached copy of Buildium, let alone how old — and
 * gotcha 57 is two engineering sessions misreading a sync window as a missing
 * feature. A user makes that mistake faster and with less context.
 *
 * ⚠ `isStale` is the SERVER'S verdict, not a threshold to re-apply here. The
 * same rule as billing's `block`: a client that recombines `staleHours` into
 * its own decision eventually disagrees with the one the chat footer states,
 * and then two places on one screen contradict each other.
 */

import { apiFetch } from './api';
import { pmsInfo, type PmsId } from '../lib/pms';

export interface MirrorFreshness {
  /** Newest core sync. Null = never synced — render as such, never "just now". */
  asOf: string | null;
  stalestTable: string | null;
  stalestAt: string | null;
  staleHours: number | null;
  isStale: boolean;
  /** False when nothing has synced OR the read failed. Both mean "cannot say". */
  hasData: boolean;
  /**
   * Which system the mirror is copied from (`lib/pms.ts`). `null` when the
   * backend could not say, or named a system this build does not know;
   * callers keep their Buildium wording then, as before Rentvine.
   */
  pms: PmsId | null;
  /** "Rentvine", for "Last sync from Rentvine". */
  pmsName: string | null;
}

interface Wire {
  as_of?: string | null;
  stalest_table?: string | null;
  stalest_at?: string | null;
  stale_hours?: number | null;
  is_stale?: boolean;
  has_data?: boolean;
  pms?: string | null;
  pms_name?: string | null;
}

export async function fetchMirrorFreshness(): Promise<MirrorFreshness> {
  const res = await apiFetch('/api/v1/mirror/freshness');
  if (!res.ok) throw new Error('Could not read sync status.');
  const w: Wire = await res.json();
  return {
    asOf: w.as_of ?? null,
    stalestTable: w.stalest_table ?? null,
    stalestAt: w.stalest_at ?? null,
    staleHours: w.stale_hours ?? null,
    isStale: w.is_stale ?? false,
    hasData: w.has_data ?? false,
    pms: pmsInfo(w.pms)?.id ?? null,
    pmsName: w.pms_name ?? null,
  };
}

/**
 * "2 hours ago" — the caption a person reads.
 *
 * ⚠ Returns null rather than a placeholder when there is nothing to say.
 * A caption is decoration; inventing "just now" for an account that has never
 * synced is the false-currency claim this whole feature exists to prevent
 * (gotcha 26's class: unknown must not render as current).
 */
export function freshnessLabel(f: MirrorFreshness | null, now: number = Date.now()): string | null {
  if (!f) return null;
  if (!f.hasData || !f.asOf) return 'Sync status unknown';

  const ms = now - new Date(f.asOf).getTime();
  if (Number.isNaN(ms)) return 'Sync status unknown';

  const mins = Math.floor(ms / 60_000);
  if (mins < 2) return 'Synced just now';
  if (mins < 60) return `Synced ${mins} minutes ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `Synced ${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `Synced ${days} day${days === 1 ? '' : 's'} ago`;
}

/**
 * The extra sentence when one table has fallen behind the rest.
 *
 * ⚠ This is the honesty gap 0057 was built for: a lease row synced a minute
 * ago hides day-old bills behind it, so the newest timestamp alone reads as
 * "everything is current" when it is not. Names the TABLE, because "some data
 * is stale" tells a reader nothing they can act on.
 */
export function stalenessNote(f: MirrorFreshness | null): string | null {
  if (!f?.isStale || !f.stalestTable || f.staleHours == null) return null;
  const table = f.stalestTable.replace(/_/g, ' ');
  return `${table} last synced ${f.staleHours}h ago`;
}
