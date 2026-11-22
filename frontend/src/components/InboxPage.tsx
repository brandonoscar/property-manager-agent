/**
 * Inbox — the property-manager's day-to-day surface (PR #70 redesign).
 *
 * Two-pane layout:
 *   - Left rail (320px): list of recent ``buildium_events`` rows the
 *     planner produced an ``action_surface`` for. Each row shows a
 *     status pill, a tenant/unit one-liner, and the most recent
 *     assistant turn as preview.
 *   - Right pane: ``<ConversationThread>`` for the selected row,
 *     backed by an SSE subscription to
 *     ``/api/v1/buildium/events/{id}/thread/stream``. Free-text input
 *     is always pinned to the bottom.
 *
 * No modals. No JSON payload expanders by default. The thread IS the
 * surface — see ``components/conversation/*`` for the rendering
 * primitives.
 */

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from 'react';
import {
  AlertCircle,
  AlertTriangle,
  ArrowDownUp,
  ArrowLeft,
  Archive,
  Check,
  ChevronRight,
  ExternalLink,
  Inbox as InboxIcon,
  Loader2,
  Mail,
  MessageSquare,
  MoreHorizontal,
  Reply,
  RefreshCw,
  Send,
  X,
} from 'lucide-react';
import { toast } from 'sonner';

import { titleCase } from '../lib/text';
import { demoMode } from '../services/auth';
import { demoInboxRows } from '../services/demoData';
import { Button } from './Button';
import { ErrorState } from './ErrorState';
import {
  executeInboxAction,
  findingStateLabel,
  isDoneSignal,
  listInboxRows,
  replanInboxEvent,
  resummarizeInboxEvent,
  subscribeInboxThread,
  submitInboxFreeform,
  type ChainPick,
  type ConversationStreamEvent,
  type InboxAction,
  type InboxEventSummary,
  type InboxRow,
  type InboxThreadStream,
  type PlannerStatus,
  type SummaryStatus,
} from '../services/inbox';
import { ApiError } from '../services/api';
import { ConversationThread } from './conversation/ConversationThread';
import { answerIsStreaming, foldInboxStream } from './conversation/runnerFold';
import { SNOOZE_CHOICES, TodayCard, type TodayDetailItem } from './TodayCard';
import { useStore } from '../stores/store';
import type {
  ConfirmGateData,
  ConversationEvent,
  SuggestionChipData,
} from './conversation/types';

const POLL_INTERVAL_MS = 15_000;

