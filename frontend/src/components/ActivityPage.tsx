/**
 * Team Activity — the audit log (PR #70 rewrite, deepened Workstream D).
 *
 * Reads ``GET /api/v1/activity`` and renders rows reverse-chronologically,
 * grouped by calendar day (Today / Yesterday / date headers). Two filter
 * dimensions narrow the feed server-side: a **category** (CRM / Buildium /
 * Assistant / Team / Scheduled) and a **person** (any teammate who has
 * activity). Each row carries a category badge so the stream scans at a
 * glance instead of reading as an undifferentiated list.
 *
 * Infinite scroll via ``IntersectionObserver`` on a sentinel at the list
 * bottom: when it enters the viewport we fetch the next page using the
 * previous response's ``next_cursor``. Changing a filter resets the feed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';

import { demoMode } from '../services/auth';
import { demoActivityPage } from '../services/demoData';
import { RefreshButton } from './RefreshButton';
import { SkeletonListRow } from './Skeleton';
import { ErrorState } from './ErrorState';
import { useStillLoading } from '../hooks/useStillLoading';
import {
  fetchActivity,
  fetchActivityActors,
  type ActivityActor,
  type ActivityCategory,
  type ActivityRow,
} from '../services/activity';
import { ApiError } from '../services/api';

const PAGE_SIZE = 50;

const CATEGORY_OPTIONS: { id: ActivityCategory; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'crm', label: 'CRM' },
  { id: 'buildium', label: 'Buildium' },
  { id: 'assistant', label: 'Assistant' },
  { id: 'team', label: 'Team' },
  { id: 'scheduled', label: 'Scheduled' },
];

export function ActivityPage() {
  const [category, setCategory] = useState<ActivityCategory>('all');
  // Person filter: null = everyone, else a teammate user_id.
  const [actor, setActor] = useState<string | null>(null);
  const [actors, setActors] = useState<ActivityActor[]>([]);
  const [rows, setRows] = useState<ActivityRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Single in-flight token — guards the IntersectionObserver against
  // firing twice during a slow fetch.
  const inFlightRef = useRef(false);

  const loadPage = useCallback(
    async (nextCategory: ActivityCategory, nextActor: string | null, nextCursor: string | null) => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      setLoading(true);
      try {
        const page = await fetchActivity(
          { category: nextCategory, actor: nextActor },
          nextCursor,
          PAGE_SIZE,
        );
        setRows((prev) => (nextCursor === null ? page.rows : [...prev, ...page.rows]));
        setCursor(page.next_cursor);
        setHasMore(page.next_cursor !== null);
        setLoadError(null);
      } catch (err) {
        console.warn('[activity] fetch failed', err);
        const message =
          err instanceof ApiError || err instanceof Error
            ? err.message
            : 'Could not load activity.';
        setLoadError(message);
        setHasMore(false);
      } finally {
        setLoading(false);
        inFlightRef.current = false;
      }
    },
    [],
  );

  // Populate the person dropdown once (real mode only) from the actors
  // endpoint, so it lists only people who actually have activity.
  useEffect(() => {
    if (demoMode) {
      setActors(demoActorsFromRows(demoActivityPage().rows));
      return;
    }
    let cancelled = false;
    void fetchActivityActors()
      .then((list) => {
        if (!cancelled) setActors(list);
      })
      .catch((err) => console.warn('[activity] actors fetch failed', err));
    return () => {
      cancelled = true;
    };
  }, []);

  // Reset + load whenever a filter changes.
  useEffect(() => {
    if (demoMode) {
      // Demo isolation: in-memory fixtures, no network. Apply the same
      // filters client-side so the controls feel live.
      const all = demoActivityPage().rows;
      setRows(all.filter((r) => matchesCategory(r, category) && matchesActor(r, actor)));
      setCursor(null);
      setHasMore(false);
      setLoadError(null);
      return;
    }
    setRows([]);
    setCursor(null);
    setHasMore(true);
    void loadPage(category, actor, null);
  }, [category, actor, loadPage]);

  // Infinite-scroll sentinel.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (demoMode) return;
    if (!sentinelRef.current) return;
    if (!hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && cursor) {
            void loadPage(category, actor, cursor);
          }
        }
      },
      { rootMargin: '120px' },
    );
    observer.observe(sentinelRef.current);
    return () => observer.disconnect();
  }, [cursor, hasMore, category, actor, loadPage]);

  const groups = useMemo(() => groupByDay(rows), [rows]);
  const filtered = category !== 'all' || actor !== null;

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      <header className="shrink-0 border-b" style={{ borderColor: 'var(--line)' }}>
        <div className="px-12 mx-auto w-full" style={{ maxWidth: '1100px' }}>
          <div className="h-12 flex items-center">
            {/* A Settings section heading, like "Billing" and "APIs"; the
                page title is Settings' own. */}
            <h2
              className="text-[16px] font-semibold tracking-tight"
              style={{ color: 'var(--ink)', letterSpacing: '-0.01em' }}
            >
              Team Activity
            </h2>
            <span className="flex-1" />
            {!demoMode && (
              <RefreshButton
                onClick={() => {
                  // Same reset the filter effect does — a refresh that
                  // appended to the existing rows would show yesterday's
                  // list with today's on the end.
                  setRows([]);
                  setCursor(null);
                  setHasMore(true);
                  void loadPage(category, actor, null);
                }}
                busy={loading}
                label="activity"
                testId="activity-refresh"
              />
            )}
            <PersonFilter actors={actors} value={actor} onChange={setActor} />
          </div>
          {/* Category chips — a horizontal, scrollable filter row. */}
          <div className="flex items-center gap-1.5 pb-2.5 -mt-0.5 overflow-x-auto">
            {CATEGORY_OPTIONS.map((opt) => (
              <CategoryChip
                key={opt.id}
                label={opt.label}
                active={category === opt.id}
                onClick={() => setCategory(opt.id)}
              />
            ))}
          </div>
        </div>
      </header>

      <div className="flex-1 min-w-0 overflow-y-auto">
        <div className="mx-auto w-full px-12 py-6" style={{ maxWidth: '1100px' }}>
          {rows.length === 0 && loading && !loadError && (
            <InitialLoadingState onCancel={() => setLoading(false)} />
          )}

          {rows.length === 0 && !loading && !loadError && (
            <EmptyState
              title={filtered ? 'No activity matches these filters.' : 'No activity yet.'}
              subtitle={
                filtered
                  ? 'Try a different category or person — or clear the filters to see everything.'
                  : "Once Occupella takes an action you'll see it here within a few seconds."
              }
              ctaLabel={filtered ? 'Clear filters' : undefined}
              onCta={
                filtered
                  ? () => {
                      setCategory('all');
                      setActor(null);
                    }
                  : undefined
              }
            />
          )}

          {loadError && rows.length === 0 && (
            <ErrorState
              title="Couldn't load activity"
              body={loadError}
              onRetry={() => void loadPage(category, actor, null)}
              data-testid="activity-error"
            />
          )}

          {groups.map((group) => (
            <section key={group.key} className="mb-5">
              <div
                className="sticky top-0 z-[1] -mx-2 px-2 py-1.5 mb-0.5 text-[12px] font-semibold uppercase tracking-wide"
                style={{ color: 'var(--ink-subtle)', background: 'var(--canvas)' }}
                data-testid="activity-day-header"
              >
                {group.label}
              </div>
              {group.rows.map((row, i) => (
                <ActivityRowItem key={row.id} row={row} staggerIndex={i} />
              ))}
            </section>
          ))}

          {hasMore && (
            <div ref={sentinelRef} className="h-8 flex items-center justify-center">
              <span className="text-[12px]" style={{ color: 'var(--ink-faint)' }}>
                {loading ? 'Loading…' : ' '}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── category classification + tone ──────────────────────────────────
//
// Kept in lock-step with the backend ``_CATEGORY_PREDICATES`` so a row's
// on-screen badge matches the bucket its category filter would place it in.

type CategoryTone = 'crm' | 'buildium' | 'assistant' | 'team' | 'neutral';

interface RowCategory {
  key: ActivityCategory | 'other';
  label: string;
  tone: CategoryTone;
}

function categorize(row: ActivityRow): RowCategory {
  const at = row.action_type || '';
  if (at.startsWith('crm_')) return { key: 'crm', label: 'CRM', tone: 'crm' };
  if (at.startsWith('member_')) return { key: 'team', label: 'Team', tone: 'team' };
  if (row.source === 'webhook') return { key: 'buildium', label: 'Buildium', tone: 'buildium' };
  if (row.source === 'chat') return { key: 'assistant', label: 'Assistant', tone: 'assistant' };
  if (row.source === 'scheduled') return { key: 'scheduled', label: 'Scheduled', tone: 'neutral' };
  return { key: 'other', label: row.source_label || row.source || 'Activity', tone: 'neutral' };
}

/** Foreground / background tokens per tone. Colour is emphasis-only
 *  (white-default rule): each badge is a soft tint, never a loud fill. */
function toneColors(tone: CategoryTone): { fg: string; bg: string } {
  switch (tone) {
    case 'crm':
      return { fg: 'var(--iris)', bg: 'var(--iris-soft)' };
    case 'buildium':
      return { fg: 'var(--positive)', bg: 'var(--positive-bg)' };
    case 'assistant':
      return { fg: 'var(--info)', bg: 'var(--info-bg)' };
    case 'team':
      return { fg: 'var(--caution)', bg: 'var(--caution-bg)' };
    default:
      return { fg: 'var(--ink-muted)', bg: 'var(--canvas-raised)' };
  }
}

function matchesCategory(row: ActivityRow, category: ActivityCategory): boolean {
  if (category === 'all') return true;
  return categorize(row).key === category;
}

function matchesActor(row: ActivityRow, actor: string | null): boolean {
  if (!actor) return true;
  return row.actor.id === actor;
}

// ── day grouping ────────────────────────────────────────────────────

interface DayGroup {
  key: string;
  label: string;
  rows: ActivityRow[];
}

function groupByDay(rows: ActivityRow[]): DayGroup[] {
  const groups: DayGroup[] = [];
  let current: DayGroup | null = null;
  for (const row of rows) {
    const key = dayKey(row.created_at);
    if (!current || current.key !== key) {
      current = { key, label: dayLabel(row.created_at), rows: [] };
      groups.push(current);
    }
    current.rows.push(row);
  }
  return groups;
}

function dayKey(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'unknown';
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Earlier';
  const today = new Date();
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(today) - startOf(d)) / 86_400_000);
  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}),
  });
}

