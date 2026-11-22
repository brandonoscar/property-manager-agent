/**
 * CRM (lead pipeline) service client.
 *
 * The CRM is a self-updating pipeline: Occupella moves each lead between
 * stages when a *verifiable fact* occurs — a reply is sent, a tour lands
 * on the connected Google Calendar, or an application / lease shows up in
 * Buildium. The user never drags a card; they only approve the
 * irreversible actions (sending a reply, booking a tour).
 *
 * STATUS: the section ships behind a "Coming soon" gate. The backend
 * (routes/crm.py + a leads table) is scaffolded but dark
 * (``HELIXIS_CRM__ENABLED`` defaults OFF), so ``listCrmBoard`` returns
 * the in-memory preview today. The types + fetch seam below are the
 * contract the backend will fill once ingestion is wired.
 */

import { apiFetch, ApiError } from './api';
import { demoMode } from './auth';
import {
  demoColdLeads,
  demoCrmBoard,
  demoCrmOverview,
  demoLeadDraft,
  demoLeadMessages,
  demoStageLeads,
} from './demoData';

/** Pipeline stages, in order. "Scheduled" (not "Toured") because Occupella
 *  moves a card here the moment a tour hits the calendar. */
export type CrmStage = 'new' | 'contacted' | 'scheduled' | 'applied' | 'leased';

/** Renters are Fair-Housing regulated (drafts only, never scored/ranked);
 *  owners are B2B sales (may be prioritized). The kind drives what Occupella
 *  is *allowed* to do with the lead. */
export type CrmLeadKind = 'renter' | 'owner';

export type CrmSource =
  | 'zillow'
  | 'apartments'
  | 'referral'
  | 'web_form'
  | 'email'
  | 'sms'
  | 'manual'
  | 'buildium';

/** Pipeline membership. 'lost' (won't-pursue) drops a lead off the board but
 *  keeps it for records; 'active' is the default. */
export type CrmLeadStatus = 'active' | 'lost';

/** Why a card is in its current stage — the fact Occupella observed. Every
 *  auto-move carries one so "automatic" is never opaque. */
export type CrmReasonKind =
  | 'draft' // a reply is drafted, waiting on the user (New)
  | 'reply_sent' // an outbound reply went out (→ Contacted)
  | 'calendar' // a tour/call landed on Google Calendar (→ Scheduled)
  | 'buildium' // an application/lease appeared in Buildium (→ Applied/Leased)
  | 'inquiry' // the lead just arrived
  | 'stale'; // no movement for 3+ days — flagged, NOT moved

export interface CrmMoveReason {
  kind: CrmReasonKind;
  /** One human line: "Moved when your reply sent." */
  text: string;
  /** Relative label for display, e.g. "8:02a" / "1d". */
  at?: string | null;
}

export interface CrmLead {
  id: string;
  name: string;
  /** Two-letter avatar seed. */
  initials: string;
  kind: CrmLeadKind;
  source: CrmSource;
  stage: CrmStage;
  /** Pipeline membership — 'lost' leads are hidden from the board. */
  status: CrmLeadStatus;
  /** Unit/property of interest (renters) or a short doors summary (owners). */
  unit: string | null;
  /** Email (reply target for email leads). Null for phone-only leads. */
  email?: string | null;
  /** Phone (SMS identity + reply target). Null when the lead came by a
   *  channel without one (a masked email relay, a web form). */
  phone?: string | null;
  /** One-line context ("Wants to tour Maple Court 4B this weekend."). */
  note: string;
  reason: CrmMoveReason;
  /** True when a drafted action is waiting on the user (renders "needs you"). */
  needsYou: boolean;
  /** Calendar chip when Scheduled, e.g. "Tour · Sat 2:00p". */
  calendarLabel: string | null;
  /** The booked tour datetime (ISO). Set once Occupella books a tour; drives the
   *  Scheduled week agenda + the calendar chip. */
  tourAt?: string | null;
  /** Set when the card was auto-moved recently (renders the "moved" badge). */
  movedAt: string | null;
  /** Buildium applicant id once the lead is matched to a real application
   *  (renders the "In Buildium" badge in the drawer). */
  buildiumApplicantId?: number | null;
}

