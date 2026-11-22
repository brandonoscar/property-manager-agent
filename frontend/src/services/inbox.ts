/**
 * Occupella Inbox data layer (Phase A.2).
 *
 * Wraps three backend endpoints the Inbox page uses:
 *   - GET  /api/v1/buildium/recent-events?include=action_surface,enrichment
 *   - POST /api/v1/buildium/events/:id/actions/:i/execute
 *   - POST /api/v1/buildium/events/:id/freeform
 *
 * Types mirror the JSON shapes the backend produces verbatim (see the
 * canonical example in the Phase A.2 prompt). Demo mode (gated by
 * ``demoMode``, default OFF) renders populated fixtures at the page
 * seam (``demoData.ts``); here the rule is the inverse — every
 * MUTATING call short-circuits in demo so a demo click can NEVER reach
 * the backend or write to a real table. Reads in demo are served by the
 * page from fixtures, not from these functions.
 */

import { apiFetch, ApiError } from './api';
import { demoMode } from './auth';

// ── Phase A.2.5 click-chain types ────────────────────────────────

/** One choice on a click-chain follow-up question. Mirrors the
 *  backend ``FollowupOption`` 1:1. */
export interface FollowupOption {
  label: string;
  description?: string | null;
  parameter_patch: Record<string, unknown>;
  needs_followup: boolean;
  followup_hint?: string | null;
}

/** A single multiple-choice question shown in the ActionChainModal.
 *  Mirrors the backend ``FollowupQuestion``. */
export interface FollowupQuestion {
  prompt: string;
  options: FollowupOption[];
  allow_freeform: boolean;
}

/** Terminal signal from ``POST /actions/{i}/followup``: the planner
 *  has gathered enough context; the frontend renders the
 *  confirmation step with these merged parameters. */
export interface DoneSignal {
  done: true;
  merged_parameters: Record<string, unknown>;
}

/** One committed pick in the click-chain. Posted back to ``/execute``
 *  so the backend can persist + audit the conversation. */
export interface ChainPick {
  prompt: string;
  label: string;
  parameter_patch: Record<string, unknown>;
}

// PR #99 — V2 suggestion categories. Only these two are allowed
// to surface in the inbox; anything else (re-categorize, raise
// priority, "open in Buildium") is the kind of "feels like 2008"
// suggestion the V2 prompt deliberately refuses to emit.
export type InboxActionType = 'information_need' | 'bonus';

// PR #99 — confidence drives the OFFER vs QUESTION card shape:
//   high   → OFFER (single primary button, agent is ready to act)
//   medium → QUESTION (chips + inline text, agent is asking)
//   low    → QUESTION (same shape; the "low" gradient is a future
//                       affordance — same render today)
export type InboxActionConfidence = 'high' | 'medium' | 'low';

// PR #99 — concrete next-button shape carried on each V2 action.
// Mirrors the backend's PrimaryAction Pydantic model. The frontend
// reads ``label`` for the button text, ``kind`` to pick the icon /
// affordance, and ``draft`` to pre-fill the inline editor for
// message-shaped actions.
export type InboxPrimaryActionKind =
  | 'send_message'
  | 'create_note'
  | 'create_task'
  | 'schedule'
  | 'request_info'
  | 'freeform';

export interface InboxPrimaryActionTarget {
  kind: 'tenant' | 'owner' | 'vendor' | 'internal';
  name?: string | null;
}

export interface InboxPrimaryAction {
  label: string;
  kind: InboxPrimaryActionKind;
  target?: InboxPrimaryActionTarget | null;
  draft?: string | null;
}

export interface InboxAction {
  tool_id: string;
  label: string;
  intent: string;
  parameters: Record<string, unknown>;
  /** Pre-baked next question. ``null``/absent → action skips straight
   *  to the confirmation step in the modal. Absent on v1 rows. */
  first_followup?: FollowupQuestion | null;
  // PR #98 — optional reasoning fields. The planner backend doesn't
  // emit any of these yet; we add them to the type so the new
  // "Why this?" UI can render WHEN they arrive, and degrade silently
  // (button still appears but expands to "Reasoning trace coming
  // soon.") when they're absent. Backend can fill them in a follow-up
  // PR without another frontend ship.
  rationale?: string | null;
  triggers?: string[] | null;
  alternative?: string | null;
  // PR #99 — V2 suggestion shape. Optional so V1 surfaces (the two
  // rows in prod before this lands) still parse. The frontend
  // dispatches on ``type``+``confidence`` to render an OFFER card
  // (primary button + inline edit + Skip + Why-this) or a QUESTION
  // card (title-as-question + 2-3 chips + inline text + Why-this).
  // When ``type`` / ``confidence`` are absent, the renderer falls
  // back to a minimal OFFER with whatever fields are present.
  type?: InboxActionType | null;
  confidence?: InboxActionConfidence | null;
  primary_action?: InboxPrimaryAction | null;
  /** Set by the server (``tool_catalog.mark_unavailable``) on an action that
   *  writes to a PMS this workspace is not on, e.g. a Buildium write on a
   *  Rentvine card. The Inbox hides it; the executor refuses it with 409. */
  unavailable_reason?: string | null;
}

