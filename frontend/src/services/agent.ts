/**
 * SSE client for streaming agent events from the backend.
 */

import { ApiAuthError, ApiError, apiFetch } from './api';
import { demoMode } from './auth';
import {
  captureConfirmation,
  captureTaskCompleted,
  captureTaskFailed,
  captureTaskSubmitted,
} from './analytics';

export interface HelixisEvent {
  type: string;
  data: Record<string, unknown>;
  timestamp: number;
}

/**
 * When the chat UI is hosted inside the Occupella Electron browser, the preload
 * bridge exposes `window.helixis.page.context()` — the clean main text of the
 * active browser tab (extracted via Mozilla Readability in the main process).
 *
 * We pull it just before a run and attach it as the ADR 0003 context block
 * (`current_page_url` / `current_page_title` / `current_page_text`), so the
 * copilot can answer "what's on my screen". Everywhere else (plain web, the
 * Chrome sidebar) the bridge is absent and this returns `{}`, so the request
 * carries no page context and nothing changes.
 */
interface ElectronPageContext {
  url: string;
  title: string;
  text: string;
}
interface ElectronBridge {
  page?: { context?: () => Promise<ElectronPageContext | null> };
}

async function electronPageContext(): Promise<Record<string, string>> {
  const get = (globalThis as { helixis?: ElectronBridge }).helixis?.page?.context;
  if (typeof get !== 'function') return {};
  try {
    // Never let a slow/hung extraction stall the send — cap the wait.
    const page = await Promise.race([
      get(),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 1500)),
    ]);
    if (!page || !page.text) return {};
    return {
      current_page_url: page.url,
      current_page_title: page.title,
      current_page_text: page.text,
    };
  } catch {
    return {};
  }
}

/** Statuses whose body is written FOR THE USER and must reach them verbatim.
 *
 * - **402** billing — `billing/gate.py` returns three distinct blocks (trial
 *   ended, subscription lapsed, turns exhausted). Each names Settings →
 *   Billing and each asks for a DIFFERENT action, and this is the moment a
 *   trial converts. It is a decision, not a crash.
 * - **429** the daily token budget.
 * - **409** a run is already active in this thread (live-watch session lock).
 *
 * Everything else keeps the "Agent request failed:" framing: a 500 IS a
 * failure, and stripping the prefix would put the backend's error text in the
 * agent's own mouth.
 */
const PASS_THROUGH_STATUSES: ReadonlySet<number> = new Set([402, 409, 429]);

/** How an ApiError from /agent/run is phrased for the user.
 *
 * Extracted as a pure function so the framing decision is testable without
 * driving the whole streaming loop — `runAgent` awaits page context, analytics
 * and a live SSE reader before it can reach this branch, so a test that went
 * through it would be measuring the harness (gotcha 71's lesson, applied in
 * the other direction: run the loop when the failure is invisible behind a
 * 2xx, isolate when the decision is a pure one).
 */
export function agentErrorMessage(status: number, message: string): string {
  return PASS_THROUGH_STATUSES.has(status) ? message : `Agent request failed: ${message}`;
}

export interface RunAgentOptions {
  task: string;
  companyId: string;
  sessionId?: string;
  token: string;
  onEvent: (event: HelixisEvent) => void;
  onError: (error: Error) => void;
  onDone: () => void;
}