export interface CrmActivityItem {
  id: string;
  leadName: string;
  /** Stage the lead moved *to*; null means "arrived / no move". */
  toStage: CrmStage | null;
  kind: CrmReasonKind;
  /** "lease signed · Buildium · 9:14a" */
  text: string;
}

export interface CrmBoard {
  leads: CrmLead[];
  activity: CrmActivityItem[];
  /** Backend feature flag. false → the section is still "Coming soon". */
  enabled: boolean;
}

/** One entry in a lead's conversation: a text, email, call, or voicemail. */
export interface CrmMessage {
  id: string;
  direction: 'inbound' | 'outbound';
  channel: 'sms' | 'email' | 'call' | 'voicemail';
  body: string;
  created_at?: string | null;
  /** Voicemail recording (channel='voicemail'); null otherwise. */
  recording_url?: string | null;
  /** Voicemail transcription text; null until Twilio transcribes it. */
  transcription?: string | null;
}

/** Stage metadata: label + the single trigger that moves a card in, plus
 *  the accent used on the column header + progress bar. */
export const CRM_STAGES: {
  key: CrmStage;
  label: string;
  /** Short header caption — the fact that lands a card here. */
  trigger: string;
  accent: string;
}[] = [
  { key: 'new', label: 'New', trigger: 'on inquiry', accent: 'var(--color-info)' },
  { key: 'contacted', label: 'Contacted', trigger: 'on reply sent', accent: 'var(--color-iris)' },
  { key: 'scheduled', label: 'Scheduled', trigger: 'on calendar', accent: 'var(--iris-light)' },
  { key: 'applied', label: 'Applied', trigger: 'on Buildium', accent: 'var(--color-caution)' },
  { key: 'leased', label: 'Leased', trigger: 'on Buildium', accent: 'var(--color-positive)' },
];

/** The move-rules, surfaced in the UI so the automation is legible. */
export const CRM_MOVE_RULES: { trigger: string; to: CrmStage; via: string }[] = [
  { trigger: 'You approve & send a reply', to: 'contacted', via: 'outbound email' },
  { trigger: 'A tour or call lands on your Google Calendar', to: 'scheduled', via: 'calendar event' },
  { trigger: 'An application shows up in Buildium', to: 'applied', via: 'Buildium webhook' },
  { trigger: 'A lease is signed in Buildium', to: 'leased', via: 'Buildium webhook' },
];

/** Thrown when the backend CRM route is dark (flag OFF → 404). The page
 *  treats this as "show the preview + Coming soon", not an error. Mirrors
 *  the DevRuns disabled-section pattern. */
export class CrmDisabledError extends Error {
  constructor() {
    super('CRM is not enabled on this backend yet.');
    this.name = 'CrmDisabledError';
  }
}

/** Two-letter avatar seed from a name, else the phone's last digits. */
function deriveInitials(name: string, phone: string | null): string {
  const n = (name || '').trim();
  if (n) {
    const parts = n.split(/\s+/);
    const two = (parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '');
    return (two || n.slice(0, 2)).toUpperCase();
  }
  const digits = (phone || '').replace(/\D/g, '');
  return digits.length >= 2 ? digits.slice(-2) : '#';
}

/** A stage-appropriate "why it's here" line for a real lead (the backend
 *  returns a lean shape; the presentational reason is derived client-side). */
function deriveReason(stage: CrmStage): CrmMoveReason {
  switch (stage) {
    case 'contacted':
      return { kind: 'reply_sent', text: 'Reply sent.', at: null };
    case 'scheduled':
      return { kind: 'calendar', text: 'On your calendar.', at: null };
    case 'applied':
      return { kind: 'buildium', text: 'Application in Buildium.', at: null };
    case 'leased':
      return { kind: 'buildium', text: 'Lease signed.', at: null };
    case 'new':
    default:
      return { kind: 'inquiry', text: 'New — reply to move forward.', at: null };
  }
}

/**
 * Fill a raw backend lead out to the full {@link CrmLead} shape the UI
 * renders. The `/crm/board` response is deliberately lean (id, name, kind,
 * stage, source, unit, note, phone) — the presentational fields (initials,
 * reason, needsYou, calendarLabel, movedAt) are derived here so the board
 * never reads an undefined field. Idempotent: existing fields win, so the
 * richer demo board passes through unchanged.
 */