export function InboxPage() {
  const [rows, setRows] = useState<InboxRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Rail dismissals (founder ask 2026-08-20: "you should be able to x
  // them out from the sidebar"). No server archive endpoint exists yet
  // (the detail pane's Archive is disabled chrome), so dismissal is
  // per-device via localStorage — same pattern as the Today brief's
  // per-day hide. Capped so the ledger can't grow unbounded.
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(DISMISSED_ROWS_KEY);
      return new Set(raw ? (JSON.parse(raw) as string[]) : []);
    } catch {
      return new Set();
    }
  });
  /** Write the ledger and mirror it to this device. One place, so undo and
   *  dismiss cannot drift into disagreeing about what was stored. */
  const writeDismissed = (next: Set<string>) => {
    try {
      localStorage.setItem(DISMISSED_ROWS_KEY, JSON.stringify([...next].slice(-500)));
    } catch {
      /* storage full/blocked — the change still applies this session */
    }
    return next;
  };
  const restoreRow = (id: string) => {
    setDismissedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return writeDismissed(next);
    });
  };
  const restoreAllDismissed = () => {
    const count = dismissedIds.size;
    setDismissedIds(writeDismissed(new Set()));
    // Says the NUMBER, because the control that fired it is about to
    // disappear — without it there is no confirmation anything happened on a
    // rail that was already showing every row the server sent.
    toast(count === 1 ? '1 row restored' : `${count} rows restored`);
  };
  const dismissRow = (id: string, subject?: string) => {
    setDismissedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return writeDismissed(next);
    });
    if (selectedId === id) {
      setSelectedId(null);
      setMobileDetailOpen(false);
    }
    // ⚠ The X is one click, next to the timestamp, and it HIDES A REAL EVENT
    // with no server-side archive behind it — so before this there was no way
    // back at all short of clearing site data. A destructive-feeling action
    // that cannot be taken back is one people stop using, which quietly costs
    // the feature rather than the row.
    toast('Dismissed on this device', {
      description: subject,
      action: { label: 'Undo', onClick: () => restoreRow(id) },
    });
  };
  // Mobile one-pane flow (Stage 3): a TAP opens the detail pane; the
  // list auto-select (below) never does — otherwise the first poll
  // would yank a phone user straight into a detail view.
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  // A clicked Today-card item — shown in the right pane INSTEAD of the
  // thread (clicking a sidebar card always opens into content here,
  // never navigates to another page).
  const [todayDetail, setTodayDetail] = useState<TodayDetailItem | null>(null);
  // #69 — "My work" vs the whole company queue. On a shared 10-person
  // account, ``mine`` narrows the feed to the rows the webhook router
  // assigned to the current user (backend ``assignee=me``).
  const [mine, setMine] = useState(false);
  // Completed view. Mirrors ``mine`` exactly — a boolean owned here, rendered
  // as chips in the rail — because that pattern is already proven in this
  // file. Purely additive: false is the pre-existing behaviour byte for byte.
  const [completed, setCompleted] = useState(false);

  const load = useCallback(
    async (kind: 'initial' | 'poll' | 'manual') => {
      if (kind === 'manual') setRefreshing(true);
      try {
        const fetched = await listInboxRows(mine);
        setRows(fetched);
        setLoadError(null);
        // Keep the sidebar badge honest while this page is open — acting on
        // an item should clear it now, not at the next 90s poll.
        //
        // ⚠ Only from the UNFILTERED view. `mine` is a rail chip that narrows
        // the list to your own items, and publishing that number would make
        // the badge under-report for anybody who had ever clicked it — a
        // wrong count that looks exactly like a right one. When the filter is
        // on, the background poller's total stands.
        if (!mine) useStore.getState().setInboxCount(fetched.length);
      } catch (err) {
        console.warn('[inbox] fetch failed', err);
        if (kind === 'initial') {
          const message =
            err instanceof ApiError || err instanceof Error
              ? err.message
              : 'Could not load the inbox.';
          setLoadError(message);
        }
      } finally {
        if (kind === 'manual') setRefreshing(false);
      }
    },
    [mine],
  );

  useEffect(() => {
    if (demoMode) {
      // Demo mode renders from in-memory fixtures (services/demoData.ts):
      // no network/DB call, so demo data never lands in real tables, and
      // the rows flow through the exact same render path as live data.
      setRows(demoInboxRows());
      return;
    }
    let cancelled = false;
    const tick = async (kind: 'initial' | 'poll') => {
      if (cancelled) return;
      await load(kind);
    };
    void tick('initial');
    const interval = window.setInterval(() => void tick('poll'), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [load]);

  // Visible = the planner produced a surface AND nobody's clicked yet.
  const visibleRows = useMemo(
    () =>
      (rows ?? []).filter(
        (r) =>
          r.status === 'done' &&
          r.action_surface !== null &&
          // THE one behavioural line. Unexecuted rows are the active queue;
          // executed ones are the Completed view. Before this, executed rows
          // were unreachable in the UI — you clicked, and the item vanished
          // with no way to see what happened.
          (completed ? r.executed_action_index !== null : r.executed_action_index === null) &&
          !dismissedIds.has(r.id),
      ),
    [rows, dismissedIds, completed],
  );

  // Failure visibility: rows that arrived but could NOT be turned into an
  // actionable card — the planner failed/timed out, or the event itself
  // errored. Without surfacing these, a dropped maintenance emergency is
  // simply invisible (visibleRows requires a non-null action_surface). We
  // show them with an explicit "couldn't process — retry" treatment.
  const failedRows = useMemo(
    () =>
      (rows ?? []).filter(
        (r) =>
          r.executed_action_index === null &&
          // not an already-actionable row
          !(r.status === 'done' && r.action_surface !== null) &&
          (r.status === 'failed' ||
            r.planner_status === 'failed' ||
            r.planner_status === 'timed_out'),
      ),
    [rows],
  );

  const handleRetry = useCallback(
    async (rowId: string) => {
      try {
        await replanInboxEvent(rowId);
        toast.success('Retrying — Occupella is reprocessing this item.');
        // Optimistically move it out of the failed bucket; the next poll
        // reconciles with the worker's real progress.
        setRows((prev) =>
          prev
            ? prev.map((r) =>
                r.id === rowId ? { ...r, planner_status: 'pending', status: 'running' } : r,
              )
            : prev,
        );
        void load('manual');
      } catch (err) {
        console.warn('[inbox] retry failed', err);
        toast.error('Could not retry — please try again.');
      }
    },
    [load],
  );

  // Auto-select the first row when the list lands.
  useEffect(() => {
    if (selectedId !== null) return;
    if (visibleRows.length === 0) return;
    setSelectedId(visibleRows[0].id);
  }, [visibleRows, selectedId]);

  const selectedRow = visibleRows.find((r) => r.id === selectedId) ?? null;

  return (
    // PR #120 — page chrome lives inside the rail (Inbox + Refresh) per
    // the mockup. The shared h-12 horizontal strip that previously
    // spanned both panes is removed; the right pane shows its own
    // breadcrumb (← Inbox · …) inside the EventHeader card.
    // ⚠ `@container`: the list/detail split keys on the INBOX's width, not
    // the window's. At a 900px window the sidebar takes 260px, and a split
    // keyed on the viewport left the detail pane 320px wide.
    <div className="@container flex-1 flex overflow-hidden min-h-0">
      <RowList
        rows={visibleRows}
        rowsState={rows}
        loadError={loadError}
        selectedId={selectedId}
        onSelect={(id) => {
          setSelectedId(id);
          setTodayDetail(null);
          setMobileDetailOpen(true);
        }}
        mobileHidden={mobileDetailOpen}
        onDismissRow={dismissRow}
        dismissedCount={dismissedIds.size}
        onRestoreDismissed={restoreAllDismissed}
        onOpenTodayItem={(item) => {
          setTodayDetail(item);
          setMobileDetailOpen(true);
        }}
        demoMode={demoMode}
        refreshing={refreshing}
        onRefresh={() => void load('manual')}
        failedRows={failedRows}
        onRetry={handleRetry}
        completed={completed}
        onToggleCompleted={setCompleted}
        mine={mine}
        onToggleMine={(next) => {
          setMine(next);
          // Scope changed → drop the current selection so the newly
          // fetched list auto-selects its first row (the load re-runs via
          // the useEffect dependency on ``load``, which depends on ``mine``).
          setSelectedId(null);
          setMobileDetailOpen(false);
        }}
      />
      {todayDetail ? (
        <TodayDetailPane
          item={todayDetail}
          mobileVisible={mobileDetailOpen}
          onBack={() => {
            setTodayDetail(null);
            setMobileDetailOpen(false);
          }}
        />
      ) : (
      <ThreadPane
        row={selectedRow}
        mobileVisible={mobileDetailOpen}
        onBackToInbox={() => {
          setSelectedId(null);
          setMobileDetailOpen(false);
        }}
        onExecuted={(rowId) => {
          // Optimistically remove the row from the list. The next
          // poll cycle will reconcile.
          setRows((prev) => (prev ? prev.filter((r) => r.id !== rowId) : prev));
          // Reset selection so the next row auto-selects.
          setSelectedId(null);
          setMobileDetailOpen(false);
        }}
      />
      )}
    </div>
  );
}

// ── Right pane: a Today-card item's detail ──────────────────────────
//
// Today rows are digests (brief counts, noticed findings, reminders),
// not webhook threads — so their detail is the full text plus the
// dismiss action, presented exactly where a message's thread would be.
function TodayDetailPane({
  item,
  mobileVisible,
  onBack,
}: {
  item: TodayDetailItem;
  mobileVisible: boolean;
  onBack: () => void;
}) {
  const kindLabel =
    item.kind === 'noticed' ? 'Occupella noticed' : item.kind === 'reminder' ? 'Reminder' : 'Today';
  return (
    <section
      className={`flex-1 min-w-0 flex flex-col ${mobileVisible ? '' : '@max-[899px]:hidden'}`}
      data-testid="inbox-today-detail"
    >
      <div className="flex-1 min-h-0 overflow-y-auto px-12 @max-[899px]:px-4 py-8">
        <div className="mx-auto" style={{ maxWidth: '760px' }}>
          <button
            type="button"
            onClick={onBack}
            className="inline-flex items-center gap-1 text-[13px] mb-4"
            style={{ color: 'var(--ink-muted)' }}
            data-testid="inbox-today-detail-back"
          >
            <ArrowLeft size={13} /> Inbox
          </button>
          <div className="eyebrow mb-2">{kindLabel}</div>
          {/* Sans, like an event's title in the same pane: an item title,
              not a page title. */}
          <h1 className="text-[20px] font-medium tracking-tight mb-4" style={{ color: 'var(--ink)' }}>
            {item.title}
          </h1>

          {/* SUMMARY — same card the message pane leads with. */}
          {(item.detail || item.amount != null) && (
            <div
              className="rounded-[var(--r-md)] mb-4"
              style={{
                background: 'var(--canvas)',
                border: '0.5px solid var(--card-edge)',
                padding: '16px 18px',
              }}
            >
              <div className="eyebrow mb-2">Summary</div>
              <p
                className="text-[14px] m-0"
                style={{ color: 'var(--ink)', lineHeight: 1.65 }}
              >
                {item.detail}
              </p>
              {item.amount != null && item.amount > 0 && (
                <p
                  className="text-[13px] mt-2 mb-0 font-medium"
                  style={{ color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}
                >
                  {item.amount.toLocaleString('en-US', {
                    style: 'currency',
                    currency: 'USD',
                    maximumFractionDigits: 0,
                  })}{' '}
                  involved
                </p>
              )}
            </div>
          )}

          {/* The grouped findings — "18 Hidden Risks" opens showing all
              18, in the thread view's "noticed" card. */}
          {item.findings && item.findings.length > 0 && (
            <div
              className="rounded-[var(--r-md)] mb-4"
              style={{
                background: 'var(--canvas)',
                border: '0.5px solid var(--card-edge)',
                padding: '16px 18px',
              }}
              data-testid="inbox-today-detail-findings"
            >
              <div className="eyebrow mb-1" style={{ color: 'var(--iris)' }}>
                What Occupella noticed
              </div>
              {item.findings.map((f, i) => (
                <div
                  key={i}
                  className="py-2.5"
                  style={{
                    borderTop: i === 0 ? 'none' : '0.5px solid var(--line)',
                  }}
                >
                  <div className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
                    {f.title}
                  </div>
                  {f.detail && (
                    <div
                      className="text-[13px] mt-0.5"
                      style={{ color: 'var(--ink-muted)', lineHeight: 1.55 }}
                    >
                      {f.detail}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* THE ACTION ROW.
              Order is least destructive first, which is also the order a PM
              reaches for them: not now → already dealt with → stop tracking.
              Dismiss sits last because `reminders/aging.py` never brings a
              dismissed card back, while snooze and handled both reopen. */}
          {(item.onSnooze || item.onHandled || item.onDismiss) && (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {item.onSnooze && (
                <>
                  <span className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
                    Remind me in
                  </span>
                  {SNOOZE_CHOICES.map((choice) => (
                    <button
                      key={choice.days}
                      type="button"
                      data-testid={`inbox-today-detail-snooze-${choice.days}`}
                      onClick={() => {
                        item.onSnooze?.(choice.days);
                        onBack();
                      }}
                      className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium"
                      style={{
                        borderColor: 'var(--line-strong)',
                        background: 'var(--canvas)',
                        color: 'var(--ink)',
                      }}
                    >
                      {choice.label}
                    </button>
                  ))}
                </>
              )}
              {item.onHandled && (
                <button
                  type="button"
                  data-testid="inbox-today-detail-handled"
                  onClick={() => {
                    item.onHandled?.();
                    onBack();
                  }}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium"
                  style={{
                    borderColor: 'var(--line-strong)',
                    background: 'var(--canvas)',
                    color: 'var(--ink)',
                  }}
                >
                  I handled this
                </button>
              )}
              {item.onDismiss && (
                <button
                  type="button"
                  data-testid="inbox-today-detail-dismiss"
                  onClick={() => {
                    item.onDismiss?.();
                    onBack();
                  }}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium"
                  style={{
                    borderColor: 'var(--line-strong)',
                    background: 'var(--canvas)',
                    color: 'var(--ink)',
                  }}
                >
                  Dismiss
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// ── Left rail: list of rows ────────────────────────────────────


// PR #120 — Mockup-derived category buckets for the rail's filter
// chip row. Buildium's task ``category`` enum has many values
// ("GeneralInquiry", "Maintenance", "Inquiry", "RentPayment", etc.);
// the rail's filter pills group them into 3 user-facing buckets
// (Maintenance / Rent / General). Returns the bucket key + the
// display label + the icon + the semantic color tokens for the
// category badge inside each row card.
type RowCategoryBucket = 'maintenance' | 'rent' | 'general';

function rowCategoryBucket(row: InboxRow): RowCategoryBucket {
  const raw = (row.enrichment?.task?.category ?? '').toLowerCase();
  if (
    raw.includes('maint') ||
    raw.includes('repair') ||
    raw.includes('broken') ||
    raw.includes('leak') ||
    raw.includes('work') ||
    // Common Buildium maintenance categories that don't contain the
    // word "maintenance" — without these, an "HVAC" or "Plumbing"
    // task falls through to the General bucket (caught in demo: every
    // row badged GENERAL despite being HVAC/plumbing/etc.).
    raw.includes('hvac') ||
    raw.includes('a/c') ||
    raw.includes('air') ||
    raw.includes('cool') ||
    raw.includes('heat') ||
    raw.includes('plumb') ||
    raw.includes('electric') ||
    raw.includes('applianc') ||
    raw.includes('pest') ||
    raw.includes('lock') ||
    raw.includes('water') ||
    raw.includes('fixture') ||
    raw.includes('damage')
  ) {
    return 'maintenance';
  }
  if (
    raw.includes('rent') ||
    raw.includes('payment') ||
    raw.includes('ledger') ||
    raw.includes('balance')
  ) {
    return 'rent';
  }
  return 'general';
}



// PR #120 — relative-time formatter used by the row card top strip.
// Differs from ``formatRowTime`` (which the legacy row item used) in
// that it appends the "ago" suffix per the mockup ("2m ago", "1h
// ago", "3d ago"). Anything older than a day collapses to the
// locale-formatted date so we don't render "47h ago".
function formatRelativeTime(iso: string): string {
  try {
    const date = new Date(iso);
    const diff = Date.now() - date.getTime();
    if (diff < 60_000) return 'just now';
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)}h ago`;
    if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`;
    return date.toLocaleDateString();
  } catch {
    return '';
  }
}


// PR #120 — derive a short right-aligned "attention chip" for each
// row card. The mockup shows two examples:
//   * amber "2nd mirror in 90 days"  — recurrence signal
//   * red   "Name mismatch"          — data-quality signal
// We pull from existing fields without inventing new shape: unit
// history count → recurrence; reporter name absent / differs from
// the lease tenants → name-mismatch hint. Returns null when nothing
// noteworthy applies.
/** Day buckets for the rail — the organizing scheme that keeps 200 rows
 *  scannable (founder direction, 2026-08-20): rows group under TODAY /
 *  YESTERDAY / THIS WEEK / EARLIER eyebrows, each group one card. */
export function groupRowsByDay(
  rows: InboxRow[],
  now: Date = new Date(),
): Array<{ label: string; rows: InboxRow[] }> {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const today = startOfDay(now);
  const day = 24 * 60 * 60 * 1000;
  const buckets: Array<{ label: string; rows: InboxRow[] }> = [
    { label: 'Today', rows: [] },
    { label: 'Yesterday', rows: [] },
    { label: 'This week', rows: [] },
    { label: 'Earlier', rows: [] },
  ];
  for (const row of rows) {
    const t = new Date(row.event_time).getTime();
    if (!Number.isFinite(t)) {
      buckets[3].rows.push(row);
    } else if (t >= today) {
      buckets[0].rows.push(row);
    } else if (t >= today - day) {
      buckets[1].rows.push(row);
    } else if (t >= today - 6 * day) {
      buckets[2].rows.push(row);
    } else {
      buckets[3].rows.push(row);
    }
  }
  return buckets.filter((b) => b.rows.length > 0);
}


// PR #120 — derive a single-letter avatar token + a deterministic
// gradient based on the name. Used in the row card's bottom strip
// and again in the EventHeader Assignee meta cell.


function RowList({
  rows,
  rowsState,
  loadError,
  selectedId,
  onSelect,
  demoMode,
  refreshing,
  onRefresh,
  failedRows,
  onRetry,
  mine,
  onToggleMine,
  completed,
  onToggleCompleted,
  mobileHidden,
  onDismissRow,
  dismissedCount,
  onRestoreDismissed,
  onOpenTodayItem,
}: {
  rows: InboxRow[];
  rowsState: InboxRow[] | null;
  loadError: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  demoMode: boolean;
  refreshing: boolean;
  onRefresh: () => void;
  failedRows: InboxRow[];
  onRetry: (id: string) => void;
  mine: boolean;
  onToggleMine: (mine: boolean) => void;
  completed: boolean;
  onToggleCompleted: (completed: boolean) => void;
  /** Mobile one-pane: the rail hides while the detail pane is open. */
  mobileHidden: boolean;
  /** X on a row — hides it from this device's rail (local dismissal).
   *  The subject rides along so the undo toast can name the row: a bare
   *  "Dismissed" leaves somebody who clicked the wrong X with nothing to
   *  check the offer against. */
  onDismissRow: (id: string, subject?: string) => void;
  /** How many rows this device is hiding — 0 hides the restore control. */
  dismissedCount: number;
  /** Bring them all back. Blunt on purpose: the ledger is a flat set of ids
   *  with no per-row screen, so "restore all" is the honest shape of the
   *  only thing that can actually be offered. */
  onRestoreDismissed: () => void;
  /** A Today-card row was clicked — show it in the detail pane. */
  onOpenTodayItem: (item: TodayDetailItem) => void;
}) {
  // PR #120 — filter chip state. "All" is the default; user can pick
  // a category bucket and the list filters locally. Changing the row
  // poll order on the server isn't worth the round-trip — the rail
  // already has every row in memory.
  const [filter, setFilter] = useState<'all' | RowCategoryBucket>('all');

  const filteredRows = useMemo(
    () => (filter === 'all' ? rows : rows.filter((r) => rowCategoryBucket(r) === filter)),
    [rows, filter],
  );

  return (
    <aside
      // PR #120 — flex column so the header + footer don't scroll with
      // the rows. Width pinned to 320px per the brief ("keep the width
      // and length of the inbox sidebar the same").
      className={`w-[320px] shrink-0 border-r flex flex-col @max-[899px]:w-full @max-[899px]:border-r-0 ${mobileHidden ? '@max-[899px]:hidden' : ''}`}
      style={{
        borderColor: 'var(--line)',
        // The rail had no background, so it inherited the white canvas
        // and the whole three-pane page read as one white sheet — the
        // white event cards had nothing to sit ON (founder call,
        // 2026-08-18: "the inbox sidebar looks too much white"). The
        // tint is the same raised token the rest of the app uses, so
        // the cards read as raised and the rail as the surface behind.
        background: 'var(--canvas-raised)',
      }}
    >
      {/* PR #120 — rail header chrome (matches helixis_inbox_only.html).
          Replaces the page-level h-12 horizontal strip. Sits inside
          the rail so the right pane can render its own breadcrumb
          + title row independently. */}
      <div
        className="shrink-0 px-5 pt-5 pb-3"
        data-testid="inbox-rail-header"
      >
        <div className="flex items-center justify-between mb-1">
          <h1
            data-testid="inbox-rail-title"
            className="text-[20px] font-medium tracking-tight"
            style={{
              color: 'var(--ink)',
              fontFamily: 'var(--font-display)',
              letterSpacing: '-0.01em',
            }}
          >
            Inbox
          </h1>
          {!demoMode && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onRefresh}
              disabled={refreshing}
              aria-label="Refresh inbox"
              data-testid="inbox-refresh"
              icon={
                <RefreshCw
                  size={13}
                  className={refreshing ? 'animate-spin' : ''}
                  style={{ color: 'var(--ink-muted)' }}
                />
              }
            >
              Refresh
            </Button>
          )}
        </div>
        {rows.length > 0 && (
          <div
            className="text-[12px] mb-4"
            style={{ color: 'var(--ink-muted)' }}
            data-testid="inbox-rail-subtitle"
          >
            {rows.length === 1
              ? '1 new item needs your attention'
              : `${rows.length} new items need your attention`}
          </div>
        )}
        {/* #69 — scope toggle: "My work" (server ``assignee=me``) vs the
            whole company queue. Shown even when the current scope is empty
            so a user whose "My work" is empty can switch back to All. Hidden
            in demo mode (no per-user routing in fixtures). */}
        {!demoMode && (
          <div className="flex items-center gap-2 mb-3 flex-wrap" data-testid="inbox-scope-toggle">
            <Segmented label="Whose work">
            <FilterChip
              label="My work"
              active={mine}
              onClick={() => onToggleMine(true)}
              testId="inbox-scope-mine"
            />
            <FilterChip
              label="All"
              active={!mine}
              onClick={() => onToggleMine(false)}
              testId="inbox-scope-all"
            />
            </Segmented>
            {/* Completed is independent of scope (it shows done items for
                whichever scope is picked), so it is its own one-segment
                toggle rather than a third segment. */}
            <Segmented label="Completed items">
            <FilterChip
              label="Completed"
              active={completed}
              onClick={() => onToggleCompleted(!completed)}
              testId="inbox-scope-completed"
            />
            </Segmented>
            {/* ⚠ The undo toast only rescues the LAST dismissal. Everything
                hidden before that had no exit at all — the ledger is
                localStorage with no screen anywhere that reads it, so a row
                dismissed last week was gone until somebody cleared site data.
                Only rendered when there is something to bring back. */}
            {dismissedCount > 0 && (
              <button
                type="button"
                onClick={onRestoreDismissed}
                data-testid="inbox-restore-dismissed"
                className="text-[13px] underline underline-offset-2"
                style={{ color: 'var(--iris)' }}
              >
                {`${dismissedCount} hidden · Restore`}
              </button>
            )}
          </div>
        )}
        {/* Filter chip row — All · N / Maintenance / Rent / General +
            "Newest first" sort pill on the right. The category chips
            filter the rail locally; the sort pill is descriptive
            (the API already returns rows in event_time DESC). */}
        {rows.length > 0 && (
          <div className="flex items-center gap-2 flex-wrap" data-testid="inbox-rail-filters">
            <Segmented label="Category">
            <FilterChip
              label={`All · ${rows.length}`}
              active={filter === 'all'}
              onClick={() => setFilter('all')}
              testId="inbox-filter-all"
            />
            <FilterChip
              label="Maintenance"
              active={filter === 'maintenance'}
              onClick={() => setFilter('maintenance')}
              testId="inbox-filter-maintenance"
            />
            <FilterChip
              label="Rent"
              active={filter === 'rent'}
              onClick={() => setFilter('rent')}
              testId="inbox-filter-rent"
            />
            <FilterChip
              label="General"
              active={filter === 'general'}
              onClick={() => setFilter('general')}
              testId="inbox-filter-general"
            />
            </Segmented>
            <span
              className="ml-auto inline-flex items-center gap-1.5 text-[12px]"
              style={{
                color: 'var(--ink-muted)',
                border: '0.5px solid var(--line)',
                borderRadius: 'var(--r-sm)',
                padding: '4px 9px',
              }}
              title="Newest first"
            >
              <ArrowDownUp size={11} />
              Newest first
            </span>
          </div>
        )}
      </div>

      {/* Rows — scrollable region between header + footer. The FEED
          below stays Buildium webhooks + inbound email ONLY (founder
          direction 2026-07-07 — background findings never become rail
          cards). The Today strip above it is that direction's promised
          "Occupella found something" surface (2026-08-15 audit debt #2):
          morning brief + noticed findings + reminders in ONE dismissible
          card, visually distinct from the event cards. */}

      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-3">
        <TodayCard onOpenItem={onOpenTodayItem} />
        {/* Failure visibility — events that arrived but couldn't be turned
            into an actionable card. Shown ABOVE the normal feed so a dropped
            item is never silently missing; each offers a one-click retry
            (re-runs the planner). */}
        {failedRows.length > 0 && (
          <div className="pt-1 pb-2 flex flex-col gap-2" data-testid="inbox-failed-section">
            <div
              className="flex items-center gap-1.5 text-[12px] font-medium uppercase tracking-wide px-1"
              style={{ color: 'var(--caution)' }}
            >
              <AlertTriangle size={12} />
              {failedRows.length === 1
                ? "1 item couldn't be processed"
                : `${failedRows.length} items couldn't be processed`}
            </div>
            {failedRows.map((row) => (
              <FailedRowCard key={row.id} row={row} onRetry={onRetry} />
            ))}
          </div>
        )}
        {rowsState === null && loadError === null && (
          <div className="py-1 flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <SkeletonRow key={i} />
            ))}
          </div>
        )}
        {rowsState !== null && rows.length === 0 && failedRows.length === 0 && loadError === null && (
          <EmptyState
            title="Nothing in your inbox yet."
            subtitle="New Buildium events show up within a few seconds of arriving."
          />
        )}
        {loadError !== null && rows.length === 0 && (
          <ErrorState
            title="Couldn't load your inbox"
            body={loadError}
            onRetry={onRefresh}
            data-testid="inbox-error"
          />
        )}
        {filteredRows.length === 0 && rows.length > 0 && (
          <div
            className="mt-4 text-center text-[13px]"
            style={{ color: 'var(--ink-subtle)' }}
            data-testid="inbox-rail-filter-empty"
          >
            No {filter} items right now.
          </div>
        )}
        <div className="flex flex-col gap-3 py-1">
          {groupRowsByDay(filteredRows).map((group) => (
            <div
              key={group.label}
              data-testid={`inbox-day-group-${group.label}`}
              className="overflow-hidden"
              style={{
                background: 'var(--canvas)',
                border: '0.5px solid var(--card-edge)',
                borderRadius: 'var(--r-md)',
              }}
            >
              {/* Day header INSIDE the card, like the TodayCard's "TODAY"
                  row — the label and its rows read as one object. */}
              <div className="eyebrow px-3 pt-2.5 pb-1.5">{group.label}</div>
                {group.rows.map((row, i) => (
                  <RowItem
                    key={row.id}
                    row={row}
                    active={row.id === selectedId}
                    onClick={() => onSelect(row.id)}
                    onDismiss={(subject) => onDismissRow(row.id, subject)}
                    last={i === group.rows.length - 1}
                  />
                ))}
            </div>
          ))}
        </div>
      </div>

      {/* PR #120 — "you're all caught up" footer per the mockup. Only
          renders when there ARE rows shown (otherwise we'd be saying
          "all caught up" right next to "nothing here yet" — confusing). */}
      {filteredRows.length > 0 && (
        <div
          className="shrink-0 mx-3 mb-3 flex items-center justify-center gap-2 text-[12px]"
          style={{
            color: 'var(--ink-subtle)',
            background: 'var(--canvas)',
            border: '1px dashed var(--card-edge)',
            borderRadius: 'var(--r-md)',
            padding: '12px 14px',
          }}
          data-testid="inbox-rail-footer"
        >
          <Check size={13} style={{ color: 'var(--positive)' }} />
          {filteredRows.length === 1
            ? "You're all caught up after this one"
            : filteredRows.length === 2
              ? "You're all caught up after these two"
              : `You're all caught up after these ${filteredRows.length}`}
        </div>
      )}
    </aside>
  );
}


/**
 * A segmented control (brief, 2026-09-29): one 6px box holding its options,
 * the chosen one raised in white. Replaces free-standing filter pills, which
 * read as tags rather than as a choice.
 */
function Segmented({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--r-sm)] border"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
    >
      {children}
    </div>
  );
}

// One segment of a Segmented control.
function FilterChip({
  label,
  active,
  onClick,
  testId,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      data-active={active}
      aria-pressed={active}
      className="inline-flex items-center h-7 text-[13px] font-medium transition-colors whitespace-nowrap"
      style={{
        padding: '0 10px',
        borderRadius: 4,
        border: 'none',
        background: active ? 'var(--canvas)' : 'transparent',
        boxShadow: active ? 'var(--shadow-sm)' : 'none',
        color: active ? 'var(--ink)' : 'var(--ink-muted)',
        cursor: 'pointer',
      }}
    >
      {label}
    </button>
  );
}


function RowItem({
  row,
  active,
  onClick,
  onDismiss,
  last,
}: {
  row: InboxRow;
  active: boolean;
  onClick: () => void;
  onDismiss: (subject: string) => void;
  /** Last row in its day group — suppresses the divider. */
  last: boolean;
}) {
  const subject = useMemo(() => deriveSubject(row), [row]);
  const preview = useMemo(() => derivePreview(row), [row]);

  // Summary-row shape (founder direction, 2026-08-20, from the TodayCard
  // reference): one bold title line + one muted subtitle line, hairline
  // dividers inside a grouped card — no status dots (founder call, same
  // day), no chips. Category/property/assignee detail lives in the detail
  // pane, one tap away; the rail is for scanning 200 of these.

  return (
    <button
      type="button"
      onClick={onClick}
      className="group block w-full text-left transition-colors cursor-pointer"
      data-testid="inbox-row-item"
      data-active={active}
      style={{
        background: active ? 'var(--iris-soft)' : 'transparent',
        borderBottom: last ? 'none' : '0.5px solid var(--line)',
        // Same row as TodayCard's, so the digest and the events below it
        // read as one list.
        padding: '6px 12px',
        color: 'var(--ink)',
      }}
      onMouseEnter={(e) => {
        if (!active) (e.currentTarget as HTMLElement).style.background = 'var(--canvas-hover)';
      }}
      onMouseLeave={(e) => {
        if (!active) (e.currentTarget as HTMLElement).style.background = 'transparent';
      }}
    >
      <div className="flex items-center gap-2">
        <span className="flex-1 min-w-0 truncate text-[13px] font-medium">{subject}</span>
        {/* Time swaps for the X on hover — dismissal is available but
            never crowds the resting row. */}
        <span
          className="shrink-0 text-[12px] group-hover:hidden"
          style={{ color: 'var(--ink-faint)' }}
        >
          {formatRelativeTime(row.event_time)}
        </span>
        <span
          role="button"
          tabIndex={0}
          aria-label="Dismiss from inbox"
          title="Dismiss from inbox"
          data-testid="inbox-row-dismiss"
          className="hidden group-hover:inline-flex shrink-0 items-center justify-center w-5 h-5 rounded-[var(--r-xs)]"
          style={{ color: 'var(--ink-faint)' }}
          onClick={(e) => {
            e.stopPropagation();
            onDismiss(subject);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.stopPropagation();
              e.preventDefault();
              onDismiss(subject);
            }
          }}
        >
          <X size={13} strokeWidth={1.75} />
        </span>
        {/* The "this opens" affordance — chevron rides every row, quiet
            at rest, ink on hover (founder: rows looked stale/static). */}
        <ChevronRight
          size={14}
          strokeWidth={1.75}
          className="shrink-0 transition-colors"
          style={{ color: active ? 'var(--iris)' : 'var(--line-strong)' }}
        />
      </div>
      <div
        className="truncate text-[12px] mt-0.5"
        style={{ color: 'var(--ink-muted)', lineHeight: 1.5 }}
      >
        {preview}
      </div>
      {/* A risk card outlives the condition behind it: the finding heals when
          the detector stops emitting it, and nothing rewrites the card. So the
          row says what is still true, read fresh on every request rather than
          kept in sync. Measured before this existed: 35 of 81 risk cards
          described a problem that no longer existed.

          Only ever rendered when something HAS moved -- `findingStateLabel`
          returns null for an untouched card, so this is not a badge that sits
          on every row and stops being read (gotcha 52). */}
      {findingStateLabel(row.finding_state) && (
        <div
          className="truncate text-[12px] mt-1"
          data-testid="inbox-row-finding-state"
          style={{ color: 'var(--ink-faint)' }}
        >
          {findingStateLabel(row.finding_state)}
        </div>
      )}
    </button>
  );
}

// PR #120 — StatusPill removed. The new RowItem expresses status via
// the red NEW badge + category badge in the top strip; the
// per-status (done/running/failed/queued) text labels are no longer
// rendered (the rail only shows rows in the visible/done bucket).

// ── Right pane: conversation thread ────────────────────────────

function ThreadPane({
  row,
  onExecuted,
  onBackToInbox,
  mobileVisible,
}: {
  row: InboxRow | null;
  /** Mobile one-pane: the detail pane only shows after a tap. */
  mobileVisible: boolean;
  onExecuted: (rowId: string) => void;
  // PR #120 — breadcrumb back-link in the detail pane. Clears the
  // selection so the empty-state placeholder shows (and on a narrow
  // viewport you'd see the rail again). Visual-only on wide screens
  // where the rail is always present.
  onBackToInbox: () => void;
}) {
  const [events, setEvents] = useState<ConversationEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  /**
   * Has this item turned into a conversation?
   *
   * The suggested-actions card and a chat are two different surfaces for the
   * same item, and they were being shown at once: the user typed into a box
   * inside the card, and the reply arrived UNDER the card, so the thing they
   * were talking to stayed on screen offering three buttons they had just
   * declined to press. Typing is the signal that the suggestions were not what
   * they wanted, so the card gives way and the item becomes a chat.
   *
   * ⚠ Temporary, not a mode switch. The card is regenerated planner output,
   * not something the message destroys, so `Back to suggested actions` returns
   * to it with the conversation intact underneath.
   */
  const [conversing, setConversing] = useState(false);
  /**
   * The composer's text, owned HERE rather than by either composer.
   *
   * ⚠ There are two of them — one inside the suggestions card, one docked at
   * the bottom once this is a chat — and the switch between them unmounts the
   * first. Left as local state, the very message that triggers the switch is
   * typed into a component that is about to disappear, so a failed send comes
   * back to an empty box with the user's words gone. Found by a test, not by
   * reading it.
   */
  const [freeformValue, setFreeformValue] = useState('');
  // The scroll region holding the card + the conversation under it. Needed so
  // a reply that grows past the fold can be followed — see `scrollRef` use
  // below.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef<InboxThreadStream | null>(null);
  const chainRef = useRef<ChainPick[]>([]);

  // Subscribe when the row changes.
  useEffect(() => {
    setEvents([]);
    setBusy(false);
    setThinking(false);
    // ⚠ Reset with the row. Carrying it over would open the NEXT item as a
    // chat with no messages in it and its suggestions hidden — the card
    // missing for a conversation that never happened. Same for the draft:
    // a half-typed message about one work order must not follow the user to
    // a different one.
    setConversing(false);
    setFreeformValue('');
    chainRef.current = [];
    streamRef.current?.abort();
    streamRef.current = null;

    if (!row) return;
    if (demoMode) return;

    const handleEvent = (event: ConversationStreamEvent) => {
      setEvents((prev) => [...prev, event as ConversationEvent]);
      // The freeform submit turns the thinking pill ON and has no other
      // way to turn it off: the POST returns ``{"status":"recorded"}``
      // the instant the task is spawned, ~52s (p50) before the run
      // produces anything. The run's own terminal events are the only
      // honest "it stopped" signal we get on this stream.
      if (event.type === 'done' || event.type === 'error') setThinking(false);
    };

    const stream = subscribeInboxThread(row.id, handleEvent);
    streamRef.current = stream;
    // Backstop: the SSE stream terminates on ``[DONE]``, which
    // ``subscribeInboxThread`` consumes WITHOUT emitting an event — so a run
    // that closes its session without a final ``done`` would leave the
    // thinking pill spinning forever. A pill that never stops is the same
    // lie as no pill at all.
    // ⚠ Optional-chained deliberately, though the type says it is always
    // present: this runs inside the row-select effect, so a stream object
    // missing it throws during commit and React unmounts the WHOLE pane.
    // A blank inbox is a far worse failure than a pill that relies on the
    // done/error events below.
    void stream.done?.then(() => setThinking(false));
    return () => {
      stream.abort();
    };
  }, [row?.id]);

  // PR #87 — HOTFIX: hooks must run unconditionally on every render.
  // ``useMemo`` previously lived AFTER the ``if (!row) return`` early
  // return below, which produced 4 hooks on the null-row render and 5
  // hooks the moment a row was selected. React responds by throwing
  // ``Rendered more hooks than during the previous render`` and the
  // error boundary blanks the page. ``stripDuplicateSummaryTurn`` is a
  // no-op when ``summary`` is empty, so passing ``''`` while row is
  // null is safe and keeps the hook count stable across transitions.
  const summaryForCompare = row?.action_surface?.summary?.trim() ?? '';
  const visibleEvents = useMemo(
    () => {
      // PR #93 — suggestion chips now render inside EventHeader as
      // the numbered workflow. Filter them out of the SSE event
      // stream so they don't double up in ConversationThread's
      // SuggestionRow renderer below. Confirm-gate, narration, and
      // assistant_message events pass through unchanged so the
      // click-chain UX is identical.
      const stripped = stripDuplicateSummaryTurn(events, summaryForCompare);
      // A typed question runs the chat's agent, whose events arrive raw
      // (token / tool_start / handoff / done). Fold them the way chat does,
      // or the reply and its steps never draw here (see runnerFold.ts).
      return foldInboxStream(
        stripped.filter((e) => e.type !== 'suggestion_chip'),
        thinking,
      );
    },
    [events, summaryForCompare, thinking],
  );

  // Follow the conversation as it grows.
  //
  // The card is tall, so the reply to a freeform message lands BELOW the fold:
  // the user bubble, the narration and the streaming answer all render off the
  // bottom edge of the pane and nothing brings them into view. It reads as
  // text bleeding off the bottom of the screen — the reply is there, you just
  // never see it arrive.
  //
  // ⚠ Only follows when the user is ALREADY near the bottom. Yanking the
  // viewport down while someone is deliberately reading the card above is the
  // opposite bug, and it is the more annoying one: it fights the user.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom > 240) return;
    // rAF so the measurement happens after this render has laid out — reading
    // scrollHeight synchronously here sees the PREVIOUS content height and
    // stops one message short, every time.
    const id = requestAnimationFrame(() => {
      // ⚠ Assign scrollTop when scrollTo is missing rather than calling it
      // blind. jsdom does not implement Element.scrollTo, and because this
      // runs inside rAF the TypeError is thrown after the test has finished —
      // so the suite still reports "579 passed" while printing 10 errors
      // nobody has to act on. Read the error count, not the pass line.
      if (typeof el.scrollTo === 'function') {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
      } else {
        el.scrollTop = el.scrollHeight;
      }
    });
    return () => cancelAnimationFrame(id);
  }, [visibleEvents, thinking]);

  // PR #93 — workflow-section Re-plan affordance. Resets the row's
  // planner state via the PR #89 endpoint; the worker picks it up
  // on the next claim pass and the 15s poll surfaces the new
  // suggestions. Optimistic UX is light-touch — we just toast on
  // failure; the workflow itself updates on the next poll. Wrapped
  // in useCallback so EventHeader's render doesn't churn on every
  // ThreadPane re-render.
  const handleWorkflowReplan = useCallback(async () => {
    if (!row) return;
    try {
      await replanInboxEvent(row.id);
      toast.success('Replan queued — new suggestions in ~30s.');
    } catch (err) {
      const message =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : 'Could not queue replan.';
      toast.error(message);
    }
  }, [row]);

  if (!row) {
    return (
      // Only makes sense BESIDE the list. Below the split the list is the whole
      // screen and this pane squeezed in next to it, text wrapping a word per
      // line (measured at 375px).
      <section className="flex-1 min-w-0 flex items-center justify-center @max-[899px]:hidden">
        <div
          className="text-[14px] text-center max-w-md"
          style={{ color: 'var(--ink-subtle)' }}
        >
          Select an event on the left to read what Occupella is suggesting.
        </div>
      </section>
    );
  }

  const handlePickSuggestion = (chip: SuggestionChipData) => {
    // Optimistically collapse the chip into a "you said: …" turn so
    // the scrollback reads like a conversation. The turn id is
    // chip-specific so multiple picks accumulate as separate bubbles.
    const turnId = `user-pick-${chip.action_index}-${Date.now()}`;
    setEvents((prev) => [
      ...prev,
      {
        type: 'assistant_message_start',
        data: { turn_id: turnId },
      },
      {
        type: 'assistant_message_token',
        data: { turn_id: turnId, content: `→ ${chip.label}` },
      },
      {
        type: 'assistant_message_end',
        data: { turn_id: turnId },
      },
    ]);
    chainRef.current = [
      ...chainRef.current,
      {
        prompt: 'Inbox suggestion',
        label: chip.label,
        parameter_patch: chip.parameter_patch,
      },
    ];

    // Render a confirm gate locally — every write is one click away
    // from confirm by house rule, regardless of what the planner
    // emitted upstream.
    const action = row.action_surface?.actions?.[chip.action_index];
    if (!action) return;
    const baseParams = action.parameters ?? {};
    const merged: Record<string, unknown> = { ...baseParams };
    for (const pick of chainRef.current) {
      for (const [k, v] of Object.entries(pick.parameter_patch)) {
        merged[k] = v;
      }
    }
    const confirmEvent: ConversationEvent = {
      type: 'confirm_gate',
      data: {
        action_index: chip.action_index,
        summary: `Confirm: ${action.intent}`,
        payload_preview: merged,
        confirm_label: 'Execute',
        cancel_label: 'Cancel',
      },
    };
    setEvents((prev) => [...prev, confirmEvent]);
  };

  // PR #99 — inline-confirm parent handler. Replaces the chip →
  // confirm_gate → bottom-strip flow with a direct execute that the
  // ``InlineConfirmButton`` wraps in a 5s countdown + Cancel link.
  const handleConfirm = async (data: ConfirmGateData) => {
    setBusy(true);
    setThinking(true);
    const sessionId = streamRef.current?.sessionId ?? null;
    try {
      const response = await executeInboxAction(
        row.id,
        data.action_index,
        data.payload_preview as Record<string, unknown>,
        null,
        chainRef.current,
        sessionId,
      );
      toast.success(response.result?.summary ?? 'Action executed');
      // The SSE stream will push the closing "Done — …" turn. We just
      // mark the row as executed locally so the list drops it.
      onExecuted(row.id);
    } catch (err) {
      const message =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : 'Execute failed.';
      toast.error(message);
    } finally {
      setBusy(false);
      setThinking(false);
    }
  };

  const handleCancel = (_data: ConfirmGateData) => {
    // Drop the trailing confirm-gate event so the user can pick a
    // different chip without leaving the gate stranded.
    setEvents((prev) => {
      let lastConfirmIdx = -1;
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].type === 'confirm_gate') {
          lastConfirmIdx = i;
          break;
        }
      }
      if (lastConfirmIdx === -1) return prev;
      return [...prev.slice(0, lastConfirmIdx), ...prev.slice(lastConfirmIdx + 1)];
    });
  };

  // PR #99 step 7 — accepts the freeform text directly from the
  // integrated input inside SuggestedWorkflow. The pre-PR-#99
  // bottom-of-pane textarea is removed in the same commit, so
  // ``freeform`` state is no longer the source of truth — the input
  // component holds its own value and hands it to us on submit.
  const handleFreeformSubmit = async (note: string) => {
    const trimmed = note.trim();
    if (!trimmed || !row) return;
    // PR #129 — was: optimistically inject an ``assistant_message``
    // event containing ``→ {text}`` so the user's input echoed below
    // the workflow card. That rendered as a typography-only
    // AssistantBubble in the inbox variant — i.e. "orphan text"
    // sitting on the canvas below the freeform input with no
    // container, no role, no styling. Removed.
    //
    // The freeform input itself shows "✓ Sent" via
    // ``IntegratedFreeformInput``'s state machine (PR #126).
    // The backend /freeform endpoint invokes the orchestrator agent; its
    // streamed reply + any confirm_gate flow back through THIS row's SSE
    // stream and render as real AssistantBubble / ConfirmGate turns — but
    // only if we hand it the live stream session id (else it publishes to a
    // dead channel and the user sees nothing). Pass streamRef's sessionId.
    //
    // ⚠ Echo the message as a ``user_bubble`` and raise the thinking pill
    // BEFORE awaiting. The POST returns ``{"status":"recorded"}`` the
    // instant the task is spawned, and the run's first streamed token is a
    // median of 52 SECONDS later (p90: the 240s deadline). Without this the
    // screen after submit is a toast and an empty pane — indistinguishable
    // from nothing having happened, which is exactly what it looked like.
    //
    // PR #129 removed an earlier echo because it was injected as an
    // ``assistant_message`` and rendered as orphan typography on the canvas.
    // ``user_bubble`` is the right event: ConversationThread already renders
    // it through ``UserBubble``, so this adds a turn to an existing
    // conversation rather than a new surface. Nothing in the inbox layout
    // changes — the pane simply stops being empty.
    const echo: ConversationEvent = {
      type: 'user_bubble',
      // ⚠ Tagged so the failure path can retract THIS bubble and no other.
      // A bubble left standing for a message the server never accepted is a
      // false record of the conversation — the user reads it as sent.
      data: { content: trimmed, pending_echo: true },
    };
    setEvents((prev) => [...prev, echo]);
    setThinking(true);
    try {
      await submitInboxFreeform(row.id, trimmed, streamRef.current?.sessionId ?? null);
      // ⚠ AFTER the POST resolves, deliberately — and an earlier cut had it
      // before, which cost the user their error message.
      //
      // The two composers are different components, so switching unmounts one
      // and mounts the other with fresh local state. Flipping early meant a
      // REFUSED send swapped the card away and took its inline error chip and
      // Retry button with it, leaving a bare composer and a toast. Nothing is
      // lost by waiting: it is the STREAM that takes a median of 52 seconds,
      // while this POST returns as soon as the task is spawned.
      setConversing(true);
    } catch (err) {
      setEvents((prev) => prev.filter((e) => e !== echo));
      // `conversing` is untouched here — a refused send never flipped it, so
      // the composer the user is looking at is the same one that holds their
      // text and is about to show the Retry chip.
      // A 409 is the session lock and is NOT a failure: the previous message
      // is still running, which means the pill is telling the truth and must
      // stay up. Every other error means nothing is running — drop it, or it
      // spins against a run that does not exist.
      const stillRunning = err instanceof ApiError && err.status === 409;
      if (!stillRunning) setThinking(false);
      // The backend's own wording ("An agent run is already active for this
      // inbox item") reads as a fault; it is the system working. Say what is
      // actually happening, in the words the user would use.
      const message = stillRunning
        ? 'Occupella is still working on your last message. It will reply here when it finishes.'
        : err instanceof ApiError || err instanceof Error
          ? err.message
          : 'Could not record note.';
      toast.error(message);
      // PR #126 — re-throw so the IntegratedFreeformInput's local
      // state machine flips to its 'error' phase and shows the
      // inline Retry affordance.
      throw err;
    }
  };

  // PR #86 — strip the leading assistant turn when it just echoes
  // ``row.action_surface.summary``. The EventHeader above already
  // renders every fact in that string, so re-rendering it as a
  // free-floating text turn looks like a redundant dump. We only
  // touch the FIRST complete assistant turn — any real LLM-streamed
  // content after it shows up normally. The ``visibleEvents`` memo
  // itself is declared above the early return — see PR #87 hotfix
  // note — so the hook order is stable across the null → row
  // transition.

  return (
    <section
      className={`flex-1 min-w-0 flex flex-col ${mobileVisible ? '' : '@max-[899px]:hidden'}`}
    >
      {/* pb-16 rather than py-8: the last thing in this pane is a streaming
          reply or a thinking pill, and 32px leaves it sitting on the bottom
          edge as if it were cut off. */}
      <div
        ref={scrollRef}
        data-testid="inbox-detail-scroll"
        data-follow-scroll
        className="flex-1 min-h-0 overflow-y-auto px-12 @max-[899px]:px-4 pt-8 pb-16"
      >
        <div className="mx-auto" style={{ maxWidth: '760px' }}>
          {/* PR #85 — always-visible event header, sourced from
              row.enrichment (NOT the action_surface). When the
              Gemini planner stalls, the right pane used to show a
              near-empty white panel; the header fills that gap with
              every fact we already have on the row.

              PR #93 — EventHeader now also hosts the numbered
              "Suggested workflow" section at the bottom of the
              card. We thread the pick / replan callbacks down so
              [Execute] reuses the existing confirm-gate flow and
              [Re-plan] fires the PR #89 reset endpoint. ``busy``
              disables the buttons while a write is in flight. */}
          <EventHeader
            row={row}
            onReplan={handleWorkflowReplan}
            onFreeformSubmit={handleFreeformSubmit}
            freeformPlaceholder={
              row.action_surface?.freeform_placeholder ??
              'Or describe what you want me to do...'
            }
            busy={busy}
            onBackToInbox={onBackToInbox}
            onExecuted={onExecuted}
            // The summary and "What Occupella noticed" stay in both states —
            // they are the context the conversation is ABOUT, and losing them
            // the moment you ask a question is what makes a chat feel like it
            // started from nothing. Only the suggested-actions card gives way.
            conversing={conversing}
            freeformValue={freeformValue}
            onFreeformChange={setFreeformValue}
          />
          {/* PR #98 — "still drafting" was being shown even when the
              planner had already returned. That's the lie that
              hurts trust: the inbox claims the agent is working when
              it's actually idle. Gate strictly on the planner being
              ACTIVELY pending/in_progress AND no streamed events
              having arrived yet. When the planner is done (succeeded
              / failed / timed_out), render nothing here — the
              workflow section above already communicates state via
              its rationale + Why-this affordances. */}
          {visibleEvents.length === 0 &&
          !thinking &&
          (row.planner_status === 'pending' || row.planner_status === 'in_progress') ? (
            <div
              className="text-[13px] py-6"
              style={{ color: 'var(--ink-subtle)' }}
              data-testid="inbox-empty-drafting"
            >
              Occupella is still drafting suggestions for this event. You can add a note below.
            </div>
          ) : visibleEvents.length === 0 && !thinking ? (
            // PR #98 — planner is DONE but we have no streamed
            // chips to render (the SuggestedWorkflow card lives
            // above in EventHeader). Render nothing rather than
            // the misleading "still drafting" line.
            null
          ) : (
            <ConversationThread
              events={visibleEvents}
              onPickSuggestion={handlePickSuggestion}
              onConfirm={handleConfirm}
              onCancel={handleCancel}
              busy={busy}
              // Dots only between turns, as in chat: once the answer is
              // typing itself out, its own cursor says it is working.
              thinking={thinking && !busy && !answerIsStreaming(visibleEvents)}
            />
          )}
        </div>
      </div>

      {/* The composer, once this item is a conversation.

          PR #99 moved the sticky textarea INTO the suggestions card so the
          screen read as one artifact rather than form-then-chat. That was
          right while the card was the surface. It stops being right the
          moment somebody types: their words and the reply then sit BELOW the
          card, so the thing they were talking to stayed on screen the whole
          time, still offering the buttons they had just passed over. The
          composer comes back out here and the card goes away.

          Docked OUTSIDE the scroll container, which is the actual difference
          between a chat and a page with a text box on it — the composer holds
          still while the conversation scrolls behind it. */}
      {conversing ? (
        <div
          data-testid="inbox-conversation-composer"
          className="shrink-0 px-12 @max-[899px]:px-4 pb-6"
          style={{ borderTop: '0.5px solid var(--line)', paddingTop: 14 }}
        >
          <div className="mx-auto" style={{ maxWidth: '760px' }}>
            {/* Temporary, and it has to look it. Nothing here destroyed the
                suggestions — they are regenerated planner output and still
                on the row — so the way back is one control, always present. */}
            <button
              type="button"
              data-testid="inbox-back-to-suggestions"
              onClick={() => setConversing(false)}
              className="inline-flex items-center gap-1.5 mb-2"
              style={{
                background: 'transparent',
                border: 'none',
                padding: 0,
                fontSize: 12,
                color: 'var(--ink-muted)',
                cursor: 'pointer',
              }}
            >
              <ArrowLeft size={13} />
              Back to suggested actions
            </button>
            <IntegratedFreeformInput
              onSubmit={handleFreeformSubmit}
              placeholder="Reply to Occupella..."
              busy={busy}
              value={freeformValue}
              onChange={setFreeformValue}
            />
          </div>
        </div>
      ) : null}
    </section>
  );
}