async function runDemoAgent({
  task,
  onEvent,
  onError,
  onDone,
}: RunAgentOptions): Promise<AbortController> {
  const controller = new AbortController();

  // ±20% jitter so step timings feel organic, not mechanical. Floor at
  // 120ms so the very-fast steps still register visually.
  const jitter = (ms: number): number => {
    const factor = 0.8 + Math.random() * 0.4;
    return Math.max(120, Math.round(ms * factor));
  };

  const sleep = (ms: number) =>
    new Promise<void>((resolve, reject) => {
      const t = setTimeout(resolve, ms);
      controller.signal.addEventListener('abort', () => {
        clearTimeout(t);
        reject(new DOMException('aborted', 'AbortError'));
      });
    });

  const emit = (type: string, data: Record<string, unknown>) => {
    if (controller.signal.aborted) return;
    onEvent({ type, data, timestamp: Date.now() });
  };

  const streamText = async (text: string) => {
    let i = 0;
    while (i < text.length) {
      if (controller.signal.aborted) return;
      const size = 2 + Math.floor(Math.random() * 4);
      emit('token', { content: text.slice(i, i + size) });
      i += size;
      await sleep(22);
    }
  };

  const plan = buildDemoPlan(task);

  (async () => {
    try {
      await sleep(jitter(380));

      for (const step of plan.steps) {
        if (step.kind === 'tool' || step.kind === 'subagent') {
          // A subagent step is a routed specialist — emit a handoff so the
          // chat activity trail shows "→ <Agent>" (mirrors the real backend
          // handoff event).
          if (step.kind === 'subagent') {
            emit('handoff', { agent: step.tool });
          }
          emit(step.kind === 'tool' ? 'tool_start' : 'subagent_start', {
            tool: step.tool,
            description: step.description,
          });
          await sleep(jitter(step.latencyMs));
          emit(step.kind === 'tool' ? 'tool_result' : 'subagent_result', {
            tool: step.tool,
            result: step.result,
            duration_ms: step.latencyMs,
          });
          await sleep(jitter(160));
        } else if (step.kind === 'thinking') {
          emit('agent_thinking', { text: step.text });
          await sleep(jitter(step.latencyMs));
        }
      }

      await sleep(jitter(200));
      await streamText(plan.reply);
      await sleep(jitter(120));

      // PR #116 Phase B — emit the structured tabular block after the
      // prose reply has streamed. Order matters: the prose reads as
      // "here's what I found", the table delivers the actual rows,
      // so the user gets context before the data structure. Renderer
      // dispatches via ConversationThread's tabular_data case.
      if (plan.tabular) {
        emit('tabular_data', { data: plan.tabular } as Record<string, unknown>);
        await sleep(jitter(80));
      }

      // Email drafts render as real email cards (EmailDraftCard) after the
      // short prose lead-in — mirrors the live show_email_draft flow, where
      // the model composes the drafts and the cards frame them.
      for (const draft of plan.emailDrafts ?? []) {
        emit('email_draft', { data: draft } as Record<string, unknown>);
        await sleep(jitter(120));
      }

      // Wave-2 cards — detail cards, charts, schedules, checklists.
      for (const card of plan.cards ?? []) {
        emit(card.type, { data: card.data } as Record<string, unknown>);
        await sleep(jitter(100));
      }

      // Plans that surface a confirmation card emit a `confirm` event
      // immediately after the streamed reply. The ConfirmCard already
      // renders pendingConfirmation for the user; in demo mode the
      // approve/deny click is best-effort and harmless if the API
      // call fails.
      if (plan.confirm) {
        emit('confirm', {
          confirm_id: `demo-${Date.now()}`,
          action: plan.confirm.action,
          title: plan.confirm.title,
          details: plan.confirm.details,
          items: plan.confirm.items ?? [],
          ...(plan.confirm.requestBody
            ? {
                payload_preview: {
                  request_body: plan.confirm.requestBody,
                  ...(plan.confirm.editableFields
                    ? { editable_fields: plan.confirm.editableFields }
                    : {}),
                },
              }
            : {}),
        });
      }

      emit('done', { summary: plan.reply });
      onDone();
    } catch (err) {
      // Cancel is a normal flow — but ONLY abort is. Swallowing everything
      // here froze the demo silently: any other throw left the composer
      // spinning forever with no error and no done (release audit bug #1).
      if (err instanceof DOMException && err.name === 'AbortError') return;
      onError(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  return controller;
}

// ── Demo plans ───────────────────────────────────────────────────────

type DemoStepKind = 'tool' | 'subagent' | 'thinking';

interface DemoStep {
  kind: DemoStepKind;
  /** Tool/subagent identifier — surfaces in the UI as the card title. */
  tool?: string;
  /** Status line shown while the step is running. */
  description?: string;
  /** Result line shown after the step completes. */
  result?: string;
  /** For 'thinking' steps: the italic narration. */
  text?: string;
  /** Base wall-clock duration; jittered ±20% at runtime. */
  latencyMs: number;
}

interface DemoConfirm {
  action: string;
  title: string;
  details: string;
  items?: string[];
  /** Mirrors the live gate's payload_preview.request_body — lets demo gates
   *  carry the exact outbound payload (e.g. the rendered SMS for the
   *  phone-bubble preview in ConfirmCard). */
  requestBody?: Record<string, unknown>;
  /** Mirrors the live gate's payload_preview.editable_fields
   *  (edit-before-approve): fields of requestBody the approver may rewrite. */
  editableFields?: string[];
}

interface DemoPlan {
  steps: DemoStep[];
  reply: string;
  confirm?: DemoConfirm;
  // PR #116 Phase B — optional structured tabular data. When set,
  // the demo agent emits a ``tabular_data`` event before the prose
  // ``reply`` lands, so the user sees a scannable table (with typed
  // status badges, monospaced currency, etc.) instead of the
  // markdown-bullet block that pre-#116 rendering produced. The
  // typed shape lives in components/conversation/types.ts —
  // ``TabularData``. Imported lazily so this services/* layer
  // stays decoupled from React component types.
  tabular?: unknown;
  /** Email drafts the demo "composed" — emitted as ``email_draft`` events so
   *  they render as EmailDraftCards, mirroring the live show_email_draft
   *  flow. Same shape as components/conversation/types.ts EmailDraftData:
   *  each entry is one card; 2-4 variants slide through Inbox-stepper
   *  style. */
  emailDrafts?: Array<{
    to: string[];
    cc: string[];
    variants: Array<{ label: string; subject: string; body: string }>;
  }>;
  /** Gen-UI wave 2 — typed cards ({type, data}) emitted after the prose,
   *  mirroring the live shared-card-queue flow. type is the SSE event type
   *  (detail_card | chart | schedule | checklist). */
  cards?: Array<{ type: string; data: Record<string, unknown> }>;
}

function buildDemoPlan(task: string): DemoPlan {
  const t = task.toLowerCase();

  // Ordered before late-rent: "text Maria a rent reminder" mentions rent but
  // is a texting ask, so the SMS route must win.
  if (/\btext\b|\bsms\b/.test(t)) {
    return PLAN_TEXT;
  }
  if (/late|overdue|delinquent|past.?due|behind on rent/.test(t)) {
    return PLAN_LATE_RENT;
  }
  if (/maintenance|fix|repair|leak|broken|hvac|plumbing|work order/.test(t)) {
    return PLAN_MAINTENANCE;
  }
  if (/email|respond|reply|inbox|tenant.*message|message.*tenant/.test(t)) {
    return PLAN_EMAIL;
  }
  if (/vacancy|vacant|available|listing|market.*rent|comp/.test(t)) {
    return PLAN_VACANCY;
  }
  if (/owner|report|summary|monthly|this month|noi|p&l/.test(t)) {
    return PLAN_OWNER_REPORT;
  }
  if (/schedule|showing|tour|calendar|availability|prospect/.test(t)) {
    return PLAN_SCHEDULE;
  }
  return PLAN_FALLBACK;
}

const PLAN_LATE_RENT: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'buildium.list_overdue_leases',
      description: 'Pulling overdue ledger from Buildium…',
      result: '3 leases overdue • $4,820 outstanding',
      latencyMs: 700,
    },
    {
      kind: 'subagent',
      tool: 'rent_analyzer',
      description: 'Cross-checking against partial-payment history…',
      result: '1 lease has a payment plan in place — excluded',
      latencyMs: 900,
    },
    {
      kind: 'tool',
      tool: 'buildium.get_tenant_contact',
      description: 'Fetching contacts for 2 tenants…',
      result: 'Got phone + email for Maria Lopez, James Chen',
      latencyMs: 500,
    },
    {
      kind: 'thinking',
      text: 'Drafting reminder messages with payment links…',
      latencyMs: 700,
    },
  ],
  // PR #116 Phase B — the markdown bullets here used to BE the
  // answer; now the structured ``tabular`` block below is. The
  // prose is downgraded to the lead-in line + the demo-mode
  // footnote, since the table carries the actual data.
  reply:
    `I found **2 tenants** who are overdue. ` +
    `I'll send each a friendly reminder with a Buildium pay-link. ` +
    `Approve to send.`,
  // PR #116 Phase B — structured rent table. Replaces what used to
  // render as markdown bullets like "- **Maria Lopez** — $1,840 · 14
  // days late". Each cell is typed (text / currency / status); the
  // status badge ("Late" / "Late 14d") routes through the Badge
  // component's tone system.
  tabular: {
    kind: 'rent_aging',
    title: '2 tenants overdue',
    columns: [
      { key: 'tenant', label: 'Tenant' },
      { key: 'amount', label: 'Total Owed', align: 'right' },
      { key: 'days_late', label: 'Days Late', align: 'right' },
      { key: 'status', label: 'Status', align: 'left' },
    ],
    rows: [
      {
        tenant: { kind: 'text', value: 'Maria Lopez' },
        amount: { kind: 'currency', cents: 184000, currency: 'USD' },
        days_late: { kind: 'text', value: '14 days' },
        status: { kind: 'status', status: { label: 'Late', tone: 'caution' } },
      },
      {
        tenant: { kind: 'text', value: 'James Chen' },
        amount: { kind: 'currency', cents: 218000, currency: 'USD' },
        days_late: { kind: 'text', value: '21 days' },
        status: { kind: 'status', status: { label: 'Late', tone: 'caution' } },
      },
    ],
    footnote: 'Demo mode — no message will actually be sent.',
  },
  confirm: {
    action: 'send_reminders',
    title: 'Send 2 rent reminders?',
    details:
      'Sends a Buildium pay-link reminder to each overdue tenant. ' +
      'Tone is friendly, and every message is shown for approval before it sends.',
    items: ['Maria Lopez · $1,840', 'James Chen · $2,180'],
  },
};

const PLAN_MAINTENANCE: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'webhooks.recent',
      description: 'Scanning new maintenance webhooks…',
      result: '4 unresolved tickets in last 24h',
      latencyMs: 500,
    },
    {
      kind: 'subagent',
      tool: 'triage_agent',
      description: 'Classifying urgency from photos + descriptions…',
      result: '1 emergency (water leak), 2 standard, 1 cosmetic',
      latencyMs: 1200,
    },
    {
      kind: 'tool',
      tool: 'buildium.list_vendors',
      description: 'Pulling preferred-vendor list for plumbing…',
      result: '3 vendors available • 2 within 10mi',
      latencyMs: 600,
    },
    {
      kind: 'thinking',
      text: 'Drafting work-order + tenant comms for the emergency…',
      latencyMs: 700,
    },
  ],
  reply:
    `Triage complete. The water leak at **540 Market #2** is the only ` +
    `emergency.\n\n` +
    `**Recommended vendor:** Northside Plumbing (4.8★, 6mi away, on-call).\n` +
    `**ETA:** 90 minutes.\n\n` +
    `I drafted the tenant heads-up — it's in the approval card below, and ` +
    `you can edit it before it goes out.`,
  confirm: {
    action: 'dispatch_vendor',
    title: 'Dispatch Northside Plumbing + notify tenant?',
    details:
      'Creates a Buildium work order assigned to Northside Plumbing, ' +
      'requests a 90-min ETA, and sends the tenant the message below.',
    items: ['Work order #WO-NEW · 540 Market #2', 'Tenant notification · Riverside 2A'],
    requestBody: {
      body:
        'Hi — a plumber from Northside Plumbing is on the way and should ' +
        'arrive within 90 minutes. They will call before they knock. Reply ' +
        'if you need to delay access.',
    },
    editableFields: ['body'],
  },
};

