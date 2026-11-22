/**
 * Webhooks data layer.
 *
 * Exposes a single surface — listWebhooks, getWebhook, sendReply, executeAction —
 * that the UI calls regardless of mode. In demo mode, data is generated and
 * mutated locally (with a rolling timeline of new events). In production mode,
 * these functions wire to the backend's /api/v1/webhooks endpoints and the
 * platform SDKs (Buildium, AppFolio, Zillow, etc.). The UI contract and state
 * shape are identical — swapping modes is a configuration change.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type WebhookSource =
  | 'buildium'
  | 'appfolio'
  | 'zillow'
  | 'rentmanager'
  | 'yardi';

export type WebhookStatus = 'new' | 'handled' | 'ignored';

export type WebhookEventType =
  | 'maintenance_request'
  | 'lease_application'
  | 'payment_failed'
  | 'payment_received'
  | 'listing_inquiry'
  | 'lease_signed'
  | 'move_out_notice';

export interface WebhookEvent {
  id: string;
  source: WebhookSource;
  eventType: WebhookEventType;
  title: string;
  summary: string;
  property: string;
  tenant?: string;
  applicant?: string;
  amount?: number;
  urgency: 'low' | 'normal' | 'high';
  status: WebhookStatus;
  receivedAt: number;
  payload: Record<string, unknown>;
}

export type ActionKind = 'confirm' | 'follow_up' | 'workflow';

export interface WorkflowStep {
  tool: string;
  description: string;
  result: string;
  latencyMs: number;
}

export interface WebhookActionOption {
  id: string;
  label: string;
  kind: ActionKind;
  destructive: boolean;
  // follow_up: agent asks this question; the user's freeform reply runs
  // the remaining workflow/outcome.
  followUp?: string;
  // workflow: ordered tool calls simulated inside the webhook thread.
  workflow?: WorkflowStep[];
}

export interface WebhookMessage {
  id: string;
  role: 'agent' | 'user';
  content: string;
  timestamp: number;
  options?: WebhookActionOption[];
  // Tool events attached by workflow runs (rendered as cards).
  events?: { type: string; data: Record<string, unknown>; timestamp: number }[];
}

export const SOURCE_LABELS: Record<WebhookSource, string> = {
  buildium: 'Buildium',
  appfolio: 'AppFolio',
  zillow: 'Zillow',
  rentmanager: 'Rent Manager',
  yardi: 'Yardi',
};

// ---- Demo data ---------------------------------------------------------

const DEMO_PROPERTIES = [
  '1208 Oak St',
  '540 Market Ave',
  '88 Pine Ln',
  '2211 Maple Dr',
  '714 Elm Ct',
];

const DEMO_TENANTS = [
  'Mei Chen',
  'Jordan Ortiz',
  'Priya Patel',
  'Sam Rivera',
  'Dana Kowalski',
];

const DEMO_APPLICANTS = [
  'Sarah Kim',
  'Marcus Webb',
  'Elena Rodriguez',
  'Theo Nakamura',
];

function rand<T>(items: T[]): T {
  return items[Math.floor(Math.random() * items.length)];
}

function makeId(): string {
  return `wh_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function initialDemoSeeds(): WebhookEvent[] {
  const now = Date.now();
  const m = (mins: number) => now - mins * 60_000;
  return [
    {
      id: makeId(),
      source: 'buildium',
      eventType: 'maintenance_request',
      title: 'New maintenance request — 1208 Oak St #3A',
      summary: 'HVAC not cooling. Tenant flagged as urgent. Mei Chen.',
      property: '1208 Oak St #3A',
      tenant: 'Mei Chen',
      urgency: 'high',
      status: 'new',
      receivedAt: m(6),
      payload: {
        category: 'HVAC',
        description: 'AC blowing warm air, unit reads 81F at 10am.',
        tenant_phone: '(555) 201-8842',
      },
    },
    {
      id: makeId(),
      source: 'appfolio',
      eventType: 'lease_application',
      title: 'Lease application submitted — unit 4B',
      summary: 'Sarah Kim applied for 540 Market Ave #4B. Credit 742, income 3.4x rent.',
      property: '540 Market Ave #4B',
      applicant: 'Sarah Kim',
      urgency: 'normal',
      status: 'new',
      receivedAt: m(22),
      payload: {
        credit_score: 742,
        monthly_income: 8100,
        prior_evictions: 0,
        employment: 'Designer, 3 yrs',
      },
    },
    {
      id: makeId(),
      source: 'buildium',
      eventType: 'payment_failed',
      title: 'Rent payment failed — J. Ortiz',
      summary: 'ACH returned R01 (insufficient funds). $2,050 due. 540 Market Ave #2.',
      property: '540 Market Ave #2',
      tenant: 'Jordan Ortiz',
      amount: 2050,
      urgency: 'high',
      status: 'new',
      receivedAt: m(48),
      payload: {
        return_code: 'R01',
        attempted_on: '2026-04-18',
        lease_late_fee: 75,
      },
    },
    {
      id: makeId(),
      source: 'zillow',
      eventType: 'listing_inquiry',
      title: 'Listing inquiry from Zillow — 88 Pine Ln #C',
      summary: 'Marcus Webb asked about move-in date and pet policy.',
      property: '88 Pine Ln #C',
      applicant: 'Marcus Webb',
      urgency: 'low',
      status: 'new',
      receivedAt: m(74),
      payload: {
        question:
          'Is the unit still available June 1? Do you allow a 35lb dog?',
        source_listing: 'zillow.com/homedetails/88-pine-ln-c',
      },
    },
    {
      id: makeId(),
      source: 'buildium',
      eventType: 'payment_received',
      title: 'Rent received — M. Chen',
      summary: '$2,150 auto-pay posted. 1208 Oak St #3A. Account current.',
      property: '1208 Oak St #3A',
      tenant: 'Mei Chen',
      amount: 2150,
      urgency: 'low',
      status: 'handled',
      receivedAt: m(140),
      payload: { method: 'ACH', balance_after: 0 },
    },
    {
      id: makeId(),
      source: 'appfolio',
      eventType: 'move_out_notice',
      title: 'Move-out notice — P. Patel',
      summary: '60-day notice received. 2211 Maple Dr #1. Lease ends 6/27/26.',
      property: '2211 Maple Dr #1',
      tenant: 'Priya Patel',
      urgency: 'normal',
      status: 'handled',
      receivedAt: m(220),
      payload: { notice_date: '2026-04-18', reason: 'Relocating for work' },
    },
    {
      id: makeId(),
      source: 'yardi',
      eventType: 'lease_signed',
      title: 'Lease signed — 714 Elm Ct #2',
      summary: 'Dana Kowalski signed 12-month lease. Move-in 5/1/26. $2,400/mo.',
      property: '714 Elm Ct #2',
      tenant: 'Dana Kowalski',
      amount: 2400,
      urgency: 'low',
      status: 'handled',
      receivedAt: m(360),
      payload: { term_months: 12, deposit: 2400 },
    },
    {
      id: makeId(),
      source: 'rentmanager',
      eventType: 'listing_inquiry',
      title: 'Listing inquiry from Rent Manager portal',
      summary: 'Elena Rodriguez — 3BR unit. Looking to tour this weekend.',
      property: '2211 Maple Dr #3',
      applicant: 'Elena Rodriguez',
      urgency: 'normal',
      status: 'ignored',
      receivedAt: m(540),
      payload: { requested_tour: '2026-04-20' },
    },
  ];
}

function rollingDemoEvent(): WebhookEvent {
  const templates: Omit<WebhookEvent, 'id' | 'receivedAt' | 'status'>[] = [
    {
      source: 'buildium',
      eventType: 'maintenance_request',
      title: `New maintenance request — ${rand(DEMO_PROPERTIES)}`,
      summary: `Leaking faucet reported by ${rand(DEMO_TENANTS)}. Non-urgent.`,
      property: rand(DEMO_PROPERTIES),
      tenant: rand(DEMO_TENANTS),
      urgency: 'normal',
      payload: { category: 'Plumbing' },
    },
    {
      source: 'zillow',
      eventType: 'listing_inquiry',
      title: `Listing inquiry from Zillow`,
      summary: `${rand(DEMO_APPLICANTS)} asked about availability and pet policy.`,
      property: rand(DEMO_PROPERTIES),
      applicant: rand(DEMO_APPLICANTS),
      urgency: 'low',
      payload: { question: 'Still available? Pet policy?' },
    },
    {
      source: 'appfolio',
      eventType: 'lease_application',
      title: `Lease application submitted`,
      summary: `${rand(DEMO_APPLICANTS)} applied. Initial screening pending.`,
      property: rand(DEMO_PROPERTIES),
      applicant: rand(DEMO_APPLICANTS),
      urgency: 'normal',
      payload: { credit_score: 700 + Math.floor(Math.random() * 80) },
    },
    {
      source: 'buildium',
      eventType: 'payment_received',
      title: `Rent received — ${rand(DEMO_TENANTS)}`,
      summary: `$${(1800 + Math.floor(Math.random() * 800)).toLocaleString()} auto-pay posted.`,
      property: rand(DEMO_PROPERTIES),
      tenant: rand(DEMO_TENANTS),
      amount: 1800 + Math.floor(Math.random() * 800),
      urgency: 'low',
      payload: { method: 'ACH' },
    },
  ];
  const pick = rand(templates);
  return {
    ...pick,
    id: makeId(),
    receivedAt: Date.now(),
    status: 'new',
  };
}

// ---- Action options per event type --------------------------------------

export function optionsForEvent(event: WebhookEvent): WebhookActionOption[] {
  const tenant = event.tenant ?? event.applicant ?? 'the contact';
  const prop = event.property;

  switch (event.eventType) {
    case 'maintenance_request':
      return [
        {
          id: 'dispatch',
          label: 'Dispatch preferred vendor',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_lookup_unit',
              description: `Loading unit ${prop} and vendor preferences...`,
              result: `Preferred HVAC vendor for ${prop}: Northside Mechanical. Rated 4.8, avg response 3 hrs.`,
              latencyMs: 900,
            },
            {
              tool: 'buildium_get_work_order',
              description: 'Reading the full work order details...',
              result: 'WO-2041: "AC not cooling, unit reads 81F". Urgent. Tenant available after 2pm.',
              latencyMs: 750,
            },
            {
              tool: 'vendor_dispatch',
              description: 'Sending dispatch request to Northside Mechanical...',
              result: 'Accepted. Tech Jamie confirmed for today 2:30–4:30pm. Job #NM-18821.',
              latencyMs: 1600,
            },
            {
              tool: 'twilio_sms',
              description: `Notifying ${tenant} of the appointment window...`,
              result: `SMS delivered to ${tenant}. Acknowledgement received.`,
              latencyMs: 700,
            },
            {
              tool: 'buildium_update_work_order',
              description: 'Updating work order status to In Progress in Buildium...',
              result: 'Work order status: In Progress. Vendor attached. ETA logged.',
              latencyMs: 650,
            },
          ],
        },
        {
          id: 'ask_tenant',
          label: 'Message tenant for more details',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `Based on "${event.payload.description ?? 'AC not cooling'}", I'd ask ${tenant} for:\n\n` +
            `1. Current thermostat setpoint vs. room reading\n` +
            `2. Whether the outdoor condenser fan is spinning\n` +
            `3. When it started (today, or has it been off for a while?)\n` +
            `4. A short photo/video of the unit\n\n` +
            `Want me to send all four, or narrow it down? Reply with numbers (e.g. "1, 3") or ` +
            `describe what to add or remove.`,
          workflow: [
            {
              tool: 'draft_message',
              description: 'Drafting a short, polite tenant message based on your guidance...',
              result:
                'Draft ready. 2 sentences, friendly tone, asks for photo + current temperature, ' +
                'mentions the vendor appointment window.',
              latencyMs: 900,
            },
            {
              tool: 'twilio_sms',
              description: `Sending the message to ${tenant}...`,
              result: `SMS delivered. Will surface their reply back into this thread.`,
              latencyMs: 600,
            },
          ],
        },
        {
          id: 'flag_emergency',
          label: 'Flag as emergency and notify owner',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_update_work_order',
              description: 'Escalating work order priority to Emergency...',
              result: 'Priority: Emergency. SLA shortened to 2 hrs.',
              latencyMs: 500,
            },
            {
              tool: 'buildium_owner_lookup',
              description: 'Resolving the property owner contact...',
              result: `Owner of ${prop}: Kowalski Holdings LLC · Dana Kowalski · (555) 202-4411.`,
              latencyMs: 600,
            },
            {
              tool: 'send_owner_email',
              description: 'Emailing the owner a one-paragraph summary...',
              result: 'Email sent. Subject: "Emergency maintenance — action underway at " + property.',
              latencyMs: 900,
            },
            {
              tool: 'vendor_page',
              description: 'Paging the on-call emergency vendor line...',
              result: 'On-call page acknowledged by Jamie at Northside Mechanical. ETA 45 min.',
              latencyMs: 1100,
            },
          ],
        },
        {
          id: 'schedule_inspection',
          label: 'Schedule inspection first',
          kind: 'follow_up',
          destructive: false,
          followUp:
            `${tenant} said they're home after 2pm weekdays. Your preferred inspector ` +
            `(Jamie at Northside) has open slots:\n\n` +
            `1. Tomorrow 2–4pm\n` +
            `2. Thursday 2–4pm\n` +
            `3. Friday 10am–12pm (tenant would need to accommodate)\n\n` +
            `Pick a slot (reply with the number) or propose a different window.`,
          workflow: [
            {
              tool: 'google_calendar_create',
              description: 'Creating the inspection event on your Google Calendar...',
              result: 'Event created. Link copied to the Buildium work order note.',
              latencyMs: 750,
            },
            {
              tool: 'twilio_sms',
              description: `Sending ${tenant} a reminder with the window...`,
              result: 'SMS delivered. Reminder set for 1 hour before.',
              latencyMs: 500,
            },
          ],
        },
      ];

    case 'payment_failed':
      return [
        {
          id: 'remind',
          label: 'Send tenant a payment reminder',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_lease_ledger',
              description: 'Pulling the lease ledger and current balance...',
              result: `Balance: $${(event.amount ?? 0).toLocaleString()} past due. Auto-pay source: Chase ****2211.`,
              latencyMs: 700,
            },
            {
              tool: 'draft_message',
              description: 'Drafting a reminder with a payment-method update link...',
              result: 'Draft ready. 3-line reminder, friendly tone, includes secure payment link.',
              latencyMs: 800,
            },
            {
              tool: 'gmail_send',
              description: `Emailing ${tenant}...`,
              result: 'Email delivered. Read receipt pending.',
              latencyMs: 500,
            },
          ],
        },
        {
          id: 'retry_ach',
          label: 'Retry ACH in 3 days',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_schedule_retry',
              description: 'Scheduling an ACH retry in Buildium for 3 days out...',
              result: 'Retry scheduled 2026-04-24. Tenant will be notified 24 hrs before.',
              latencyMs: 700,
            },
            {
              tool: 'helixis_task_create',
              description: 'Creating a follow-up task in Occupella to monitor the retry...',
              result: 'Task created. Will ping you if the retry also fails.',
              latencyMs: 500,
            },
          ],
        },
        {
          id: 'late_fee',
          label: 'Start late-fee process per lease terms',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `Lease §6.2 sets a $75 late fee after a 5-day grace. ${tenant} is on **day 3** ` +
            `with **14 prior on-time payments** — so there's a courtesy case here.\n\n` +
            `Options:\n` +
            `1. **Schedule the fee** — auto-apply on day 6 if still unpaid (recommended).\n` +
            `2. **Apply now** — post the fee immediately.\n` +
            `3. **Waive this time** — zero fee, note the courtesy in the ledger.\n\n` +
            `Which one? (Reply with the number.)`,
          workflow: [
            {
              tool: 'buildium_apply_fee',
              description: 'Posting the late fee to the lease ledger...',
              result: 'Late fee of $75 posted per lease §6.2.',
              latencyMs: 700,
            },
            {
              tool: 'buildium_send_notice',
              description: 'Sending the statutory late notice...',
              result: 'Notice queued. Certified mail + email delivery.',
              latencyMs: 900,
            },
          ],
        },
        {
          id: 'escalate',
          label: 'Escalate to property manager',
          kind: 'confirm',
          destructive: false,
        },
      ];

    case 'lease_application':
      return [
        {
          id: 'run_screening',
          label: 'Run full background + credit screening',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_get_applicant',
              description: 'Reading the full applicant record...',
              result: `${tenant}: credit score 742 · income 3.4× rent · employer verified (3 yrs).`,
              latencyMs: 700,
            },
            {
              tool: 'screening_request',
              description: 'Initiating TransUnion SmartMove screening...',
              result: 'Screening initiated. Applicant email sent to complete credit auth.',
              latencyMs: 1100,
            },
            {
              tool: 'eviction_search',
              description: 'Running public-records eviction search...',
              result: 'No eviction filings found in the past 7 years.',
              latencyMs: 900,
            },
            {
              tool: 'helixis_task_create',
              description: 'Creating a follow-up task to review the final report...',
              result: 'Task created. Report typically returns within 2–24 hrs.',
              latencyMs: 400,
            },
          ],
        },
        {
          id: 'approve',
          label: 'Approve and send lease to sign',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `Proposed terms:\n\n` +
            `- **Term:** 12 months\n` +
            `- **Rent:** $2,400/mo\n` +
            `- **Deposit:** $2,400 (1 month)\n` +
            `- **Move-in:** 2026-05-01\n` +
            `- **Template:** your standard residential lease\n\n` +
            `**Two flags I want you to see:**\n` +
            `1. Market rent for this unit is **$2,550** — are you approving at $2,400 intentionally ` +
            `(e.g. to close faster), or match market?\n` +
            `2. ${tenant}'s employment is 3 yrs same company — **no co-signer needed** per your rules.\n\n` +
            `Reply "send as-is", "match market", or describe changes.`,
          workflow: [
            {
              tool: 'buildium_create_lease',
              description: 'Creating the lease in Buildium with your terms...',
              result: 'Lease created. Move-in 2026-05-01. All parties added.',
              latencyMs: 1000,
            },
            {
              tool: 'send_for_signature',
              description: 'Sending the lease for e-signature...',
              result: `Sent to ${tenant}. Reminder scheduled for 48 hrs.`,
              latencyMs: 800,
            },
          ],
        },
        {
          id: 'request_docs',
          label: 'Request additional income documentation',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `Their file already has: employment verification (The Work Number, 3 yrs tenure) ` +
            `and reported income of $8,100/mo. What's missing for your comfort?\n\n` +
            `1. Last 2 pay stubs\n` +
            `2. Offer letter (useful if they're new to the role)\n` +
            `3. Most recent tax return (captures variable income, side work)\n` +
            `4. 3 months of bank statements\n\n` +
            `Reply with numbers (e.g. "1, 3") or describe what you want.`,
          workflow: [
            {
              tool: 'gmail_send',
              description: `Emailing ${tenant} with the doc request...`,
              result: 'Email delivered. Secure upload link included.',
              latencyMs: 700,
            },
          ],
        },
        {
          id: 'decline',
          label: 'Decline application',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `FCRA requires an adverse-action notice citing the specific reason.\n\n` +
            `Looking at this file, here are the defensible reasons available:\n` +
            `1. Credit score below minimum (score 742 — **this one doesn't apply**)\n` +
            `2. Income below 3× rent (3.4× — **doesn't apply either**)\n` +
            `3. Insufficient rental history / references\n` +
            `4. Other (you tell me)\n\n` +
            `Heads-up: 1 and 2 would be a problem — they're not true for this applicant and citing ` +
            `a false reason opens FCRA exposure. Tell me the real reason and I'll phrase it for the notice.`,
          workflow: [
            {
              tool: 'send_adverse_action',
              description: 'Sending the adverse-action notice...',
              result: 'Notice delivered. 60-day report-dispute window noted.',
              latencyMs: 900,
            },
            {
              tool: 'buildium_close_application',
              description: 'Closing the application in Buildium...',
              result: 'Application status: Declined.',
              latencyMs: 500,
            },
          ],
        },
      ];

    case 'listing_inquiry':
      return [
        {
          id: 'reply_avail',
          label: 'Reply with availability and tour link',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'zillow_get_listing',
              description: 'Confirming current availability on the Zillow listing...',
              result: `${prop}: available, next open tour slot Saturday 10am–2pm.`,
              latencyMs: 700,
            },
            {
              tool: 'calendly_generate_link',
              description: 'Generating a Calendly tour link limited to this weekend...',
              result: 'Link created with 30-min slots, Sat–Sun only.',
              latencyMs: 500,
            },
            {
              tool: 'zillow_reply',
              description: `Replying to ${tenant} via Zillow...`,
              result: 'Reply sent. Includes availability, pet policy, tour link.',
              latencyMs: 800,
            },
          ],
        },
        {
          id: 'pre_qualify',
          label: 'Send pre-qualification questions',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'zillow_reply',
              description: 'Sending the standard pre-qualification questionnaire...',
              result: 'Questionnaire sent. Captures income, credit range, move-in date.',
              latencyMs: 700,
            },
          ],
        },
        {
          id: 'archive',
          label: 'Archive — already filled',
          kind: 'confirm',
          destructive: false,
        },
      ];

    case 'payment_received':
      return [
        {
          id: 'confirm_receipt',
          label: 'Send receipt to tenant',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_receipt_pdf',
              description: 'Rendering the payment receipt PDF...',
              result: 'Receipt generated. Includes balance and next due date.',
              latencyMs: 500,
            },
            {
              tool: 'gmail_send',
              description: `Emailing the receipt to ${tenant}...`,
              result: 'Email delivered.',
              latencyMs: 400,
            },
          ],
        },
        {
          id: 'note_ledger',
          label: 'Add note to lease ledger',
          kind: 'confirm',
          destructive: false,
        },
      ];

    case 'lease_signed':
      return [
        {
          id: 'welcome_email',
          label: 'Send welcome + move-in packet',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'helixis_packet_generate',
              description: 'Assembling the move-in packet...',
              result: 'Packet: wi-fi info, trash day, keys pickup, local vendor list.',
              latencyMs: 900,
            },
            {
              tool: 'gmail_send',
              description: `Emailing ${tenant} the welcome packet...`,
              result: 'Email delivered. Read receipt requested.',
              latencyMs: 500,
            },
          ],
        },
        {
          id: 'schedule_walkthrough',
          label: 'Schedule move-in walkthrough',
          kind: 'follow_up',
          destructive: true,
          followUp:
            `Lease starts **2026-05-01**. Standard walkthrough is the same day. ${tenant}'s ` +
            `application preference: weekday mornings.\n\n` +
            `Slots I can book:\n` +
            `1. 5/1 at 10am (move-in day, tenant likely present)\n` +
            `2. 4/30 at 10am (day before — catches issues before keys hand over)\n` +
            `3. 5/2 at 10am (day after — easier for maintenance but tenant already moved in)\n\n` +
            `Pick a number, or propose another time.`,
          workflow: [
            {
              tool: 'google_calendar_create',
              description: 'Creating the walkthrough event...',
              result: 'Event created. Tenant and maintenance invited.',
              latencyMs: 700,
            },
          ],
        },
      ];

    case 'move_out_notice':
      return [
        {
          id: 'ack',
          label: 'Acknowledge notice and schedule final inspection',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_moveout_ack',
              description: 'Logging the move-out notice acknowledgement in Buildium...',
              result: 'Notice acknowledged. Lease end date confirmed.',
              latencyMs: 600,
            },
            {
              tool: 'google_calendar_create',
              description: 'Scheduling the final inspection the day after lease end...',
              result: 'Inspection scheduled. Tenant and maintenance invited.',
              latencyMs: 800,
            },
          ],
        },
        {
          id: 'list_unit',
          label: 'Create listing to re-market the unit',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'buildium_pull_unit_photos',
              description: 'Pulling unit photos and prior listing copy...',
              result: 'Found 12 photos and a 180-word description from the last listing.',
              latencyMs: 900,
            },
            {
              tool: 'zillow_draft_listing',
              description: 'Drafting a fresh Zillow listing...',
              result: 'Draft ready. Priced at $2,500 based on recent comps.',
              latencyMs: 1100,
            },
            {
              tool: 'apartments_draft_listing',
              description: 'Draft syndicating to Apartments.com...',
              result: 'Draft ready on Apartments.com.',
              latencyMs: 600,
            },
          ],
        },
        {
          id: 'deposit_guide',
          label: 'Send security deposit return process',
          kind: 'workflow',
          destructive: true,
          workflow: [
            {
              tool: 'gmail_send',
              description: `Emailing ${tenant} the deposit return process...`,
              result: 'Email delivered. Timeline + deduction rules attached.',
              latencyMs: 600,
            },
          ],
        },
      ];
  }
}

export function initialSummaryFor(event: WebhookEvent): string {
  const src = SOURCE_LABELS[event.source];
  const header = `**${src}** · ${event.property}`;
  const body = demoContextFor(event);
  const prompt = demoPromptFor(event);
  return `${header}\n\n${body}\n\n${prompt}`;
}

function demoContextFor(event: WebhookEvent): string {
  switch (event.eventType) {
    case 'maintenance_request': {
      const who = event.tenant ?? 'the tenant';
      return (
        `**Summary.** ${who} reported: *${event.payload.description ?? event.summary}* ` +
        `Urgency flagged **${event.urgency}**.\n\n` +
        `**Context I pulled.**\n` +
        `- Tenant history: ${who} — 14 consecutive on-time payments, no prior complaints this lease.\n` +
        `- Unit history: last HVAC service Aug 2024 (filter replacement, Northside Mechanical). No open work orders.\n` +
        `- Warranty: parts + labor covered through Jun 2027 — warranty-path saves ~$450 vs cash-pay.\n` +
        `- Weather: outside 84°F and rising. Tenant comfort risk within ~4 hours; Yelp responses degrade quickly past that.`
      );
    }
    case 'payment_failed': {
      const who = event.tenant ?? 'the tenant';
      const amt = event.amount ? `$${event.amount.toLocaleString()}` : 'the full amount';
      return (
        `**Summary.** ACH for ${amt} returned **R01 (insufficient funds)** on 2026-04-18.\n\n` +
        `**Context I pulled.**\n` +
        `- Payment history: **14 consecutive on-time** payments before this — looks like a bank-side blip, not a pattern.\n` +
        `- Lease §6.2: $75 late fee after a **5-day grace**; ${who} is on day 3.\n` +
        `- Auto-pay source on file: Chase ****2211. No recent update.\n` +
        `- ${who} has **not** been notified yet — Buildium suppresses auto-emails on R01 until you review.`
      );
    }
    case 'lease_application': {
      const who = event.applicant ?? 'the applicant';
      return (
        `**Summary.** ${who} applied for ${event.property}.\n\n` +
        `**Context I pulled.**\n` +
        `- Credit score: **742** (above your 700 minimum).\n` +
        `- Income: **$8,100/mo** — **3.4× rent** (above your 3× rule).\n` +
        `- Employment: Designer, 3 yrs same company. Verified via The Work Number.\n` +
        `- Eviction records: none in the past 7 years.\n` +
        `- Market check: unit rents at **$2,400**. Comparable units in-building rent **$2,350–$2,475**.`
      );
    }
    case 'listing_inquiry': {
      const who = event.applicant ?? 'the prospect';
      return (
        `**Summary.** ${who} inquired via ${SOURCE_LABELS[event.source]}.\n\n` +
        `**Context I pulled.**\n` +
        `- Listing: ${event.property} — 14 days on market, 12 inquiries, 3 tours booked.\n` +
        `- Their question: *${event.payload.question ?? 'general availability'}*\n` +
        `- Pet policy on this unit: up to 40 lb with $400 pet deposit.\n` +
        `- Open tour slots this week: Sat 10a–2p, Sun 11a–3p.`
      );
    }
    case 'payment_received': {
      const who = event.tenant ?? 'the tenant';
      const amt = event.amount ? `$${event.amount.toLocaleString()}` : 'payment';
      return (
        `**Summary.** ${amt} from ${who} posted via ACH. Ledger balance: **$0.00**.\n\n` +
        `No action required — this is an informational webhook. ` +
        `Options below are courtesy follow-ups.`
      );
    }
    case 'lease_signed': {
      const who = event.tenant ?? 'the tenant';
      const rent = event.amount ? `$${event.amount.toLocaleString()}/mo` : 'market rent';
      return (
        `**Summary.** ${who} signed for ${event.property} — 12 months at ${rent}. Move-in **2026-05-01**.\n\n` +
        `**Context I pulled.**\n` +
        `- Deposit: $${(event.amount ?? 2400).toLocaleString()} — collected and held in trust.\n` +
        `- First month prorated (19 days): $${Math.round((event.amount ?? 2400) * 19 / 30).toLocaleString()}.\n` +
        `- Tenant insurance: required per lease §4.1 — not yet on file.\n` +
        `- Keys: front-office pickup 9–5, or lockbox code — your call.`
      );
    }
    case 'move_out_notice': {
      const who = event.tenant ?? 'the tenant';
      return (
        `**Summary.** ${who} gave 60-day notice on 2026-04-18. Lease ends **2026-06-27**. Reason: relocating for work.\n\n` +
        `**Context I pulled.**\n` +
        `- Security deposit on file: $2,200.\n` +
        `- State requires deposit return within **21 days** of move-out.\n` +
        `- This is the first turnover in this unit since 2024. Last listed at $2,450; current market comps $2,500–$2,650.\n` +
        `- No open work orders; unit condition at last inspection: good.`
      );
    }
  }
}

function demoPromptFor(event: WebhookEvent): string {
  switch (event.eventType) {
    case 'maintenance_request':
      return `**How should I handle this?**`;
    case 'payment_failed':
      return `**How do you want to respond?** Given ${event.tenant ?? 'their'} history, I'd lean toward a gentle reminder before any fee — but you call it.`;
    case 'lease_application':
      return `**They meet your rules on paper.** What's next?`;
    case 'listing_inquiry':
      return `**Reply options below.** I can also pre-qualify before booking a tour if you prefer that flow.`;
    case 'payment_received':
      return `**Anything to send the tenant?**`;
    case 'lease_signed':
      return `**What's the next move?**`;
    case 'move_out_notice':
      return `**What do you want me to do first?**`;
  }
}

// ---- Data source interface ----------------------------------------------

export function seedWebhooks(): WebhookEvent[] {
  if (demoMode) return initialDemoSeeds();
  // Production: fetch from /api/v1/webhooks. Empty until wired.
  return [];
}

// ── Real-event wiring ────────────────────────────────────────────────
//
// The sidebar Webhooks page reads from this. Today the only inbound
// source is Buildium and the backend endpoint is Buildium-scoped:
// ``GET /api/v1/buildium/recent-events`` returns
// ``[{id, event_name, event_time, status, last_error}]``. We map each
// row to the demo-shaped ``WebhookEvent`` the UI already renders, and
// stamp ``source: 'buildium'`` on the way through.
//
// TODO: replace with unified ``GET /api/v1/webhooks/recent`` once
// AppFolio / Yardi / Zillow land. At that point the mapping moves
// server-side and this function becomes a pass-through.

interface BuildiumRecentEvent {
  id: string;
  event_name: string;
  event_time: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  last_error: string | null;
}

function eventTypeFor(eventName: string): WebhookEventType {
  // Best-effort mapping for ``optionsForEvent`` so the side detail
  // pane has a non-empty option set. Real-event detail content is
  // sparse by design until we expose the raw payload server-side.
  if (eventName.startsWith('Task.') || eventName.startsWith('WorkOrder.')) {
    return 'maintenance_request';
  }
  if (eventName.startsWith('Lease.MoveOut')) return 'move_out_notice';
  if (eventName.startsWith('Lease.') || eventName.startsWith('LeaseTenant.')) {
    return 'lease_signed';
  }
  if (
    eventName.startsWith('LeaseTransaction.') ||
    eventName.startsWith('Bill.') ||
    eventName.startsWith('BankAccount.Transaction.')
  ) {
    return 'payment_received';
  }
  if (
    eventName.startsWith('Applicant.') ||
    eventName.startsWith('ApplicantApplication.')
  ) {
    return 'lease_application';
  }
  return 'maintenance_request';
}

function statusFor(
  backend: BuildiumRecentEvent['status'],
): { status: WebhookStatus; urgency: WebhookEvent['urgency'] } {
  // Backend has 4 lifecycle states; the UI surface has 3. ``failed``
  // is folded into ``new`` with urgent flagging so it pops in the
  // list — the red dot + Urgent chip already render correctly.
  switch (backend) {
    case 'done':
      return { status: 'handled', urgency: 'normal' };
    case 'failed':
      return { status: 'new', urgency: 'high' };
    case 'pending':
    case 'running':
    default:
      return { status: 'new', urgency: 'normal' };
  }
}

function buildiumEventToWebhookEvent(raw: BuildiumRecentEvent): WebhookEvent {
  const { status, urgency } = statusFor(raw.status);
  const receivedAt = Date.parse(raw.event_time);
  return {
    id: raw.id,
    source: 'buildium',
    eventType: eventTypeFor(raw.event_name),
    title: raw.event_name,
    summary:
      raw.last_error ??
      (raw.status === 'done'
        ? 'Webhook delivered and processed.'
        : 'Webhook received from Buildium.'),
    property: '—',
    urgency,
    status,
    receivedAt: Number.isFinite(receivedAt) ? receivedAt : Date.now(),
    payload: raw as unknown as Record<string, unknown>,
  };
}

/**
 * Real-mode fetch for the sidebar Webhooks page. Demo mode short-
 * circuits to the existing seed so investor demos still look
 * populated; production mode pulls from the backend Buildium queue
 * and adapts the rows to the demo-shaped UI contract.
 */