// ── Helpers ────────────────────────────────────────────────────

const DISMISSED_ROWS_KEY = 'occupella.inbox.dismissedRows';

function deriveSubject(row: InboxRow): string {
  const enrichment = row.enrichment;
  const task = enrichment?.task;
  const title = task?.title;
  const tenantsBlock = enrichment?.active_lease?.tenants ?? [];
  const tenant = tenantsBlock[0];
  // Headline casing regardless of how the Buildium webhook / manager
  // typed it — "water heater pilot out" renders "Water Heater Pilot Out"
  // (founder call, 2026-08-20). titleCase never lowercases, so "AC",
  // "HVAC" and unit codes survive.
  if (title && tenant) {
    return titleCase(`${title} — ${tenant.first_name ?? ''} ${tenant.last_name ?? ''}`.trim());
  }
  return titleCase(title ?? row.event_name);
}

function derivePreview(row: InboxRow): string {
  const summary = row.action_surface?.summary;
  return summary ?? 'Occupella is reading this event.';
}

// PR #120 — formatRowTime removed. The new RowItem uses
// formatRelativeTime (defined above) which appends "ago" per the
// mockup ("2m ago" vs "2m").

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
    <div className="py-16 flex flex-col items-center gap-3 text-center px-4">
      <InboxIcon size={28} style={{ color: 'var(--iris)' }} strokeWidth={1.5} />
      <div className="text-[14px] font-medium" style={{ color: 'var(--ink)' }}>
        {title}
      </div>
      <div className="text-[13px] max-w-xs" style={{ color: 'var(--ink-subtle)' }}>
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