const PLAN_EMAIL: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'inbox.fetch_recent',
      description: 'Reading inbox…',
      result: '12 messages • 3 awaiting response',
      latencyMs: 400,
    },
    {
      kind: 'subagent',
      tool: 'intent_classifier',
      description: 'Classifying tenant intents…',
      result: '1 lease renewal · 1 noise complaint · 1 maintenance',
      latencyMs: 800,
    },
    {
      kind: 'tool',
      tool: 'memory.recall_context',
      description: 'Pulling history for these tenants…',
      result: 'Found 4 prior interactions',
      latencyMs: 600,
    },
    {
      kind: 'thinking',
      text: 'Drafting responses…',
      latencyMs: 700,
    },
  ],
  reply:
    'Three tenants need replies. For Sarah\u2019s renewal I drafted three ' +
    'approaches — slide through and pick one. The other two are ready as ' +
    'drafted. Approve to send.',
  emailDrafts: [
    {
      to: ['Sarah Chen <sarah.chen@gmail.com>'],
      cc: [],
      variants: [
        {
          label: 'Standard renewal',
          subject: 'Your lease renewal — Unit 4B',
          body:
            'Hi Sarah,\n\nThanks for letting us know early. Your renewal terms ' +
            'are attached — rent stays at $1,850/mo for a 12-month term ' +
            'starting October 1.\n\nHappy to hop on a call if you have ' +
            'questions.\n\nBest,\nDana\nSunrise Property Management',
        },
        {
          label: 'Warmer + thank-you',
          subject: 'We would love to keep you at Unit 4B',
          body:
            'Hi Sarah,\n\nYou have been a wonderful tenant these past two ' +
            'years, and we would love to keep you at Maple Court. Renewal ' +
            'terms are attached — rent stays flat at $1,850/mo.\n\nIf ' +
            'anything about the unit needs attention before you decide, tell ' +
            'me and I will get it handled.\n\nWarmly,\nDana',
        },
        {
          label: 'Offer 18-month term',
          subject: 'Renewal options for Unit 4B — including an 18-month lock',
          body:
            'Hi Sarah,\n\nThanks for reaching out early. Two options this ' +
            'year:\n\n1. 12 months at $1,850/mo (no change)\n2. 18 months at ' +
            '$1,825/mo — a small discount for the longer lock\n\nHappy to ' +
            'walk through either.\n\nBest,\nDana',
        },
      ],
    },
    {
      to: ['Marcus Williams <m.williams@outlook.com>'],
      cc: [],
      variants: [
        {
          label: '',
          subject: 'Re: Noise after 10pm — Unit 7C',
          body:
            'Hi Marcus,\n\nThanks for flagging this. I reached out to the ' +
            'upstairs tenant today and asked them to keep it down after 9pm. ' +
            'I will check back with you in 48 hours to make sure it has ' +
            'improved.\n\nBest,\nDana',
        },
      ],
    },
    {
      to: ['DeAndre Patel <dpatel@yahoo.com>'],
      cc: [],
      variants: [
        {
          label: '',
          subject: 'Work order #WO-2044 confirmed — plumber Thursday 9–11am',
          body:
            'Hi DeAndre,\n\nYour work order #WO-2044 is confirmed. The plumber ' +
            'is scheduled for Thursday between 9 and 11am — they will call ' +
            'ahead before arriving.\n\nBest,\nDana',
        },
      ],
    },
  ],
  confirm: {
    action: 'send_replies',
    title: 'Send 3 tenant replies?',
    details:
      'Sends each of the three drafted replies via the connected Gmail ' +
      'inbox. You can edit any draft in the Inbox before approving it.',
    items: [
      'Sarah Chen · Lease renewal',
      'Marcus Williams · Noise complaint',
      'DeAndre Patel · Maintenance',
    ],
  },
};