export interface InboxActionSurface {
  schema_version?: number;
  summary: string;
  actions: InboxAction[];
  freeform_placeholder: string;
  // PR #98 — optional agent-noticed signals. When the planner emits
  // these, the "What Occupella noticed" section renders them
  // verbatim. When absent, we derive bullets deterministically from
  // ``enrichment`` (see ``buildAgentNoticed`` in InboxPage.tsx).
  signals?: string[] | null;
  // UI refinement #2b — exact summary substrings to emphasize in
  // Violet (tenant/owner, address, the core request). The frontend
  // wraps only these spans; absent → plain summary.
  summary_highlights?: string[] | null;
}

export interface InboxEnrichmentTask {
  id: number;
  unit_id: number | null;
  property_id: number | null;
  assigned_to_user_id: number | null;
  // PR #103 — resolved assignee display name. Backend decorates this
  // on read by joining ``buildium_staff``; absent when the assignee
  // user_id doesn't resolve (sync hasn't run yet, user was removed
  // from Buildium, etc.). The inbox header cell falls back to ``—``
  // in that case so the column always renders something.
  assignee_name?: string | null;
  title: string | null;
  description: string | null;
  category: string | null;
  priority: string | null;
  status: string | null;
  due_date: string | null;
  requested_by_first_name?: string | null;
  requested_by_last_name?: string | null;
  // Buildium ``RequestedByUserEntity.Type`` (Tenant / RentalOwner / Contact
  // / …). Resident, owner, and contact requests all arrive as the SAME
  // Task.Created webhook; this is the only field that distinguishes them, so
  // the card uses it to badge an owner or contact request. Absent → generic
  // task (staff todorequest or pre-Type row).
  requested_by_type?: string | null;
  raw?: Record<string, unknown> | null;
}

// Mirror of the backend request_kind_label (agent/enrichment/models.py). Maps
// a RequestedByUserEntity.Type to the Inbox request-kind label, or null when
// absent/unrecognised so a generic task stays unlabelled.
const REQUEST_KIND_LABELS: Record<string, string> = {
  tenant: 'Resident request',
  rentalowner: 'Owner request',
  owner: 'Owner request',
  contact: 'Contact request',
  association: 'Association request',
};

export function requestKindLabel(
  requestedByType: string | null | undefined,
): string | null {
  if (!requestedByType) return null;
  return REQUEST_KIND_LABELS[requestedByType.trim().toLowerCase()] ?? null;
}

export interface InboxEnrichmentTenant {
  id: number;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
}

export interface InboxEnrichmentLease {
  id: number;
  status: string | null;
  lease_from_date: string | null;
  lease_to_date: string | null;
  tenants: InboxEnrichmentTenant[];
}

export interface InboxEnrichmentUnit {
  id: number;
  property_id: number | null;
  unit_number: string | null;
  address_line1: string | null;
}

export interface InboxEnrichment {
  schema_version?: number;
  event_name?: string;
  task: InboxEnrichmentTask;
  unit?: InboxEnrichmentUnit | null;
  active_lease?: InboxEnrichmentLease | null;
  history_unit?: Array<{ id: number; title?: string | null; category?: string | null }>;
  history_property?: Array<{ id: number; title?: string | null; category?: string | null }>;
}

// PR #89 — the LLM planner moved from the synchronous webhook receiver
// into the worker. Each row carries its planner lifecycle so the
// inbox can render honest state instead of pretending floor-only
// rows are intentional. ``succeeded`` is the default the backend
// returns when the column is absent (pre-migration deploys).
export type PlannerStatus =
  | 'pending'
  | 'in_progress'
  | 'succeeded'
  | 'timed_out'
  | 'failed'
  // Migration 0080 — an event the Inbox allowlist deliberately excludes
  // (a lease transaction, an owner-ledger entry). Widened for ACCURACY, not
  // because anything renders it: the recent-events query filters on
  // `inbox_like_patterns()`, so a row carrying this status is one this
  // client never fetches. Nothing here should branch on it — if a future
  // surface does start fetching non-inbox rows, that is the moment to
  // decide what it should look like, not now.
  | 'not_applicable';

