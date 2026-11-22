/**
 * "Synced 2 hours ago" — one caption, on every screen showing mirrored data.
 *
 * ⚠ The exact time lives on `title`, the relative one in the text. A reader
 * scanning a grid wants "is this current"; a reader about to act on a number
 * wants the timestamp, and making them compute it from "3 days ago" is how a
 * date gets misremembered by one.
 *
 * ⚠ It renders SOMETHING whenever it has been asked to render, including
 * "Sync status unknown". A caption that disappears when the answer is bad is
 * worse than none: the screen then looks identical whether the data is fresh
 * or the sync has been dead for a week.
 */

import { useEffect, useState } from 'react';

import {
  fetchMirrorFreshness,
  freshnessLabel,
  stalenessNote,
  type MirrorFreshness,
} from '../services/mirrorFreshness';

export function DataFreshness({
  className = '',
  /**
   * Change this to re-read the stamp.
   *
   * ⚠ The fetch is mount-only, so a caller that has just resynced would
   * otherwise keep showing the age it displayed BEFORE the sync — the one
   * number the user was watching, frozen at the value that made them click.
   */
  reloadKey = 0,
  /**
   * Hands the parent what was read, so a page that also needs to know which
   * system it syncs from (the Operations page's Re-sync) doesn't fetch the
   * same endpoint a second time.
   */
  onLoad,
}: {
  className?: string;
  reloadKey?: number;
  onLoad?: (freshness: MirrorFreshness) => void;
}) {
  const [freshness, setFreshness] = useState<MirrorFreshness | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchMirrorFreshness()
      .then((f) => {
        if (cancelled) return;
        setFreshness(f);
        onLoad?.(f);
      })
      .catch(() => {
        // ⚠ Say so rather than vanishing. An unreadable sync status and a
        // healthy one must not look the same.
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
    // `onLoad` is deliberately not a dependency: a parent passing an inline
    // callback would otherwise refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  const label = failed ? 'Sync status unknown' : freshnessLabel(freshness);
  if (!label) return null;

  const note = stalenessNote(freshness);
  const exact = freshness?.asOf ? new Date(freshness.asOf).toLocaleString() : undefined;

  return (
    <span
      className={`inline-flex items-center gap-1.5 text-[12px] tabular-nums ${className}`}
      style={{ color: 'var(--ink-faint)' }}
      title={
        exact
          ? `Last sync from ${freshness?.pmsName ?? 'Buildium'}: ${exact}`
          : 'Sync status could not be read'
      }
    >
      <span>{label}</span>
      {note && (
        <span style={{ color: 'var(--warning, var(--ink-muted))' }}>· {note}</span>
      )}
    </span>
  );
}
