/**
 * CRM — the self-updating lead pipeline.
 *
 * Two views, one section:
 *   • Overview (landing) — monthly analytics on top, then each stage as a
 *     clickable header showing only its most-recent leads + a "View all".
 *   • Stage list — click a stage → a focused, scrollable, searchable list of
 *     every lead in that step (scales past a couple to 50+).
 * Click a lead in either view → the detail drawer (SMS thread + the AI
 * "what Occupella noticed" + a pre-filled, editable, approve-to-send reply).
 *
 * Data: demo mode → in-memory fixtures that mirror the real API shape;
 * otherwise the live backend (/crm/overview, /crm/leads). A dark backend
 * (flag off → CrmDisabledError) falls back to the demo overview — and the
 * page labels that fallback with an explicit "Example data" banner
 * (services/crm.loadCrmOverview carries the isDemo flag) so a real
 * signed-in user never mistakes sample leads for their pipeline.
 */

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

import { useFocusTrap } from '../hooks/useFocusTrap';
import { TRANSCRIPT_LABEL, transcriptCaveat } from '../lib/transcript';
import { ApiError } from '../services/api';
import {
  callLead,
  CRM_STAGES,
  createLead,
  getLeadDraft,
  loadCrmOverview,
  loadColdLeads,
  listLeadMessages,
  loadStageLeads,
  moveLeadStage,
  sendLeadReply,
  setApplicantStatus,
  setLeadStatus,
  type CrmLead,
  type CrmLeadDraft,
  type CrmLeadKind,
  type CrmMessage,
  type CrmMetrics,
  type CrmOverview,
  type CrmSource,
  type CrmStage,
} from '../services/crm';
import { RefreshButton } from './RefreshButton';
import { ErrorState } from './ErrorState';
import { Skeleton } from './Skeleton';
import { FormError } from './FormError';

// Muted lilac for the Scheduled stage — a lighter tint of the brand
// purple family (not the old neon #d9b3ff), so the funnel reads calm.
const LILAC = 'var(--iris-light)';
const STAGE_ACCENT: Record<CrmStage, string> = {
  new: 'var(--color-info)',
  contacted: 'var(--color-iris)',
  scheduled: LILAC,
  applied: 'var(--color-caution)',
  leased: 'var(--color-positive)',
};

// ── token helpers ────────────────────────────────────────────────

function sourceStyle(src: CrmSource): { label: string; color: string; bg: string } {
  switch (src) {
    case 'zillow':
      return { label: 'Zillow', color: 'var(--color-info)', bg: 'rgba(101,146,203,0.12)' };
    case 'apartments':
      return { label: 'Apts.com', color: 'var(--color-positive)', bg: 'rgba(92,189,138,0.12)' };
    case 'referral':
      return { label: 'Referral', color: 'var(--color-iris-hover)', bg: 'var(--iris-soft)' };
    case 'web_form':
      return { label: 'Web form', color: LILAC, bg: 'var(--iris-soft)' };
    case 'sms':
      return { label: 'SMS', color: 'var(--color-positive)', bg: 'rgba(92,189,138,0.10)' };
    case 'buildium':
      return { label: 'Buildium', color: 'var(--color-iris)', bg: 'var(--iris-soft)' };
    case 'email':
    default:
      return { label: 'Email', color: 'var(--color-ink-muted)', bg: 'rgba(14,22,32,0.05)' };
  }
}

function Avatar({ lead, size = 28 }: { lead: CrmLead; size?: number }) {
  const bg = lead?.kind === 'owner' ? 'var(--positive)' : 'var(--iris)';
  return (
    <span
      className="grid place-items-center rounded-full font-semibold shrink-0"
      style={{ width: size, height: size, fontSize: Math.max(12, size * 0.4), background: bg, color: 'var(--canvas)' }}
    >
      {lead?.initials ?? '?'}
    </span>
  );
}

function SourceChip({ src }: { src: CrmSource }) {
  const s = sourceStyle(src);
  return (
    <span
      className=" uppercase rounded-[var(--r-xs)] px-1.5 py-0.5"
      style={{ fontSize: 12, letterSpacing: '0.05em', color: s.color, background: s.bg }}
    >
      {s.label}
    </span>
  );
}

function leadTitle(lead: CrmLead): string {
  return lead.name || lead.phone || 'New lead';
}

// ── analytics band ───────────────────────────────────────────────

function Tile({
  label,
  value,
  delta,
  tone,
}: {
  label: string;
  value: string;
  delta?: string;
  tone?: 'up' | 'down' | 'flat';
}) {
  const dColor =
    tone === 'up'
      ? 'var(--color-positive)'
      : tone === 'down'
        ? 'var(--color-danger)'
        : 'var(--color-ink-faint)';
  return (
    <div
      className="rounded-[var(--r-md)] px-3.5 py-3"
      style={{ background: 'var(--color-canvas)', border: '1px solid var(--color-line)' }}
    >
      <div
        className="uppercase"
        style={{ fontSize: 12, letterSpacing: '0.04em', color: 'var(--color-ink-subtle)' }}
      >
        {label}
      </div>
      <div
        className="font-semibold"
        style={{ fontSize: 20, letterSpacing: '-0.02em', marginTop: 3, fontVariantNumeric: 'tabular-nums' }}
      >
        {value}
      </div>
      {delta && (
        <div className="tabular-nums" style={{ fontSize: 12, marginTop: 2, color: dColor }}>
          {delta}
        </div>
      )}
    </div>
  );
}

function MetricsBand({ metrics, stages }: { metrics: CrmMetrics; stages: CrmOverview['stages'] }) {
  const conv = metrics.total > 0 ? Math.round((metrics.leased / metrics.total) * 100) : 0;
  const deltaPct =
    metrics.new_prev_month > 0
      ? Math.round(((metrics.new_this_month - metrics.new_prev_month) / metrics.new_prev_month) * 100)
      : null;
  const maxCount = Math.max(1, ...stages.map((s) => s.count));

  return (
    <div
      className="rounded-[var(--r-lg)] p-4 mb-4"
      style={{ border: '0.5px solid var(--card-edge)', background: 'var(--canvas)' }}
    >
      <div
        className=" uppercase mb-3"
        style={{ fontSize: 12, letterSpacing: '0.12em', color: 'var(--color-ink-subtle)' }}
      >
        This month
      </div>
      <div className="grid gap-2.5" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))' }}>
        <Tile
          label="New leads"
          value={String(metrics.new_this_month)}
          delta={deltaPct === null ? 'new' : `${deltaPct >= 0 ? '▲' : '▼'} ${Math.abs(deltaPct)}% vs last mo`}
          tone={deltaPct === null ? 'flat' : deltaPct >= 0 ? 'up' : 'down'}
        />
        <Tile label="Replies sent" value={String(metrics.replies_this_month)} />
        <Tile label="In pipeline" value={String(metrics.active)} />
        <Tile label="Leased" value={String(metrics.leased)} />
        <Tile label="Total leads" value={String(metrics.total)} />
        <Tile label="New → leased" value={`${conv}%`} />
      </div>

      {/* funnel — current count per stage */}
      <div className="flex items-end gap-1.5 mt-5" style={{ height: 56 }}>
        {stages.map((s) => (
          <div
            key={s.key}
            className="flex-1 rounded-t-[6px] relative"
            style={{
              height: `${Math.max(8, (s.count / maxCount) * 100)}%`,
              background: STAGE_ACCENT[s.key],
            }}
          >
            <span
              className="absolute left-0 right-0 text-center tabular-nums"
              style={{ top: -15, fontSize: 12, color: 'var(--color-ink-subtle)' }}
            >
              {s.count}
            </span>
            <span
              className="absolute left-0 right-0 text-center uppercase"
              style={{ bottom: -15, fontSize: 12, letterSpacing: '0.04em', color: 'var(--color-ink-faint)' }}
            >
              {s.label}
            </span>
          </div>
        ))}
      </div>
      <div style={{ height: 14 }} />
    </div>
  );
}