// PR #92 — summarizer lifecycle. The worker writes a plain-English
// narrative ("Sarah Chen at 123 Main St #4B reported a leak…") that
// the Inbox detail pane renders above the field grid. The states
// mirror the planner's 5-state machine but the values are slightly
// different — there's no 'in_progress' interim (the claim doesn't
// flip the status, see queue.py:claim_summary_batch docstring) and
// the failure-but-recoverable state is called 'retrying' instead of
// 'timed_out' since timeout isn't the only retryable cause.
export type SummaryStatus =
  | 'pending'
  | 'succeeded'
  | 'failed'
  | 'retrying';

export interface InboxEventSummary {
  status: SummaryStatus;
  text: string | null;
  error: string | null;
  attempts: number;
  last_attempted_at: string | null;
}

export interface InboxRow {
  id: string;
  event_name: string;
  event_time: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  last_error: string | null;
  action_surface: InboxActionSurface | null;
  enrichment: InboxEnrichment | null;
  executed_action_index: number | null;
  // The outcome of that execution. The backend has ALWAYS persisted these
  // (``execution_result``/``executed_at`` are written next to
  // ``executed_action_index`` in the same UPDATE), and the client has never
  // asked for them — so a finished item could only ever say "Sent", never
  // what actually happened. Same "fetched and thrown away" shape as the
  // mirror bugs; here the data was on disk the whole time.
  execution_result?: Record<string, unknown> | null;
  executed_at?: string | null;
  // PR #89 — planner lifecycle. Backend supplies a default
  // 'succeeded' when the migration hasn't been applied yet, so this
  // is always present at runtime.
  planner_status: PlannerStatus;
  planner_error: string | null;
  // PR #92 — summarizer lifecycle + text. Backend defaults to
  // ``{status: 'succeeded', text: null, ...}`` on pre-migration
  // deploys, which the frontend treats as "no summary yet" rather
  // than blowing up.
  summary: InboxEventSummary;
  // Risk cards only: how much of this card is still true, read at request
  // time from the findings behind it. A finding heals when the detector stops
  // emitting it and nothing ever told the card -- measured 2026-09-09, 35 of
  // 81 risk cards described a problem that no longer existed and 9 more
  // overstated their count. Absent on every other card type, so this is
  // optional rather than nullable: the backend omits the key entirely.
  finding_state?: FindingState;
}

export type FindingStateName = 'open' | 'partly_resolved' | 'all_resolved' | 'unknown';

export interface FindingState {
  state: FindingStateName;
  total: number;
  open: number;
  resolved: number;
  dismissed: number;
  unknown: number;
}

/**
 * The line to show on a risk card whose findings have moved since it was
 * raised, or `null` when there is nothing worth saying.
 *
 * - `open` returns null: the card is accurate, and a badge on every card is
 *   one people stop reading (gotcha 52).
 * - `unknown` returns null too, deliberately. "We could not check" is not
 *   information a property manager can act on, and rendering doubt on a card
 *   that may be perfectly current would train them to ignore the badge that
 *   matters. The backend still reports it; the API is where that shows up.
 */
export function findingStateLabel(state: FindingState | undefined): string | null {
  if (!state) return null;
  if (state.state === 'all_resolved') {
    return state.total === 1
      ? 'Resolved since this was raised'
      : `All ${state.total} resolved since this was raised`;
  }
  if (state.state === 'partly_resolved') {
    return `${state.resolved} of ${state.total} resolved since this was raised`;
  }
  return null;
}

export interface ExecuteActionResponse {
  status: 'executed';
  result: {
    buildium_id: number | null;
    summary: string;
    [key: string]: unknown;
  };
}

export interface ApiErrorDetail {
  /** Present on 502 errors raised by handlers — see HandlerError. */
  error?: string;
  tool_id?: string;
}

const INBOX_LIMIT = 20;

/**
 * Fetch the Inbox feed. Returns rows with action_surface + enrichment
 * inflated. Filtering for "needs user action" lives in the caller
 * (InboxPage) so the service stays a thin wire-format wrapper.
 *
 * ``mine`` (#69) adds ``assignee=me`` so the backend narrows to the rows the
 * webhook router assigned to the current user — the per-user "My work" view
 * for a shared team account. Omitted/false returns the whole company queue.
 */