// SMS Stage 4 demo — exercises the confirm gate's phone-bubble preview
// (ConfirmCard renders request_body.message as the actual text when the
// gated action is send_text_message). Mirrors the live sms_tools gate:
// last-4 recipient, registered template, exact rendered body.
const PLAN_TEXT: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'sms.lookup_consent',
      description: 'Checking the SMS consent ledger…',
      result: 'Maria Alvarez · consent on file · quiet hours OK (2:14 PM local)',
      latencyMs: 500,
    },
    {
      kind: 'thinking',
      text: 'Filling the rent-reminder template…',
      latencyMs: 500,
    },
  ],
  reply:
    'Maria has SMS consent on file and it is well inside her quiet-hours ' +
    'window. Here is the exact text — approve to send it.',
  confirm: {
    action: 'send_text_message',
    title: 'Send this text message?',
    details:
      'Sends the rent_reminder template to Maria Alvarez. Consent verified; ' +
      'the TCPA quiet-hours check re-runs at send time.',
    items: ['To: Maria Alvarez (···4471)', 'Template: rent_reminder'],
    requestBody: {
      to: 'Maria Alvarez (···4471)',
      template_id: 'rent_reminder',
      message:
        'Occupella: Hi Maria, your rent of $1,840.00 for 128 Lexington Ave #4B ' +
        'is due Sep 1. Reply STOP to unsubscribe, HELP for help.',
    },
  },
};