// ── row ─────────────────────────────────────────────────────────────

function ActivityRowItem({ row, staggerIndex }: { row: ActivityRow; staggerIndex: number }) {
  const cat = useMemo(() => categorize(row), [row]);
  const displayName = useMemo(() => actorLabel(row), [row]);
  const tone = toneColors(cat.tone);
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.16,
        ease: [0.2, 0.8, 0.2, 1],
        delay: Math.min(staggerIndex * 0.012, 0.18),
      }}
      className="flex gap-3 py-2.5 border-b"
      style={{ borderColor: 'var(--line)' }}
    >
      <div className="w-[86px] shrink-0 pt-0.5">
        <span
          className="text-[12px] tabular-nums"
          style={{ color: 'var(--ink-faint)' }}
          title={new Date(row.created_at).toLocaleString()}
        >
          {formatClockTime(row.created_at)}
        </span>
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span
            className="text-[13px] font-medium"
            style={{ color: row.actor.is_self ? 'var(--ink-muted)' : 'var(--ink)' }}
          >
            {displayName}
          </span>
          <span
            className="text-[12px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-[var(--r-xs)]"
            style={{ color: tone.fg, background: tone.bg }}
            data-testid="activity-category-badge"
          >
            {cat.label}
          </span>
        </div>
        <div className="text-[14px] leading-relaxed mt-0.5" style={{ color: 'var(--ink)' }}>
          {row.description}
        </div>
        {(row.property || row.tenant) && (
          <div className="text-[12px] mt-1" style={{ color: 'var(--ink-subtle)' }}>
            {[row.property, row.tenant].filter(Boolean).join(' · ')}
          </div>
        )}
      </div>
    </motion.div>
  );
}