export async function listInboxRows(mine = false): Promise<InboxRow[]> {
  const assigneeParam = mine ? '&assignee=me' : '';
  const res = await apiFetch(
    `/api/v1/buildium/recent-events?include=action_surface,enrichment,executed_action_index,execution_result,executed_at${assigneeParam}&limit=${INBOX_LIMIT}`,
  );
  const rows = (await res.json()) as InboxRow[];
  return Array.isArray(rows) ? rows : [];
}

/**
 * Execute the action at ``actionIndex`` on the event. The user's edits
 * from the confirmation modal land in ``parameterOverrides``; the
 * backend deep-merges them into the planner's proposed parameters
 * before invoking the handler.
 *
 * PR #70: pass ``sessionId`` to have the backend stream narration +
 * a closing assistant turn on the per-event SSE channel. Callers that
 * aren't subscribed (e.g. legacy polling clients) pass ``null``.
 *
 * Throws an ``ApiError`` on 4xx/5xx; callers inspect ``error.status``
 * to branch (409 = already executed, 502 = handler error with detail).
 */
export async function executeInboxAction(
  eventId: string,
  actionIndex: number,
  parameterOverrides: Record<string, unknown> | null,
  freeformNote: string | null = null,
  chain: ChainPick[] | null = null,
  sessionId: string | null = null,
): Promise<ExecuteActionResponse> {
  if (demoMode) {
    // Demo isolation: never POST a write. Return a synthetic success so
    // the UI flows (toast + row update) without touching the backend.
    return {
      status: 'executed',
      result: { buildium_id: null, summary: 'Demo mode — nothing was actually sent.' },
    };
  }
  const res = await apiFetch(
    `/api/v1/buildium/events/${encodeURIComponent(eventId)}/actions/${actionIndex}/execute`,
    {
      method: 'POST',
      body: JSON.stringify({
        parameter_overrides: parameterOverrides,
        freeform_note: freeformNote,
        chain,
        session_id: sessionId,
      }),
    },
  );
  return (await res.json()) as ExecuteActionResponse;
}

// ── PR #70 — thread SSE subscription ────────────────────────────

/** One event on the conversational thread SSE stream. */
export interface ConversationStreamEvent {
  type: string;
  data: Record<string, unknown>;
  timestamp?: number;
}

/**
 * Subscribe to a row's conversational thread. The promise resolves
 * once the connection closes (server-side EOF, network blip, or
 * caller invoked ``abort()``). Each event is delivered via
 * ``onEvent``. The returned ``sessionId`` is the value the caller
 * should pass on subsequent ``executeInboxAction`` POSTs so the
 * backend publishes back into the same channel.
 *
 * Implementation uses ``fetch`` with a ``ReadableStream`` rather than
 * ``EventSource`` because EventSource doesn't let us attach auth
 * headers — and the Inbox endpoints are auth-scoped.
 */
export interface InboxThreadStream {
  sessionId: string;
  abort: () => void;
  done: Promise<void>;
}

export function subscribeInboxThread(
  eventId: string,
  onEvent: (event: ConversationStreamEvent) => void,
  options: { sessionId?: string } = {},
): InboxThreadStream {
  const sessionId = options.sessionId ?? generateSessionId();
  const controller = new AbortController();

  const done = (async () => {
    try {
      const res = await apiFetch(
        `/api/v1/buildium/events/${encodeURIComponent(eventId)}/thread/stream?session_id=${encodeURIComponent(sessionId)}`,
        { signal: controller.signal },
      );
      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events end with a blank line. Parse one event at a time.
        let separatorIndex: number;
        while ((separatorIndex = buffer.indexOf('\n\n')) !== -1) {
          const rawEvent = buffer.slice(0, separatorIndex);
          buffer = buffer.slice(separatorIndex + 2);
          const dataLine = rawEvent
            .split('\n')
            .find((line) => line.startsWith('data:'));
          if (!dataLine) continue;
          const payload = dataLine.slice('data:'.length).trim();
          if (payload === '[DONE]') return;
          try {
            const parsed = JSON.parse(payload) as ConversationStreamEvent;
            onEvent(parsed);
          } catch {
            // Skip malformed payloads — better than crashing the
            // whole stream because one event was odd.
          }
        }
      }
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return;
      // Surface the failure via an internal ERROR event so the parent
      // can render a "connection lost" hint without us throwing into
      // the React tree.
      // `connection: true` marks it as the STREAM failing, not the agent: the
      // conversation fold (runnerFold.foldInboxStream) must not turn
      // `String(err)` into a reply bubble.
      onEvent({ type: 'error', data: { message: String(err), connection: true } });
    }
  })();

  return {
    sessionId,
    abort: () => controller.abort(),
    done,
  };
}