const PLAN_VACANCY: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'buildium.list_vacancies',
      description: 'Checking vacancy report…',
      result: '2 units available · 1 turning over Aug 1',
      latencyMs: 500,
    },
    {
      kind: 'subagent',
      tool: 'pricing_agent',
      description: 'Comparing rent comps within 1mi…',
      result: 'Avg comp rent $2,140 · current asking $1,950',
      latencyMs: 1500,
    },
    {
      kind: 'thinking',
      text: 'Drafting listing copy + recommendation…',
      latencyMs: 700,
    },
  ],
  reply:
    `Two vacancies, one pricing opportunity:\n\n` +
    `**1208 Oak St #2B** — listed $1,950/mo · *9.7% below comps*. ` +
    `Recommend bumping to **$2,080**.\n` +
    `**540 Market Ave #1** — listed $2,200/mo · *in-line with comps*, no change recommended.\n\n` +
    `**Photo selection:** I'd lead with the kitchen on #2B (best lit, ` +
    `recently renovated) and the corner-unit balcony on #1.\n\n` +
    `Listing copy is drafted and ready to publish to Zillow + Apartments.com. ` +
    `Here's where the #2B turnover stands:`,
  cards: [
    {
      type: 'checklist',
      data: {
        title: 'Lease-up plan — 1208 Oak St #2B',
        items: [
          { text: 'Move-out inspection completed', done: true },
          { text: 'Make-ready work orders closed (paint + carpet)', done: true },
          { text: 'Reprice listing to $2,080 (9.7% below comps today)', done: false },
          { text: 'Publish to Zillow + Apartments.com', done: false },
          { text: 'Schedule first showings', done: false },
        ],
      },
    },
  ],
};

const PLAN_OWNER_REPORT: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'buildium.list_properties',
      description: 'Fetching property roster…',
      result: '12 properties under management',
      latencyMs: 400,
    },
    {
      kind: 'subagent',
      tool: 'financials_agent',
      description: 'Pulling rent collected, expenses, NOI per property…',
      result: 'Aggregated April financials',
      latencyMs: 1400,
    },
    {
      kind: 'subagent',
      tool: 'narrative_agent',
      description: 'Writing owner-friendly highlights…',
      result: '3 highlights · 1 flag (HVAC overrun on 421 Maple)',
      latencyMs: 900,
    },
    {
      kind: 'thinking',
      text: 'Composing the report…',
      latencyMs: 600,
    },
  ],
  reply:
    `### April highlights\n\n` +
    `- **Collections:** $98,420 collected of $98,420 due — **100% on-time** for the second month running.\n` +
    `- **Renewals:** 3 leases renewed on terms; 1 (Patel · #2A) negotiated a 4% bump after the kitchen refresh.\n` +
    `- **Maintenance:** 11 work orders closed · avg age 3.4 days · zero escalations.\n\n` +
    `### Flag\n\n` +
    `- **421 Maple HVAC** — repairs ran $2,140 (budget $1,400). Vendor cited compressor age. ` +
    `I'd recommend full unit replacement next quarter (~$6k, 12-yr life).\n\n` +
    `NOI trend is below. Report ready to email to owners. ` +
    `*Demo mode — no email sent.*`,
  cards: [
    {
      type: 'chart',
      data: {
        title: 'NOI by month',
        kind: 'line',
        unit: 'currency',
        points: [
          { label: 'Nov', value: 61200 },
          { label: 'Dec', value: 60150 },
          { label: 'Jan', value: 62480 },
          { label: 'Feb', value: 63900 },
          { label: 'Mar', value: 64810 },
          { label: 'Apr', value: 67240 },
        ],
      },
    },
    {
      type: 'detail_card',
      data: {
        title: 'April portfolio numbers',
        subtitle: '12 properties · 49 units',
        status: 'On plan',
        tone: 'good',
        fields: [
          { label: 'Collections', value: '$98,420 of $98,420 — 100% on-time' },
          { label: 'Gross revenue', value: '$98,420' },
          { label: 'OpEx', value: '$31,180' },
          { label: 'NOI', value: '$67,240 (+3.7% vs March)' },
          { label: 'Work orders', value: '11 closed · avg age 3.4 days' },
        ],
        footnote: 'Figures from Buildium GL, April close.',
      },
    },
  ],
};

