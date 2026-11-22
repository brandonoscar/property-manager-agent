/**
 * Shared types for the conversational engine (PR #70).
 *
 * The frontend's view of a ``HelixisEvent`` mirrors what the backend
 * emits on the SSE thread stream. Only the event types the Inbox /
 * Chat surfaces actually render are typed here; the backend may emit
 * additional types (browser, memory, etc.) — the renderer falls
 * through silently when a type isn't in this union.
 */

export type ConversationEventType =
  // The work loop's hand-off (``suggest_next_steps``): what to do now that
  // the question is answered, as clickable steps. Emitted AFTER the prose —
  // the reply still says what it recommends; these are the shortcut.
  | 'next_steps'
  // The assistant asking for the blanks (``request_details``) when a request
  // is too vague to act on, as a small form rather than a paragraph of
  // questions. It ASKS — submitting sends an ordinary message, so every write
  // it leads to still passes its own confirmation.
  | 'form'
  | 'narration'
  // The model's own working-out, streamed while it thinks. NOT the answer
  // (``assistant_message_token``) and NOT the sentence it writes for the user
  // before a tool call (``thinking``) — this is the reasoning underneath both.
  // It rides the SAME activity trail as the steps, in stream order, so the
  // wait reads as work rather than as a spinner; it does NOT count towards
  // the trail's "N steps" summary, because a thought is not a step.
  | 'reasoning'
  // A step taken INSIDE a nested specialist consult. Out on the parent stream
  // that whole consult is ONE tool row that sits there for ninety seconds;
  // this is the work happening under it. Indented, and NOT counted as a step —
  // the parent took one step, and reporting the specialist's fan-out as the
  // parent's own would make the trail summary meaningless.
  | 'substep'
  // One sentence saying what was ASKED, published within a couple of seconds
  // of Enter. Everything else on the wire arrives only once the model has
  // started; this is the only thing that can fill the first few seconds, which
  // is where "is this broken?" gets decided. Rendered ABOVE the trail and at
  // readable size — it is addressed to the user, not telemetry.
  | 'restatement'
  | 'assistant_message_start'
  | 'assistant_message_token'
  | 'assistant_message_end'
  | 'suggestion_chip'
  | 'confirm_gate'
  | 'thinking'
  // The agent auto-injects relevant company memory before each turn and
  // emits this so the client can show a "recalled from memory" line. The
  // recall itself is prompt-injected server-side; this is the UI signal.
  | 'memory_recall'
  // PR #75 — Chat surfaces user input as its own right-aligned bubble
  // instead of the Inbox's "→ {text}" assistant-styled line. The
  // Inbox path never emits ``user_bubble``; ChatThread does.
  | 'user_bubble'
  // PR #116 Phase B — structured tabular content. The agent emits
  // this when its response carries tabular data the user should
  // actually be able to scan + act on (rent aging report,
  // maintenance work-order list, etc.) instead of a wall of
  // markdown bullets. The renderer dispatches to ``TabularContent``
  // (typed table with Badge-rendered status cells, monospaced
  // currency, optional row actions).
  | 'tabular_data'
  // Structured static map. A location tool (``properties_near`` /
  // ``find_places_near_property``) auto-emits this with its geocoded
  // points, so a map renders inline in chat without the model relaying
  // a URL. The renderer dispatches to ``MapCard`` (an <img> pointed at
  // our key-free ``/api/v1/maps/static`` proxy).
  | 'map'
  // Downloadable artifacts a code-execution run produced. ``run_computation``
  // auto-emits this with signed download links, so files it generates (a CSV,
  // an owner statement) render as download buttons inline — no model URL relay.
  // The renderer dispatches to ``ArtifactCard``.
  | 'artifacts'
  // An email draft the agent composed and chose to present as a real email
  // card (To / Subject / body) instead of chat prose. Presentation only —
  // nothing sends without the send tool's confirmation gate. The renderer
  // dispatches to ``EmailDraftCard``.
  | 'email_draft'
  // Gen-UI wave 2 (2026-08-19) — one record's labeled facts, a numeric
  // series, a day's slots, a step plan. All model-composed content; renderers
  // DetailCard / ChartCard / ScheduleCard / ChecklistCard.
  | 'detail_card'
  | 'chart'
  | 'schedule'
  | 'checklist'
  // S6 disclosure envelope (PR #680) — emitted once per reasoning-loop
  // turn, just before ``done``, carrying the "How I got this" receipts
  // (sources / confidence / unverified / specialist trail). The renderer
  // dispatches to ``DisclosurePanel``.
  | 'disclosure'
  // The token + tool events from the existing event surface — Chat
  // primitives reuse the same union for rendering. PR #70 doesn't
  // emit these on the Inbox stream but we type them for forward-
  // compatibility with PR #71.
  | 'token'
  | 'tool_start'
  | 'tool_result'
  | 'done'
  | 'error';