export async function getRecentWebhookEvents(): Promise<WebhookEvent[]> {
  if (demoMode) return initialDemoSeeds();
  const res = await apiFetch('/api/v1/buildium/recent-events');
  const rows = (await res.json()) as BuildiumRecentEvent[];
  if (!Array.isArray(rows)) return [];
  return rows.map(buildiumEventToWebhookEvent);
}

export function startDemoTimeline(
  onNew: (event: WebhookEvent) => void,
): () => void {
  if (!demoMode) return () => {};
  // First injection in 45s, then roughly every 40-90s
  let timer: ReturnType<typeof setTimeout>;
  const tick = () => {
    onNew(rollingDemoEvent());
    timer = setTimeout(tick, 40_000 + Math.random() * 50_000);
  };
  timer = setTimeout(tick, 45_000);
  return () => clearTimeout(timer);
}

/**
 * Execute a selected action for a webhook.
 * Demo mode returns a plausible confirmation; production mode dispatches to
 * the backend which calls the source platform's API.
 */
export async function executeWebhookAction(
  event: WebhookEvent,
  optionId: string,
): Promise<string> {
  const option = optionsForEvent(event).find((o) => o.id === optionId);
  if (!option) return 'Unknown option.';

  if (demoMode) {
    await new Promise((r) => setTimeout(r, 900));
    return demoActionOutcome(event, optionId);
  }

  // Production stub. Wire to backend when available.
  const res = await apiFetch(`/api/v1/webhooks/${event.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ option_id: optionId }),
  });
  const body = (await res.json()) as { message: string };
  return body.message;
}

function demoActionOutcome(event: WebhookEvent, optionId: string): string {
  const tenant = event.tenant ?? event.applicant ?? 'the contact';
  const prop = event.property;
  switch (optionId) {
    case 'dispatch':
      return `Dispatched Northside Mechanical to ${prop}. ETA today 2–4pm. Tenant ${tenant} notified via SMS.`;
    case 'ask_tenant':
      return `Sent ${tenant} a message requesting photos and current thermostat reading. Awaiting reply.`;
    case 'flag_emergency':
      return `Flagged as emergency. Owner notified. On-call vendor paged.`;
    case 'schedule_inspection':
      return `Inspection scheduled for tomorrow 10am with ${tenant}. Added to calendar.`;
    case 'remind':
      return `Payment reminder sent to ${tenant}. Includes link to update payment method.`;
    case 'retry_ach':
      return `ACH retry scheduled for 2026-04-22. Tenant notified of new attempt date.`;
    case 'late_fee':
      return `Late fee of $75 will post per lease §6.2 on 2026-04-24 if unpaid. Notice mailed.`;
    case 'escalate':
      return `Escalated to property manager. Thread flagged in their queue.`;
    case 'run_screening':
      return `Full screening initiated (credit, eviction, employment verification). Results typically 2–24 hrs.`;
    case 'approve':
      return `Application approved. Lease draft sent to ${tenant} for e-signature.`;
    case 'request_docs':
      return `Requested 2 recent pay stubs and proof of employment. Email sent.`;
    case 'decline':
      return `Application declined. Adverse-action notice sent to comply with FCRA.`;
    case 'reply_avail':
      return `Replied to ${tenant} with availability, tour booking link, and pet policy.`;
    case 'pre_qualify':
      return `Pre-qualification questionnaire sent. Captures income, credit range, move-in date.`;
    case 'archive':
      return `Archived. No reply sent.`;
    case 'confirm_receipt':
      return `Receipt emailed to ${tenant}. PDF attached.`;
    case 'note_ledger':
      return `Note added to lease ledger.`;
    case 'welcome_email':
      return `Welcome packet sent — move-in checklist, wi-fi info, trash day. Keys pickup confirmed.`;
    case 'schedule_walkthrough':
      return `Walkthrough scheduled 4/30 3pm. Calendar invite sent to ${tenant} and maintenance.`;
    case 'ack':
      return `Notice acknowledged. Final inspection set for 6/25 10am.`;
    case 'list_unit':
      return `Draft listing created — photos and copy pulled from prior listing. Awaiting your review before going live.`;
    case 'deposit_guide':
      return `Security deposit process guide sent. Includes timeline and deduction rules.`;
    default:
      return `Completed.`;
  }
}