/** "Tour · Sat 2:00p" from an ISO datetime, for the Scheduled calendar chip. */
export function tourChipLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Tour';
  const day = d.toLocaleDateString(undefined, { weekday: 'short' });
  let h = d.getHours();
  const min = String(d.getMinutes()).padStart(2, '0');
  const ampm = h >= 12 ? 'p' : 'a';
  h = h % 12 || 12;
  return `Tour · ${day} ${h}:${min}${ampm}`;
}

export function normalizeLead(raw: Partial<CrmLead>): CrmLead {
  const stage = (raw.stage as CrmStage) ?? 'new';
  const name = raw.name ?? '';
  const phone = raw.phone ?? null;
  // The backend sends the tour datetime as snake_case ``tour_at``; accept both.
  const tourAt = raw.tourAt ?? (raw as { tour_at?: string | null }).tour_at ?? null;
  const buildiumApplicantId =
    raw.buildiumApplicantId ??
    (raw as { buildium_applicant_id?: number | null }).buildium_applicant_id ??
    null;
  return {
    id: raw.id ?? '',
    name,
    initials: raw.initials || deriveInitials(name, phone),
    kind: (raw.kind as CrmLeadKind) ?? 'renter',
    source: (raw.source as CrmSource) ?? 'email',
    stage,
    status: (raw.status as CrmLeadStatus) ?? 'active',
    unit: raw.unit ?? null,
    email: raw.email ?? null,
    phone,
    note: raw.note ?? '',
    reason: raw.reason ?? deriveReason(stage),
    needsYou: Boolean(raw.needsYou),
    // Prefer an explicit label (demo board), else derive from the booked tour.
    calendarLabel: raw.calendarLabel ?? (tourAt ? tourChipLabel(tourAt) : null),
    tourAt,
    movedAt: raw.movedAt ?? null,
    buildiumApplicantId,
  };
}

/**
 * Load the pipeline board.
 *
 * Demo mode → in-memory fixtures. Otherwise hit the backend; a 404 means
 * the flag is off (feature dark) and surfaces as {@link CrmDisabledError}
 * so the page can render the preview board behind the Coming-soon gate.
 * The fetched payload is normalized so a lean lead can't crash the board.
 */
export async function listCrmBoard(): Promise<CrmBoard> {
  if (demoMode) return demoCrmBoard();
  let res: Response;
  try {
    res = await apiFetch('/api/v1/crm/board');
  } catch (err) {
    // Network / auth failure — let the page fall back to the preview.
    throw err instanceof ApiError ? err : new CrmDisabledError();
  }
  if (res.status === 404) throw new CrmDisabledError();
  if (!res.ok) throw new ApiError(res.status, 'Could not load the CRM board.');
  const raw = (await res.json()) as {
    leads?: Partial<CrmLead>[];
    activity?: CrmActivityItem[];
    enabled?: boolean;
  };
  return {
    enabled: raw.enabled ?? true,
    leads: (raw.leads ?? []).map(normalizeLead),
    activity: raw.activity ?? [],
  };
}

// ── Overview (landing) + stage list ──────────────────────────────

/** This-month + lifetime analytics for the overview band. */
export interface CrmMetrics {
  new_this_month: number;
  new_prev_month: number;
  replies_this_month: number;
  active: number;
  leased: number;
  total: number;
}

/** One stage on the overview: its count + the few most-recent leads. */
export interface CrmStageGroup {
  key: CrmStage;
  label: string;
  count: number;
  recent: CrmLead[];
}

export interface CrmOverview {
  stages: CrmStageGroup[];
  metrics: CrmMetrics;
  /** Leads still in play but quiet 3+ days — the "needs a nudge" count. */
  cold_count: number;
}

/**
 * The overview plus an explicit data-source flag. ``isDemo: true`` means
 * the payload is the fixture preview (demo build, or the backend CRM flag
 * is dark → {@link CrmDisabledError}), NOT the live pipeline — the page
 * must label it so a real signed-in user never mistakes sample leads for
 * their own (2026-08-15 audit, trust liability).
 */
export interface CrmOverviewSource {
  overview: CrmOverview;
  isDemo: boolean;
}

/**
 * Load the overview with the fixture-vs-live distinction made explicit,
 * instead of leaving the page to infer it from a caught error. Live
 * fetch failures still throw — a real user must see an error, never
 * unlabeled fabricated data.
 */