function SkeletonRow() {
  return (
    <div
      className="rounded-[var(--r-sm)] p-3 border animate-pulse"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-elevated)' }}
    >
      <div
        className="h-2.5 w-16 rounded mb-2"
        style={{ background: 'var(--canvas-hover)' }}
      />
      <div
        className="h-3 w-3/4 rounded"
        style={{ background: 'var(--canvas-hover)' }}
      />
    </div>
  );
}

// Failure-visibility card: an event that arrived but couldn't be turned
// into an actionable card (planner failed/timed out, or the event errored).
// Surfaced so a dropped item is never silently missing; the Retry button
// re-runs the planner via ``replanInboxEvent``.
function FailedRowCard({
  row,
  onRetry,
}: {
  row: InboxRow;
  onRetry: (id: string) => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const title = row.enrichment?.task?.title?.trim() || row.event_name;
  const reason =
    row.planner_error?.trim() ||
    row.last_error?.trim() ||
    (row.planner_status === 'timed_out'
      ? 'Processing timed out before it finished.'
      : "Occupella couldn't process this event.");

  return (
    <div
      className="rounded-[var(--r-sm)] p-3 border"
      style={{ borderColor: 'var(--caution-edge, var(--line))', background: 'var(--canvas-elevated)' }}
      data-testid="inbox-failed-card"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={14} style={{ color: 'var(--caution)', marginTop: 2 }} />
        <div className="min-w-0 flex-1">
          <div
            className="text-[13px] font-medium truncate"
            style={{ color: 'var(--ink)' }}
            title={title}
          >
            {title}
          </div>
          <div className="text-[12px] mt-0.5" style={{ color: 'var(--ink-muted)' }}>
            {reason}
          </div>
        </div>
      </div>
      <div className="mt-2.5 flex justify-end">
        <Button
          variant="ghost"
          size="sm"
          disabled={retrying}
          data-testid="inbox-failed-retry"
          onClick={() => {
            setRetrying(true);
            onRetry(row.id);
          }}
          icon={
            retrying ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <RefreshCw size={13} />
            )
          }
        >
          {retrying ? 'Retrying…' : 'Retry'}
        </Button>
      </div>
    </div>
  );
}

// Re-export so the deeper-followup path (used by ActionChainModal in
// pre-PR-#70 code) doesn't break callers that still import this.
export { isDoneSignal };


// ── PR #85 — Event header card + helpers ─────────────────────────


/**
 * Mirror of the PR #84 backend dedupe rule: if address_line1 already
 * ends in a "#NNN" suffix where NNN is 3+ digits pure-numeric, the
 * property itself is named with that suffix — don't re-append
 * unit_number. Short or alphanumeric suffixes are treated as unit
 * suffixes and we do append.
 */
function formatPropertyLine(
  addressLine1: string | null | undefined,
  unitNumber: string | null | undefined,
): string {
  const addr = (addressLine1 ?? '').trim();
  const unit = (unitNumber ?? '').trim();
  if (!addr && !unit) return '—';
  if (!unit) return addr || '—';
  if (!addr) return `#${unit}`;
  // Strip trailing "#NNN" (3+ digit pure-numeric) from comparison.
  if (/#(\d{3,})\s*$/.test(addr)) {
    // Address already includes a building/property suffix; don't
    // append unit. Matches PR #84's surface_builder rule.
    return addr;
  }
  return `${addr} #${unit}`;
}