/** How to name the actor. "You" for self; the display name / email for a
 *  known user; otherwise the integration that produced the row (webhook
 *  rows carry ``actor.id='system'``) — never a bare "Unknown user". */
function actorLabel(row: ActivityRow): string {
  if (row.actor.is_self) return 'You';
  if (row.actor.name) return row.actor.name;
  if (row.actor.email) return row.actor.email;
  if (row.source === 'webhook' || row.actor.id === 'system') {
    if (row.source_label && row.source_label.toLowerCase().startsWith('buildium')) {
      return 'Buildium';
    }
    return 'Integration';
  }
  return 'System';
}

// ── filter controls ─────────────────────────────────────────────────

function CategoryChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      data-testid={`activity-category-${label.toLowerCase()}`}
      className="shrink-0 h-7 px-3 rounded-[var(--r-sm)] text-[12px] font-medium border transition-colors"
      style={{
        background: active ? 'var(--iris-soft)' : 'transparent',
        color: active ? 'var(--iris)' : 'var(--ink-muted)',
        borderColor: active ? 'transparent' : 'var(--line)',
      }}
    >
      {label}
    </button>
  );
}

function PersonFilter({
  actors,
  value,
  onChange,
}: {
  actors: ActivityActor[];
  value: string | null;
  onChange: (next: string | null) => void;
}) {
  // Self first (labelled "You"), then everyone else already name-sorted
  // by the backend.
  const ordered = useMemo(() => {
    const self = actors.filter((a) => a.is_self);
    const others = actors.filter((a) => !a.is_self);
    return [...self, ...others];
  }, [actors]);

  return (
    <label className="inline-flex items-center gap-2">
      <span className="sr-only">Filter by person</span>
      <select
        data-testid="activity-person-select"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        className="h-7 pl-2.5 pr-7 rounded-[var(--r-sm)] border text-[12px] font-medium appearance-none cursor-pointer"
        style={{
          borderColor: 'var(--line)',
          background: 'var(--canvas)',
          color: 'var(--ink-muted)',
          backgroundImage:
            "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M3 4.5L6 7.5L9 4.5' stroke='%23888' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>\")",
          backgroundRepeat: 'no-repeat',
          backgroundPosition: 'right 8px center',
        }}
      >
        <option value="">All people</option>
        {ordered.map((a) => (
          <option key={a.id} value={a.id}>
            {a.is_self ? 'You' : a.name || a.email || 'Unknown'}
          </option>
        ))}
      </select>
    </label>
  );
}