export async function loadCrmOverview(): Promise<CrmOverviewSource> {
  if (demoMode) return { overview: demoCrmOverview(), isDemo: true };
  try {
    return { overview: await getCrmOverview(), isDemo: false };
  } catch (err) {
    if (err instanceof CrmDisabledError) {
      return { overview: demoCrmOverview(), isDemo: true };
    }
    throw err;
  }
}

/** Load the CRM landing page: per-stage counts + recent leads + analytics. */
export async function getCrmOverview(): Promise<CrmOverview> {
  if (demoMode) return demoCrmOverview();
  const res = await apiFetch('/api/v1/crm/overview');
  if (res.status === 404) throw new CrmDisabledError();
  if (!res.ok) throw new ApiError(res.status, 'Could not load the CRM overview.');
  const raw = (await res.json()) as {
    stages?: { key: CrmStage; label: string; count: number; recent?: Partial<CrmLead>[] }[];
    metrics?: CrmMetrics;
    cold_count?: number;
  };
  return {
    stages: (raw.stages ?? []).map((s) => ({
      key: s.key,
      label: s.label,
      count: s.count ?? 0,
      recent: (s.recent ?? []).map(normalizeLead),
    })),
    metrics:
      raw.metrics ??
      { new_this_month: 0, new_prev_month: 0, replies_this_month: 0, active: 0, leased: 0, total: 0 },
    cold_count: raw.cold_count ?? 0,
  };
}

/** The going-cold leads ("needs a nudge") + the day threshold used. */
export interface CrmColdLeads {
  leads: CrmLead[];
  count: number;
  threshold_days: number;
}

/** Load leads that have gone quiet 3+ days (active, not leased), coldest first. */
export async function listColdLeads(): Promise<CrmColdLeads> {
  if (demoMode) return demoColdLeads();
  const res = await apiFetch('/api/v1/crm/leads/cold');
  if (res.status === 404) throw new CrmDisabledError();
  if (!res.ok) throw new ApiError(res.status, 'Could not load going-cold leads.');
  const raw = (await res.json()) as {
    leads?: Partial<CrmLead>[];
    count?: number;
    threshold_days?: number;
  };
  return {
    leads: (raw.leads ?? []).map(normalizeLead),
    count: raw.count ?? 0,
    threshold_days: raw.threshold_days ?? 3,
  };
}

export interface CrmStageLeads {
  leads: CrmLead[];
  has_more: boolean;
}

/** Load the leads in one stage (the stage-detail list), filtered + paginated. */
export async function listStageLeads(opts: {
  stage: CrmStage;
  q?: string;
  limit?: number;
  offset?: number;
}): Promise<CrmStageLeads> {
  if (demoMode) return demoStageLeads(opts.stage, opts.q);
  const params = new URLSearchParams({ stage: opts.stage });
  if (opts.q) params.set('q', opts.q);
  if (opts.limit != null) params.set('limit', String(opts.limit));
  if (opts.offset != null) params.set('offset', String(opts.offset));
  const res = await apiFetch(`/api/v1/crm/leads?${params.toString()}`);
  // ⚠ 404 is the CRM flag being dark, not a missing stage — `_require_crm_flag`
  // raises it on every CRM route (api/routes/crm.py). Its two siblings
  // (`getCrmOverview`, `listColdLeads`) have always mapped it; this one did
  // not, so the one endpoint that could not tell "feature off" from "server
  // error" was the drill-down a user reaches by clicking a stage count.
  if (res.status === 404) throw new CrmDisabledError();
  if (!res.ok) throw new ApiError(res.status, 'Could not load leads.');
  const raw = (await res.json()) as { leads?: Partial<CrmLead>[]; has_more?: boolean };
  return { leads: (raw.leads ?? []).map(normalizeLead), has_more: Boolean(raw.has_more) };
}

/**
 * The drill-down loaders, with the fixture-vs-live distinction made explicit —
 * exactly what {@link loadCrmOverview} does for the landing page.
 *
 * ⚠ THE OVERVIEW'S POLICY NEVER REACHED THE TWO VIEWS BELOW IT. It falls back
 * to the demo fixture on {@link CrmDisabledError} and labels it; the stage and
 * cold views each swallowed EVERY error into an empty list, so with the CRM
 * flag dark the overview showed demo counts ("Contacted: 7") and clicking one
 * showed "No leads in this stage yet" — the page contradicting itself. And a
 * real 500 rendered the same way, which is a fabricated absence (gotcha 26).
 *
 * A live fetch failure still THROWS. The caller must show an error; unlabeled
 * fabricated data is the one thing this pair exists to prevent.
 */