// ── overview: stage columns (recent + view all) ──────────────────

/**
 * A lead card on the pipeline board.
 *
 * ⚠ This used to render three fields — avatar, unit, source — while `CrmLead`
 * carried nine. The board could not tell you what any lead WANTED (`note`) or
 * which ones were waiting on you (`needsYou`), which is the product's whole
 * promise, and the data was already on the object every time. The board read
 * as a mockup for that reason alone, not because anything was missing behind
 * it: the fixtures, the API and `crm_lead_messages` all carried this already.
 *
 * ⚠ `needsYou` leads. It is the only field that answers "what do I do next",
 * so it goes first and is the one thing on the card allowed an accent colour.
 * Everything else is muted per DESIGN.md — a card where four things compete
 * for attention directs it nowhere.
 */
function MiniLead({ lead, onOpen }: { lead: CrmLead; onOpen: (l: CrmLead) => void }) {
  return (
    <button
      type="button"
      data-testid="crm-lead-card"
      onClick={() => onOpen(lead)}
      className="flex flex-col gap-1 rounded-[8px] px-2.5 py-2 text-left w-full"
      style={{ background: 'var(--canvas)', border: '0.5px solid var(--card-edge)' }}
    >
      <div className="flex items-center gap-2">
        <Avatar lead={lead} size={22} />
        <span className="font-semibold truncate" style={{ fontSize: 12 }}>
          {leadTitle(lead)}
        </span>
      </div>
      {lead.unit && (
        <span className="truncate" style={{ fontSize: 12, color: 'var(--color-ink-muted)' }}>
          {lead.unit}
        </span>
      )}
      {lead.note && (
        // Two lines, not one. A lead's ask is the reason to open the card and a
        // single truncated line cuts most of them mid-sentence.
        <span
          data-testid="crm-lead-note"
          style={{
            fontSize: 12,
            lineHeight: 1.35,
            color: 'var(--color-ink-subtle)',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {lead.note}
        </span>
      )}
      <div className="flex items-center gap-1.5 flex-wrap">
        {lead.needsYou && (
          <span
            data-testid="crm-lead-needs-you"
            className=" uppercase rounded-[var(--r-xs)] px-1.5 py-0.5"
            style={{
              fontSize: 12,
              letterSpacing: '0.05em',
              color: 'var(--color-accent)',
              background: 'rgba(var(--accent-rgb), 0.10)',
            }}
          >
            Needs you
          </span>
        )}
        <SourceChip src={lead.source} />
        {lead.calendarLabel && (
          <span
            data-testid="crm-lead-tour"
            className="tabular-nums rounded-[var(--r-xs)] px-1.5 py-0.5 "
            style={{
              fontSize: 12,
              color: STAGE_ACCENT.scheduled,
              background: 'var(--surface-sunken, rgba(14,22,32,0.05))',
            }}
          >
            {lead.calendarLabel}
          </span>
        )}
        {lead.buildiumApplicantId != null && (
          <span
            data-testid="crm-lead-buildium"
            className=" uppercase rounded-[var(--r-xs)] px-1.5 py-0.5"
            style={{
              fontSize: 12,
              letterSpacing: '0.05em',
              color: 'var(--color-ink-subtle)',
              background: 'var(--surface-sunken, rgba(14,22,32,0.05))',
            }}
          >
            In Buildium
          </span>
        )}
        {lead.movedAt && (
          // Occupella moved this card by itself. Saying so is the difference
          // between a board that reorganised overnight and one the user has to
          // assume they misremembered.
          <span
            data-testid="crm-lead-moved"
            className="tabular-nums"
            style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}
          >
            moved {lead.movedAt}
          </span>
        )}
      </div>
    </button>
  );
}

function OverviewView({
  reloadKey,
  onOpenStage,
  onOpenLead,
  onOpenCold,
  onSourceChange,
  onLoadingChange,
}: {
  reloadKey: number;
  onOpenStage: (stage: CrmStage, label: string) => void;
  onOpenLead: (l: CrmLead) => void;
  onOpenCold: () => void;
  /** Reports whether the loaded payload is the fixture preview, so the
   *  page can render the example-data banner (never inferred here). */
  onSourceChange: (isDemo: boolean) => void;
  /** ⚠ Reports the fetch, so the header's Refresh button can spin on a REAL
   *  signal. This view keeps its old rows on screen while refetching, so
   *  without this a refresh that returns identical data is indistinguishable
   *  from a click that missed — and a fixed-duration fake spin would be a
   *  claim about a request nobody measured. */
  onLoadingChange: (loading: boolean) => void;
}) {
  const [overview, setOverview] = useState<CrmOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    onLoadingChange(true);
    // loadCrmOverview makes the fixture-vs-live distinction explicit:
    // demo mode and a dark backend (CrmDisabledError) return the preview
    // with isDemo=true; the page labels it with the example-data banner.
    loadCrmOverview()
      .then((r) => {
        if (!cancelled) {
          setOverview(r.overview);
          onSourceChange(r.isDemo);
        }
      })
      .catch((err) => {
        // A real signed-in user hitting a genuine fetch failure must NOT be
        // shown fabricated demo leads — that would mislead a design partner
        // into thinking fake pipeline is real. Surface an error instead; the
        // demo fallback stays only for demo mode / CrmDisabledError (inside
        // loadCrmOverview).
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load your pipeline.');
        }
      })
      .finally(() => {
        if (!cancelled) onLoadingChange(false);
      });
    return () => {
      cancelled = true;
      // A cancelled fetch still has to release the button, or navigating
      // away mid-refresh leaves it disabled and spinning forever.
      onLoadingChange(false);
    };
    // onSourceChange is a stable setState-backed callback from CrmPage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey, retryTick]);

  if (error !== null && overview === null) {
    return (
      <ErrorState
        title="Couldn't load your pipeline"
        body={error}
        onRetry={() => setRetryTick((t) => t + 1)}
        data-testid="crm-error"
      />
    );
  }

  if (overview === null) {
    return (
      <div className="flex flex-col gap-3">
        <Skeleton variant="rect" width="100%" height={120} />
        <Skeleton variant="rect" width="100%" height={260} />
      </div>
    );
  }

  return (
    <>
      <MetricsBand metrics={overview.metrics} stages={overview.stages} />
      {overview.cold_count > 0 && (
        <button
          type="button"
          onClick={onOpenCold}
          className="w-full flex items-center gap-3 rounded-[var(--r-md)] px-4 py-3 mb-4 text-left"
          style={{ background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.28)' }}
        >
          <span style={{ fontSize: 16 }}>◷</span>
          <span style={{ fontSize: 13, color: 'var(--color-ink)' }}>
            <b style={{ fontWeight: 600 }}>
              {overview.cold_count} {overview.cold_count === 1 ? 'lead is' : 'leads are'} going cold
            </b>
            <span style={{ color: 'var(--color-ink-muted)' }}> — quiet 3+ days. Send a follow-up before they slip.</span>
          </span>
          <span className="ml-auto" style={{ fontSize: 13, color: 'var(--color-caution)' }}>
            Review →
          </span>
        </button>
      )}
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(5, minmax(150px, 1fr))', overflowX: 'auto' }}>
        {overview.stages.map((s) => (
          <div
            key={s.key}
            className="flex flex-col min-w-0 rounded-[var(--r-md)]"
            style={{ background: 'var(--color-canvas)', border: '1px solid var(--color-line)' }}
          >
            <button
              type="button"
              onClick={() => onOpenStage(s.key, s.label)}
              className="flex items-center gap-2 px-3 py-2.5 w-full text-left rounded-t-[var(--r-md)]"
              style={{ borderBottom: '1px solid var(--color-line)', background: 'transparent' }}
            >
              <span className="rounded-[2px]" style={{ width: 3, height: 24, background: STAGE_ACCENT[s.key] }} />
              <span className="font-semibold" style={{ fontSize: 13 }}>
                {s.label}
              </span>
              <span className="tabular-nums" style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}>
                {s.count}
              </span>
              <span className="ml-auto" style={{ color: 'var(--color-ink-subtle)', fontSize: 14 }}>
                →
              </span>
            </button>
            <div className="flex flex-col gap-1.5 p-2">
              {s.recent.length > 0 ? (
                s.recent.map((lead) => <MiniLead key={lead.id} lead={lead} onOpen={onOpenLead} />)
              ) : (
                <div
                  className="text-center italic px-1 py-3"
                  style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}
                >
                  Nothing here yet
                </div>
              )}
            </div>
            {s.count > s.recent.length && (
              <button
                type="button"
                onClick={() => onOpenStage(s.key, s.label)}
                className="mx-2 mb-2.5 rounded-[8px] py-2 text-center"
                style={{
                  fontSize: 12,
                  color: 'var(--color-iris-hover)',
                  background: 'var(--iris-tint)',
                  border: '1px dashed var(--iris-ring)',
                }}
              >
                View all {s.count} →
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

// ── stage list (drill-in) ────────────────────────────────────────

// ── Scheduled week agenda (leads grouped by tour day, in time order) ──

function dayKey(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayHeading(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

function timeLabel(iso: string): string {
  const d = new Date(iso);
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, '0');
  const ap = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return `${h}:${m} ${ap}`;
}

/** The Scheduled stage as an agenda: leads with a booked tour grouped by day
 *  (soonest first), each day's tours in time order; any Scheduled lead without
 *  a tour datetime falls into a trailing "No time set" group. */
function ScheduledAgenda({
  leads,
  onOpenLead,
}: {
  leads: CrmLead[];
  onOpenLead: (l: CrmLead) => void;
}) {
  const withTour = leads
    .filter((l) => l.tourAt)
    .sort((a, b) => new Date(a.tourAt as string).getTime() - new Date(b.tourAt as string).getTime());
  const withoutTour = leads.filter((l) => !l.tourAt);

  const groups: { key: string; heading: string; leads: CrmLead[] }[] = [];
  for (const lead of withTour) {
    const key = dayKey(lead.tourAt as string);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.leads.push(lead);
    else groups.push({ key, heading: dayHeading(lead.tourAt as string), leads: [lead] });
  }
  if (withoutTour.length) {
    groups.push({ key: 'none', heading: 'No time set', leads: withoutTour });
  }

  return (
    <div className="flex flex-col gap-4">
      {groups.map((g) => (
        <div key={g.key}>
          <div
            className="font-semibold uppercase tracking-wide mb-1.5"
            style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}
          >
            {g.heading}
          </div>
          <div
            className="rounded-[var(--r-lg)] overflow-hidden"
            style={{
              border: '0.5px solid var(--card-edge)',
              background: 'var(--canvas)',
            }}
          >
            {g.leads.map((lead) => (
              <button
                key={lead.id}
                type="button"
                data-testid="crm-lead-card"
                onClick={() => onOpenLead(lead)}
                className="flex items-center gap-3 w-full text-left px-4 py-3"
                style={{ borderBottom: '1px solid var(--color-line)' }}
              >
                <div
                  className="shrink-0 tabular-nums text-right"
                  style={{ fontSize: 12, color: STAGE_ACCENT.scheduled, width: 72 }}
                >
                  {lead.tourAt ? timeLabel(lead.tourAt) : '—'}
                </div>
                <Avatar lead={lead} size={30} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold truncate" style={{ fontSize: 14 }}>
                      {leadTitle(lead)}
                    </span>
                    <SourceChip src={lead.source} />
                  </div>
                  <div
                    className="truncate"
                    style={{ fontSize: 12, color: 'var(--color-ink-muted)', marginTop: 2 }}
                  >
                    {lead.unit || lead.note || 'Tour'}
                  </div>
                </div>
                <span style={{ color: 'var(--color-ink-faint)', fontSize: 16 }}>›</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function StageListView({
  stage,
  label,
  reloadKey,
  onBack,
  onOpenLead,
  onSourceChange,
}: {
  stage: CrmStage;
  label: string;
  reloadKey: number;
  onBack: () => void;
  onOpenLead: (l: CrmLead) => void;
  onSourceChange: (isDemo: boolean) => void;
}) {
  const [leads, setLeads] = useState<CrmLead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [q, setQ] = useState('');

  useEffect(() => {
    let cancelled = false;
    const handle = setTimeout(() => {
      loadStageLeads({ stage, q: q || undefined, limit: 100 })
        .then((r) => {
          if (cancelled) return;
          setLeads(r.leads);
          setError(null);
          onSourceChange(r.isDemo);
        })
        .catch((err) => {
          // ⚠ NEVER `setLeads([])` here. An empty list is an ANSWER — "this
          // stage has no leads" — and a failed fetch has not earned it. That
          // read as a clean screen for a 500, a timeout or a dropped
          // connection, which is this product's worst failure mode
          // (gotcha 26). The overview above this view has always used
          // ErrorState + retry; the drill-downs were never told.
          if (cancelled) return;
          setError(err instanceof Error ? err.message : 'Could not load leads.');
        });
    }, q ? 220 : 0); // debounce typing; instant on stage open
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
    // onSourceChange is a stable setState-backed callback from CrmPage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, q, reloadKey, retryTick]);

  return (
    <>
      <div className="flex items-center gap-3 mb-3">
        <button
          type="button"
          onClick={onBack}
          className="rounded-[var(--r-sm)] px-2.5 py-1.5"
          style={{ fontSize: 13, color: 'var(--color-ink-muted)', border: '1px solid var(--color-line-strong)' }}
        >
          ← CRM
        </button>
        <span className="font-semibold" style={{ fontSize: 16, color: STAGE_ACCENT[stage] }}>
          {label}
        </span>
        <span className="tabular-nums" style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}>
          {leads?.length ?? '·'}
        </span>
      </div>

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search these leads…"
        className="w-full rounded-[var(--r-sm)] px-3 py-2 mb-3 focus:outline-none"
        style={{
          background: 'var(--color-canvas-elevated)',
          border: '1px solid var(--color-line-strong)',
          color: 'var(--color-ink)',
          fontSize: 13,
        }}
      />

      {/* ⚠ The error branch comes BEFORE the list, and stale rows are not kept
          behind it. `q` refetches, so showing the previous search's leads under
          a failed one would answer a question nobody asked. */}
      {error !== null ? (
        <ErrorState
          title="Couldn't load these leads"
          body={error}
          onRetry={() => setRetryTick((t) => t + 1)}
          data-testid="crm-stage-error"
        />
      ) : /* The Scheduled stage renders as a week agenda (grouped by tour day);
          every other stage is the flat list. */
      stage === 'scheduled' && leads && leads.length > 0 ? (
        <ScheduledAgenda leads={leads} onOpenLead={onOpenLead} />
      ) : (
        <div
          className="rounded-[var(--r-lg)] overflow-hidden"
          style={{ border: '0.5px solid var(--card-edge)', background: 'var(--canvas)' }}
        >
          {leads === null ? (
            <div className="p-4 flex flex-col gap-2">
              <Skeleton variant="rect" width="100%" height={44} />
              <Skeleton variant="rect" width="100%" height={44} />
            </div>
          ) : leads.length === 0 ? (
            <div className="text-center italic py-10" style={{ fontSize: 13, color: 'var(--color-ink-faint)' }}>
              {q ? 'No leads match that search.' : 'No leads in this stage yet.'}
            </div>
          ) : (
            leads.map((lead) => (
            <button
              key={lead.id}
              type="button"
              onClick={() => onOpenLead(lead)}
              className="flex items-center gap-3 w-full text-left px-4 py-3"
              style={{ borderBottom: '1px solid var(--color-line)' }}
            >
              <Avatar lead={lead} size={30} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-semibold truncate" style={{ fontSize: 14 }}>
                    {leadTitle(lead)}
                  </span>
                  <SourceChip src={lead.source} />
                </div>
                <div className="truncate" style={{ fontSize: 12, color: 'var(--color-ink-muted)', marginTop: 2 }}>
                  {lead.unit ? `${lead.unit} · ` : ''}
                  {lead.note || 'No message yet'}
                </div>
              </div>
              <span style={{ color: 'var(--color-ink-faint)', fontSize: 16 }}>›</span>
            </button>
          ))
          )}
        </div>
      )}
    </>
  );
}

// ── going-cold list ("needs a nudge") ────────────────────────────

function ColdListView({
  reloadKey,
  onBack,
  onOpenLead,
  onSourceChange,
}: {
  reloadKey: number;
  onBack: () => void;
  onOpenLead: (l: CrmLead) => void;
  onSourceChange: (isDemo: boolean) => void;
}) {
  const [leads, setLeads] = useState<CrmLead[] | null>(null);
  const [thresholdDays, setThresholdDays] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    loadColdLeads()
      .then((r) => {
        if (cancelled) return;
        setLeads(r.leads);
        // ⚠ The server's own window, not a number typed twice. Both strings
        // below said "3 days" while `threshold_days` sat unread on the
        // response — so a backend change to the window would have left two
        // sentences quietly lying about it.
        setThresholdDays(r.threshold_days);
        setError(null);
        onSourceChange(r.isDemo);
      })
      .catch((err) => {
        // ⚠ See StageListView: an empty list is an answer, and a failed fetch
        // has not earned one. This one was worse — the empty state below
        // CONGRATULATED the user ("every lead's had recent activity 🎉") for a
        // request that never completed.
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Could not load going-cold leads.');
      });
    return () => {
      cancelled = true;
    };
    // onSourceChange is a stable setState-backed callback from CrmPage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey, retryTick]);

  return (
    <>
      <div className="flex items-center gap-3 mb-3">
        <button
          type="button"
          onClick={onBack}
          className="rounded-[var(--r-sm)] px-2.5 py-1.5"
          style={{ fontSize: 13, color: 'var(--color-ink-muted)', border: '1px solid var(--color-line-strong)' }}
        >
          ← CRM
        </button>
        <span className="font-semibold" style={{ fontSize: 16, color: 'var(--color-caution)' }}>
          ◷ Going cold
        </span>
        <span className="tabular-nums" style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}>
          {leads?.length ?? '·'}
        </span>
      </div>
      <div className="mb-3" style={{ fontSize: 13, color: 'var(--color-ink-muted)' }}>
        In play (not leased or lost) but quiet {thresholdDays ?? 3}+ days — open one to send a
        follow-up.
      </div>

      {error !== null ? (
        <ErrorState
          title="Couldn't load going-cold leads"
          body={error}
          onRetry={() => setRetryTick((t) => t + 1)}
          data-testid="crm-cold-error"
        />
      ) : (
      <div
        className="rounded-[var(--r-lg)] overflow-hidden"
        style={{ border: '0.5px solid var(--card-edge)', background: 'var(--canvas)' }}
      >
        {leads === null ? (
          <div className="p-4 flex flex-col gap-2">
            <Skeleton variant="rect" width="100%" height={44} />
            <Skeleton variant="rect" width="100%" height={44} />
          </div>
        ) : leads.length === 0 ? (
          <div className="text-center italic py-10" style={{ fontSize: 13, color: 'var(--color-ink-faint)' }}>
            {/* ⚠ This used to read "every lead's had recent activity 🎉" — an
                assertion about the WHOLE pipeline made by a query that only
                read the quiet end of it. With zero leads on file it
                congratulated a brand-new customer for an achievement they had
                not made; on a failed fetch (now impossible — see the error
                branch) it congratulated them for a request that never ran.
                This sentence claims only what the response actually says. */}
            No leads have gone quiet for {thresholdDays ?? 3}+ days.
          </div>
        ) : (
          leads.map((lead) => (
            <button
              key={lead.id}
              type="button"
              onClick={() => onOpenLead(lead)}
              className="flex items-center gap-3 w-full text-left px-4 py-3"
              style={{ borderBottom: '1px solid var(--color-line)' }}
            >
              <Avatar lead={lead} size={30} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-semibold truncate" style={{ fontSize: 14 }}>
                    {leadTitle(lead)}
                  </span>
                  <SourceChip src={lead.source} />
                  <span
                    className=" uppercase rounded-[var(--r-xs)] px-1.5 py-0.5"
                    style={{ fontSize: 12, letterSpacing: '0.05em', color: 'var(--color-caution)', background: 'rgba(251,191,36,0.12)' }}
                  >
                    {CRM_STAGES.find((s) => s.key === lead.stage)?.label ?? lead.stage}
                  </span>
                </div>
                <div className="truncate" style={{ fontSize: 12, color: 'var(--color-ink-muted)', marginTop: 2 }}>
                  {lead.unit ? `${lead.unit} · ` : ''}
                  {lead.note || 'No message yet'}
                </div>
              </div>
              <span style={{ color: 'var(--color-ink-faint)', fontSize: 16 }}>›</span>
            </button>
          ))
        )}
      </div>
      )}
    </>
  );
}

// ── lead detail drawer (conversation + AI assist + reply) ────────

function LeadDrawer({
  lead,
  onClose,
  onMutated,
  cold = false,
}: {
  lead: CrmLead;
  onClose: () => void;
  onMutated: () => void;
  cold?: boolean;
}) {
  const [messages, setMessages] = useState<CrmMessage[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [assist, setAssist] = useState<CrmLeadDraft | null>(null);
  // Local stage/status so a manual move / mark-lost reflects instantly in the
  // drawer without waiting on a board refetch (which also fires via onMutated).
  const [curStage, setCurStage] = useState<CrmStage>(lead.stage);
  const [curStatus, setCurStatus] = useState(lead.status);
  const [moving, setMoving] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const stage = CRM_STAGES.find((s) => s.key === curStage);

  // Escape closes the drawer (parity with the backdrop click). Bound only
  // while the drawer is mounted — it renders solely when a lead is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Escape alone is half of it: without the trap, Tab walks straight out of a
  // drawer that visually covers the page and into the board behind it, which
  // is still focusable and completely invisible. See useFocusTrap's header.
  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  const move = async (next: CrmStage) => {
    if (next === curStage || moving || !lead.id) return;
    const prev = curStage;
    setCurStage(next); // optimistic
    setMoving(true);
    setControlError(null);
    try {
      await moveLeadStage(lead.id, next);
      onMutated();
    } catch (err) {
      setCurStage(prev); // revert on failure
      setControlError(err instanceof ApiError || err instanceof Error ? err.message : 'Could not move.');
    } finally {
      setMoving(false);
    }
  };

  const toggleLost = async () => {
    if (moving || !lead.id) return;
    const next = curStatus === 'lost' ? 'active' : 'lost';
    setMoving(true);
    setControlError(null);
    try {
      await setLeadStatus(lead.id, next);
      setCurStatus(next);
      onMutated();
      if (next === 'lost') onClose(); // a lost lead drops off the board
    } catch (err) {
      setControlError(err instanceof ApiError || err instanceof Error ? err.message : 'Could not update.');
    } finally {
      setMoving(false);
    }
  };

  const [applicantBusy, setApplicantBusy] = useState(false);
  // Push a decision back to Buildium (the reverse of the applicant
  // auto-move). The webhook the write triggers updates the mirror and,
  // for Approved, the auto-move advances the lead's stage on refresh.
  const pushApplicantStatus = async (status: 'Approved' | 'Rejected') => {
    if (!lead.id || applicantBusy) return;
    setApplicantBusy(true);
    setControlError(null);
    try {
      await setApplicantStatus(lead.id, status);
      setCallMsg(`Application set to ${status} in Buildium.`);
    } catch (err) {
      setControlError(
        err instanceof ApiError || err instanceof Error ? err.message : 'Could not update.',
      );
    } finally {
      setApplicantBusy(false);
    }
  };

  const [calling, setCalling] = useState(false);
  const [callMsg, setCallMsg] = useState<string | null>(null);
  const placeCall = async () => {
    if (!lead.id || calling) return;
    setCalling(true);
    setCallMsg(null);
    try {
      await callLead(lead.id);
      setCallMsg('Calling your phone now — pick up to connect.');
    } catch (err) {
      setCallMsg(err instanceof ApiError || err instanceof Error ? err.message : 'Could not call.');
    } finally {
      setCalling(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    if (!lead.id) {
      setMessages([]);
      return;
    }
    listLeadMessages(lead.id)
      .then((m) => {
        if (!cancelled) setMessages(m);
      })
      .catch((err) => {
        if (!cancelled) {
          setMessages([]);
          setLoadError(
            err instanceof ApiError || err instanceof Error ? err.message : 'Could not load messages.',
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [lead.id]);

  // AI assist: "what Occupella noticed" + a drafted reply. Pre-fills the
  // composer only if it's still empty (never clobbers what the PM typed).
  useEffect(() => {
    let cancelled = false;
    if (!lead.id) return;
    getLeadDraft(lead.id)
      .then((d) => {
        if (cancelled) return;
        setAssist(d);
        if (d.draft_reply) setDraft((cur) => cur || d.draft_reply || '');
      })
      .catch(() => {
        if (!cancelled) setAssist({ signals: [], draft_reply: null });
      });
    return () => {
      cancelled = true;
    };
  }, [lead.id]);

  // A lead arrives by one channel and is answered on it: text if it has a
  // phone, else email. `canReply` gates the composer on having either.
  const replyChannel: 'sms' | 'email' = lead.phone ? 'sms' : 'email';
  const canReply = Boolean(lead.phone || lead.email);
  const draftIsAI = Boolean(assist?.draft_reply) && draft.trim() === assist?.draft_reply?.trim();

  const send = async () => {
    const body = draft.trim();
    if (!body || !lead.id || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await sendLeadReply(lead.id, body, replyChannel);
      setMessages((prev) => [
        ...(prev ?? []),
        { id: `local-${prev?.length ?? 0}`, direction: 'outbound', channel: replyChannel, body },
      ]);
      setDraft('');
    } catch (err) {
      setSendError(err instanceof ApiError || err instanceof Error ? err.message : 'Could not send.');
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end"
      style={{ background: 'rgba(0,0,0,0.55)' }}
      onClick={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={`Lead — ${leadTitle(lead)}`}
        className="h-full flex flex-col"
        style={{
          width: 'min(420px, 100%)',
          background: 'var(--canvas)',
          borderLeft: '1px solid var(--card-edge)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center gap-3 px-5 h-14 shrink-0" style={{ borderBottom: '1px solid var(--color-line)' }}>
          <Avatar lead={lead} size={30} />
          <div className="min-w-0 flex-1">
            <div className="font-semibold truncate" style={{ fontSize: 14, color: 'var(--color-ink)' }}>
              {leadTitle(lead)}
            </div>
            <div style={{ fontSize: 12, color: 'var(--color-ink-subtle)' }}>
              {lead.unit ?? (lead.kind === 'owner' ? 'Owner lead' : 'Renter')}
              {stage ? ` · ${stage.label}` : ''}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid place-items-center w-8 h-8 rounded-[var(--r-sm)]"
            style={{ color: 'var(--color-ink-muted)', border: '1px solid var(--color-line-strong)' }}
          >
            ✕
          </button>
        </div>

        {/* contact chips — tap the number to dial from your device, or the
            Call button to bridge through your business line (logged). */}
        <div className="flex items-center gap-2 px-5 py-3 flex-wrap shrink-0" style={{ borderBottom: '1px solid var(--color-line)' }}>
          <SourceChip src={lead.source} />
          {lead.buildiumApplicantId != null && (
            <span
              className="inline-flex items-center rounded-[var(--r-sm)] px-2 py-1"
              style={{
                fontSize: 12,
                color: 'var(--color-ink-muted)',
                border: '1px solid var(--color-line-strong)',
                fontVariantNumeric: 'tabular-nums',
              }}
              title={`Matched to Buildium applicant #${lead.buildiumApplicantId} — stage follows their application`}
            >
              In Buildium · #{lead.buildiumApplicantId}
            </span>
          )}
          {lead.buildiumApplicantId != null && (
            <>
              <button
                type="button"
                disabled={applicantBusy}
                onClick={() => void pushApplicantStatus('Approved')}
                className="rounded-[var(--r-sm)] px-2 py-1 disabled:opacity-50"
                style={{ fontSize: 12, color: 'var(--color-ink)', border: '1px solid var(--color-line-strong)' }}
                title="Set this application to Approved in Buildium"
              >
                {applicantBusy ? 'Working…' : 'Approve in Buildium'}
              </button>
              <button
                type="button"
                disabled={applicantBusy}
                onClick={() => void pushApplicantStatus('Rejected')}
                className="rounded-[var(--r-sm)] px-2 py-1 disabled:opacity-50"
                style={{ fontSize: 12, color: 'var(--color-ink-muted)', border: '1px solid var(--color-line-strong)' }}
                title="Set this application to Rejected in Buildium"
              >
                Reject
              </button>
            </>
          )}
          {lead.phone ? (
            <>
              <a
                href={`tel:${lead.phone}`}
                className="font-mono"
                style={{ fontSize: 12, color: 'var(--color-iris-hover)', textDecoration: 'underline', textUnderlineOffset: 2 }}
              >
                {lead.phone}
              </a>
              <button
                type="button"
                onClick={placeCall}
                disabled={calling}
                className="inline-flex items-center gap-1 rounded-[var(--r-sm)] px-2 py-1"
                style={{
                  fontSize: 12,
                  color: 'var(--color-iris-hover)',
                  background: 'var(--iris-tint)',
                  border: '1px solid var(--iris-ring)',
                  opacity: calling ? 0.6 : 1,
                }}
              >
                📞 {calling ? 'Calling…' : 'Call'}
              </button>
            </>
          ) : (
            <span style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}>no phone on file</span>
          )}
          {callMsg && (
            <span style={{ fontSize: 12, color: 'var(--color-ink-muted)', width: '100%' }}>{callMsg}</span>
          )}
        </div>

        {/* manual controls: stage picker + mark lost. The board self-updates on
            verified facts; this is the by-hand override. */}
        <div className="px-5 py-3 shrink-0" style={{ borderBottom: '1px solid var(--color-line)' }}>
          <div className="flex items-center justify-between mb-2">
            <span
              className=" uppercase"
              style={{ fontSize: 12, letterSpacing: '0.1em', color: 'var(--color-ink-subtle)' }}
            >
              Stage
            </span>
            <button
              type="button"
              onClick={toggleLost}
              disabled={moving}
              style={{
                fontSize: 12,
                color: curStatus === 'lost' ? 'var(--color-iris-hover)' : 'var(--color-ink-subtle)',
                textDecoration: 'underline',
                textUnderlineOffset: 2,
                opacity: moving ? 0.5 : 1,
              }}
            >
              {curStatus === 'lost' ? '↩ Restore lead' : 'Mark as lost'}
            </button>
          </div>
          {curStatus === 'lost' && (
            <div
              className="mb-2 rounded-[var(--r-sm)] px-2.5 py-1.5"
              style={{ fontSize: 12, color: 'var(--color-caution)', background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.22)' }}
            >
              Lost — hidden from the board, kept for records.
            </div>
          )}
          <div className="flex flex-wrap gap-1.5">
            {CRM_STAGES.map((s) => {
              const on = s.key === curStage;
              return (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => move(s.key)}
                  disabled={moving}
                  className="rounded-[var(--r-sm)] px-2.5 py-1"
                  style={{
                    fontSize: 12,
                    fontWeight: on ? 600 : 400,
                    color: on ? '#fff' : 'var(--color-ink-muted)',
                    background: on ? STAGE_ACCENT[s.key] : 'var(--color-canvas-elevated)',
                    border: `1px solid ${on ? STAGE_ACCENT[s.key] : 'var(--color-line-strong)'}`,
                    opacity: moving && !on ? 0.6 : 1,
                    cursor: moving ? 'default' : 'pointer',
                  }}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          {controlError && <FormError className="mt-2">{controlError}</FormError>}
        </div>

        {/* going-cold nudge banner */}
        {cold && curStatus === 'active' && (
          <div
            className="mx-5 mt-3 rounded-[var(--r-md)] px-3.5 py-2.5 shrink-0 flex items-center gap-2"
            style={{ background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.28)' }}
          >
            <span style={{ fontSize: 13 }}>◷</span>
            <span style={{ fontSize: 12, color: 'var(--color-ink-muted)' }}>
              <b style={{ color: 'var(--color-caution)', fontWeight: 600 }}>Going cold</b> — quiet 3+ days. A quick follow-up keeps it warm.
            </span>
          </div>
        )}

        {/* What Occupella noticed */}
        {assist && assist.signals.length > 0 && (
          <div
            className="mx-5 mt-3 rounded-[var(--r-md)] px-3.5 py-3 shrink-0"
            style={{ background: 'var(--iris-tint)', border: '1px solid var(--iris-ring)' }}
          >
            <div
              className=" uppercase mb-2 flex items-center gap-1.5"
              style={{ fontSize: 12, letterSpacing: '0.1em', color: 'var(--color-iris-hover)' }}
            >
              What Occupella noticed
            </div>
            <div className="flex flex-col gap-1.5">
              {assist.signals.map((s, i) => (
                <div key={i} className="flex items-start gap-2" style={{ fontSize: 13, color: 'var(--color-ink)', lineHeight: 1.4 }}>
                  <span className="mt-1.5 shrink-0 rounded-full" style={{ width: 5, height: 5, background: 'var(--color-iris-hover)' }} />
                  {s}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* thread */}
        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-2.5">
          {messages === null ? (
            <Skeleton variant="rect" width="100%" height={64} />
          ) : messages.length === 0 ? (
            <div className="text-center italic mt-6" style={{ fontSize: 13, color: 'var(--color-ink-faint)' }}>
              {loadError ?? 'No messages yet — send the first reply below.'}
            </div>
          ) : (
            messages.map((m) =>
              m.channel === 'call' || m.channel === 'voicemail' ? (
                // Calls + voicemails read as centered system events, not chat
                // bubbles — they're things that happened, not messages typed.
                // A transcribed voicemail hangs its text under the pill.
                <div key={m.id} className="self-center flex flex-col items-center gap-1.5 max-w-[85%]">
                  <div
                    className="flex items-center gap-2 rounded-full px-3 py-1"
                    style={{
                      background: 'var(--caution-bg)',
                      border: '1px solid rgba(215,162,74,0.28)',
                      color: 'var(--color-ink-muted)',
                      fontSize: 12,
                    }}
                  >
                    <span>{m.body}</span>
                    {m.channel === 'voicemail' && m.recording_url && (
                      <a
                        href={m.recording_url}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: 'var(--color-iris-hover)', textDecoration: 'underline' }}
                      >
                        Listen
                      </a>
                    )}
                  </div>
                  {/* ⚠ NO QUOTATION MARKS. This used to render
                      `“{m.transcription}”`, and quotes are a claim that these
                      are the caller's words. Automatic speech recognition
                      makes no such claim and gets exactly the tokens a manager
                      acts on wrong — a rent amount, a unit number. Twilio
                      sends no confidence value to gate on, so the controls are
                      to stop presenting a guess as a quotation and to name the
                      figures worth checking against the recording, which is
                      one click away above. */}
                  {m.channel === 'voicemail' && m.transcription && (
                    <div
                      className="rounded-[10px] px-3 py-2"
                      data-testid="voicemail-transcription"
                      style={{
                        background: 'var(--color-canvas-elevated)',
                        border: '1px solid var(--color-line-strong)',
                        color: 'var(--color-ink-muted)',
                        fontSize: 13,
                        lineHeight: 1.5,
                      }}
                    >
                      <div
                        className=" uppercase"
                        style={{
                          fontSize: 12,
                          letterSpacing: '0.12em',
                          color: 'var(--color-ink-subtle)',
                          marginBottom: 4,
                        }}
                      >
                        {TRANSCRIPT_LABEL}
                      </div>
                      <div style={{ fontStyle: 'italic' }}>{m.transcription}</div>
                      {transcriptCaveat(m.transcription) && (
                        <div
                          data-testid="voicemail-figure-caveat"
                          style={{ marginTop: 6, fontSize: 12, color: 'var(--color-caution)' }}
                        >
                          {transcriptCaveat(m.transcription)}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <div
                  key={m.id}
                  className="max-w-[80%] rounded-[12px] px-3 py-2"
                  style={{
                    alignSelf: m.direction === 'outbound' ? 'flex-end' : 'flex-start',
                    background: m.direction === 'outbound' ? 'var(--iris-soft)' : 'var(--color-canvas-elevated)',
                    border: `1px solid ${m.direction === 'outbound' ? 'var(--iris-ring)' : 'var(--color-line-strong)'}`,
                    color: 'var(--color-ink)',
                    fontSize: 13,
                    lineHeight: 1.45,
                  }}
                >
                  {m.body}
                </div>
              ),
            )
          )}
        </div>

        {/* reply composer — sends on the lead's channel (text or email) */}
        <div className="px-5 py-4 shrink-0" style={{ borderTop: '1px solid var(--color-line)' }}>
          {!canReply && (
            <div className="mb-2" style={{ fontSize: 12, color: 'var(--color-caution)' }}>
              This lead has no phone or email — add one to reply.
            </div>
          )}
          {sendError && <FormError className="mb-2">{sendError}</FormError>}
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={
              canReply
                ? replyChannel === 'email'
                  ? 'Write an email…'
                  : 'Write a reply…'
                : 'Add a phone or email to reply to this lead'
            }
            disabled={!canReply || sending}
            rows={2}
            className="w-full rounded-[var(--r-sm)] px-3 py-2 resize-none focus:outline-none"
            style={{ background: 'var(--color-canvas-elevated)', border: '1px solid var(--color-line-strong)', color: 'var(--color-ink)', fontSize: 13 }}
          />
          <div className="flex items-center justify-between mt-2">
            {/* TODO(brandon): "you approve every reply" is an approval claim,
                held until HELIXIS_NOTES__AUTONOMOUS_ENABLED is confirmed off
                in Render. */}
            <span style={{ fontSize: 12, color: draftIsAI ? 'var(--color-iris-hover)' : 'var(--color-ink-faint)' }}>
              {draftIsAI ? 'Occupella drafted this — edit before sending' : 'Occupella never auto-sends — you approve every reply.'}
            </span>
            <button
              type="button"
              onClick={send}
              disabled={!canReply || sending || !draft.trim()}
              className="rounded-[var(--r-sm)] px-3 py-1.5 font-medium"
              style={{
                fontSize: 13,
                color: 'var(--color-iris-hover)',
                background: 'var(--iris-tint)',
                border: '1px solid var(--iris-ring)',
                opacity: !canReply || sending || !draft.trim() ? 0.5 : 1,
              }}
            >
              {sending
                ? 'Sending…'
                : replyChannel === 'email'
                  ? 'Approve & email'
                  : 'Approve & send'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── add-lead modal (hand-enter a lead) ───────────────────────────

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span
        className=" uppercase"
        style={{ fontSize: 12, letterSpacing: '0.08em', color: 'var(--color-ink-subtle)' }}
      >
        {label}
      </span>
      {children}
    </label>
  );
}

const INPUT_STYLE: CSSProperties = {
  background: 'var(--color-canvas-elevated)',
  border: '1px solid var(--color-line-strong)',
  color: 'var(--color-ink)',
  fontSize: 13,
  borderRadius: 'var(--r-sm)',
  padding: '7px 11px',
};

function AddLeadModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState<CrmLeadKind>('renter');
  const [stage, setStage] = useState<CrmStage>('new');
  const [phone, setPhone] = useState('');
  const [email, setEmail] = useState('');
  const [unit, setUnit] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSave = Boolean(name.trim() || phone.trim() || email.trim()) && !saving;

  // ⚠ This modal shipped with NO keyboard dismissal at all — the backdrop click
  // was the only way out, so a keyboard user who opened "Add lead" was stuck in
  // a form they could Tab straight out of and could not close. The drawer above
  // had both halves; this one had neither.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  const submit = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      await createLead({
        name: name.trim(),
        kind,
        stage,
        phone: phone.trim() || null,
        email: email.trim() || null,
        unit: unit.trim() || null,
        note: note.trim() || null,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : 'Could not create the lead.');
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.55)' }}
      onClick={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Add a lead"
        className="w-full flex flex-col rounded-[var(--r-lg)]"
        style={{
          maxWidth: 440,
          background: 'var(--canvas)',
          border: '1px solid var(--card-edge)',
          boxShadow: 'var(--shadow-md)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-5 h-14 shrink-0" style={{ borderBottom: '1px solid var(--color-line)' }}>
          <div className="font-semibold" style={{ fontSize: 16, color: 'var(--color-ink)' }}>
            Add a lead
          </div>
          <span style={{ fontSize: 12, color: 'var(--color-ink-subtle)' }}>walk-in, phone call, other</span>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto grid place-items-center w-8 h-8 rounded-[var(--r-sm)]"
            style={{ color: 'var(--color-ink-muted)', border: '1px solid var(--color-line-strong)' }}
          >
            ✕
          </button>
        </div>

        <div className="px-5 py-4 flex flex-col gap-3">
          <Field label="Name">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Jordan Reyes" style={INPUT_STYLE} />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <div className="flex gap-1.5">
                {(['renter', 'owner'] as CrmLeadKind[]).map((k) => (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setKind(k)}
                    className="flex-1 rounded-[var(--r-sm)] py-1.5 capitalize"
                    style={{
                      fontSize: 12,
                      color: kind === k ? '#fff' : 'var(--color-ink-muted)',
                      background: kind === k ? 'var(--color-iris)' : 'var(--color-canvas-elevated)',
                      border: `1px solid ${kind === k ? 'var(--color-iris)' : 'var(--color-line-strong)'}`,
                    }}
                  >
                    {k}
                  </button>
                ))}
              </div>
            </Field>
            <Field label="Stage">
              <select value={stage} onChange={(e) => setStage(e.target.value as CrmStage)} style={INPUT_STYLE}>
                {CRM_STAGES.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Phone">
              <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 415 555 0134" style={INPUT_STYLE} />
            </Field>
            <Field label="Email">
              <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="jordan@email.com" style={INPUT_STYLE} />
            </Field>
          </div>

          <Field label="Unit / interest">
            <input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="Maple Court 4B" style={INPUT_STYLE} />
          </Field>
          <Field label="Note">
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Where they came from, what they want…"
              rows={2}
              style={{ ...INPUT_STYLE, resize: 'none' }}
            />
          </Field>

          {error && <FormError>{error}</FormError>}
        </div>

        <div className="px-5 py-4 flex items-center justify-between shrink-0" style={{ borderTop: '1px solid var(--color-line)' }}>
          <span style={{ fontSize: 12, color: 'var(--color-ink-faint)' }}>
            Needs a name, phone, or email.
          </span>
          <button
            type="button"
            onClick={submit}
            disabled={!canSave}
            className="rounded-[var(--r-sm)] px-3.5 py-2 font-medium"
            style={{
              fontSize: 13,
              color: 'var(--color-iris-hover)',
              background: 'var(--iris-tint)',
              border: '1px solid var(--iris-ring)',
              opacity: canSave ? 1 : 0.5,
            }}
          >
            {saving ? 'Adding…' : 'Add lead'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── page ─────────────────────────────────────────────────────────

type CrmView =
  | { mode: 'overview' }
  | { mode: 'stage'; stage: CrmStage; label: string }
  | { mode: 'cold' };

export function CrmPage() {
  const [view, setView] = useState<CrmView>({ mode: 'overview' });
  const [openLead, setOpenLead] = useState<CrmLead | null>(null);
  // Whether the open lead was reached from the going-cold list (drives the
  // drawer's cold banner). Reset whenever a lead is opened.
  const [openCold, setOpenCold] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  // True when the rendered pipeline is the fixture preview (demo build or
  // the backend CRM flag is dark) — drives the example-data banner below.
  const [demoData, setDemoData] = useState(false);
  // Bumped after any mutation (move / mark-lost / add) to refetch the views.
  const [reloadKey, setReloadKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const bump = () => setReloadKey((k) => k + 1);
  const openLeadFrom = (l: CrmLead, fromCold = false) => {
    setOpenCold(fromCold);
    setOpenLead(l);
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      <div className="h-12 shrink-0 border-b" style={{ borderColor: 'var(--line)' }}>
        <div className="h-full flex items-center gap-3 px-6 w-full">
          <h1
            className="m-0 font-medium"
            style={{ fontSize: 20, color: 'var(--color-ink)', fontFamily: 'var(--font-display)', letterSpacing: '-0.01em' }}
          >
            Leasing
          </h1>
          <span style={{ fontSize: 13, color: 'var(--color-ink-subtle)' }}>Lead pipeline</span>
          {/* `bump` is what every mutation already calls to refetch the
              views — a lead arriving by inbound text changes this list with
              nobody on this page having done anything. */}
          <span className="ml-auto">
            <RefreshButton onClick={bump} busy={refreshing} label="leads" testId="crm-refresh" />
          </span>
          <button
            type="button"
            onClick={() => setAddOpen(true)}
            className="rounded-[var(--r-sm)] px-3 py-1.5 font-medium"
            style={{
              fontSize: 13,
              color: 'var(--color-iris-hover)',
              background: 'var(--iris-tint)',
              border: '1px solid var(--iris-ring)',
            }}
          >
            ＋ Add lead
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="px-6 py-6 w-full" style={{ maxWidth: 1200, margin: '0 auto' }}>
          {/* Fixture-data honesty (2026-08-15 audit): when the pipeline
              below is the sample preview — not the live API — say so,
              plainly, on every CRM view. */}
          {demoData && (
            <div
              data-testid="crm-demo-banner"
              className="flex items-baseline gap-2.5 rounded-[var(--r-md)] px-4 py-2.5 mb-4"
              style={{
                background: 'var(--info-bg)',
                border: '1px solid var(--color-line-strong)',
              }}
            >
              <span
                className=" uppercase shrink-0"
                style={{ fontSize: 12, letterSpacing: '0.08em', color: 'var(--color-info)' }}
              >
                Example data
              </span>
              <span style={{ fontSize: 13, color: 'var(--color-ink-muted)' }}>
                The leasing CRM isn't enabled for your workspace yet — these leads are
                samples, not your pipeline.
              </span>
            </div>
          )}
          {view.mode === 'overview' ? (
            <OverviewView
              reloadKey={reloadKey}
              onOpenStage={(stage, label) => setView({ mode: 'stage', stage, label })}
              onOpenLead={openLeadFrom}
              onOpenCold={() => setView({ mode: 'cold' })}
              onSourceChange={setDemoData}
              onLoadingChange={setRefreshing}
            />
          ) : view.mode === 'cold' ? (
            <ColdListView
              reloadKey={reloadKey}
              onBack={() => setView({ mode: 'overview' })}
              onOpenLead={(l) => openLeadFrom(l, true)}
              onSourceChange={setDemoData}
            />
          ) : (
            <StageListView
              stage={view.stage}
              label={view.label}
              reloadKey={reloadKey}
              onBack={() => setView({ mode: 'overview' })}
              onOpenLead={openLeadFrom}
              onSourceChange={setDemoData}
            />
          )}
        </div>
      </div>

      {openLead && (
        <LeadDrawer lead={openLead} onClose={() => setOpenLead(null)} onMutated={bump} cold={openCold} />
      )}
      {addOpen && (
        <AddLeadModal
          onClose={() => setAddOpen(false)}
          onCreated={() => {
            setAddOpen(false);
            bump();
          }}
        />
      )}
    </div>
  );
}