export interface ConversationEvent {
  type: ConversationEventType;
  data: Record<string, unknown>;
  timestamp?: number;
}

/** Narrowed shape for an ``assistant_message_token`` event. */
export interface AssistantTokenData {
  turn_id: string;
  content: string;
}

/** Narrowed shape for ``suggestion_chip``. */
export interface SuggestionChipData {
  action_index: number;
  label: string;
  parameter_patch: Record<string, unknown>;
  is_destructive: boolean;
}

/** Narrowed shape for ``confirm_gate``. */
export interface ConfirmGateData {
  action_index: number;
  summary: string;
  payload_preview: Record<string, unknown>;
  confirm_label: string;
  cancel_label: string;
  /** PR #75 Bug E — server-issued id the frontend POSTs back to
   *  ``/agent/confirm`` to unblock the awaiting tool call. Optional
   *  because the Inbox preview path historically rendered confirm
   *  gates without one (preview-only). */
  confirm_id?: string;
}

/** Narrowed shape for ``user_bubble``. */
export interface UserBubbleData {
  /** The text the user typed. */
  content: string;
}


// ── PR #116 Phase B — Structured tabular data ──────────────────────


/**
 * Status tone vocabulary kept narrow on purpose. Each agent-emitted
 * status string maps to one of these tones via ``TabularStatusValue``;
 * the Badge component then maps each tone to an existing semantic
 * token (--positive / --caution / --danger / neutral).
 *
 * Adding a new tone is deliberate. Don't widen this casually —
 * every renderer downstream switches on this set.
 */
export type TabularStatusTone =
  | 'neutral'
  | 'positive'
  | 'caution'
  | 'danger'
  | 'info';

/** Typed status value — replaces stringly-typed "EVICTION PENDING". */
export interface TabularStatusValue {
  /** What the badge actually says ("Eviction pending", "Late 14d"). */
  label: string;
  /** Maps to a Badge tone (and through that to a semantic token). */
  tone: TabularStatusTone;
}

/**
 * Per-cell tagged-union. Each cell carries its own kind so the
 * renderer dispatches correctly (right-aligned monospaced for
 * currency, Badge for status, ISO-localized for date).
 */
export type TabularCellValue =
  | { kind: 'text'; value: string }
  | { kind: 'currency'; cents: number; currency?: 'USD' }
  | { kind: 'status'; status: TabularStatusValue }
  | { kind: 'date'; iso: string };

export interface TabularColumn {
  key: string;
  label: string;
  align?: 'left' | 'right' | 'center';
}

export interface TabularData {
  /** Domain identifier — lets the renderer (or analytics) special-
   *  case behavior per table kind. Common kinds today:
   *  ``rent_aging``, ``maintenance_list``. Open string so a new
   *  agent-emitted kind doesn't require a frontend ship. */
  kind: string;
  title?: string;
  columns: TabularColumn[];
  /** Each row keyed by column.key → cell. Cells whose key isn't in
   *  ``columns`` are ignored on render. Cells missing from a row
   *  render as an em-dash placeholder. */
  rows: Record<string, TabularCellValue>[];
  /** Optional footnote rendered below the table (e.g. "Demo mode —
   *  no message will actually be sent."). */
  footnote?: string;
}

