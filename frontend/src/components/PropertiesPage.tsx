/**
 * Properties × Roles routing grid page (PR #81).
 *
 * One row per Buildium property + a sticky "(no property)" default
 * row at the top. Each row has five dropdowns (the five categories
 * Brandon designed); options come from the synced ``buildium_staff``
 * cache. NO typing, NO inviting teammates, NO "add property" button
 * (hard rule from the brief — every dropdown source is Buildium).
 *
 * Routing the data:
 * 1. On mount: GET /properties + GET /properties/staff in parallel.
 * 2. Cell change: PUT /properties/{id}/assignment/{category} with
 *    the chosen buildium_user_id (or null to clear).
 * 3. Re-sync button: POST /properties/sync → refresh staff + grid.
 *
 * Pagination: 50 properties per page. Renders a simple
 * Previous / Next pair if ``total > page_size``.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';

import {
  CATEGORIES,
  CATEGORY_LABELS,
  type Category,
  type DefaultRow,
  type PropertyRow,
  type StaffOption,
  fetchPropertiesGrid,
  fetchStaffOptions,
  setDefaultAssignment,
  setPropertyAssignment,
  syncBuildiumStaff,
} from '../services/properties';
import { demoMode } from '../services/auth';
import { runBuildiumBackfill } from '../services/buildium';
import { connectedPmsRow, isPmsConnector, listConnectors, pmsLastSyncAt } from '../services/connectors';
import { demoPropertiesGrid, demoStaffOptions } from '../services/demoData';
import { CellConfigurator } from './properties/CellConfigurator';
import { PropertyCockpit } from './PropertyCockpit';
import { SavingPill, type SavingPillState } from './SavingPill';
import { ErrorState } from './ErrorState';
import { EmptyState } from './EmptyState';
import { Skeleton } from './Skeleton';
import { useStore } from '../stores/store';
import { count } from '../lib/plural';

// PR #82 — how long the "✓ Saved" pill lingers before fading.
const SAVED_FADE_MS = 2000;

function cellKey(rowKey: number | null, category: Category): string {
  return `${rowKey ?? 'default'}:${category}`;
}

interface Toast {
  message: string;
  tone: 'info' | 'error';
}

export type EmptyReason = 'not_connected' | 'never_synced' | 'no_properties' | undefined;

/** Why the grid is empty, and the PMS the sentence should name. */
interface EmptyGrid {
  reason: EmptyReason;
  /** The workspace's own system ("Rentvine"), or the systems it can choose
   *  from ("Buildium or Rentvine") when it has connected none. Buildium when we
   *  could not tell (older wording). */
  pms: string;
}

/** "A", "A or B", "A, B or C": the systems a workspace can still choose from. */
function eitherOf(names: string[]): string {
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')} or ${names[names.length - 1]}`;
}

/**
 * Why is the properties grid empty? `reason: undefined` when we could not tell.
 *
 * ⚠ Asked ONLY when the grid comes back with nothing, so the common path
 * costs no extra request. It reads the same PMS `lastSyncAt` stamp the
 * getting-started checklist ticks off, so the two screens cannot disagree
 * about whether this company has ever synced.
 *
 * ⚠ Every uncertainty returns `undefined`, which keeps the older wording. A
 * connectors call that failed is not evidence that somebody's portfolio is
 * empty, and stating a cause we could not verify is the defect being fixed,
 * pointed the other way.
 *
 * ⚠ It names the workspace's own PMS. A Rentvine workspace used to be told to
 * "Connect your Buildium account", which one PMS per workspace forbids it to
 * do (rule 16).
 */
async function classifyEmptyGrid(): Promise<EmptyGrid> {
  try {
    const rows = await listConnectors();
    const pmsRows = rows.filter((r) => isPmsConnector(r.id));
    if (pmsRows.length === 0) return { reason: undefined, pms: 'Buildium' };
    const mine = connectedPmsRow(rows);
    if (!mine) {
      return { reason: 'not_connected', pms: eitherOf(pmsRows.map((r) => r.name)) };
    }
    if (!(mine.apiConnected || mine.webhookConnected)) return { reason: 'not_connected', pms: mine.name };
    return {
      reason: pmsLastSyncAt(rows) === undefined ? 'never_synced' : 'no_properties',
      pms: mine.name,
    };
  } catch {
    return { reason: undefined, pms: 'Buildium' };
  }
}