const PLAN_SCHEDULE: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'calendar.fetch_open_slots',
      description: 'Checking open slots in next 7 days…',
      result: '8 open windows · M-F 9-5',
      latencyMs: 400,
    },
    {
      kind: 'subagent',
      tool: 'prospect_matcher',
      description: 'Cross-checking with active prospects…',
      result: '3 prospects waiting on availability',
      latencyMs: 700,
    },
    {
      kind: 'thinking',
      text: 'Drafting outreach with 3 slot options each…',
      latencyMs: 600,
    },
  ],
  reply:
    `Three prospects, three personalized slot offers:\n\n` +
    `- **Alicia Reyes** (#2B) — Tue 11am · Wed 2pm · Thu 4pm\n` +
    `- **Jordan Park** (#1) — Wed 10am · Thu 11am · Fri 1pm\n` +
    `- **Sasha Romero** (#1) — Tue 3pm · Wed 4pm · Fri 11am\n\n` +
    `Each gets a single SMS with a calendar deeplink. First-clicker per ` +
    `unit gets the slot; the others fall back to the next time.`,
  cards: [
    {
      type: 'schedule',
      data: {
        title: 'Showing offers — Unit 2B (Alicia Reyes)',
        day: 'Tue–Thu',
        slots: [
          { time: 'Tue 11:00', label: 'Alicia Reyes · Unit 2B', state: 'proposed' },
          { time: 'Wed 14:00', label: 'Alicia Reyes · Unit 2B', state: 'proposed' },
          { time: 'Wed 15:30', label: 'Vendor walkthrough — blocked', state: 'busy' },
          { time: 'Thu 16:00', label: 'Alicia Reyes · Unit 2B', state: 'proposed' },
        ],
      },
    },
  ],
  confirm: {
    action: 'send_showing_invites',
    title: 'Send 3 showing invitations?',
    details:
      'Sends each prospect a personalized SMS with their three slot ' +
      'options. First-clicker per unit gets the slot.',
    items: ['Alicia Reyes', 'Jordan Park', 'Sasha Romero'],
  },
};

const PLAN_FALLBACK: DemoPlan = {
  steps: [
    {
      kind: 'tool',
      tool: 'memory.search',
      description: 'Checking whether I have seen this before…',
      result: 'No prior matches — treating as a fresh request',
      latencyMs: 500,
    },
    {
      kind: 'subagent',
      tool: 'intent_router',
      description: 'Working out the right tools to chain…',
      result: 'Plan: pull Buildium context · narrate · stop for confirmation',
      latencyMs: 700,
    },
    {
      kind: 'tool',
      tool: 'buildium.list_rentals',
      description: 'Pulling context from the Buildium account…',
      result:
        '14 properties · 49 units · 91.8% occupied · 7 open work orders',
      latencyMs: 1100,
    },
    {
      kind: 'thinking',
      text: 'Synthesising an answer in your tone…',
      latencyMs: 600,
    },
  ],
  reply:
    `Here's a synthesized answer grounded in your Buildium account ` +
    `(14 properties, 49 units, 91.8% occupied). In the real runtime the ` +
    `agent would chain Buildium API calls — tenants, leases, work orders, ` +
    `GL — and pause for confirmation before any destructive action.\n\n` +
    `Try one of the example prompts on the empty hero — *"Who's behind on ` +
    `rent this month?"* or *"What maintenance is unresolved?"* — to see ` +
    `richer multi-step demos.\n\n` +
    `*Demo mode: figures are fabricated but follow Buildium's data shape.*`,
};

export async function runAgent(options: RunAgentOptions): Promise<AbortController> {
  if (demoMode) return runDemoAgent(options);

  // ``token`` is no longer destructured — apiFetch resolves the bearer
  // token internally via getAccessToken so all auth flows through one
  // path. The RunAgentOptions surface keeps `token` on its signature
  // for call-site compatibility.
  const { task, companyId, sessionId, onEvent, onError, onDone } = options;
  const controller = new AbortController();

  // Analytics: wrap the caller's onDone / onError so completion and
  // failure events are captured without touching call sites.
  captureTaskSubmitted(task.length);
  const taskStartTime = Date.now();
  const wrappedOnDone = () => {
    captureTaskCompleted(Date.now() - taskStartTime);
    onDone();
  };
  const wrappedOnError = (error: Error) => {
    captureTaskFailed(error.message);
    onError(error);
  };

  // If we're hosted in the Electron browser, attach the active tab's page
  // context so the copilot can reason about what the user is looking at.
  const pageContext = await electronPageContext();

  let response: Response;
  try {
    // apiFetch attaches the Bearer token, sets JSON content-type, and
    // throws ApiAuthError / ApiError on non-2xx so the streaming reader
    // below only runs against a known-good response.
    response = await apiFetch('/api/v1/agent/run', {
      method: 'POST',
      body: JSON.stringify({
        task,
        company_id: companyId,
        session_id: sessionId,
        ...(Object.keys(pageContext).length > 0 ? { context: pageContext } : {}),
      }),
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof ApiAuthError) {
      wrappedOnError(new Error('Session expired. Please sign in again.'));
      return controller;
    }
    if (err instanceof ApiError) {
      wrappedOnError(new Error(agentErrorMessage(err.status, err.message)));
      return controller;
    }
    if (err instanceof Error && err.name !== 'AbortError') {
      wrappedOnError(err);
    }
    return controller;
  }

  // A dropped POST stream does NOT mean the run died — the turn keeps going
  // server-side, and our turns run to 240s, so a connection blip is routine.
  // Re-POSTing would start a SECOND run (and hit the session lock), so the
  // resume path is GET /agent/watch with the cursor: reattach to the run
  // already in flight and pick up where the stream stopped. Before this, a
  // blip at second 190 lost the whole answer.
  let streamCursor: number | undefined;
  const reattachOrFail = (error: Error) => {
    if (controller.signal.aborted || error.name === 'AbortError') return;
    // Without a session id there is nothing to reattach TO — the server
    // minted one, but this client never learned it. Fail as before.
    if (!sessionId) {
      wrappedOnError(error);
      return;
    }
    void watchSession({
      sessionId,
      onEvent,
      onDone: wrappedOnDone,
      // watchSession exhausts its own retries first; anything reaching here is
      // a real failure, so it still surfaces to the user.
      onError: wrappedOnError,
      after: streamCursor,
      controller,
    });
  };

  pumpSseBody(response, onEvent, wrappedOnDone, reattachOrFail, (seq) => {
    streamCursor = seq;
  });

  return controller;
}

/**
 * Read a streaming SSE response body and dispatch parsed events. Shared by
 * ``runAgent`` (the driver's POST stream) and ``watchSession`` (a teammate
 * attaching read-only via GET /agent/watch) so both consume the exact same
 * wire shape.
 */
function pumpSseBody(
  response: Response,
  onEvent: (event: HelixisEvent) => void,
  onDone: () => void,
  onError: (error: Error) => void,
  onCursor?: (seq: number) => void,
): void {
  try {
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let pendingSeq: number | null = null;

    const read = async () => {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          onDone();
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          // The SSE `id:` field is the resume cursor. Reported only AFTER the
          // matching event is dispatched, so a reconnect never skips an event
          // the user hasn't actually seen.
          if (line.startsWith('id: ')) {
            const seq = Number(line.slice(4).trim());
            if (Number.isFinite(seq)) pendingSeq = seq;
            continue;
          }
          if (line.startsWith('data: ') && line !== 'data: [DONE]') {
            try {
              const event: HelixisEvent = JSON.parse(line.slice(6));
              onEvent(event);
              if (pendingSeq !== null) {
                onCursor?.(pendingSeq);
                pendingSeq = null;
              }
            } catch {
              // Skip malformed events
            }
          }
          if (line === 'data: [DONE]') {
            onDone();
            return;
          }
        }
      }
    };

    read().catch((err: unknown) => {
      // A stream aborted mid-read (turn ended, a new run superseded this one,
      // navigation, unmount) is not a user-facing failure. Chrome surfaces it
      // as an AbortError OR a bare "BodyStreamBuffer was aborted" — swallow
      // both so an aborted stream never lands as an assistant error bubble.
      if (err instanceof Error) {
        if (err.name === 'AbortError') return;
        if (/BodyStreamBuffer was aborted/i.test(err.message)) return;
      }
      onError(err instanceof Error ? err : new Error(String(err)));
    });
  } catch (err) {
    if (err instanceof Error && err.name !== 'AbortError') {
      onError(err);
    }
  }
}