/** Narrowed shape for ``tabular_data`` ConversationEvent. */
export interface TabularDataPayload {
  data: TabularData;
}

export interface MapMarker {
  lat: number;
  lng: number;
}

/** A static map a location tool chose to render. ``url`` is our key-free
 *  ``/api/v1/maps/static`` proxy path (never the raw provider URL). */
export interface MapData {
  url: string;
  caption?: string;
  markers: MapMarker[];
}

/** Narrowed shape for a ``map`` ConversationEvent. */
export interface MapPayload {
  data: MapData;
}

/** One downloadable file a code-execution run produced. ``url`` is a
 *  short-lived signed link to the artifacts storage bucket. */
export interface ArtifactLink {
  name: string;
  size?: number | null;
  url: string;
}

/** Downloadable artifacts a ``run_computation`` turn produced. */
export interface ArtifactsData {
  artifacts: ArtifactLink[];
}

/** One complete alternative the agent composed — its own label (the
 *  agent's name for the approach), subject, and body. */
export interface EmailDraftVariant {
  label: string;
  subject: string;
  body: string;
}

/** Email draft(s) the agent composed — written during the turn, never a
 *  canned template. 2-4 variants render as slide-through numbered options
 *  (Inbox-stepper style); one renders as a plain email card. Rendered by
 *  EmailDraftCard. */
export interface EmailDraftData {
  to: string[];
  cc: string[];
  variants: EmailDraftVariant[];
}

/** Narrowed shape for an ``email_draft`` ConversationEvent. */
export interface EmailDraftPayload {
  data: EmailDraftData;
}

/** One record's labeled facts (a work order, tenant, lease…) — the model
 *  picks the labels and values for THIS answer. */
export interface DetailCardData {
  title: string;
  subtitle: string;
  status: string;
  tone: 'neutral' | 'good' | 'caution' | 'danger';
  fields: Array<{ label: string; value: string }>;
  footnote: string;
}

/** A single numeric series the model chose to chart. */
export interface ChartData {
  title: string;
  kind: 'bar' | 'line';
  unit: 'currency' | 'percent' | 'count';
  points: Array<{ label: string; value: number }>;
}

/** A day's time slots (showings, vendor visits). */
export interface ScheduleData {
  title: string;
  day: string;
  slots: Array<{ time: string; label: string; state: 'proposed' | 'confirmed' | 'busy' }>;
}

/** A multi-step plan with done-flags — re-emitted as steps complete. */
export interface ChecklistData {
  title: string;
  items: Array<{ text: string; done: boolean }>;
}

/** Narrowed shape for an ``artifacts`` ConversationEvent. */
export interface ArtifactsPayload {
  data: ArtifactsData;
}

/**
 * Next-steps card — the work loop's hand-off (``suggest_next_steps``).
 *
 * ``prompt`` is the exact request sent when a step is clicked, so the user
 * never retypes what the assistant just proposed. ``note_hint`` is the offer
 * to write something down: the decision a manager would otherwise scribble on
 * a pad and lose.
 */
export interface NextStepsData {
  steps: Array<{ label: string; prompt: string }>;
  note_hint?: string;
}

/**
 * A form the assistant showed because the request was too vague to act on.
 *
 * The blanks are visible instead of being asked for in a paragraph, and the
 * ones it already knows arrive prefilled. Submitting composes an ordinary
 * chat message — this card asks, it never writes, so anything it eventually
 * leads to still passes its own confirmation.
 */
export interface FormFieldData {
  name: string;
  label: string;
  kind: 'text' | 'textarea' | 'choice' | 'date';
  value?: string;
  options?: string[];
}

export interface FormData {
  title: string;
  intro?: string;
  submit_label: string;
  fields: FormFieldData[];
}