function formatDueDate(due: string | null | undefined): string {
  if (!due) return '—';
  const parsed = new Date(due);
  if (Number.isNaN(parsed.getTime())) return '—';
  // "May 17" style — short month + day, locale-independent.
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${months[parsed.getUTCMonth()]} ${parsed.getUTCDate()}`;
}


function formatReporter(
  first: string | null | undefined,
  last: string | null | undefined,
): string {
  const full = `${(first ?? '').trim()} ${(last ?? '').trim()}`.trim();
  return full || '—';
}


// PR #89 — planner lifecycle visualization. Three visible states:
//
//   pending / in_progress → subtle iris-tinted "Occupella is thinking…"
//                            chip with a pulsing dot, no spinner.
//   timed_out / failed    → iris "Replan" pill the user can click to
//                            re-queue the planner. Tooltip on hover
//                            shows the truncated planner_error.
//   succeeded             → renders nothing (the action_surface IS
//                            the planner's output, no extra UI needed).
//
// Optimistic-update: clicking Replan flips a local displayedStatus to
// ``'pending'`` immediately so the user sees the chip swap without
// waiting for the 15s poll. The next poll's row prop overrides this
// local state via the useEffect sync below.
function PlannerStatusAffordance({
  eventId,
  plannerStatus,
  plannerError,
}: {
  eventId: string;
  plannerStatus: PlannerStatus;
  plannerError: string | null;
}) {
  const [displayedStatus, setDisplayedStatus] = useState<PlannerStatus>(plannerStatus);
  const [replanning, setReplanning] = useState(false);

  // Re-sync when the parent's prop changes (i.e. the next poll
  // arrived with a fresh status from the backend).
  useEffect(() => {
    setDisplayedStatus(plannerStatus);
  }, [plannerStatus]);

  const handleReplan = useCallback(async () => {
    if (replanning) return;
    setReplanning(true);
    // Optimistic — flip the chip before the network call so the
    // affordance feels instant.
    setDisplayedStatus('pending');
    try {
      await replanInboxEvent(eventId);
    } catch (err) {
      // On failure, revert and surface to the user. Don't strand the
      // pill in the "pending" state.
      setDisplayedStatus(plannerStatus);
      const message =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : 'Could not queue replan.';
      toast.error(message);
    } finally {
      setReplanning(false);
    }
  }, [eventId, plannerStatus, replanning]);

  if (displayedStatus === 'succeeded') {
    return null;
  }

  if (displayedStatus === 'pending' || displayedStatus === 'in_progress') {
    return (
      <span
        data-testid="inbox-planner-pending"
        className="shrink-0 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[12px] uppercase tracking-wider animate-pulse"
        style={{
          background: 'var(--iris-soft)',
          color: 'var(--iris)',
          border: '1px solid var(--iris-ring)',
        }}
      >
        <span
          aria-hidden
          className="w-1.5 h-1.5 rounded-full"
          style={{ background: 'var(--iris)' }}
        />
        Thinking
      </span>
    );
  }

  // timed_out / failed → Replan pill.
  const truncatedError = plannerError ? plannerError.slice(0, 120) : null;
  return (
    <button
      type="button"
      data-testid="inbox-planner-replan"
      onClick={() => void handleReplan()}
      disabled={replanning}
      title={truncatedError ?? undefined}
      className="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[12px] uppercase tracking-wider"
      style={{
        background: 'var(--iris-soft)',
        color: 'var(--iris)',
        border: '1px solid var(--iris-ring)',
        opacity: replanning ? 0.5 : 1,
        cursor: replanning ? 'wait' : 'pointer',
      }}
    >
      <RefreshCw size={10} style={{ color: 'var(--iris)' }} />
      Replan
    </button>
  );
}


// PR #92 — narrative summary the agent wrote about this event.
// Renders one of three things inside the EventHeader card, above the
// field grid:
//
//   succeeded with text  → the paragraph itself, plus a small
//                           Resummarize pill the user can click
//                           if the summary is wrong.
//   succeeded with empty  → render nothing (degrades cleanly on
//                           pre-migration deploys where the model
//                           returns no text).
//   pending / retrying   → shimmer placeholder so the user knows
//                           a summary is coming. Same animate-pulse
//                           idiom as the planner Thinking chip.
//   failed               → a muted "Summary unavailable" line + a
//                           prominent Resummarize pill.
//
// Click-to-resummarize uses the same optimistic-update pattern as
// PR #89's PlannerStatusAffordance: we flip the local state to
// 'pending' immediately so the shimmer appears, the network call
// runs in the background, and the 15s poll picks up the real
// status from the worker.
// UI refinement #2a/#2b — the old blanket amber recurrence highlight
// is gone (it lit up the WHOLE paragraph). Emphasis is now SPARSE and
// Violet, driven by explicit planner-supplied phrases
// (``summary_highlights``: tenant/owner, address, the core request).
// We wrap only those verbatim substrings; everything else stays
// default body text. The emphasis style (Violet + weight + a faint
// underline) is deliberately distinct from the card's ambient Violet
// glow so highlighted entities actually pop (obs#5). Absent / no match
// → plain text.
// Commit 3 — a highlighted span in body copy is ONE of two things,
// never both: a NAVIGABLE ENTITY (a person / property / unit — coloured
// --iris, normal weight) or EMPHASIS (the core request — --ink at
// weight 600). Before this they were all bold-blue, which read three
// different things ("Maria Alvarez", "unit 4B", "hasn't cooled in three
// days") identically — and one of them isn't a link. Deterministic
// classification from the phrase shape: a short proper-noun phrase or
// anything with a unit/number/# is an entity; the longer descriptive
// phrase is emphasis.
function classifyHighlight(phrase: string): 'entity' | 'emphasis' {
  const p = phrase.trim();
  if (/\bunit\b|#|\d/i.test(p)) return 'entity';
  const words = p.split(/\s+/);
  if (words.length <= 3 && words.every((w) => /^[A-Z]/.test(w))) return 'entity';
  return 'emphasis';
}

function renderSummaryWithHighlight(
  text: string,
  highlights?: string[] | null,
): ReactNode[] {
  if (!text) return [text];
  const phrases = (highlights ?? [])
    .filter((p): p is string => typeof p === 'string' && p.trim().length > 1)
    // Longest first so a shorter overlapping phrase can't pre-empt a
    // longer, more specific one.
    .sort((a, b) => b.length - a.length);
  if (phrases.length === 0) return [text];
  const escaped = phrases.map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const re = new RegExp(`(${escaped.join('|')})`, 'gi');
  const parts: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m[0].length === 0) {
      re.lastIndex++;
      continue;
    }
    if (m.index > cursor) parts.push(text.slice(cursor, m.index));
    const kind = classifyHighlight(m[0]);
    parts.push(
      <span
        key={`hl-${key++}`}
        data-testid="inbox-summary-highlight"
        data-highlight-kind={kind}
        style={
          kind === 'entity'
            ? { color: 'var(--iris)', fontWeight: 400 }
            : { color: 'var(--ink)', fontWeight: 600 }
        }
      >
        {m[0]}
      </span>,
    );
    cursor = m.index + m[0].length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}


// obs#2 — compact "Received" timestamp for the email meta block.
function formatReceived(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}


function SummarySection({
  eventId,
  summary,
  highlights,
}: {
  eventId: string;
  // ``InboxEventSummary | undefined`` rather than the type's strict
  // shape: the API contract guarantees ``summary`` on every response
  // (PR #92), but if a pre-migration row leaks through OR a future
  // refactor relaxes the column set, the section must not crash the
  // whole EventHeader by dereferencing ``.status`` on undefined.
  // Render nothing in that case — same effect as
  // ``succeeded with empty text``.
  summary: InboxEventSummary | undefined;
  // UI refinement #2b — exact phrases to emphasize in Violet.
  highlights?: string[] | null;
}) {
  const [displayedStatus, setDisplayedStatus] = useState<SummaryStatus>(
    summary?.status ?? 'succeeded',
  );
  const [displayedText, setDisplayedText] = useState<string | null>(summary?.text ?? null);
  const [resummarizing, setResummarizing] = useState(false);

  // Sync with the parent's prop changes (next poll arrived with
  // a fresh status / text from the backend).
  useEffect(() => {
    setDisplayedStatus(summary?.status ?? 'succeeded');
    setDisplayedText(summary?.text ?? null);
  }, [summary?.status, summary?.text]);

  const handleResummarize = useCallback(async () => {
    if (resummarizing) return;
    setResummarizing(true);
    // Optimistic — flip the section to pending so the shimmer
    // appears before the network call resolves.
    setDisplayedStatus('pending');
    try {
      await resummarizeInboxEvent(eventId);
    } catch (err) {
      // Revert on failure; surface to the user.
      setDisplayedStatus(summary?.status ?? 'succeeded');
      setDisplayedText(summary?.text ?? null);
      const message =
        err instanceof ApiError || err instanceof Error
          ? err.message
          : 'Could not queue resummarize.';
      toast.error(message);
    } finally {
      setResummarizing(false);
    }
  }, [eventId, resummarizing, summary?.status, summary?.text]);

  // Treat "succeeded with no text" as the empty pre-migration state
  // and render nothing — better than a bare title with weird spacing.
  // Also returns null when the API didn't return a summary object
  // at all (defensive — see prop type comment).
  if (!summary || (displayedStatus === 'succeeded' && !displayedText)) {
    return null;
  }

  const isShimmer = displayedStatus === 'pending' || displayedStatus === 'retrying';
  const isFailed = displayedStatus === 'failed';

  return (
    // PR #120 — section is now its own bordered card per the mockup.
    // The iris "SUMMARY" label sits at the top with a refresh icon
    // anchored to the right; the body sits beneath at 13px / 1.7 line
    // height with optional inline amber highlights for recurrence
    // sentences. Was a flat header + paragraph living inside the
    // outer EventHeader card.
    <div
      className="mb-3.5"
      data-testid="inbox-event-summary"
      style={{
        // Commit 3 — resting content surface: white + card-edge, not a
        // brand-tinted panel.
        background: 'var(--canvas)',
        border: '0.5px solid var(--card-edge)',
        borderRadius: 'var(--r-md)',
        padding: '16px 18px',
      }}
    >
      <div className="flex items-center justify-between mb-2.5">
        <div
          className="uppercase"
          style={{
            // Muted eyebrow — structural section label, not a highlight.
            // Accent stays reserved for the "What Occupella noticed" insight
            // card + inline summary highlights below.
            color: 'var(--ink-subtle)',
            fontSize: '12px',
            letterSpacing: '0.08em',
            fontWeight: 500,
          }}
        >
          Summary
        </div>
        <Button
          variant="icon"
          size="sm"
          onClick={handleResummarize}
          disabled={resummarizing}
          aria-label="Resummarize"
          title={
            summary.error
              ? `Resummarize — last error: ${summary.error.slice(0, 120)}`
              : 'Resummarize'
          }
          data-testid="inbox-summary-resummarize"
          icon={
            <RefreshCw
              size={13}
              className={resummarizing ? 'animate-spin' : ''}
              style={{ color: 'var(--ink-muted)' }}
            />
          }
        />
      </div>
      {isShimmer ? (
        <div
          data-testid="inbox-summary-shimmer"
          className="rounded-[var(--r-sm)] animate-pulse"
          style={{
            background: 'var(--iris-soft)',
            border: '1px solid var(--iris-ring)',
            height: 72,
          }}
        />
      ) : isFailed ? (
        <p
          data-testid="inbox-summary-failed"
          className="leading-snug"
          style={{ color: 'var(--ink-subtle)', fontSize: 'var(--text-body)' }}
        >
          Summary unavailable.
        </p>
      ) : (
        <p
          data-testid="inbox-summary-text"
          // PR #120 — 13px / 1.7 line height per the mockup. Sentences
          // mentioning recurrence patterns ("second time", "in 90
          // days", etc.) get wrapped in an amber highlight pill so the
          // signal jumps off the page.
          style={{
            color: 'var(--ink)',
            fontSize: '13px',
            lineHeight: 1.7,
          }}
        >
          {displayedText ? renderSummaryWithHighlight(displayedText, highlights) : null}
        </p>
      )}
    </div>
  );
}


// PR #113 — ResummarizePill removed. The control is now an `icon`
// Button anchored to the SummarySection header row above.


// PR #93 — numbered workflow rendering of the planner's
// ``action_surface.actions`` array. Replaces the floating chip row
// that ConversationThread used to render at the top of the SSE
// stream; chips now appear as numbered steps inside the EventHeader
// card so they read like a sequenced workflow instead of a
// detached button strip.
//
// Each step shows: a small iris-tinted "1." / "2." marker, the
// action's ``label`` (bold), the action's ``intent`` (lighter
// secondary line), and per-step [Execute] / [Skip] controls.
// [Execute] synthesizes a SuggestionChipData and hands it to the
// parent's ``onPick`` callback (same downstream path the chip row
// took — confirm gate opens, write fires on confirm).
// [Skip] hides the step locally; it returns on the next 15s poll.
//
// TODO(rationale): the planner does not currently emit a per-action
// "why" / rationale field. The brief calls for a one-liner under
// the title explaining why the agent suggested it ("Broken glass
// is a safety risk; Buildium has this filed as General Inquiry").
// When the planner ships that field (separate PR), render it here
// between the label and the buttons.
// PR #98 — derive 2–4 bullets describing what the planner "noticed"
// about this event. Reads ``action_surface.signals`` verbatim if the
// backend supplies them; otherwise composes deterministic strings
// from ``enrichment`` so users see the agent's premises immediately
// instead of waiting on a planner refresh.
//
// Pure function. No LLM calls in the frontend. If 0–1 bullets can be
// derived, callers should hide the section entirely — better to
// render nothing than a thin one-line section masquerading as
// "analysis."
// Commit 2 — deterministic "blocking gap" thresholds. A gap only
// surfaces when a MISSING field actually BLOCKS the next step AND the
// item is hot/old enough to matter. This is a RULE, not a judgment:
// identical tickets get identical treatment (inconsistency is what
// destroys trust in an agent), so it's computed here in code, never by
// the model. Tune the numbers in one place.
//
// Only the ASSIGNEE gap is raised — an unassigned item blocks work. A
// missing due date on an already-assigned item does NOT block, so it is
// intentionally never a gap. "Hot/old enough" combines priority + age:
// the idle-hours bar drops as priority rises. LOW priority is absent
// from the map on purpose → an unassigned low-priority item NEVER
// raises a gap, no matter how old (silence is the hard part).
const GAP_ASSIGNEE_IDLE_HOURS: Record<string, number> = {
  urgent: 24, // urgent + unassigned ~1 day → nudge
  high: 48, // high + unassigned ~2 days → nudge
  medium: 72,
  normal: 72, // normal + unassigned ~3 days → nudge
  // low: omitted on purpose — never nudges.
};

function hoursSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 3_600_000;
}

// Returns the single most-blocking gap sentence for this row, or null.
// Max one gap — never stacks. Deterministic + templated here (never
// model-written). Exported for unit tests.
export function computeBlockingGap(row: InboxRow): string | null {
  const task = row.enrichment?.task;
  if (!task) return null;

  // GAP: no assignee. Blocking — unassigned work can't move — but only
  // once the item is hot/old enough to matter.
  const assigned =
    task.assigned_to_user_id != null ||
    (task.assignee_name?.trim() ?? '').length > 0;
  if (!assigned) {
    const priority = (task.priority?.trim() || 'normal').toLowerCase();
    const threshold = GAP_ASSIGNEE_IDLE_HOURS[priority];
    const idle = hoursSince(row.event_time);
    if (threshold != null && idle != null && idle >= threshold) {
      const days = Math.max(1, Math.floor(idle / 24));
      const dayWord = days === 1 ? 'day' : 'days';
      const pri =
        priority === 'normal' || priority === 'medium'
          ? ''
          : ` on a ${priority}-priority item`;
      return `No one's assigned yet — unassigned for ${days} ${dayWord}${pri}.`;
    }
  }
  return null;
}

function buildAgentNoticed(row: InboxRow): string[] {
  // Commit 2 — a deterministic blocking gap (e.g. no assignee on a hot
  // item) is the highest-priority observation; prepend it so it leads
  // the list, alongside the pattern/context bullets below.
  const gap = computeBlockingGap(row);

  // Backend-supplied signals win when present — they reflect the
  // planner's actual reasoning trace.
  const explicit = row.action_surface?.signals;
  if (Array.isArray(explicit) && explicit.length > 0) {
    const sig = explicit
      .filter((s): s is string => typeof s === 'string' && s.trim().length > 0)
      .slice(0, 4);
    return (gap ? [gap, ...sig] : sig).slice(0, 4);
  }

  const bullets: string[] = [];
  if (gap) bullets.push(gap);
  const task = row.enrichment?.task;
  const unit = row.enrichment?.unit;

  // 1) Reporter + property/unit context — the "who and where" line.
  const reporter = formatReporter(
    task?.requested_by_first_name,
    task?.requested_by_last_name,
  );
  const property = formatPropertyLine(unit?.address_line1, unit?.unit_number);
  if (reporter !== '—' && property !== '—') {
    bullets.push(`Reported by ${reporter} at ${property}`);
  } else if (reporter !== '—') {
    bullets.push(`Reported by ${reporter}`);
  } else if (property !== '—') {
    bullets.push(`Filed against ${property}`);
  }

  // 2) Category — what Buildium thinks this is. The category often
  // disagrees with the actual content of the description, which is
  // signal in itself for the planner.
  const category = task?.category?.trim();
  if (category) {
    bullets.push(`Filed in Buildium as "${category}"`);
  }

  // 3) Priority — also a Buildium classification the user might want
  // to challenge based on the task's text.
  const priority = task?.priority?.trim();
  if (priority) {
    bullets.push(`Buildium priority: ${priority}`);
  }

  // 4) Recent-history signal — if there are prior tickets on the
  // same unit or property, that's worth surfacing as a pattern hint.
  const unitHist = row.enrichment?.history_unit ?? [];
  if (unitHist.length > 0) {
    const word = unitHist.length === 1 ? 'ticket' : 'tickets';
    bullets.push(`${unitHist.length} prior ${word} on this unit in the last 90 days`);
  } else {
    const propHist = row.enrichment?.history_property ?? [];
    if (propHist.length > 0) {
      const word = propHist.length === 1 ? 'ticket' : 'tickets';
      bullets.push(
        `${propHist.length} prior ${word} on this property in the last 90 days`,
      );
    }
  }

  // Cap at 4 — the brief's maximum. Section hides at <2 (handled by
  // the caller, not here).
  return bullets.slice(0, 4);
}


// PR #120 — per-bullet semantic color for ``WHAT HELIXIS NOTICED``.
// Keyword match against the bullet's content:
//   * positive (green) — tenant cooperation cues ("offered",
//     "agreed", "provided", "willing")
//   * caution (amber)  — recurrence / pattern cues ("previous",
//     "prior", "similar", "again", "history", "earlier", "recent",
//     "pattern", "last month", "in NN days")
//   * danger  (red)    — data-quality / mismatch cues ("differs",
//     "mismatch", "missing", "discrepancy", "different from", "not on")
//   * iris    (default)— everything else
// Pure helper, no LLM calls.
function signalDotColor(text: string): string {
  const t = text.toLowerCase();
  if (/(differs?|mismatch|missing|discrepancy|different from|not on the lease|conflict)/.test(t)) {
    return 'var(--danger)';
  }
  if (
    /(no one's assigned|unassigned|no assignee|overdue|past due|previous|prior|similar|again|history|earlier|recently|recent|pattern|last (week|month|year)|repeat|second|third|2nd|3rd|in \d+ (days?|weeks?|months?))/.test(
      t,
    )
  ) {
    return 'var(--caution)';
  }
  if (/(offered|agreed|provided|willing|consent|cooperat|happy to)/.test(t)) {
    return 'var(--positive)';
  }
  return 'var(--iris)';
}