function EmptyState({
  title,
  subtitle,
  ctaLabel,
  onCta,
}: {
  title: string;
  subtitle: string;
  ctaLabel?: string;
  onCta?: () => void;
}) {
  return (
    <div className="py-16 flex flex-col items-center gap-3 text-center">
      <div className="text-[16px] font-medium" style={{ color: 'var(--ink)' }}>
        {title}
      </div>
      <div className="text-[13px] max-w-md" style={{ color: 'var(--ink-subtle)' }}>
        {subtitle}
      </div>
      {ctaLabel && onCta && (
        <button
          type="button"
          onClick={onCta}
          className="mt-2 text-[13px] font-medium underline underline-offset-2"
          style={{ color: 'var(--iris)' }}
        >
          {ctaLabel}
        </button>
      )}
    </div>
  );
}

/** Clock time within a day group ("2:14 PM"). The day itself is carried
 *  by the group header, so per-row we only need the time. */
function formatClockTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/** Derive the demo person dropdown from the demo rows (demo mode has no
 *  actors endpoint). Dedupes by actor id. */
function demoActorsFromRows(rows: ActivityRow[]): ActivityActor[] {
  const byId = new Map<string, ActivityActor>();
  for (const r of rows) {
    if (r.actor.id === 'system') continue;
    if (!byId.has(r.actor.id)) byId.set(r.actor.id, r.actor);
  }
  return [...byId.values()].sort((a, b) =>
    (a.name || a.email || a.id).localeCompare(b.name || b.email || b.id),
  );
}

// PR #117 Phase D — initial-load skeleton + time-bounded "still
// loading…" affordance. Three rows of shimmer give the page real shape
// during the first network round-trip; after 2s with no response, the
// bar flips to a "still loading… cancel" prompt so a stuck call doesn't
// read as a frozen page.
function InitialLoadingState({ onCancel }: { onCancel: () => void }) {
  const stillLoading = useStillLoading(true, 2000);
  return (
    <div data-testid="activity-loading" className="flex flex-col gap-2">
      {[0, 1, 2].map((i) => (
        <SkeletonListRow key={i} testIdPrefix={`activity-skeleton-${i}`} />
      ))}
      <div className="mt-2 text-center text-[12px]" style={{ color: 'var(--ink-muted)' }}>
        {stillLoading ? (
          <span data-testid="activity-still-loading">
            Still loading…{' '}
            <button
              type="button"
              data-testid="activity-loading-cancel"
              onClick={onCancel}
              className="underline cursor-pointer"
              style={{ color: 'var(--iris)' }}
            >
              Cancel
            </button>
          </span>
        ) : (
          <span data-testid="activity-loading-hint">Loading…</span>
        )}
      </div>
    </div>
  );
}