/**
 * Card events the renderer draws AFTER an assistant turn's prose.
 *
 * ⚠ **This list is the whole reason `next_steps`, `map` and `form` never
 * appeared in chat.** `ChatThread` re-emits card events out of a message's
 * stored `events`, and it did so from FOUR separate hardcoded filters — so a
 * card the backend published, the store kept, and `ConversationThread` knew
 * how to draw was still dropped on the floor between them, silently, because
 * nobody added it to a fifth list. Measured 2026-09-01: `detail_card` and
 * `tabular_data` survived the fold; `next_steps`, `map` and `form` did not.
 *
 * One list now, read by the fold AND by the guard that compares it against
 * what the backend queues (gotcha 28's both-sides rule). Adding a card type
 * means adding it HERE, not in a filter.
 */
export const CARD_EVENT_TYPES = [
  'tabular_data',
  'artifacts',
  'email_draft',
  'detail_card',
  'chart',
  'schedule',
  'checklist',
  'map',
  'form',
  // S6 receipts ("How I got this"). After the answer, before the hand-off —
  // it explains the thing above it. Published on every reasoning-loop turn
  // (orchestrator.py) and, until 2026-09-01, dropped by the fold like the
  // rest, so the panel has never once rendered in chat.
  'disclosure',
  // ⚠ LAST on purpose. The next-steps chips are the hand-off after everything
  // else has been said, so a table or a form rendered under them would read
  // as arriving after the conversation moved on.
  'next_steps',
] as const;

export type CardEventType = (typeof CARD_EVENT_TYPES)[number];

/**
 * ⚠ `memory_recall` is deliberately NOT here. It is published on the same
 * stream and was dropped by the same fold, but it says "I recalled this
 * before answering" — it belongs ABOVE the prose, not in the after-the-answer
 * run. It is emitted from the in-order event loop instead; putting it here
 * would fix the drop and render it in the wrong place.
 */
export function isCardEvent(type: string): type is CardEventType {
  return (CARD_EVENT_TYPES as readonly string[]).includes(type);
}

/**
 * The object a card renderer should draw, from whatever the wire handed over.
 *
 * ⚠ **This exists because `'data' in payload` TOOK CHAT DOWN** (Sentry
 * PYTHON-FASTAPI-3M, 2026-09-07). Eight card branches unwrapped their payload
 * with that expression against a value TYPED as an object, and `in` throws
 * `TypeError` the moment its right operand is a primitive. `tool_result` is a
 * `jsonb` column and asyncpg returns `jsonb` as a **string** with no codec
 * registered — so every hydrated card arrived as JSON TEXT, the first one
 * threw, and the React error boundary replaced the entire conversation with
 * "Occupella hit an error". Nothing was wrong with the card.
 *
 * So this never throws, whatever it is handed:
 *  - a JSON string is parsed (a string that is not JSON yields `null`);
 *  - `{data: …}` is unwrapped, but only to an object — a card whose real
 *    payload has its own `data` FIELD keeps it;
 *  - anything that is not an object at the end yields `null`, and the caller's
 *    existing shape guard drops the card.
 *
 * A dropped card costs one card. A thrown one costs the whole answer.
 */
export function cardPayload(raw: unknown): Record<string, unknown> | null {
  let value = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const outer = value as Record<string, unknown>;
  const inner = outer.data;
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
    return inner as Record<string, unknown>;
  }
  // ⚠ `'data' in outer` was the old test and it is the wrong question: a
  // payload carrying `data: null`, `data: []` or a scalar `data` would unwrap
  // to something no renderer can read. Unwrap only when there is an object
  // underneath; otherwise this IS the payload.
  return outer;
}

/**
 * `tool_name` prefix on a persisted card row (`helixis_chat_messages`).
 *
 * ⚠ Must equal `CARD_HISTORY_PREFIX` in `common/events.py` — the backend
 * writes rows with it and this side hydrates on it, so a drift of one
 * character silently restores nothing and reloading a thread goes back to
 * dropping every card. Pinned across both files by
 * `test_card_event_types.py`.
 */
export const CARD_HISTORY_PREFIX = 'card:';