// PR #98 — "WHAT HELIXIS NOTICED" section between SummarySection and
// the metadata grid. Renders the bullets ``buildAgentNoticed``
// derived; hides entirely when fewer than 2 bullets are available
// (thin one-bullet sections read as filler, which undermines the
// "agent showing its work" voice).
//
// PR #120 — section is now its own iris-tinted card per the mockup,
// with a sparkles icon + iris-hover uppercase label, and per-bullet
// colored dots (green / amber / red / iris) based on the bullet's
// content.
function AgentNoticed({ row }: { row: InboxRow }) {
  const bullets = useMemo(() => buildAgentNoticed(row), [row]);
  if (bullets.length < 2) {
    return null;
  }
  return (
    <div
      className="mb-3.5"
      data-testid="inbox-agent-noticed"
      style={{
        // Commit 3 — resting content surface: white + card-edge. The
        // sparkles + iris label + per-bullet dots carry the brand
        // accent; the card itself no longer needs a fill.
        background: 'var(--canvas)',
        border: '0.5px solid var(--card-edge)',
        borderRadius: 'var(--r-md)',
        padding: '16px 18px',
      }}
    >
      <div className="flex items-center gap-1.5 mb-3">
        <div
          className="uppercase"
          style={{
            color: 'var(--iris-hover)',
            fontSize: '12px',
            letterSpacing: '0.08em',
            fontWeight: 500,
          }}
        >
          What Occupella noticed
        </div>
      </div>
      <ul
        className="flex flex-col gap-2.5"
        style={{ color: 'var(--ink)' }}
        data-testid="inbox-agent-noticed-list"
      >
        {bullets.map((bullet, i) => (
          <li
            key={i}
            className="flex items-start gap-2.5"
            data-testid={`inbox-agent-noticed-bullet-${i}`}
            style={{ fontSize: '13px', lineHeight: 1.5 }}
          >
            <span
              aria-hidden
              className="shrink-0"
              data-testid={`inbox-agent-noticed-dot-${i}`}
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                marginTop: 7,
                background: signalDotColor(bullet),
                display: 'inline-block',
              }}
            />
            <span>{bullet}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}


// Email-ingest rows (email_triage maps an inbound email onto the task
// shape). Detected by the Email.* event family or the "Email" category.
function isEmailRow(row: InboxRow): boolean {
  if ((row.event_name ?? '').startsWith('Email.')) {
    return true;
  }
  return (row.enrichment?.task?.category ?? '').trim().toLowerCase() === 'email';
}

// The full inbound email, rendered verbatim — the user explicitly wants
// the real message, not a summary. Body lives on enrichment.task.description
// (email_triage mapping); the sender/thread come off task.raw. Renders
// only for email rows; null otherwise.
function EmailBodyCard({ row }: { row: InboxRow }) {
  if (!isEmailRow(row)) {
    return null;
  }
  const task = row.enrichment?.task;
  const body = (task?.description ?? '').trim();
  if (!body) {
    return null;
  }
  const raw = (task?.raw ?? {}) as Record<string, unknown>;
  const fromName = typeof raw.from_name === 'string' ? raw.from_name.trim() : '';
  const fromEmail = typeof raw.from_email === 'string' ? raw.from_email.trim() : '';
  const sender = fromName || fromEmail || 'Unknown sender';
  const threadId = typeof raw.thread_id === 'string' ? raw.thread_id : '';
  const gmailUrl = threadId
    ? `https://mail.google.com/mail/u/0/#all/${threadId}`
    : 'https://mail.google.com/mail/u/0/#inbox';

  return (
    <div
      className="mb-3.5"
      data-testid="inbox-email-body"
      style={{
        background: 'var(--canvas-raised)',
        border: '0.5px solid var(--line)',
        borderRadius: 'var(--r-md)',
        padding: '16px 18px',
      }}
    >
      <div className="flex items-center gap-1.5 mb-3">
        <Mail size={14} style={{ color: 'var(--ink-faint)' }} />
        <div
          className="uppercase"
          style={{
            color: 'var(--ink-faint)',
            fontSize: '12px',
            letterSpacing: '0.08em',
            fontWeight: 500,
          }}
        >
          Email from {sender}
        </div>
      </div>
      <div
        data-testid="inbox-email-body-text"
        style={{
          color: 'var(--ink)',
          fontSize: '13px',
          lineHeight: 1.55,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {body}
      </div>
      <a
        href={gmailUrl}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="inbox-open-in-gmail"
        className="inline-flex items-center gap-1 text-[12px] font-medium mt-3"
        style={{ color: 'var(--iris)' }}
      >
        Open in Gmail
        <ExternalLink size={12} />
      </a>
    </div>
  );
}


// Email-ingest — the drafted reply, editable, with a Review & send
// button. The planner emits the draft on the ``reply_to_email`` action
// (``primary_action.draft`` / ``parameters.body``); sending executes that
// action (body override = the user's edits), which the backend routes to
// Composio Gmail on the original thread. Never auto-sends. Renders the
// composer once the planner has produced the reply action; until then a
// quiet "drafting" hint while the planner is still working.
function EmailReplyComposer({
  row,
  onExecuted,
}: {
  row: InboxRow;
  onExecuted: (rowId: string) => void;
}) {
  const actions = row.action_surface?.actions ?? [];
  const idx = actions.findIndex((a) => a.tool_id === 'reply_to_email');
  const action = idx >= 0 ? actions[idx] : null;
  const initialDraft =
    action?.primary_action?.draft ??
    (typeof action?.parameters?.body === 'string' ? (action.parameters.body as string) : '') ??
    '';

  const [draft, setDraft] = useState(initialDraft);
  const [state, setState] = useState<'idle' | 'sending' | 'sent'>('idle');

  // Re-seed when the row (or the planner's draft) changes.
  useEffect(() => {
    setDraft(initialDraft);
    setState('idle');
  }, [row.id, initialDraft]);

  const send = useCallback(async () => {
    if (!draft.trim() || state === 'sending' || idx < 0) return;
    setState('sending');
    try {
      const resp = await executeInboxAction(row.id, idx, { body: draft });
      toast.success(resp?.result?.summary ?? 'Reply sent');
      setState('sent');
      onExecuted(row.id);
    } catch (err) {
      const message =
        err instanceof ApiError || err instanceof Error ? err.message : 'Reply failed to send.';
      toast.error(message);
      setState('idle');
    }
  }, [draft, state, idx, row.id, onExecuted]);

  // Planner hasn't produced the reply yet — show a quiet hint while it's
  // actively working; otherwise render nothing (e.g. failed/again later).
  if (!action || idx < 0) {
    const pending =
      row.planner_status === 'pending' || row.planner_status === 'in_progress';
    if (!pending) return null;
    return (
      <div
        className="text-[13px]"
        data-testid="inbox-email-reply-drafting"
        style={{ color: 'var(--ink-faint)' }}
      >
        Occupella is drafting a reply…
      </div>
    );
  }

  return (
    <div
      data-testid="inbox-email-reply"
      style={{
        background: 'var(--canvas-raised)',
        border: '0.5px solid var(--iris-ring)',
        borderRadius: 'var(--r-md)',
        padding: 18,
      }}
    >
      <div className="flex items-center gap-1.5 mb-3">
        <Reply size={14} style={{ color: 'var(--iris-hover)' }} />
        <div
          className="uppercase"
          style={{
            color: 'var(--iris-hover)',
            fontSize: '12px',
            letterSpacing: '0.08em',
            fontWeight: 500,
          }}
        >
          Drafted reply — review before sending
        </div>
      </div>
      <textarea
        data-testid="inbox-email-reply-textarea"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        disabled={state === 'sending' || state === 'sent'}
        rows={8}
        className="w-full resize-y rounded-[var(--r-sm)] p-3 text-[13px]"
        style={{
          background: 'var(--canvas)',
          border: '0.5px solid var(--line)',
          color: 'var(--ink)',
          lineHeight: 1.55,
        }}
      />
      <div className="flex items-center gap-3 mt-3">
        <button
          type="button"
          onClick={send}
          disabled={!draft.trim() || state === 'sending' || state === 'sent'}
          data-testid="inbox-email-reply-send"
          className="inline-flex items-center gap-1.5 text-[13px] font-medium rounded-[var(--r-sm)] transition-colors cursor-pointer disabled:cursor-default disabled:opacity-60"
          style={{
            color: 'var(--iris)',
            background: 'var(--iris-soft)',
            border: '1px solid var(--iris-ring)',
            padding: '6px 14px',
          }}
        >
          <Send size={13} />
          {state === 'sending' ? 'Sending…' : state === 'sent' ? '✓ Sent' : 'Review & send'}
        </button>
        <span className="text-[12px]" style={{ color: 'var(--ink-faint)' }}>
          Sends via Gmail on the original thread. Nothing sends until you click.
        </span>
      </div>
    </div>
  );
}


// ── Dynamic action cards ─────────────────────────────────────────────
//
// The planner emits a VARIABLE list of actions (action_surface.actions);
// we render one card per action — 1-2 for a simple event, up to 6 when the
// signals justify it — instead of the old fixed 4-button decision grid.
// Each card maps to a registered backend action (tool_id -> HANDLERS),
// executed through executeInboxAction -> confirm/idempotency, so the model
// can only choose + fill registered actions, never invent them. Message /
// note cards carry an editable draft; "open in X" floor actions (noop_*)
// render as links.

// Irreversible outbound actions — one click SENDS to a tenant / email with no
// undo. These require an explicit SECOND "confirm" click so a misclick can't
// fire them. Internal writes (notes, tasks, task-status) execute on one click.
const DESTRUCTIVE_INBOX_TOOLS = new Set(['send_tenant_message', 'reply_to_email']);

function ActionCard({
  row,
  action,
  index,
  onExecuted,
}: {
  row: InboxRow;
  action: InboxAction;
  index: number;
  onExecuted: (rowId: string) => void;
}) {
  const pa = action.primary_action ?? null;
  const toolId = action.tool_id ?? '';
  const isLink = toolId.startsWith('noop_');
  const params = action.parameters ?? {};
  const linkUrl =
    typeof params.buildium_url === 'string'
      ? (params.buildium_url as string)
      : typeof params.gmail_url === 'string'
        ? (params.gmail_url as string)
        : null;

  const draftSeed =
    pa?.draft ?? (typeof params.body === 'string' ? (params.body as string) : '') ?? '';
  const hasDraft =
    !isLink &&
    (draftSeed.trim().length > 0 || pa?.kind === 'send_message' || pa?.kind === 'create_note');

  const isDestructive = DESTRUCTIVE_INBOX_TOOLS.has(toolId);
  const [draft, setDraft] = useState(draftSeed);
  const [state, setState] = useState<'idle' | 'confirming' | 'sending' | 'sent'>('idle');
  useEffect(() => {
    setDraft(draftSeed);
    setState('idle');
  }, [row.id, index, draftSeed]);
  // An armed "confirm" auto-disarms after a few seconds so a forgotten primed
  // button can't sit ready to fire an irreversible send on a later stray click.
  useEffect(() => {
    if (state !== 'confirming') return;
    const t = setTimeout(() => setState('idle'), 5000);
    return () => clearTimeout(t);
  }, [state]);

  const buttonLabel = pa?.label?.trim() || action.label?.trim() || 'Do it';

  const run = useCallback(async () => {
    if (state === 'sending' || state === 'sent') return;
    if (hasDraft && !draft.trim()) return;
    // Two-step for irreversible sends: first click ARMS the confirm, second FIRES.
    // Reversible/internal writes skip straight to execution.
    if (isDestructive && state === 'idle') {
      setState('confirming');
      return;
    }
    setState('sending');
    try {
      const overrides = hasDraft ? { body: draft } : {};
      const resp = await executeInboxAction(row.id, index, overrides);
      toast.success(resp?.result?.summary ?? 'Done');
      setState('sent');
      onExecuted(row.id);
    } catch (err) {
      const message =
        err instanceof ApiError || err instanceof Error ? err.message : 'Action failed.';
      toast.error(message);
      setState('idle');
    }
  }, [state, hasDraft, draft, row.id, index, onExecuted, isDestructive]);

  return (
    <div
      data-testid={`inbox-action-card-${index}`}
      style={{
        background: 'var(--canvas-raised)',
        border: '0.5px solid var(--iris-ring)',
        borderRadius: 'var(--r-md)',
        padding: '14px 16px',
      }}
    >
      <div className="font-medium" style={{ color: 'var(--ink)', fontSize: '13px', lineHeight: 1.4 }}>
        {action.label?.trim() || buttonLabel}
      </div>
      {action.intent?.trim() ? (
        <div
          className="mt-1"
          style={{ color: 'var(--ink-muted)', fontSize: '13px', lineHeight: 1.5 }}
        >
          {action.intent}
        </div>
      ) : null}
      {action.rationale?.trim() ? (
        <div
          className="mt-1.5 flex items-start gap-1.5"
          style={{ color: 'var(--ink-faint)', fontSize: '12px', lineHeight: 1.5 }}
        >
          <span
            aria-hidden
            style={{
              width: 5,
              height: 5,
              borderRadius: '50%',
              marginTop: 6,
              background: 'var(--iris)',
              display: 'inline-block',
              flexShrink: 0,
            }}
          />
          <span>{action.rationale}</span>
        </div>
      ) : null}
      {hasDraft ? (
        <textarea
          data-testid={`inbox-action-card-draft-${index}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={state !== 'idle'}
          rows={6}
          className="w-full resize-y rounded-[var(--r-sm)] p-3 text-[13px] mt-3"
          style={{
            background: 'var(--canvas)',
            border: '0.5px solid var(--line)',
            color: 'var(--ink)',
            lineHeight: 1.55,
          }}
        />
      ) : null}
      <div className="mt-3">
        {isLink && linkUrl ? (
          <a
            href={linkUrl}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={`inbox-action-card-link-${index}`}
            className="inline-flex items-center gap-1 text-[13px] font-medium"
            style={{ color: 'var(--iris)' }}
          >
            {buttonLabel}
            <ExternalLink size={12} />
          </a>
        ) : (
          <div className="inline-flex items-center gap-2">
            <button
              type="button"
              onClick={run}
              disabled={state === 'sending' || state === 'sent' || (hasDraft && !draft.trim())}
              data-testid={`inbox-action-card-execute-${index}`}
              className="inline-flex items-center gap-1.5 h-9 text-[14px] font-medium rounded-[var(--r-sm)] transition-colors cursor-pointer disabled:cursor-default"
              // The card's one action is the primary button (DESIGN.md
              // Buttons): solid iris, darker while it asks to confirm, gray
              // once it can't be pressed.
              style={
                state === 'sending' || state === 'sent' || (hasDraft && !draft.trim())
                  ? { color: 'var(--disabled-ink)', background: 'var(--disabled-bg)', border: '1px solid var(--disabled-bg)', padding: '0 14px' }
                  : {
                      color: '#fff',
                      background: state === 'confirming' ? 'var(--iris-press)' : 'var(--iris)',
                      border: '1px solid transparent',
                      padding: '0 14px',
                    }
              }
            >
              {state === 'sending'
                ? 'Working…'
                : state === 'sent'
                  ? '✓ Done'
                  : state === 'confirming'
                    ? 'Confirm and send'
                    : buttonLabel}
            </button>
            {state === 'confirming' ? (
              <button
                type="button"
                onClick={() => setState('idle')}
                data-testid={`inbox-action-card-cancel-${index}`}
                className="text-[12px] cursor-pointer"
                style={{ color: 'var(--ink-muted)', background: 'none', border: 'none' }}
              >
                Cancel
              </button>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}


// Design item: "one card you can flip through, A/B/C style" — the old
// vertical stack of N full-size cards (each with its own textarea) read
// as disorganized and pushed the freeform input below the fold. The
// list now renders ONE card at a time with a tab strip to flip between
// the planner's options. All cards stay MOUNTED (hidden via display:
// none) so a half-edited draft survives flipping away and back.
function ActionCardList({
  row,
  onExecuted,
}: {
  row: InboxRow;
  onExecuted: (rowId: string) => void;
}) {
  const actions = row.action_surface?.actions ?? [];
  const [activeIdx, setActiveIdx] = useState(0);

  // Reset the pager when the user switches rows — index 0 is the
  // planner's primary suggestion for the new event.
  useEffect(() => {
    setActiveIdx(0);
  }, [row.id]);

  // UI refinement #3 — drop "Open in Buildium / Gmail" external-link
  // actions (tool_id ``noop_*``). Work happens inside Occupella, not by
  // bouncing the user to another app. Keep each action's ORIGINAL index
  // so the executor still targets the right backend action.
  // Also drop an action the server marked ``unavailable_reason``: a write
  // to a PMS this workspace is not on, which could only fail.
  const visible = actions
    .map((action, i) => ({ action, i }))
    .filter(({ action }) => !action.tool_id.startsWith('noop_') && !action.unavailable_reason);
  if (visible.length === 0) {
    return null;
  }
  // Defensive clamp: a replan can shrink the action list under the
  // current index.
  const active = Math.min(activeIdx, visible.length - 1);

  return (
    <div data-testid="inbox-action-cards">
      {visible.length > 1 && (
        <div
          className="flex items-center gap-1.5 mb-2.5 flex-wrap"
          data-testid="inbox-action-tabs"
          role="tablist"
          aria-label="Suggested actions"
        >
          {visible.map(({ action, i }, pos) => {
            const selected = pos === active;
            const label = (action.label ?? action.primary_action?.label ?? '').trim();
            return (
              <button
                key={i}
                type="button"
                role="tab"
                aria-selected={selected}
                data-testid={`inbox-action-tab-${pos}`}
                onClick={() => setActiveIdx(pos)}
                className="inline-flex items-center gap-1.5 text-[12px] font-medium transition-colors"
                style={{
                  padding: '4px 10px',
                  borderRadius: 'var(--r-sm)',
                  // Commit 3 — the ACTIVE tab is the one state that earns
                  // fill (iris-soft + iris-ring); inactive tabs are white
                  // + card-edge so the selection reads at a glance.
                  border: selected ? '0.5px solid var(--iris-ring)' : '0.5px solid var(--card-edge)',
                  background: selected ? 'var(--iris-soft)' : 'var(--canvas)',
                  color: selected ? 'var(--iris)' : 'var(--ink-muted)',
                  cursor: 'pointer',
                  maxWidth: 220,
                }}
                title={label || `Option ${pos + 1}`}
              >
                <span
                  className="inline-flex items-center justify-center font-mono"
                  style={{ fontSize: 12, opacity: 0.85 }}
                >
                  {pos + 1}
                </span>
                {label ? <span className="truncate">{label}</span> : null}
              </button>
            );
          })}
          <span
            className="ml-auto text-[12px]"
            style={{ color: 'var(--ink-subtle)' }}
            data-testid="inbox-action-tabs-counter"
          >
            {active + 1} of {visible.length}
          </span>
        </div>
      )}
      {visible.map(({ action, i }, pos) => (
        <div key={i} style={pos === active ? undefined : { display: 'none' }}>
          <ActionCard row={row} action={action} index={i} onExecuted={onExecuted} />
        </div>
      ))}
    </div>
  );
}


function SuggestedWorkflow({
  row,
  onReplan,
  onFreeformSubmit,
  freeformPlaceholder,
  busy,
  onExecuted,
  freeformValue: controlledFreeform,
  onFreeformChange,
}: {
  row: InboxRow;
  onReplan: () => void;
  onFreeformSubmit?: (text: string) => Promise<void> | void;
  freeformPlaceholder?: string;
  busy: boolean;
  // Dynamic action cards execute through this (drop the row on success).
  // Standalone-test renders omit it, so cards simply don't show there.
  onExecuted?: (rowId: string) => void;
  // Lifted to ThreadPane in the real flow so the draft outlives this card
  // (typing is what unmounts it). Absent in standalone fixtures, which fall
  // back to the local state below.
  freeformValue?: string;
  onFreeformChange?: (v: string) => void;
}) {
  const actions = row.action_surface?.actions ?? [];
  const [localFreeform, setLocalFreeform] = useState('');
  const freeformValue = controlledFreeform ?? localFreeform;
  const setFreeformValue = onFreeformChange ?? setLocalFreeform;
  const freeformTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Nothing to show: no actions AND no freeform input -> no orphan header.
  if (actions.length === 0 && !onFreeformSubmit) {
    return null;
  }

  return (
    <div
      data-testid="inbox-workflow-card"
      style={{
        background: 'var(--iris-soft)',
        border: '0.5px solid var(--iris-ring)',
        borderRadius: 'var(--r-md)',
        padding: 18,
      }}
    >
      <div className="flex items-center justify-between mb-3.5">
        <div className="flex items-center gap-2">
          <div
            data-testid="inbox-workflow-header"
            className="font-medium leading-snug"
            style={{ color: 'var(--ink)', fontSize: '13px' }}
          >
            Here's what I'd do next
          </div>
        </div>
        <Button
          variant="icon"
          size="sm"
          onClick={onReplan}
          disabled={busy}
          aria-label="Re-plan suggestions"
          title="Re-plan suggestions"
          data-testid="inbox-workflow-replan"
          icon={<RefreshCw size={13} style={{ color: 'var(--ink-muted)' }} />}
        />
      </div>

      {/* Executed → show what happened, in place of the action list. The
          alternatives are gone by design (one action per item, enforced
          server-side with a 409), so re-offering them would be a lie. */}
      {row.executed_action_index !== null ? <ExecutedOutcome row={row} /> : null}

      {/* One card per planner action - variable count, each executable. */}
      {onExecuted && row.executed_action_index === null ? (
        <ActionCardList row={row} onExecuted={onExecuted} />
      ) : null}

      {/* Conversational escape hatch - always available. */}
      {onFreeformSubmit ? (
        <div className="mt-3">
          <IntegratedFreeformInput
            onSubmit={onFreeformSubmit}
            placeholder={freeformPlaceholder ?? 'Or describe what you want me to do...'}
            busy={busy}
            value={freeformValue}
            onChange={setFreeformValue}
            textareaRef={freeformTextareaRef}
          />
        </div>
      ) : null}
    </div>
  );
}




// The outcome of an executed action, rendered in place of the action list.
//
// The backend has persisted ``execution_result`` + ``executed_at`` since the
// execute route was written and the client never asked for them, so a finished
// item could only ever say "Sent" — a receipt for a REQUEST, not a
// confirmation of an OUTCOME. Those come apart constantly here: Buildium
// returns 200 and silently no-ops on several writes, so "we asked" and "it
// happened" are genuinely different facts.
//
// ⚠ Deliberately does NOT assert success. ``execution_result`` is the
// handler's own dict and its keys vary by action; anything this cannot read is
// shown as-is rather than smoothed into a confident "Done". Claiming an
// outcome we cannot see is the failure mode this whole change exists to fix.
function ExecutedOutcome({ row }: { row: InboxRow }) {
  const result = row.execution_result ?? null;
  const when = row.executed_at ? new Date(row.executed_at).toLocaleString() : null;

  // Pull the human-meaningful bits if the handler supplied them; otherwise
  // fall through to the raw pairs. Never invent a summary.
  const entries = result
    ? Object.entries(result).filter(
        ([k, v]) => k !== 'chain' && v !== null && v !== undefined && v !== '',
      )
    : [];

  return (
    <div
      className="mt-3 rounded-lg border p-3"
      style={{ borderColor: 'var(--hairline)', background: 'var(--surface-2)' }}
      data-testid="inbox-executed-outcome"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
          Completed
        </span>
        {when ? (
          <span className="text-[12px]" style={{ color: 'var(--ink-3)' }}>
            {when}
          </span>
        ) : null}
      </div>
      {entries.length > 0 ? (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {entries.map(([k, v]) => (
            <Fragment key={k}>
              <dt className="text-[12px]" style={{ color: 'var(--ink-3)' }}>
                {k.replace(/_/g, ' ')}
              </dt>
              <dd
                className="text-[12px] break-words"
                style={{ color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}
              >
                {typeof v === 'object' ? JSON.stringify(v) : String(v)}
              </dd>
            </Fragment>
          ))}
        </dl>
      ) : (
        // Honest about the gap rather than filling it. An item executed before
        // the outcome was carried has no detail to show, and saying so beats
        // implying the action did nothing.
        <p className="mt-1 text-[12px]" style={{ color: 'var(--ink-3)' }}>
          No detail was recorded for this action.
        </p>
      )}
    </div>
  );
}


// PR #99 step 7 — the free-text input the brief describes as "the
// always-available escape hatch from the offered paths." Lives at
// the bottom of the SuggestedWorkflow container as a card-shaped
// element (same width / horizontal margin as the suggestion list)
// and renders even when the actions array is empty.
//
// Local state for the textarea contents so the parent stays single-
// purpose (state lives next to the input, not threaded through
// ThreadPane). On submit we clear the input only after the parent's
// async handler resolves — if it throws, the user keeps their
// text and can retry.
// PR #126 — IntegratedFreeformInput now runs a small state machine
// instead of a single ``submitting`` boolean:
//
//   idle    → button reads "Send" with iris-gradient bg
//   sending → button shows spinner + "Sending…" (disabled)
//   error   → inline error row appears UNDER the input with the
//             error message + a Retry button; the input stays
//             interactive so the user can edit before retrying.
//             Click Retry → re-fire send() with the current value
//             (which still holds their original text).
//
// This closes the audit gap "No loading / progress feedback" and "No
// way to retry, edit, or cancel the just-submitted instruction."
// The toast.error in handleFreeformSubmit upstream is still fired
// for the global signal; the inline UI here is the localized one.
type FreeformPhase = 'idle' | 'sending' | 'error';


function IntegratedFreeformInput({
  onSubmit,
  placeholder,
  busy,
  value: controlledValue,
  onChange,
  textareaRef,
}: {
  // PR #126 — onSubmit can now reject. The input's 'error' phase
  // fires when the returned promise rejects; success transitions to
  // 'sent'. The upstream handler (e.g. ThreadPane.handleFreeformSubmit)
  // is expected to re-throw after toasting so this UI gets the
  // failure signal.
  onSubmit: (text: string) => Promise<void> | void;
  placeholder: string;
  busy: boolean;
  // PR #120 — optional controlled-value pair. When supplied (real
  // inbox flow), the decision-grid above can prefill the textarea by
  // setting ``value``. When absent (older standalone test fixtures),
  // the input falls back to local state so existing tests still work.
  value?: string;
  onChange?: (v: string) => void;
  // PR #120 — ref pass-through so decision-grid buttons can focus
  // the textarea after prefilling.
  textareaRef?: MutableRefObject<HTMLTextAreaElement | null>;
}) {
  const [localValue, setLocalValue] = useState('');
  const isControlled = controlledValue !== undefined && onChange !== undefined;
  const value = isControlled ? (controlledValue as string) : localValue;
  const setValue = isControlled ? (onChange as (v: string) => void) : setLocalValue;
  const [phase, setPhase] = useState<FreeformPhase>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const trimmed = value.trim();
  const isSending = phase === 'sending';
  // Disable Send when: parent says busy, mid-send, or empty input.
  // ``error`` phase keeps Send enabled so the user can edit + retry
  // without first clearing the error chip.
  const disabled = busy || isSending || trimmed.length === 0;

  const send = async () => {
    if (trimmed.length === 0 || isSending) return;
    // Clear any lingering error from a previous attempt so the user
    // doesn't see "stale error" + "retrying" simultaneously.
    setErrorMsg(null);
    setPhase('sending');
    try {
      await Promise.resolve(onSubmit(trimmed));
      if (!mountedRef.current) return;
      setValue('');
      // Straight back to idle. The user's message is now a bubble in the
      // conversation, which says who received it; a success chip on the
      // button would be a second, vaguer answer to the same question.
      setPhase('idle');
    } catch (err) {
      if (!mountedRef.current) return;
      const message =
        err instanceof Error && err.message
          ? err.message
          : typeof err === 'string' && err
            ? err
            : 'Could not send. Try again.';
      setErrorMsg(message);
      setPhase('error');
      // Textarea KEEPS the user's text so they can edit + retry.
    }
  };

  return (
    // PR #120 — flat single-card chrome with message icon + iris-
    // gradient Send button. PR #126 adds an inline error row below
    // when the submit rejects.
    <div className="mt-3.5" data-testid="inbox-workflow-freeform">
      <div
        style={{
          background: 'var(--canvas-raised)',
          border:
            phase === 'error'
              ? '0.5px solid var(--danger)'
              : '0.5px solid var(--card-edge)',
          borderRadius: 'var(--r-md)',
          padding: '10px 14px',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}
      >
        <MessageSquare size={16} style={{ color: 'var(--ink-faint)', flexShrink: 0 }} />
        <textarea
          ref={textareaRef}
          data-testid="inbox-workflow-freeform-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={placeholder}
          rows={1}
          // ⚠ minHeight 22 measured 576x22 in a real browser — a wide, thin
          // band inside a taller row. `self-stretch` makes the whole row the
          // target without changing its height (the send button sets that);
          // 24 is the WCAG 2.2 AA floor for when it is the tallest thing
          // there, e.g. before the button renders.
          className="flex-1 resize-none outline-none self-stretch"
          style={{
            background: 'transparent',
            color: 'var(--ink)',
            border: 'none',
            fontSize: 13,
            minHeight: 24,
            maxHeight: 140,
            padding: 0,
          }}
        />
        {/* Send / Sending. There is deliberately no third state.
            ⚠ A green "✓ Sent" used to replace this button for two seconds,
            and in a product that texts tenants and emails owners on your
            behalf, "Sent" is the most alarming word available — it reads as
            "a message went OUT", not "Occupella heard you". This file's own
            note already says it: "Sent" is a receipt for a REQUEST, not a
            confirmation of an OUTCOME.
            The message appearing in the conversation IS the receipt, and it
            is unambiguous about who received it. */}
        <button
          type="button"
          data-testid="inbox-workflow-freeform-send"
            data-phase={phase}
          disabled={disabled}
          onClick={() => void send()}
          title={
            trimmed.length === 0
              ? 'Type a message to send'
              : isSending
                ? 'Sending…'
                : undefined
          }
          className="inline-flex items-center gap-1.5 transition-opacity"
          style={{
            background: disabled ? 'var(--canvas-hover)' : 'var(--iris)',
            color: disabled ? 'var(--ink-faint)' : 'var(--canvas)',
            border: 'none',
            padding: '7px 16px',
            borderRadius: 'var(--r-sm)',
            fontSize: 12,
            fontWeight: 500,
            cursor: disabled ? 'not-allowed' : 'pointer',
            opacity: disabled ? 0.7 : 1,
          }}
        >
          {isSending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
          {isSending ? 'Sending…' : 'Send'}
        </button>
      </div>

      {/* PR #126 — inline error chip with Retry. Only shown in
          'error' phase; clears as soon as the user starts a new
          send attempt. The toast.error fires globally too via the
          parent handler — this is the persistent localized signal
          that survives until the user acts on it. */}
      {phase === 'error' && errorMsg ? (
        <div
          className="mt-2 flex items-center gap-2"
          data-testid="inbox-workflow-freeform-error"
          role="alert"
          style={{
            background: 'var(--danger-bg)',
            border: '0.5px solid var(--danger)',
            borderRadius: 8,
            padding: '8px 12px',
            fontSize: 12,
            color: 'var(--danger)',
          }}
        >
          <AlertCircle size={13} style={{ flexShrink: 0 }} />
          <span className="flex-1" data-testid="inbox-workflow-freeform-error-message">
            {errorMsg}
          </span>
          <button
            type="button"
            data-testid="inbox-workflow-freeform-retry"
            onClick={() => void send()}
            disabled={trimmed.length === 0}
            className="inline-flex items-center gap-1"
            style={{
              background: 'transparent',
              border: '0.5px solid var(--danger)',
              color: 'var(--danger)',
              padding: '4px 10px',
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 500,
              cursor: trimmed.length === 0 ? 'not-allowed' : 'pointer',
              opacity: trimmed.length === 0 ? 0.5 : 1,
            }}
          >
            <RefreshCw size={11} />
            Retry
          </button>
        </div>
      ) : null}
    </div>
  );
}
function formatEventDate(iso: string): string {
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return `${date.getMonth() + 1}/${date.getDate()}/${date.getFullYear()}`;
  } catch {
    return '';
  }
}


function EventHeader({
  row,
  onReplan,
  onFreeformSubmit,
  freeformPlaceholder,
  busy,
  onBackToInbox,
  onExecuted,
  conversing = false,
  freeformValue,
  onFreeformChange,
}: {
  row: InboxRow;
  // PR #122 — the per-step Execute / Skip / Why-this controls and
  // the InlineConfirmButton 5s-countdown were retired with the
  // numbered step list. EventHeader no longer threads pick /
  // execute callbacks down — the action_surface only powers the
  // decision-button labels now. Re-plan stays meaningful because
  // it regenerates action_surface.actions which the buttons read.
  onReplan?: () => void;
  // PR #99 step 7 — integrated freeform input. Optional so the
  // PR #87 standalone EventHeader fixture still works.
  onFreeformSubmit?: (text: string) => Promise<void> | void;
  freeformPlaceholder?: string;
  busy?: boolean;
  // PR #120 — breadcrumb back-link, supplied by ThreadPane in the
  // real flow. Standalone-test EventHeaders don't get this, so the
  // breadcrumb row simply doesn't render.
  onBackToInbox?: () => void;
  // Email-ingest — lets the email reply composer mark the row executed
  // (drop it from the list) after the reply sends. Only the real
  // ThreadPane flow passes it.
  onExecuted?: (rowId: string) => void;
  // The item has become a conversation, so the suggested-actions card gives
  // way. Everything ABOVE it — summary, what Occupella noticed, the field
  // strip — stays: that is the context the conversation is about.
  conversing?: boolean;
  // Owned by ThreadPane so the draft survives the card composer unmounting
  // in favour of the docked one. Optional: standalone EventHeader fixtures
  // pass neither and SuggestedWorkflow falls back to its own state.
  freeformValue?: string;
  onFreeformChange?: (v: string) => void;
}) {
  const task = row.enrichment?.task;
  const unit = row.enrichment?.unit;
  const title = task?.title?.trim() || row.event_name;
  const propertyLine = formatPropertyLine(unit?.address_line1, unit?.unit_number);
  const reporter = formatReporter(task?.requested_by_first_name, task?.requested_by_last_name);
  // PR #103 — backend now decorates ``task.assignee_name`` by JOINing
  // ``buildium_staff`` on read (closes the PR #86 TODO that left
  // this hardcoded to ``—``). Falls back to ``—`` when the staff
  // cache hasn't synced the assignee yet, or Buildium has no
  // assignee on the task.
  // Field-strip values — RAW (empty string when absent), NOT the old
  // "—"/"Not set" placeholders. The strip omits empty fields entirely
  // (a missing field is shorter, never a placeholder chip); a blocking
  // gap surfaces in "What Occupella noticed" instead (Commit 2).
  const assignee = task?.assignee_name?.trim() || '';
  const category = task?.category?.trim() || '';
  const priority = task?.priority?.trim() || '';
  const dueFormatted = formatDueDate(task?.due_date);
  const due = dueFormatted === '—' ? '' : dueFormatted;
  const property = propertyLine === '—' ? '' : propertyLine;

  // PR #120 — subtitle parts: "Reported by X · 5/17/2026 · 160 East
  // End Avenue #120". Skip empty segments so we don't render
  // dangling middle-dots when reporter or property is unknown.
  const subtitleParts = [
    reporter !== '—' ? `Reported by ${reporter}` : null,
    formatEventDate(row.event_time),
    propertyLine !== '—' ? propertyLine : null,
  ].filter((s): s is string => Boolean(s));

  return (
    // PR #120 — outer container is now transparent (each subsection
    // wears its own card). Was a single ``--canvas-raised`` panel
    // wrapping everything; the mockup distributes that visual weight
    // across summary + agent-noticed + each metadata cell + the
    // suggested-workflow card.
    <div data-testid="inbox-event-header" className="mb-6">
      {/* Breadcrumb row — only when ThreadPane supplies an
          ``onBackToInbox``. Visual on wide screens (rail is always
          visible) but anchors the pane's context. */}
      {onBackToInbox && (
        <div
          // min-w-0 + truncate on the title span: a long unbroken title
          // (a pasted URL, a run-on subject) must CSS-ellipsize inside
          // the pane, never widen it (#448 — ui-fuzz visual finding).
          className="flex items-center gap-2 mb-3.5 text-[12px] min-w-0"
          style={{ color: 'var(--ink-subtle)' }}
          data-testid="inbox-event-breadcrumb"
        >
          <button
            type="button"
            onClick={onBackToInbox}
            // ⚠ 48x18 measured. This is the ONLY way back from the detail
            // pane, and it was two-thirds of a target — the one control on
            // the page where a miss costs the reader their place.
            className="inline-flex items-center gap-1 min-h-6 -my-1 transition-colors shrink-0"
            style={{ color: 'var(--ink-muted)', cursor: 'pointer' }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.color = 'var(--ink)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.color = 'var(--ink-muted)';
            }}
          >
            <ArrowLeft size={13} />
            Inbox
          </button>
          <ChevronRight size={12} className="shrink-0" style={{ color: 'var(--ink-faint)' }} />
          <span className="min-w-0 truncate" style={{ color: 'var(--ink-muted)' }}>
            {title}
          </span>
        </div>
      )}

      {/* Title row + actions */}
      <div className="flex items-center justify-between gap-3 mb-1">
        {/* flex-1 + min-w-0 lets the title claim the row width and
            CSS-truncate (single line), so it never wraps or shoves the
            NEW / REPLAN / Archive controls (shrink-0) out of place. */}
        <div className="flex flex-1 items-center gap-2.5 min-w-0">
          <div
            // PR #110 — bumped event title to 16px so it clearly
            // dominates the 14px summary body below.
            // PR #120 — mockup bumps it again to 22px (display-style
            // hero for the detail pane).
            data-testid="inbox-event-title"
            className="font-medium leading-snug min-w-0 truncate"
            style={{
              color: 'var(--ink)',
              fontSize: '20px',
              letterSpacing: '-0.01em',
            }}
          >
            {title}
          </div>
          <span
            className="shrink-0 text-[12px] font-medium uppercase"
            style={{
              background: 'var(--danger-bg)',
              color: 'var(--danger)',
              padding: '2px 7px',
              borderRadius: 4,
              letterSpacing: '0.06em',
            }}
            data-testid="inbox-event-new-badge"
          >
            NEW
          </span>
          {/* PR #89 — planner lifecycle affordance. Shimmer while the
              worker is still drafting; Replan pill on timeout/failure. */}
          <PlannerStatusAffordance
            eventId={row.id}
            plannerStatus={row.planner_status}
            plannerError={row.planner_error}
          />
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {/* PR #120 — Archive + dots-menu. Visual chrome until the
              backend lands a row-archive endpoint; tooltips set
              expectations so the button looks live but the user
              knows it's not wired yet. */}
          <Button
            variant="ghost"
            size="sm"
            disabled
            aria-label="Archive — coming soon"
            title="Archive — coming soon"
            data-testid="inbox-event-archive"
            icon={<Archive size={13} />}
          >
            Archive
          </Button>
          <Button
            variant="icon"
            size="sm"
            disabled
            aria-label="More actions — coming soon"
            title="More actions — coming soon"
            data-testid="inbox-event-more"
            icon={<MoreHorizontal size={14} />}
          />
        </div>
      </div>

      {/* Subtitle line — "Reported by X · 5/17/2026 · Address" */}
      {subtitleParts.length > 0 && (
        <div
          className="text-[12px] mb-5"
          style={{ color: 'var(--ink-faint)' }}
          data-testid="inbox-event-subtitle"
        >
          {subtitleParts.join(' · ')}
        </div>
      )}

      {/* PR #92 — agent-written narrative summary above the field
          grid. Renders shimmer / text / Resummarize pill based on
          summary.status; returns null when there's nothing to show.

          PR #120 — SummarySection now wears its own bordered card. */}
      <SummarySection
        eventId={row.id}
        summary={row.summary}
        highlights={row.action_surface?.summary_highlights}
      />

      {/* PR #98 — bullets pulled from enrichment (or signals when the
          planner provides them). Makes the suggestions below stop
          feeling "predetermined" by showing the agent's premises. */}
      <AgentNoticed row={row} />

      {/* Email-ingest — the full inbound email, verbatim. Renders only
          for email rows; null for Buildium events. */}
      <EmailBodyCard row={row} />

      {/* One-line field strip — current operational state, read-only.
          Replaces the old six-box metadata grid: a single horizontal
          strip that gets SHORTER when there's less to say. Empty fields
          don't render (no "Not set"/"Unassigned" placeholder). Order:
          Priority · Category · Assignee · Due · Property. Reporter is
          NOT here — it already lives in the subtitle line above.
          Email rows have no Buildium fields, so they strip Sender ·
          Received instead. The fields update automatically as the agent
          acts through the suggested actions below; the strip is display,
          not an editor. */}
      {isEmailRow(row) ? (
        <FieldStrip
          items={[
            reporter !== '—'
              ? { key: 'sender', testid: 'inbox-strip-sender', node: reporter }
              : null,
            {
              key: 'received',
              testid: 'inbox-strip-received',
              node: formatReceived(row.event_time),
            },
          ]}
        />
      ) : (
        <FieldStrip
          items={[
            priority
              ? {
                  key: 'priority',
                  testid: 'inbox-strip-priority',
                  node: (
                    <span className="inline-flex items-center" style={{ gap: 6 }}>
                      <span
                        aria-hidden
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: '50%',
                          background: priorityDotColor(priority),
                          display: 'inline-block',
                        }}
                      />
                      {priority}
                    </span>
                  ),
                }
              : null,
            category
              ? { key: 'category', testid: 'inbox-strip-category', node: category }
              : null,
            assignee
              ? {
                  key: 'assignee',
                  testid: 'inbox-strip-assignee',
                  node: (
                    <span className="inline-flex items-center" style={{ gap: 6 }}>
                      <span
                        aria-hidden
                        className="inline-flex items-center justify-center"
                        style={{
                          width: 18,
                          height: 18,
                          borderRadius: '50%',
                          background: 'var(--iris)',
                          color: '#fff',
                          fontSize: 12,
                          fontWeight: 600,
                          lineHeight: 1,
                        }}
                      >
                        {assignee.charAt(0).toUpperCase()}
                      </span>
                      {assignee}
                    </span>
                  ),
                }
              : null,
            due ? { key: 'due', testid: 'inbox-strip-due', node: due } : null,
            property
              ? { key: 'property', testid: 'inbox-strip-property', node: property }
              : null,
          ]}
        />
      )}

      {/* UI refinement #3 — the standalone "Open in Buildium" header
          link is removed too. Work happens inside Occupella, not by
          bouncing the user out to Buildium. */}

      {/* PR #93 — workflow card at the bottom of the EventHeader.
          Renders only when the parent supplies a Re-plan handler
          (i.e., real inbox flow); the PR #87 regression fixture
          renders EventHeader standalone without these props and the
          section silently no-ops there.
          PR #122 — the numbered step list was retired; the card
          now hosts the 4 decision buttons + the freeform input.

          Email-ingest — email rows show the editable reply composer
          (the planner's drafted reply) INSTEAD of the Buildium-flavored
          decision grid; the grid's "Send to X / Add note / Create task"
          verbs don't fit an email reply. */}
      {/* ⚠ Once this is a conversation the card is GONE, not collapsed and
          not disabled. Leaving it up greyed would keep three buttons on
          screen that the user declined by typing, and the composer is docked
          at the bottom of the pane now — two input boxes for one item is
          worse than either arrangement alone. */}
      {isEmailRow(row) ? (
        onExecuted ? <EmailReplyComposer row={row} onExecuted={onExecuted} /> : null
      ) : onReplan && !conversing ? (
        <SuggestedWorkflow
          row={row}
          onReplan={onReplan}
          onFreeformSubmit={onFreeformSubmit}
          freeformPlaceholder={freeformPlaceholder}
          busy={busy ?? false}
          onExecuted={onExecuted}
          freeformValue={freeformValue}
          onFreeformChange={onFreeformChange}
        />
      ) : null}
    </div>
  );
}


// One-line field strip — replaces the six-box metadata grid.
// Renders the current operational state as a single horizontal row
// that gets SHORTER when there's less to say: empty fields are dropped
// by the caller (no "Not set"/"Unassigned" placeholder). Read-only —
// the values change automatically as the agent acts through the
// suggested actions; the strip is display, not an editor.
//
//   * Row: 13px, --ink, flex, 10px gap, wraps.
//   * Separators: middot in --line-strong.
//   * A 1px --line rule sits above the strip (16px margin above the
//     rule, 14px padding above the content).
interface StripItem {
  key: string;
  testid: string;
  node: ReactNode;
}

function FieldStrip({ items }: { items: Array<StripItem | null> }) {
  const visible = items.filter((x): x is StripItem => x !== null);
  if (visible.length === 0) return null;
  return (
    <div style={{ marginTop: 16 }} data-testid="inbox-field-strip">
      <div style={{ height: 1, background: 'var(--line)' }} />
      <div
        className="flex flex-row flex-wrap items-center"
        style={{
          gap: 10,
          paddingTop: 14,
          marginBottom: 14,
          fontSize: 13,
          color: 'var(--ink)',
        }}
      >
        {visible.map((item, i) => (
          <Fragment key={item.key}>
            {i > 0 && (
              <span aria-hidden style={{ color: 'var(--line-strong)' }}>
                ·
              </span>
            )}
            <span data-testid={item.testid} className="inline-flex items-center">
              {item.node}
            </span>
          </Fragment>
        ))}
      </div>
    </div>
  );
}

// Priority dot color — semantic, matches the strip spec: urgent →
// --danger, high/medium → --caution, low/normal/unknown → --ink-subtle.
function priorityDotColor(priority: string): string {
  const p = priority.trim().toLowerCase();
  if (p === 'urgent') return 'var(--danger)';
  if (p === 'high' || p === 'medium') return 'var(--caution)';
  return 'var(--ink-subtle)';
}


// PR #86 — text normalizer for the "first assistant turn equals
// the summary string" comparison. Collapse whitespace, strip
// trailing punctuation, lowercase. Keeps the comparison resilient
// to the planner adding/dropping a trailing period or stretching a
// space.
function normalizeForCompare(s: string): string {
  return s.replace(/\s+/g, ' ').replace(/[.,;:!?]+$/g, '').trim().toLowerCase();
}


// PR #86 — drop the leading complete assistant turn when its
// assembled content matches the row's action_surface summary
// (case-insensitive, punctuation-normalized). The EventHeader card
// above the conversation already renders the summary in
// structured form; re-rendering it as a free-floating bubble
// looks like a duplicate dump.
//
// Rules:
//   - Only the FIRST complete assistant turn is considered. Real
//     planner content after that one always renders.
//   - If the first turn is still streaming (no ``assistant_message_end``
//     seen yet), we leave it in place — better to show a partial
//     than to hide an arriving message.
//   - Events before the first ``assistant_message_start`` (narration,
//     suggestions, confirm-gate, user_bubble) are preserved.
function stripDuplicateSummaryTurn(
  events: ConversationEvent[],
  summary: string,
): ConversationEvent[] {
  if (!summary) return events;

  const firstStart = events.findIndex((e) => e.type === 'assistant_message_start');
  if (firstStart === -1) return events;

  const firstStartData = events[firstStart].data as Record<string, unknown>;
  const firstTurnId = String(firstStartData.turn_id ?? '');
  if (!firstTurnId) return events;

  let assembled = '';
  let endIdx = -1;
  for (let i = firstStart + 1; i < events.length; i++) {
    const e = events[i];
    const d = e.data as Record<string, unknown>;
    const turnId = String(d.turn_id ?? '');
    if (turnId !== firstTurnId) continue;
    if (e.type === 'assistant_message_token') {
      assembled += String(d.content ?? '');
    } else if (e.type === 'assistant_message_end') {
      endIdx = i;
      break;
    }
  }
  // Turn still streaming — let it ride.
  if (endIdx === -1) return events;

  if (normalizeForCompare(assembled) !== normalizeForCompare(summary)) return events;

  // Drop the whole turn window: start...end (inclusive). Anything
  // before the start (narrations, etc.) and anything after the end
  // is kept verbatim.
  return [...events.slice(0, firstStart), ...events.slice(endIdx + 1)];
}


// Exported for unit tests — colocated with the component to keep the
// helper's pure logic close to its caller. Mirrors the ChatThread
// ``__test__`` convention.
export const __test__ = {
  formatPropertyLine,
  formatDueDate,
  formatReporter,
  normalizeForCompare,
  stripDuplicateSummaryTurn,
  // PR #89 — exported so the affordance can be unit-tested without
  // mounting the full InboxPage. It's a small pure component with
  // one network dep (``replanInboxEvent``) — perfect for direct
  // rendering in a test.
  PlannerStatusAffordance,
  // PR #92 — same idea for the summary section: text / shimmer /
  // Resummarize pill state machine isolated from the rest of the
  // inbox so we can exercise each state directly.
  SummarySection,
  // PR #93 — workflow card (decision buttons + freeform input).
  // PR #122 retired the numbered step list; the card now hosts
  // the 4 situational decision buttons sourced from the planner's
  // action_surface and the freeform input.
  SuggestedWorkflow,
  // PR #98 — pure helper that derives "WHAT HELIXIS NOTICED" bullets
  // from enrichment. Tested directly so the bullet-generation
  // rules are pinned independently of any rendering.
  buildAgentNoticed,
  computeBlockingGap,
  // PR #98 — AgentNoticed section.
  AgentNoticed,
  // PR #103 — exported so we can pin the Assignee field's
  // assignee_name → display behavior + the metadata grid's
  // dash-fallback contract without mounting the full InboxPage.
  EventHeader,
  // Email-ingest — full email render + the editable reply composer,
  // exported so their render/send behavior is testable in isolation.
  EmailBodyCard,
  EmailReplyComposer,
  isEmailRow,
  // Dynamic action cards — one executable card per planner action.
  ActionCard,
  ActionCardList,
};