/** The empty grid's sentence for each reason, naming the workspace's PMS. */
function emptyCopy({ reason, pms }: EmptyGrid): { title: string; body: string; cta?: string } {
  if (reason === 'no_properties') {
    return {
      title: `No properties in ${pms} yet`,
      body: `${pms} synced fine — the connected account just doesn't have any rental properties in it yet. Add one in ${pms} and it shows up here on the next sync.`,
    };
  }
  if (reason === 'never_synced') {
    return {
      title: 'No properties yet',
      // "Sync data" is Buildium's button; every other system's panel says Sync now.
      body:
        pms === 'Buildium'
          ? `${pms} is connected. Run Sync data once to pull your properties in here.`
          : `${pms} is connected. Its first sync pulls your properties in here; to start one now, use Sync now in the ${pms} settings.`,
      cta: `Go to ${pms} settings`,
    };
  }
  return {
    title: 'No properties yet',
    body: `Connect your ${pms} account, then sync to pull your properties in here.`,
    cta: `Set up ${pms}`,
  };
}

export function PropertiesPage() {
  const setView = useStore((s) => s.setView);
  const [defaultRow, setDefaultRow] = useState<DefaultRow | null>(null);
  const [properties, setProperties] = useState<PropertyRow[]>([]);
  const [staff, setStaff] = useState<StaffOption[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [error, setError] = useState<string | null>(null);
  // PR #82 — per-cell save state. ``savingCells`` is the in-flight
  // set; ``savedCells`` is the recently-completed map keyed by the
  // same cell key, valued by Date.now() so the fade timer can
  // discriminate stale "saved" pills. ``failedCells`` persists
  // until the next change on the same cell.
  const [savingCells, setSavingCells] = useState<Set<string>>(() => new Set());
  const [savedCells, setSavedCells] = useState<Map<string, number>>(() => new Map());
  const [failedCells, setFailedCells] = useState<Set<string>>(() => new Set());
  // Workstream A — clicking a property name opens its cockpit page in
  // place of the grid; Back returns without losing grid state.
  const [cockpitProperty, setCockpitProperty] = useState<{ id: number; name: string } | null>(
    null,
  );
  // Why the grid is empty, when it is. ⚠ THREE states look identical here —
  // an empty table — and they need opposite instructions: not connected
  // ("connect Buildium"), connected but never synced ("run Sync data"), and
  // synced with a Buildium account that genuinely holds no rental properties
  // (nothing to do; the product is working). This screen used to show the
  // FIRST message to all three, so a customer who had already connected AND
  // synced was told to connect — the failure that reads as "there is no
  // fallback here". `undefined` = we could not tell, which keeps the old
  // wording rather than inventing a cause (gotcha 55).
  const [emptyGrid, setEmptyGrid] = useState<EmptyGrid | undefined>(undefined);

  const reload = useCallback(
    async (targetPage: number = page) => {
      setLoading(true);
      setError(null);
      if (demoMode) {
        // Demo isolation: in-memory fixtures, no network call.
        const grid = demoPropertiesGrid();
        setDefaultRow(grid.default_row);
        setProperties(grid.properties);
        setStaff(demoStaffOptions());
        setTotal(grid.total);
        setPage(grid.page);
        setLoading(false);
        return;
      }
      try {
        const [grid, staffResp] = await Promise.all([
          fetchPropertiesGrid({ page: targetPage, pageSize }),
          fetchStaffOptions(),
        ]);
        setDefaultRow(grid.default_row);
        setProperties(grid.properties);
        setStaff(staffResp.options);
        setTotal(grid.total);
        setPage(grid.page);
        setEmptyGrid(grid.properties.length === 0 ? await classifyEmptyGrid() : undefined);
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to load properties';
        setError(msg);
      } finally {
        setLoading(false);
      }
    },
    [page, pageSize],
  );

  useEffect(() => {
    void reload(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-dismiss toast after 4s.
  useEffect(() => {
    if (!toast) return;
    const handle = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(handle);
  }, [toast]);

  const handleSync = async () => {
    // Full resync, not just staff — the button's label promises Buildium
    // data, and staff-only left properties/units/leases stale (Brandon's
    // 2026-07-25 dogfood: "it only does staff members i think"). Staff
    // sync + the full mirror backfill run together; either half failing
    // is surfaced without hiding the other's success.
    setSyncing(true);
    try {
      const [staff, mirror] = await Promise.all([
        syncBuildiumStaff(),
        runBuildiumBackfill().catch(
          () =>
            ({ ok: false, error: 'Mirror sync failed.' }) as Awaited<
              ReturnType<typeof runBuildiumBackfill>
            >,
        ),
      ]);
      if (!staff.ok || !mirror.ok) {
        setToast({
          tone: 'error',
          message:
            (!staff.ok ? (staff.error ?? "Couldn't sync staff from Buildium.") : '') +
            (!staff.ok && !mirror.ok ? ' ' : '') +
            (!mirror.ok ? (mirror.error ?? "Couldn't sync Buildium data.") : ''),
        });
      } else {
        const u = staff.upserted ?? 0;
        // ⚠ Every one of these can legitimately be 1 — a manager with a
        // single building is this product's smallest real customer, and
        // "1 properties, 1 units, 1 leases" is the first sentence they see
        // after connecting Buildium. "staff" is uncountable and stays as is.
        const parts = [
          `${u} staff`,
          count(mirror.properties, 'property'),
          count(mirror.units, 'unit'),
          count(mirror.leases, 'lease'),
          count(mirror.tenants, 'tenant'),
        ];
        setToast({
          tone: 'info',
          message: `Re-synced from Buildium: ${parts.join(', ')}.`,
        });
      }
      if (staff.ok) {
        await reload(page);
      }
    } finally {
      setSyncing(false);
    }
  };

  const optimisticUpdate = useCallback(
    (
      rowKey: number | null,
      category: Category,
      buildiumUserId: number | null,
    ) => {
      if (rowKey === null) {
        setDefaultRow((prev) =>
          prev
            ? {
                assignments: {
                  ...prev.assignments,
                  [category]:
                    buildiumUserId == null
                      ? null
                      : { action_type: 'assign_to_user', buildium_user_id: buildiumUserId },
                },
              }
            : prev,
        );
      } else {
        setProperties((prev) =>
          prev.map((p) =>
            p.buildium_property_id === rowKey
              ? {
                  ...p,
                  assignments: {
                    ...p.assignments,
                    [category]:
                      buildiumUserId == null
                        ? null
                        : { action_type: 'assign_to_user', buildium_user_id: buildiumUserId },
                  },
                }
              : p,
          ),
        );
      }
    },
    [],
  );

  const handleCellChange = useCallback(
    async (
      rowKey: number | null,
      category: Category,
      buildiumUserId: number | null,
    ) => {
      // PR #82 — per-cell save state. Mark the cell as in-flight
      // BEFORE the optimistic update so the SavingPill renders on
      // the very same render tick the dropdown changes.
      const key = cellKey(rowKey, category);
      setSavingCells((prev) => {
        const next = new Set(prev);
        next.add(key);
        return next;
      });
      // Clear any prior "Failed" pill on this cell — a new save
      // attempt supersedes the stale failure.
      setFailedCells((prev) => {
        if (!prev.has(key)) return prev;
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
      // And any prior "Saved" pill — fresh save starts the cycle.
      setSavedCells((prev) => {
        if (!prev.has(key)) return prev;
        const next = new Map(prev);
        next.delete(key);
        return next;
      });

      // Optimistic update first, then rollback on error.
      const previousDefault = defaultRow;
      const previousProps = properties;
      optimisticUpdate(rowKey, category, buildiumUserId);
      try {
        if (rowKey === null) {
          await setDefaultAssignment(category, buildiumUserId);
        } else {
          await setPropertyAssignment(rowKey, category, buildiumUserId);
        }
        // Saving → Saved.
        setSavingCells((prev) => {
          if (!prev.has(key)) return prev;
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        setSavedCells((prev) => {
          const next = new Map(prev);
          next.set(key, Date.now());
          return next;
        });
      } catch (err) {
        // Saving → Failed + rollback.
        setSavingCells((prev) => {
          if (!prev.has(key)) return prev;
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        setFailedCells((prev) => {
          const next = new Set(prev);
          next.add(key);
          return next;
        });
        if (previousDefault) setDefaultRow(previousDefault);
        setProperties(previousProps);
        const msg = err instanceof Error ? err.message : 'Failed to save';
        setToast({ tone: 'error', message: msg });
      }
    },
    [defaultRow, properties, optimisticUpdate],
  );

  // PR #82 — auto-fade the "✓ Saved" pill after SAVED_FADE_MS.
  // A new change on the same cell resets the timer via the dep
  // change (a new entry in ``savedCells`` triggers a re-run).
  useEffect(() => {
    if (savedCells.size === 0) return;
    const timer = window.setTimeout(() => {
      setSavedCells(new Map());
    }, SAVED_FADE_MS);
    return () => window.clearTimeout(timer);
  }, [savedCells]);

  const cellState = useCallback(
    (rowKey: number | null, category: Category): SavingPillState | null => {
      const key = cellKey(rowKey, category);
      if (savingCells.has(key)) return 'saving';
      if (failedCells.has(key)) return 'failed';
      if (savedCells.has(key)) return 'saved';
      return null;
    },
    [savingCells, savedCells, failedCells],
  );

  const pageCount = useMemo(
    () => (total > 0 ? Math.max(1, Math.ceil(total / pageSize)) : 1),
    [total, pageSize],
  );

  if (cockpitProperty) {
    return (
      <div className="flex-1 overflow-y-auto px-12 pt-10 pb-24" data-testid="properties-page">
        <div className="w-full max-w-[900px] mx-auto">
          <PropertyCockpit
            propertyId={cockpitProperty.id}
            propertyName={cockpitProperty.name}
            onBack={() => setCockpitProperty(null)}
          />
        </div>
      </div>
    );
  }

  const emptyText = emptyCopy(emptyGrid ?? { reason: undefined, pms: 'Buildium' });

  return (
    <div className="flex-1 overflow-y-auto px-12 pt-10 pb-24" data-testid="properties-page">
      <div className="w-full max-w-[1400px] mx-auto">
        {/* PR #118 Phase E — header layout. Pre-#118 the title block
            could push the Re-sync button past the right edge on narrow
            viewports (brief flagged "Re-sync from Buildium button
            clipped at the right edge"). Adding gap-3 + min-w-0 on the
            title column and shrink-0 on the button keeps the button
            fully visible and lets the title truncate instead. */}
        <div className="flex items-end justify-between gap-3 mb-6">
          <div className="min-w-0">
            <h1
              className="text-[20px] font-semibold tracking-tight"
              style={{ color: 'var(--ink)' }}
            >
              Properties × Roles
            </h1>
            <p
              className="mt-1 text-[13px] leading-relaxed"
              style={{ color: 'var(--ink-muted)', maxWidth: 720 }}
            >
              Pick who handles each category at each property. Empty cells fall
              back to the company default row; if that's also empty, events
              route to the admin.
            </p>
          </div>
          <button
            type="button"
            data-testid="resync-button"
            disabled={syncing}
            onClick={handleSync}
            title={syncing ? 'Sync in progress…' : 'Re-sync staff + properties from Buildium'}
            className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed whitespace-nowrap cursor-pointer"
            style={{
              background: 'var(--canvas-elevated)',
              borderColor: 'var(--line)',
              color: 'var(--ink)',
            }}
          >
            <RefreshCw size={13} strokeWidth={1.75} className={syncing ? 'animate-spin' : ''} />
            {syncing ? 'Syncing…' : 'Re-sync from Buildium'}
          </button>
        </div>

        {error ? (
          <div className="mb-4">
            <ErrorState
              title="Couldn't load your properties"
              body={error}
              onRetry={() => void reload(page)}
              data-testid="properties-error"
            />
          </div>
        ) : null}

        {staff.length === 0 && !loading ? (
          <div
            className="mb-4 p-3 rounded-[var(--r-md)] border text-[13px]"
            style={{
              background: 'var(--canvas-elevated)',
              borderColor: 'var(--line)',
              color: 'var(--ink-muted)',
            }}
          >
            No Buildium staff in the cache yet. Click <strong>Re-sync from
            Buildium</strong> to populate the dropdowns.
          </div>
        ) : null}

        {/* ⚠ `overflow-clip`, NOT `overflow-hidden`, and the difference is the
            whole reason the sticky header below works. `overflow: hidden`
            makes an element a SCROLL CONTAINER — so it became the nearest
            scrollport for the sticky `<thead>`, and that container never
            scrolls, so the header could never stick. The code read as though
            it did, which is the worst version of a bug: nothing to see in the
            diff and nothing to see on screen.

            `overflow: clip` clips without creating a scroll container, so the
            page's own scroller stays the sticky reference and the rounded
            corners still cut the table. Safari before 16 does not know the
            value and falls back to visible — corners unclipped, header still
            sticky, which is the harmless direction. */}
        <div
          className="rounded-[var(--r-md)] border overflow-clip"
          style={{ borderColor: 'var(--line)' }}
        >
          <table className="w-full text-[13px]" style={{ color: 'var(--ink)' }}>
            <thead
              className="sticky top-0"
              style={{ background: 'var(--canvas-elevated)' }}
            >
              <tr>
                <th
                  className="text-left px-3 py-2 border-b font-medium"
                  style={{ borderColor: 'var(--line)', width: 280 }}
                >
                  Property
                </th>
                {CATEGORIES.map((cat) => (
                  <th
                    key={cat}
                    className="text-left px-3 py-2 border-b font-medium"
                    style={{ borderColor: 'var(--line)' }}
                  >
                    {CATEGORY_LABELS[cat]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {defaultRow ? (
                <tr
                  data-testid="default-row"
                  style={{ background: 'var(--canvas-raised)' }}
                >
                  <td
                    className="px-3 py-2.5 border-b align-middle"
                    style={{ borderColor: 'var(--line)' }}
                  >
                    <div className="font-medium">(no property)</div>
                    <div className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                      Company-wide defaults
                    </div>
                  </td>
                  {CATEGORIES.map((cat) => (
                    <td
                      key={cat}
                      className="px-3 py-2.5 border-b align-middle"
                      style={{ borderColor: 'var(--line)' }}
                    >
                      <div className="relative flex items-center gap-2">
                        <div className="flex-1 min-w-0">
                          <CellConfigurator
                            assignment={defaultRow.assignments[cat] ?? null}
                            staff={staff}
                            disabled={loading || cellState(null, cat) === 'saving'}
                            onChange={(uid) => handleCellChange(null, cat, uid)}
                          />
                        </div>
                        {cellState(null, cat) && (
                          // Overlaid on the cell's right edge — the old
                          // permanent 88px spacer clipped every select
                          // all the time (Stage 3 audit).
                          <span className="absolute right-1 top-1/2 -translate-y-1/2 z-[1]">
                            <SavingPill state={cellState(null, cat)!} />
                          </span>
                        )}
                      </div>
                    </td>
                  ))}
                </tr>
              ) : null}
              {properties.map((p) => (
                <tr
                  key={p.buildium_property_id}
                  data-testid={`property-row-${p.buildium_property_id}`}
                  className="transition-colors hover:bg-[var(--canvas-hover)]"
                >
                  <td
                    className="px-3 py-2.5 border-b align-middle"
                    style={{ borderColor: 'var(--line)' }}
                  >
                    <button
                      type="button"
                      data-testid={`property-name-${p.buildium_property_id}`}
                      onClick={() =>
                        setCockpitProperty({ id: p.buildium_property_id, name: p.name })
                      }
                      className="font-medium text-left text-[var(--iris)] hover:text-[var(--iris-hover)] hover:underline cursor-pointer"
                      title="Open property page"
                    >
                      {p.name}
                    </button>
                    {p.address ? (
                      <div className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                        {p.address}
                      </div>
                    ) : null}
                  </td>
                  {CATEGORIES.map((cat) => (
                    <td
                      key={cat}
                      className="px-3 py-2.5 border-b align-middle"
                      style={{ borderColor: 'var(--line)' }}
                    >
                      <div className="relative flex items-center gap-2">
                        <div className="flex-1 min-w-0">
                          <CellConfigurator
                            assignment={p.assignments[cat] ?? null}
                            staff={staff}
                            disabled={
                              loading || cellState(p.buildium_property_id, cat) === 'saving'
                            }
                            onChange={(uid) =>
                              handleCellChange(p.buildium_property_id, cat, uid)
                            }
                          />
                        </div>
                        {cellState(p.buildium_property_id, cat) && (
                          <span className="absolute right-1 top-1/2 -translate-y-1/2 z-[1]">
                            <SavingPill state={cellState(p.buildium_property_id, cat)!} />
                          </span>
                        )}
                      </div>
                    </td>
                  ))}
                </tr>
              ))}
              {loading && properties.length === 0
                ? Array.from({ length: 4 }).map((_, i) => (
                    <tr key={`skeleton-${i}`} data-testid="properties-skeleton-row">
                      {Array.from({ length: CATEGORIES.length + 1 }).map((__, c) => (
                        <td key={c} className="px-3 py-3.5">
                          <Skeleton variant="line" width={c === 0 ? '60%' : '80%'} />
                        </td>
                      ))}
                    </tr>
                  ))
                : null}
              {!loading && !error && properties.length === 0 ? (
                <tr>
                  <td colSpan={CATEGORIES.length + 1} className="px-3 py-2">
                    <EmptyState
                      title={emptyText.title}
                      body={emptyText.body}
                      ctaLabel={emptyText.cta}
                      onCta={emptyText.cta ? () => setView('settings') : undefined}
                      data-testid="properties-empty"
                    />
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        {total > pageSize ? (
          <div
            className="flex items-center justify-end gap-2 mt-4 text-[13px]"
            style={{ color: 'var(--ink-muted)' }}
          >
            <span>
              Page {page} of {pageCount} · {count(total, 'property')}
            </span>
            <button
              type="button"
              disabled={page <= 1 || loading}
              onClick={() => void reload(page - 1)}
              className="px-2 py-1 rounded-[var(--r-sm)] border disabled:opacity-50"
              style={{ borderColor: 'var(--line)' }}
            >
              Previous
            </button>
            <button
              type="button"
              disabled={page >= pageCount || loading}
              onClick={() => void reload(page + 1)}
              className="px-2 py-1 rounded-[var(--r-sm)] border disabled:opacity-50"
              style={{ borderColor: 'var(--line)' }}
            >
              Next
            </button>
          </div>
        ) : null}

        {toast ? (
          <div
            role="status"
            data-testid="properties-toast"
            className="fixed bottom-6 right-6 px-4 py-2.5 rounded-[var(--r-md)] border shadow text-[13px]"
            style={{
              background:
                toast.tone === 'error' ? 'var(--caution-bg)' : 'var(--canvas-elevated)',
              borderColor: toast.tone === 'error' ? 'var(--caution)' : 'var(--line)',
              color: 'var(--ink)',
              zIndex: 50,
            }}
          >
            {toast.message}
          </div>
        ) : null}
      </div>
    </div>
  );
}