export interface WatchSessionOptions {
  sessionId: string;
  onEvent: (event: HelixisEvent) => void;
  onError: (error: Error) => void;
  onDone: () => void;
  /**
   * Retries already spent. ⚠ Threaded through the recursive re-attach ON
   * PURPOSE: it used to be a fresh `let attempt = 0` in every invocation, so
   * each retry started the budget over and `WATCH_RETRY_DELAYS_MS` could
   * never be exhausted — a backend that was genuinely down got an unbounded
   * 500ms hot loop instead of the "deliberately short" bound its own comment
   * promises. Progress still resets it (see the cursor callback).
   */
  attempt?: number;
  /** Resume cursor — the last SSE `id` already rendered. Omit to replay. */
  after?: number;
  /** Reuse an existing controller (a reattach keeps the caller's abort). */
  controller?: AbortController;
}

/**
 * How many times a dropped watch stream re-attaches before giving up, and how
 * long it waits between tries. Our turns run to 240s, so a connection blip is
 * routine rather than exceptional — but an unbounded retry against a backend
 * that is genuinely down is just a hot loop, so this is deliberately short.
 */
const WATCH_RETRY_DELAYS_MS = [500, 1500, 4000];

/**
 * Attach read-only to a live agent session (multiplayer live watch). The
 * backend replays the session's event log first, then streams live events —
 * same SSE shape as ``runAgent``, so the same store handlers render it.
 * 404 means there's no live run (the persisted-transcript replay the caller
 * already did is the whole story).
 */