export async function loadStageLeads(opts: {
  stage: CrmStage;
  q?: string;
  limit?: number;
  offset?: number;
}): Promise<CrmStageLeads & { isDemo: boolean }> {
  if (demoMode) return { ...demoStageLeads(opts.stage, opts.q), isDemo: true };
  try {
    return { ...(await listStageLeads(opts)), isDemo: false };
  } catch (err) {
    if (err instanceof CrmDisabledError) {
      return { ...demoStageLeads(opts.stage, opts.q), isDemo: true };
    }
    throw err;
  }
}

/** The going-cold list, same fixture-vs-live contract as {@link loadStageLeads}. */
export async function loadColdLeads(): Promise<CrmColdLeads & { isDemo: boolean }> {
  if (demoMode) return { ...demoColdLeads(), isDemo: true };
  try {
    return { ...(await listColdLeads()), isDemo: false };
  } catch (err) {
    if (err instanceof CrmDisabledError) {
      return { ...demoColdLeads(), isDemo: true };
    }
    throw err;
  }
}

/** The AI assist for a lead: "what Occupella noticed" + a drafted first reply. */
export interface CrmLeadDraft {
  /** 2–4 short observation strings (bare strings, like the Inbox signals). */
  signals: string[];
  /** Suggested first reply to pre-fill the composer. null = none generated. */
  draft_reply: string | null;
}

/**
 * Load the lead's AI assist (signals + drafted reply). Generated once on the
 * backend and cached; a model outage returns empty signals + null draft (the
 * drawer still renders — the assist is never load-bearing).
 */
export async function getLeadDraft(leadId: string): Promise<CrmLeadDraft> {
  if (demoMode) return demoLeadDraft(leadId);
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/draft`);
  if (!res.ok) throw new ApiError(res.status, 'Could not load the draft.');
  return (await res.json()) as CrmLeadDraft;
}

/** Load a lead's conversation thread (oldest first). */
export async function listLeadMessages(leadId: string): Promise<CrmMessage[]> {
  if (demoMode) return demoLeadMessages(leadId);
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/messages`);
  if (!res.ok) throw new ApiError(res.status, 'Could not load the conversation.');
  return (await res.json()) as CrmMessage[];
}

/** Which channel a lead reply goes out on. Text if the lead has a phone, else
 *  email — a lead arrives by one channel and is answered on it. */
export type CrmReplyChannel = 'sms' | 'email';

export interface CrmReplyResult {
  message_sid: string;
  stage: CrmStage;
  channel: CrmReplyChannel;
}

/**
 * Send a user-approved reply to a lead on its channel (text or email). Never
 * auto-sent — this fires only when the user hits approve. Omit `channel` to let
 * the backend pick by the lead's contact fields. In demo mode it returns a
 * synthetic success (no backend, no Twilio/Gmail) so the preview loop works.
 */