function generateSessionId(): string {
  // ``crypto.randomUUID`` is available in every browser this app
  // targets (Chrome 92+, Firefox 95+, Safari 15.4+); the typecheck
  // protects us if a future build target lacks it.
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `thread:${crypto.randomUUID()}`;
  }
  // Fallback: timestamp + random — non-cryptographic, but the session
  // id is just a bus channel key, not a security boundary.
  return `thread:${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Phase A.2.5 — ask the backend planner for the next click-chain
 * question (or a DoneSignal). Called by ``ActionChainModal`` when the
 * user picks an option flagged ``needs_followup: true`` or types into
 * the "Other…" freeform expander.
 *
 * Discriminator: ``"done" in response`` → ``DoneSignal``, else
 * ``FollowupQuestion``. Throws ``ApiError`` on non-2xx so the modal
 * can branch on the 502 ("couldn't generate next question") case.
 */
export async function fetchDeeperFollowup(
  eventId: string,
  actionIndex: number,
  body: { previous_picks: ChainPick[]; freeform_hint?: string | null },
): Promise<FollowupQuestion | DoneSignal> {
  if (demoMode) {
    // Demo isolation: resolve the click-chain locally with a terminal
    // DoneSignal instead of calling the planner endpoint.
    return { done: true, merged_parameters: {} };
  }
  const res = await apiFetch(
    `/api/v1/buildium/events/${encodeURIComponent(eventId)}/actions/${actionIndex}/followup`,
    {
      method: 'POST',
      body: JSON.stringify({
        previous_picks: body.previous_picks,
        freeform_hint: body.freeform_hint ?? null,
      }),
    },
  );
  return (await res.json()) as FollowupQuestion | DoneSignal;
}

/** Discriminator helper exported for the modal + tests. */
export function isDoneSignal(
  resp: FollowupQuestion | DoneSignal,
): resp is DoneSignal {
  return 'done' in resp && resp.done === true;
}

/**
 * Submit the free-text follow-up the PM typed in the card's input box.
 * Phase A.2 stores it as a manual activity row; Phase A.3 routes it
 * through the LLM. Resolves on success; throws ApiError otherwise.
 */
export async function submitInboxFreeform(
  eventId: string,
  note: string,
  sessionId: string | null = null,
): Promise<void> {
  if (demoMode) return; // demo isolation: no-op, never POST
  const res = await apiFetch(
    `/api/v1/buildium/events/${encodeURIComponent(eventId)}/freeform`,
    {
      method: 'POST',
      // Pass the live thread session so the agent's reply + confirm_gate stream
      // back into the channel this row is subscribed to (mirrors executeInboxAction).
      body: JSON.stringify({ note, session_id: sessionId }),
    },
  );
  if (!res.ok) {
    throw new ApiError(res.status, `freeform failed (${res.status})`);
  }
}

// PR #89 — reset planner_status to 'pending' so the worker re-runs
// the planner on this event. Triggered from the inbox Replan pill
// when the previous run timed out or failed. ``source`` is
// forward-compat for non-Buildium event sources (LeadSimple, Gmail);
// today only "buildium" is supported and that's the default.
export async function replanInboxEvent(
  eventId: string,
  source: string = 'buildium',
): Promise<void> {
  if (demoMode) return; // demo isolation: no-op, never POST
  const res = await apiFetch(
    `/api/v1/buildium/events/${encodeURIComponent(eventId)}/replan`,
    {
      method: 'POST',
      body: JSON.stringify({ source }),
    },
  );
  if (!res.ok) {
    throw new ApiError(res.status, `replan failed (${res.status})`);
  }
}

// PR #92 — reset summary_status to 'pending' so the worker re-runs
// the summarizer on this event. Triggered from the inbox
// Resummarize pill when summary_status is 'failed' (or when the
// user thinks the existing summary is wrong). Mirrors replan
// exactly — both routes hit the same backend pattern.
export async function resummarizeInboxEvent(
  eventId: string,
  source: string = 'buildium',
): Promise<void> {
  if (demoMode) return; // demo isolation: no-op, never POST
  const res = await apiFetch(
    `/api/v1/buildium/events/${encodeURIComponent(eventId)}/resummarize`,
    {
      method: 'POST',
      body: JSON.stringify({ source }),
    },
  );
  if (!res.ok) {
    throw new ApiError(res.status, `resummarize failed (${res.status})`);
  }
}