export async function watchSession(options: WatchSessionOptions): Promise<AbortController> {
  const { sessionId, onEvent, onError, onDone } = options;
  const controller = options.controller ?? new AbortController();
  // Tracked across reconnects so each retry resumes where the last one
  // stopped. Without this a re-attach replays the turn from event zero, which
  // renders as duplicated output — the reason a naive reconnect looks broken.
  let cursor = options.after;
  let attempt = options.attempt ?? 0;

  /** Re-attempt the ATTACH itself, on the same budget as a stream drop. */
  const retryConnect = (err: Error) => {
    if (controller.signal.aborted) return;
    const delay = WATCH_RETRY_DELAYS_MS[attempt];
    if (delay === undefined) {
      onError(err);
      return;
    }
    setTimeout(() => {
      void watchSession({
        sessionId, onEvent, onError, onDone, after: cursor, controller,
        attempt: attempt + 1,
      });
    }, delay);
  };

  let response: Response;
  try {
    const query = cursor !== undefined ? `?after=${cursor}` : '';
    response = await apiFetch(
      `/api/v1/agent/watch/${encodeURIComponent(sessionId)}${query}`,
      { signal: controller.signal },
    );
  } catch (err) {
    if (err instanceof ApiAuthError) {
      onError(new Error('Session expired. Please sign in again.'));
      return controller;
    }
    if (err instanceof ApiError) {
      if (err.status === 404) {
        // No live run — not an error for the watcher, just nothing to attach.
        onDone();
        return controller;
      }
      onError(new Error(`Could not attach to the live run: ${err.message}`));
      return controller;
    }
    if (err instanceof Error && err.name !== 'AbortError') {
      // ⚠ A failure to CONNECT gets the same bounded retry as a mid-stream
      // drop. It used to surface immediately, and that is what put "Failed to
      // fetch" in front of the user: runAgent's POST stream dies on a blip,
      // reattaches a millisecond later over the SAME sick connection, that one
      // shot fails, and the answer is lost while the backend runs happily on
      // to completion. The blip that killed the POST is exactly the blip most
      // likely to kill the first re-attach.
      retryConnect(err);
    }
    return controller;
  }

  const reattach = (err: Error) => {
    // A deliberate abort (navigation, unmount, a superseding run) is not a
    // drop — never retry it.
    if (controller.signal.aborted || err.name === 'AbortError') return;
    const delay = WATCH_RETRY_DELAYS_MS[attempt];
    if (delay === undefined) {
      // Out of retries. Surface the original failure rather than a synthetic
      // one, so the user sees what actually went wrong.
      onError(err);
      return;
    }
    attempt += 1;
    setTimeout(() => {
      void watchSession({
        sessionId, onEvent, onError, onDone, after: cursor, controller, attempt,
      });
    }, delay);
  };

  pumpSseBody(
    response,
    onEvent,
    onDone,
    reattach,
    (seq) => {
      cursor = seq;
      attempt = 0; // progress resets the budget — a long turn isn't a failure
    },
  );
  return controller;
}

/**
 * How the backend resolved the confirmation (see /agent/confirm):
 *   'resolved' — a live, waiting run received the answer (normal path).
 *   'orphaned' — the run that surfaced this confirm died before the answer
 *                arrived (e.g. a redeploy). The action will NOT run.
 *   'unknown'  — no live run and no pending record (already resolved, or the
 *                durable store is off). Treated the same as orphaned by the UI.
 */
export type ConfirmResolveStatus = 'resolved' | 'orphaned' | 'unknown';

export async function confirmAction(
  confirmId: string,
  approved: boolean,
  // `token` is now resolved internally by apiFetch via getAccessToken;
  // kept on the signature so call sites don't need to update.
  _token: string,
  /** Optional decline reason (approval-pipeline PR) — recorded on the
   *  durable confirmation row for later proposal tuning. Never required. */
  reason?: string,
  /** Edit-before-approve: the user's edited values for gate-declared
   *  editable fields. Sent only when non-empty and only on an approval —
   *  the backend filters against the gate's own allowlist regardless. */
  edits?: Record<string, string>,
): Promise<ConfirmResolveStatus> {
  const trimmed = (reason ?? '').trim();
  const res = await apiFetch('/api/v1/agent/confirm', {
    method: 'POST',
    body: JSON.stringify({
      confirm_id: confirmId,
      approved,
      ...(trimmed ? { reason: trimmed } : {}),
      ...(approved && edits && Object.keys(edits).length ? { edits } : {}),
    }),
  });
  captureConfirmation(approved);
  // Back-compat: a pre-0022 backend omits ``resolve`` → treat as 'resolved'.
  try {
    const data = (await res.json()) as { resolve?: string };
    if (data?.resolve === 'orphaned' || data?.resolve === 'unknown') {
      return data.resolve;
    }
  } catch {
    // Non-JSON / empty body — fall through.
  }
  return 'resolved';
}

export async function cancelAgent(
  sessionId: string,
  _token: string,
): Promise<void> {
  await apiFetch(`/api/v1/agent/cancel/${sessionId}`, { method: 'POST' });
}

/**
 * Structured envelope returned by POST /agent/extract-document — the
 * document agent's tested extraction core (Gemini multimodal). Mirrors
 * the backend ``DocumentExtraction`` model (``document_tools.py``).
 */
export interface DocumentExtraction {
  doc_type: 'lease' | 'invoice' | 'inspection_report' | 'work_order' | 'other';
  confidence: number;
  fields: Record<string, unknown>;
  summary: string;
  warnings: string[];
}

/**
 * Send a single uploaded document (lease / invoice / inspection report /
 * image) to the document agent for structured extraction.
 *
 * The browser has already read the file as base64; we pass it + its MIME
 * type. Read-only on the backend (no write, no confirm gate). Throws
 * ApiError / ApiAuthError on a non-2xx response (415 unsupported type,
 * 502 extraction failure) so the caller can surface a toast.
 */
export async function extractDocument(params: {
  companyId: string;
  base64Content: string;
  mimeType: string;
  filename?: string;
  extractionHint?: string;
}): Promise<DocumentExtraction> {
  const res = await apiFetch('/api/v1/agent/extract-document', {
    method: 'POST',
    body: JSON.stringify({
      company_id: params.companyId,
      base64_content: params.base64Content,
      mime_type: params.mimeType,
      filename: params.filename,
      extraction_hint: params.extractionHint,
    }),
  });
  return (await res.json()) as DocumentExtraction;
}