export async function sendLeadReply(
  leadId: string,
  body: string,
  channel?: CrmReplyChannel,
): Promise<CrmReplyResult> {
  if (demoMode) return { message_sid: 'demo', stage: 'contacted', channel: channel ?? 'sms' };
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/reply`, {
    method: 'POST',
    body: JSON.stringify(channel ? { body, channel } : { body }),
  });
  // ⚠ THERE WAS A SECOND ERROR PATH HERE AND IT COULD NEVER RUN. It read the
  // backend's reason off the body and threw its own `ApiError` — but every
  // branch inside `apiFetch`'s `if (!res.ok)` ends in a throw, so `res.ok` is
  // guaranteed by the time this line is reached. A careful message nobody
  // could see, and a second home for the extraction rule that `writtenDetail`
  // now owns once (gotcha 25). This route's details are plain strings, which
  // `apiFetch` has always surfaced, so deleting the block changes no wording.
  return (await res.json()) as CrmReplyResult;
}

/**
 * Click-to-call via Twilio: rings the PM's cell, then bridges to the lead —
 * the call goes out from the business number and is logged. In demo mode it
 * returns a synthetic sid (no Twilio). The plain `tel:` link on the number is
 * the zero-backend fallback; this is the logged, business-number version.
 */
export async function callLead(leadId: string): Promise<{ call_sid: string }> {
  if (demoMode) return { call_sid: 'demo' };
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/call`, {
    method: 'POST',
  });
  if (!res.ok) {
    let detail = 'Could not place the call.';
    try {
      const j = (await res.json()) as { detail?: string };
      if (j?.detail) detail = j.detail;
    } catch {
      /* keep the default */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as { call_sid: string };
}

// ── manual controls (hand-run the pipeline) ──────────────────────

/** Surface the backend's ``detail`` on a failed mutation, else a fallback. */
async function readError(res: Response, fallback: string): Promise<ApiError> {
  let detail = fallback;
  try {
    const j = (await res.json()) as { detail?: string };
    if (j?.detail) detail = j.detail;
  } catch {
    /* keep the fallback */
  }
  return new ApiError(res.status, detail);
}

/**
 * Manually move a lead to a stage (the drawer stage picker). Unlike an
 * auto-move this is a deliberate override — Occupella's own move rules
 * (reply/calendar/Buildium) still apply afterward. Returns the updated lead.
 */
export async function moveLeadStage(leadId: string, stage: CrmStage): Promise<CrmLead> {
  if (demoMode) return normalizeLead({ id: leadId, stage });
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/stage`, {
    method: 'POST',
    body: JSON.stringify({ stage }),
  });
  if (!res.ok) throw await readError(res, 'Could not move the lead.');
  return normalizeLead((await res.json()) as Partial<CrmLead>);
}

/**
 * Mark a lead ``lost`` (won't-pursue → off the board, kept for records) or
 * restore it to ``active``. Returns the updated lead.
 */
export async function setLeadStatus(leadId: string, status: CrmLeadStatus): Promise<CrmLead> {
  if (demoMode) return normalizeLead({ id: leadId, status });
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw await readError(res, 'Could not update the lead.');
  return normalizeLead((await res.json()) as Partial<CrmLead>);
}

/** Statuses Occupella may set on a Buildium application. Deleting is
 *  impossible — the Buildium API has no delete operation on applications. */
export const APPLICANT_STATUSES = [
  'Undecided',
  'Approved',
  'Rejected',
  'Cancelled',
  'Deferred',
] as const;
export type ApplicantStatus = (typeof APPLICANT_STATUSES)[number];

/** Push a decision on a Buildium-linked lead back to Buildium (the reverse
 *  of the applicant auto-move). Fires only from the explicit drawer button. */
export async function setApplicantStatus(
  leadId: string,
  status: ApplicantStatus,
  applicationId?: number,
): Promise<{ ok: boolean; status: string; application_id: number }> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 400));
    return { ok: true, status, application_id: 1 };
  }
  const res = await apiFetch(`/api/v1/crm/leads/${encodeURIComponent(leadId)}/applicant-status`, {
    method: 'POST',
    body: JSON.stringify({ status, application_id: applicationId ?? null }),
  });
  if (!res.ok) throw await readError(res, 'Could not update the application in Buildium.');
  return res.json();
}

/** Fields for a hand-entered lead. At least one of name/phone/email required. */
export interface CreateLeadInput {
  name?: string;
  kind?: CrmLeadKind;
  stage?: CrmStage;
  phone?: string | null;
  email?: string | null;
  unit?: string | null;
  note?: string | null;
}

/**
 * Hand-enter a lead (the "＋ Add lead" form) — a walk-in, a phone call, any
 * channel Occupella doesn't watch. Source is always ``manual``. Returns the
 * created lead.
 */
export async function createLead(input: CreateLeadInput): Promise<CrmLead> {
  if (demoMode) {
    return normalizeLead({
      id: `demo-${Date.now()}`,
      name: input.name ?? '',
      kind: input.kind ?? 'renter',
      stage: input.stage ?? 'new',
      source: 'manual',
      phone: input.phone ?? null,
      email: input.email ?? null,
      unit: input.unit ?? null,
      note: input.note ?? '',
    });
  }
  const res = await apiFetch('/api/v1/crm/leads', {
    method: 'POST',
    body: JSON.stringify({ ...input, source: 'manual' }),
  });
  if (!res.ok) throw await readError(res, 'Could not create the lead.');
  return normalizeLead((await res.json()) as Partial<CrmLead>);
}
