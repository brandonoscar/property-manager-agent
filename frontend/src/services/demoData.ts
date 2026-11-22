/**
 * Demo-mode fixtures — populated, in-memory, client-only.
 *
 * Demo mode (``resolveDemoMode`` in ``auth.ts``) is gated behind the
 * build-time ``VITE_DEMO_MODE`` flag and defaults OFF. When ON, the
 * page data seams return the fixtures below INSTEAD of calling the
 * backend, so a viewer sees a living, populated app without a Buildium
 * connection.
 *
 * SAFETY INVARIANTS (do not break):
 *   1. This module performs NO network or database I/O. It returns
 *      static objects only. Demo data therefore never lands in any
 *      real table — it cannot, because nothing here writes anywhere.
 *   2. It is only ever invoked from inside an ``if (demoMode)`` branch.
 *      Production runs with ``demoMode === false`` and never reaches
 *      this code, so it cannot affect the real data path.
 *   3. Every record here is obviously synthetic (demo names, the
 *      "(demo)" markers in account info) so it can't be mistaken for
 *      live data if it ever surfaces.
 *
 * The fixtures intentionally show off real product behavior — variable
 * action-card counts (PR #367/#368), the "What Occupella noticed"
 * signals, and an email-ingest row (#365) — so a demo reflects what
 * the product actually does.
 */

import type { ActivityPage, ActivityRow } from './activity';
import type {
  CrmBoard,
  CrmColdLeads,
  CrmLead,
  CrmLeadDraft,
  CrmMessage,
  CrmOverview,
  CrmStage,
  CrmStageLeads,
} from './crm';
import { fuzzInboxRows, fuzzSeedFromSearch } from './fuzzData';
import type { InboxRow } from './inbox';
import type { PropertiesGridResponse, StaffOption } from './properties';

// Fixed timestamps (relative to a stable anchor) so the demo renders
// deterministically and re-renders identically. No Date.now().
const ANCHOR = Date.parse('2026-06-27T14:30:00Z');
const minsAgo = (m: number): string => new Date(ANCHOR - m * 60_000).toISOString();

// ── Inbox ────────────────────────────────────────────────────────
//
// Three rows that together demonstrate: a multi-card maintenance event
// (variable card count), an interpersonal complaint (single
// communication card), and an ingested email (the email feature).

/**
 * Inbox rows for demo mode.
 *
 * Normal demo: the fixed, curated fixtures below. UI-fuzz mode: when a
 * `?fuzz=<seed>` query param is present, the same fixtures are run through
 * ``fuzzInboxRows`` to produce varied edge-case rows (empty / huge / weird /
 * many-actions / null-enrichment / …) so the Playwright sweep can shake out
 * render bugs. The fuzz branch is only reachable in demo mode AND with the
 * explicit query param, so it never affects a normal demo or any real path.
 */
export function demoInboxRows(): InboxRow[] {
  const base = baseDemoInboxRows();
  const seed =
    typeof window !== 'undefined' ? fuzzSeedFromSearch(window.location.search) : null;
  return seed === null ? base : fuzzInboxRows(base, seed);
}

function baseDemoInboxRows(): InboxRow[] {
  return [
    {
      id: 'demo-evt-1',
      event_name: 'Task.Created',
      event_time: minsAgo(8),
      status: 'done',
      last_error: null,
      executed_action_index: null,
      planner_status: 'succeeded',
      planner_error: null,
      summary: {
        status: 'succeeded',
        text: 'Maria Alvarez in unit 4B reported the AC has not been cooling for three days — the third HVAC ticket on this unit in 90 days.',
        error: null,
        attempts: 1,
        last_attempted_at: minsAgo(8),
      },
      enrichment: {
        event_name: 'Task.Created',
        task: {
          id: 4101,
          unit_id: 220,
          property_id: 31,
          assigned_to_user_id: 7,
          assignee_name: 'Dana Whitfield',
          title: 'AC not cooling — unit 4B',
          description:
            'Tenant reports the air conditioning has not been cooling for the past three days. Indoor temperature reading around 81°F in the afternoon.',
          category: 'HVAC',
          priority: 'High',
          status: 'New',
          due_date: null,
          requested_by_first_name: 'Maria',
          requested_by_last_name: 'Alvarez',
        },
        unit: {
          id: 220,
          property_id: 31,
          unit_number: '4B',
          address_line1: '128 Lexington Ave',
        },
        active_lease: {
          id: 9001,
          status: 'Active',
          lease_from_date: '2025-09-01',
          lease_to_date: '2026-08-31',
          tenants: [
            {
              id: 5501,
              first_name: 'Maria',
              last_name: 'Alvarez',
              email: 'maria.alvarez@example.com',
              phone: '+1 (212) 555-0148',
            },
          ],
        },
        history_unit: [
          { id: 3880, title: 'AC weak airflow', category: 'HVAC' },
          { id: 3620, title: 'Thermostat replaced', category: 'HVAC' },
        ],
        history_property: [],
      },
      action_surface: {
        schema_version: 2,
        summary:
          'Maria Alvarez in unit 4B reports the AC has not cooled in three days — the third HVAC ticket here in 90 days.',
        summary_highlights: ['Maria Alvarez', 'unit 4B', 'has not been cooling for three days'],
        signals: [
          'Third HVAC ticket at unit 4B in the last 90 days — possible failing unit, not a one-off.',
          'Reported three days ago; tenant is likely uncomfortable in summer heat.',
          'Lease runs through Aug 2026 — worth resolving well before renewal.',
        ],
        freeform_placeholder: "or tell me what you'd like to do…",
        actions: [
          {
            tool_id: 'send_tenant_message',
            label: 'Message Maria with an ETA',
            intent: 'Acknowledge the AC issue and give Maria a service ETA',
            type: 'information_need',
            confidence: 'high',
            parameters: {
              tenant_id: 5501,
              subject: 'Your AC service',
              body: 'Hi Maria — sorry about the AC. I’m getting a technician scheduled and will confirm a window shortly.',
            },
            primary_action: {
              label: 'Send to Maria',
              kind: 'send_message',
              target: { kind: 'tenant', name: 'Maria Alvarez' },
              draft:
                'Hi Maria — sorry about the AC. I’m getting a technician scheduled and will confirm a window shortly.',
            },
            rationale: 'Three days without cooling in summer — a quick acknowledgment buys goodwill.',
            triggers: ['Reported three days ago', 'High priority HVAC'],
            alternative: 'Could wait until the vendor is booked, but that leaves Maria in the dark.',
          },
          {
            tool_id: 'create_work_order',
            label: 'Open an HVAC work order',
            intent: 'Create a work order to dispatch an HVAC vendor to unit 4B',
            type: 'information_need',
            confidence: 'high',
            parameters: {
              title: 'HVAC service — unit 4B (AC not cooling)',
              description: 'Third HVAC ticket in 90 days; please diagnose for possible compressor/unit failure.',
              priority: 'High',
            },
            primary_action: {
              label: 'Create work order',
              kind: 'create_task',
              target: { kind: 'vendor', name: null },
              draft: null,
            },
            rationale: 'A physical fault needs a vendor on site; the work order is the dispatch path.',
            triggers: ['AC not cooling', 'Recurring HVAC pattern'],
            alternative: 'A to-do task, but a work order routes to the vendor correctly.',
          },
          {
            tool_id: 'add_internal_note',
            label: 'Note the 90-day pattern',
            intent: 'Drop an internal note flagging three HVAC tickets at 4B in 90 days',
            type: 'bonus',
            confidence: 'medium',
            parameters: {
              task_id: 4101,
              body: 'Third HVAC ticket at unit 4B in 90 days (3880, 3620, this). Consider a full unit assessment vs. another patch.',
            },
            primary_action: {
              label: 'Add note',
              kind: 'create_note',
              target: { kind: 'internal', name: null },
              draft:
                'Third HVAC ticket at unit 4B in 90 days. Consider a full unit assessment vs. another patch.',
            },
            rationale: 'The pattern is the real story; future-you will want it recorded.',
            triggers: ['Two prior HVAC tickets on this unit'],
            alternative: 'Schedule an assessment now, but the PM is best placed to time that.',
          },
        ],
      },
    },
    {
      id: 'demo-evt-risk-1',
      event_name: 'Risk.FlagRaised',
      event_time: minsAgo(21),
      status: 'done',
      last_error: null,
      executed_action_index: null,
      planner_status: 'succeeded',
      planner_error: null,
      summary: {
        status: 'succeeded',
        text: 'Occupella found rent charged below the lease on 2 units — $200/mo of missed rent between them.',
        error: null,
        attempts: 1,
        last_attempted_at: minsAgo(21),
      },
      enrichment: {
        event_name: 'Risk.FlagRaised',
        task: {
          id: 4102,
          unit_id: 221,
          property_id: 31,
          assigned_to_user_id: null,
          assignee_name: null,
          title: 'Rent charged below the lease — 2 units',
          description:
            'Automatic books review: lease 24215 (unit 3C) bills $1,700/mo against a $1,850 lease, and lease 24219 (unit 2, Lexington Court) bills $1,400/mo against $1,450. Combined gap: $200/mo.',
          category: 'Accounting',
          priority: 'Normal',
          status: 'New',
          due_date: null,
          requested_by_first_name: null,
          requested_by_last_name: null,
        },
        unit: {
          id: 221,
          property_id: 31,
          unit_number: '3C',
          address_line1: '128 Lexington Ave',
        },
        active_lease: {
          id: 24215,
          status: 'Active',
          lease_from_date: '2025-10-01',
          lease_to_date: '2026-09-30',
          tenants: [
            {
              id: 5503,
              first_name: 'Priya',
              last_name: 'Natarajan',
              email: 'priya.n@example.com',
              phone: '+1 (216) 555-0193',
            },
          ],
        },
        history_unit: [],
        history_property: [],
      },
      action_surface: {
        schema_version: 2,
        summary:
          'The books review found rent billed below the lease on 2 units — $150/mo short on unit 3C and $50/mo on unit 2 — $200/mo of missed rent if both are unintentional.',
        summary_highlights: ['rent billed below the lease', '2 units', '$200/mo'],
        signals: [
          'Lease 24215 (unit 3C) bills $1,700/mo against a $1,850 lease — $150/mo short since October.',
          'Lease 24219 (unit 2) bills $1,400/mo against $1,450 — likely a renewal that never updated the charge.',
          'Neither lease has a concession memo on file — nothing marks these as intentional.',
        ],
        freeform_placeholder: "or tell me what you'd like to do…",
        actions: [
          {
            tool_id: 'buildium_update_recurring_charge',
            label: 'Fix the rent charge on unit 3C',
            intent: 'Raise the recurring rent charge on lease 24215 to the $1,850 lease amount',
            type: 'information_need',
            confidence: 'high',
            parameters: {
              lease_id: 24215,
              amount: 1850,
              memo: 'Correcting rent charge to match lease terms (was $1,700).',
            },
            primary_action: {
              label: 'Update charge to $1,850',
              kind: 'create_task',
              target: { kind: 'internal', name: 'Lease 24215 — unit 3C' },
              draft: null,
            },
            rationale:
              'The lease is the contract; $150/mo compounds to $1,800/yr if the charge stays wrong.',
            triggers: ['Charge below lease on 24215', 'No concession memo'],
            alternative:
              'If the discount is intentional, add a concession memo instead so the books explain themselves.',
          },
          {
            tool_id: 'send_tenant_message',
            label: 'Give Priya notice of the correction',
            intent: 'Tell the tenant the rent charge is being corrected to the lease amount',
            type: 'information_need',
            confidence: 'medium',
            parameters: {
              tenant_id: 5503,
              subject: 'Rent charge correction',
              body: 'Hi Priya — a bookkeeping review found your monthly charge was set below your lease amount. Starting next cycle it will match the lease at $1,850. Nothing is owed retroactively.',
            },
            primary_action: {
              label: 'Send to Priya',
              kind: 'send_message',
              target: { kind: 'tenant', name: 'Priya Natarajan' },
              draft:
                'Hi Priya — a bookkeeping review found your monthly charge was set below your lease amount. Starting next cycle it will match the lease at $1,850. Nothing is owed retroactively.',
            },
            rationale: 'A surprise rent change without notice generates a call; a heads-up prevents it.',
            triggers: ['Charge correction pending'],
            alternative: 'Correct silently — legal, but worse for the relationship.',
          },
          {
            tool_id: 'add_internal_note',
            label: 'Mark unit 2 as intentional',
            intent: 'Record the $50 gap on lease 24219 as a known concession',
            type: 'bonus',
            confidence: 'medium',
            parameters: {
              task_id: 4102,
              body: 'Lease 24219 bills $50 below lease — confirm with the owner whether this is a concession; if so, memo it so the detector stops flagging.',
            },
            primary_action: {
              label: 'Add note',
              kind: 'create_note',
              target: { kind: 'internal', name: null },
              draft:
                'Lease 24219 bills $50 below lease — confirm with the owner whether this is a concession; if so, memo it.',
            },
            rationale: 'Small gaps are sometimes deliberate — the books should say so either way.',
            triggers: ['Second unit in the same finding'],
            alternative: 'Fix it like 3C — but $50 looks more like a concession than a mistake.',
          },
        ],
      },
    },
    {
      id: 'demo-evt-2',
      event_name: 'Task.Created',
      event_time: minsAgo(42),
      status: 'done',
      last_error: null,
      executed_action_index: null,
      planner_status: 'succeeded',
      planner_error: null,
      summary: {
        status: 'succeeded',
        text: 'James Okafor in unit 2A reports recurring late-night noise from the unit above him.',
        error: null,
        attempts: 1,
        last_attempted_at: minsAgo(42),
      },
      enrichment: {
        event_name: 'Task.Created',
        task: {
          id: 4102,
          unit_id: 210,
          property_id: 31,
          assigned_to_user_id: null,
          assignee_name: null,
          title: 'Noise complaint — unit 2A',
          description:
            'Tenant reports loud music and footsteps from the unit above (3A) after midnight, three nights this week. Asking the office to address it.',
          category: 'Tenant Issue',
          priority: 'Normal',
          status: 'New',
          due_date: null,
          requested_by_first_name: 'James',
          requested_by_last_name: 'Okafor',
        },
        unit: { id: 210, property_id: 31, unit_number: '2A', address_line1: '128 Lexington Ave' },
        active_lease: {
          id: 9002,
          status: 'Active',
          lease_from_date: '2025-04-01',
          lease_to_date: '2026-03-31',
          tenants: [
            {
              id: 5502,
              first_name: 'James',
              last_name: 'Okafor',
              email: 'james.okafor@example.com',
              phone: '+1 (212) 555-0199',
            },
          ],
        },
        history_unit: [],
        history_property: [],
      },
      action_surface: {
        schema_version: 2,
        summary:
          'James Okafor in 2A reports late-night noise from 3A above him — three nights this week, and he asked the office to step in.',
        summary_highlights: ['James Okafor', 'unit 2A', 'late-night noise'],
        signals: [
          'Reporter explicitly asked the office to handle it — he expects a response.',
          'Behavioral/interpersonal issue (no physical defect) — this is a communication, not a vendor job.',
          'Three occurrences this week suggests a pattern, not a one-off.',
        ],
        freeform_placeholder: "or tell me what you'd like to do…",
        actions: [
          {
            tool_id: 'send_tenant_message',
            label: 'Acknowledge James',
            intent: 'Reassure James the office is addressing the noise and set expectations',
            type: 'information_need',
            confidence: 'high',
            parameters: {
              tenant_id: 5502,
              subject: 'Following up on the noise',
              body: 'Hi James — thanks for flagging this. I’ll reach out to the unit above about quiet hours and follow up with you.',
            },
            primary_action: {
              label: 'Send to James',
              kind: 'send_message',
              target: { kind: 'tenant', name: 'James Okafor' },
              draft:
                'Hi James — thanks for flagging this. I’ll reach out to the unit above about quiet hours and follow up with you.',
            },
            rationale: 'He asked the office to act; silence would make him feel unheard.',
            triggers: ['Reporter asked office to handle it', 'Three nights this week'],
            alternative: 'Just an internal note, but documenting without replying leaves him hanging.',
          },
        ],
      },
    },
    {
      id: 'demo-evt-3',
      event_name: 'Email.Received',
      event_time: minsAgo(95),
      status: 'done',
      last_error: null,
      executed_action_index: null,
      planner_status: 'succeeded',
      planner_error: null,
      summary: {
        status: 'succeeded',
        text: 'Owner Priya Nair emailed about the Q3 statement and asked to approve a roof repair estimate before month-end.',
        error: null,
        attempts: 1,
        last_attempted_at: minsAgo(95),
      },
      enrichment: {
        event_name: 'Email.Received',
        task: {
          id: 4103,
          unit_id: null,
          property_id: 31,
          assigned_to_user_id: null,
          assignee_name: null,
          title: 'Q3 statement + roof estimate approval',
          description:
            'Hi — thanks for the Q3 statement. Two things: (1) can you confirm the management fee line looks right, it seems higher than Q2? (2) please go ahead and approve the roofing estimate from Apex if it is still around $6,400 — I would like it done before the end of the month. Thanks, Priya',
          category: 'Email',
          priority: 'Normal',
          status: 'New',
          due_date: null,
          requested_by_first_name: 'Priya',
          requested_by_last_name: 'Nair',
        },
        unit: null,
        active_lease: null,
        history_unit: [],
        history_property: [],
      },
      action_surface: {
        schema_version: 2,
        summary:
          'Owner Priya Nair replied on the Q3 statement: she questions the management-fee line and wants the ~$6,400 Apex roof estimate approved before month-end.',
        summary_highlights: ['Priya Nair', 'roof repair estimate', 'before month-end'],
        signals: [
          'Deadline: she wants the roof repair approved before the end of the month.',
          'Dollar figure to confirm: the Apex estimate at ~$6,400.',
          'She flagged the management fee as higher than Q2 — worth checking before replying.',
        ],
        freeform_placeholder: 'edit the reply, or tell me what to change…',
        actions: [
          {
            tool_id: 'reply_to_email',
            label: 'Reply to Priya',
            intent: 'Confirm the fee line, approve the roof estimate, and set a timeline',
            type: 'information_need',
            confidence: 'high',
            parameters: {
              body: 'Hi Priya — thanks for reviewing the Q3 statement. I’ll double-check the management fee line against Q2 and send a short note if anything looks off. On the roof: I’ll approve the Apex estimate at $6,400 and aim to have it scheduled before month-end. I’ll confirm the date once it’s booked.',
            },
            primary_action: {
              label: 'Review & send',
              kind: 'send_message',
              target: { kind: 'owner', name: 'Priya Nair' },
              draft:
                'Hi Priya — thanks for reviewing the Q3 statement. I’ll double-check the management fee line against Q2 and send a short note if anything looks off. On the roof: I’ll approve the Apex estimate at $6,400 and aim to have it scheduled before month-end. I’ll confirm the date once it’s booked.',
            },
            rationale: 'She asked two clear things with a deadline; one reply keeps it moving.',
            triggers: ['Approval request with month-end deadline', 'Fee question'],
            alternative: 'Reply only about the roof, but that leaves her fee question unanswered.',
          },
        ],
      },
    },
  ];
}

// ── Activity feed ────────────────────────────────────────────────

export function demoActivityPage(): ActivityPage {
  const rows: ActivityRow[] = [
    {
      id: 'demo-act-1',
      actor: { id: 'demo-user', name: 'You', email: 'demo@occupella.local', is_self: true },
      source: 'chat',
      source_label: 'Chat',
      action_type: 'message_sent',
      description: 'Sent Maria Alvarez an ETA for the AC service in unit 4B',
      property: '128 Lexington Ave',
      tenant: 'Maria Alvarez',
      created_at: minsAgo(7),
    },
    {
      id: 'demo-act-2',
      actor: { id: 'demo-user', name: 'You', email: 'demo@occupella.local', is_self: true },
      source: 'webhook',
      source_label: 'Inbox',
      action_type: 'work_order_created',
      description: 'Opened an HVAC work order for unit 4B',
      property: '128 Lexington Ave',
      tenant: null,
      created_at: minsAgo(6),
    },
    {
      id: 'demo-act-3',
      actor: { id: 'demo-teammate', name: 'Dana Whitfield', email: 'dana@example.com', is_self: false },
      source: 'chat',
      source_label: 'Chat',
      action_type: 'note_added',
      description: 'Added an internal note about recurring HVAC tickets at 4B',
      property: '128 Lexington Ave',
      tenant: null,
      created_at: minsAgo(33),
    },
    {
      id: 'demo-act-4',
      actor: { id: 'demo-teammate', name: 'Dana Whitfield', email: 'dana@example.com', is_self: false },
      source: 'webhook',
      source_label: 'Inbox',
      action_type: 'message_sent',
      description: 'Replied to owner Priya Nair about the Q3 statement',
      property: '128 Lexington Ave',
      tenant: null,
      created_at: minsAgo(88),
    },
  ];
  return { rows, next_cursor: null };
}

// ── Properties grid ──────────────────────────────────────────────

export function demoPropertiesGrid(): PropertiesGridResponse {
  return {
    default_row: {
      assignments: {
        maintenance: { action_type: 'assign_to_user', buildium_user_id: 7 },
        leasing: { action_type: 'assign_to_user', buildium_user_id: 8 },
      },
    },
    properties: [
      {
        buildium_property_id: 31,
        name: 'Lexington Court',
        address: '128 Lexington Ave',
        assignments: {
          maintenance: { action_type: 'assign_to_user', buildium_user_id: 7 },
          leasing: { action_type: 'assign_to_user', buildium_user_id: 8 },
        },
      },
      {
        buildium_property_id: 32,
        name: 'Harbor View Apartments',
        address: '54 Wharf St',
        assignments: {
          maintenance: { action_type: 'assign_to_user', buildium_user_id: 9 },
        },
      },
      {
        buildium_property_id: 33,
        name: 'Maple Row Duplexes',
        address: '907 Maple Row',
        assignments: {},
      },
    ],
    page: 1,
    page_size: 25,
    total: 3,
  };
}

export function demoStaffOptions(): StaffOption[] {
  return [
    { buildium_user_id: 7, display_name: 'Dana Whitfield', email: 'dana@example.com', role: 'Property Manager' },
    { buildium_user_id: 8, display_name: 'Leo Park', email: 'leo@example.com', role: 'Leasing Agent' },
    { buildium_user_id: 9, display_name: 'Sam Reyes', email: 'sam@example.com', role: 'Maintenance Lead' },
  ];
}

// ── CRM (self-updating pipeline) ─────────────────────────────────
//
// A preview board for the "Coming soon" CRM section. Six leads spread
// across the five stages, each carrying the *fact* Occupella observed to
// move it there — a drafted reply (New), a sent reply (Contacted), a
// calendar tour (Scheduled), a Buildium application/lease
// (Applied/Leased). The activity feed is the running log of those moves.
// ``enabled: false`` marks the whole thing as not-yet-live.
export function demoCrmBoard(): CrmBoard {
  return {
    enabled: false,
    leads: [
      {
        id: 'demo-lead-jordan',
        name: 'Jordan Reyes',
        initials: 'JR',
        kind: 'renter',
        source: 'zillow',
        stage: 'new',
        status: 'active',
        unit: 'Maple Court 4B',
        phone: '+14155550111',
        note: 'Asked if 4B is still available — could tour this weekend.',
        reason: { kind: 'draft', text: "Reply written. Occupella won't send without you.", at: '2h' },
        needsYou: true,
        calendarLabel: null,
        movedAt: null,
      },
      {
        id: 'demo-lead-elena',
        name: 'Elena Duarte',
        initials: 'ED',
        kind: 'renter',
        source: 'zillow',
        stage: 'contacted',
        status: 'active',
        unit: 'Maple Court 4B',
        email: 'elena.duarte@example.com',
        note: 'Pet policy & parking answered — awaiting her reply.',
        reason: { kind: 'reply_sent', text: 'Moved when your reply sent.', at: '1d' },
        needsYou: false,
        calendarLabel: null,
        movedAt: null,
      },
      {
        id: 'demo-lead-renee',
        name: 'Renee Bishop',
        initials: 'RB',
        kind: 'owner',
        source: 'web_form',
        stage: 'contacted',
        status: 'active',
        unit: 'Owner · 6 doors',
        note: 'Unhappy with current PM. Intro + rates one-pager sent.',
        reason: { kind: 'reply_sent', text: 'Intro + rates one-pager sent.', at: '3h' },
        needsYou: false,
        calendarLabel: null,
        movedAt: null,
      },
      {
        id: 'demo-lead-priya',
        name: 'Priya Nair',
        initials: 'PN',
        kind: 'renter',
        source: 'apartments',
        stage: 'scheduled',
        status: 'active',
        unit: 'Riverside 2A',
        phone: '+14155550137',
        note: 'Toured interest — tour now booked.',
        reason: {
          kind: 'calendar',
          text: 'Tour booked on your Google Calendar → moved here on its own.',
          at: '8:02a',
        },
        needsYou: false,
        calendarLabel: 'Tour · Sat 2:00p',
        tourAt: '2026-07-11T14:00:00',
        movedAt: '8:02a',
      },
      {
        id: 'demo-lead-sam',
        name: 'Sam Okafor',
        initials: 'SO',
        kind: 'owner',
        source: 'referral',
        stage: 'applied',
        status: 'active',
        unit: 'Owner · 3 doors',
        note: 'Application in — deciding on terms.',
        reason: { kind: 'buildium', text: 'Application received → moved here.', at: 'yst' },
        needsYou: false,
        calendarLabel: null,
        movedAt: null,
      },
      {
        id: 'demo-lead-leo',
        name: 'Leo Franklin',
        initials: 'LF',
        kind: 'renter',
        source: 'apartments',
        stage: 'leased',
        status: 'active',
        unit: 'Riverside 3C',
        note: 'Lease signed — closed automatically.',
        reason: { kind: 'buildium', text: 'Lease signed → moved & closed automatically.', at: '9:14a' },
        needsYou: false,
        calendarLabel: null,
        movedAt: '9:14a',
      },
    ],
    activity: [
      {
        id: 'demo-act-1',
        leadName: 'Leo Franklin',
        toStage: 'leased',
        kind: 'buildium',
        text: 'lease signed · Buildium · 9:14a',
      },
      {
        id: 'demo-act-2',
        leadName: 'Priya Nair',
        toStage: 'scheduled',
        kind: 'calendar',
        text: 'tour added · your calendar · 8:02a',
      },
      {
        id: 'demo-act-3',
        leadName: 'Renee Bishop',
        toStage: 'contacted',
        kind: 'reply_sent',
        text: 'intro sent · 3h ago',
      },
      {
        id: 'demo-act-4',
        leadName: 'Sam Okafor',
        toStage: 'applied',
        kind: 'buildium',
        text: 'application in · Buildium · yst',
      },
      {
        id: 'demo-act-5',
        leadName: 'Elena Duarte',
        toStage: 'contacted',
        kind: 'reply_sent',
        text: 'reply sent · yst',
      },
      {
        id: 'demo-act-6',
        leadName: 'Jordan Reyes',
        toStage: null,
        kind: 'inquiry',
        text: 'Zillow inquiry · flagged for reply',
      },
    ],
  };
}

// Demo SMS threads per lead, so the detail drawer shows a real
// conversation. Only a couple of leads have history; the rest are
// first-touch (empty thread). Keyed by the lead id from demoCrmBoard().
const DEMO_LEAD_THREADS: Record<string, CrmMessage[]> = {
  'demo-lead-jordan': [
    {
      id: 'demo-msg-j0',
      direction: 'inbound',
      channel: 'call',
      body: '📞 Inbound call',
      created_at: minsAgo(180),
    },
    {
      id: 'demo-msg-jvm',
      direction: 'inbound',
      channel: 'voicemail',
      body: '📞 Voicemail · 0:38',
      recording_url: 'https://example.com/demo-voicemail.mp3',
      created_at: minsAgo(179),
    },
    {
      id: 'demo-msg-j1',
      direction: 'inbound',
      channel: 'sms',
      body: 'Hi! Is Maple Court 4B still available? I could tour this weekend.',
      created_at: minsAgo(120),
    },
  ],
  'demo-lead-priya': [
    {
      id: 'demo-msg-p1',
      direction: 'inbound',
      channel: 'sms',
      body: 'Saw the Riverside 2A listing — can I come see it?',
      created_at: minsAgo(4320),
    },
    {
      id: 'demo-msg-p2',
      direction: 'outbound',
      channel: 'sms',
      body: 'Absolutely — I have Saturday 2pm or Sunday 11am open. Which works?',
      created_at: minsAgo(4300),
    },
    {
      id: 'demo-msg-p3',
      direction: 'inbound',
      channel: 'sms',
      body: 'Saturday 2pm is perfect, thank you!',
      created_at: minsAgo(4290),
    },
  ],
};

/** Demo conversation for a lead (empty for first-touch leads). */
export function demoLeadMessages(leadId: string): CrmMessage[] {
  return DEMO_LEAD_THREADS[leadId] ?? [];
}

// Demo AI assist per lead — the SAME shape the backend /draft route returns
// (signals: string[], draft_reply: string | null), so the drawer UI is
// validated against the real contract.
const DEMO_LEAD_DRAFTS: Record<string, CrmLeadDraft> = {
  'demo-lead-jordan': {
    signals: [
      'Asking if Maple Court 4B is still available',
      'Wants to tour this weekend',
      'Renter — came in via Zillow',
    ],
    draft_reply:
      "Hi Jordan! Yes, 4B is still available — I'd be happy to show it to you this weekend. " +
      'I have Saturday at 2pm or Sunday at 11am open. Which works better for you?',
  },
  'demo-lead-priya': {
    signals: [
      'Toured Riverside 2A on Tuesday',
      'Confirmed Saturday 2pm tour',
      'Renter — came in via Apts.com',
    ],
    draft_reply:
      "Hi Priya! Looking forward to seeing you Saturday at 2pm at Riverside 2A. " +
      "I'll send a reminder the morning of — let me know if anything changes.",
  },
  'demo-lead-renee': {
    signals: [
      'Owns 6 doors, unhappy with current PM',
      'Owner lead — came in via web form',
      'Shopping for new management',
    ],
    draft_reply:
      "Hi Renee, thanks for reaching out. I'd love to learn about your 6 units and how we can help. " +
      'Do you have 15 minutes this week for a quick call?',
  },
};

/** Demo AI assist for a lead (empty for leads without a drafted assist). */
export function demoLeadDraft(leadId: string): CrmLeadDraft {
  return DEMO_LEAD_DRAFTS[leadId] ?? { signals: [], draft_reply: null };
}

// A wider demo pool (the 6 board leads + more) so the overview + stage lists
// look realistic. Compact helper fills the presentational defaults.
function crmDemoLead(p: {
  id: string;
  name: string;
  stage: CrmStage;
  source: CrmLead['source'];
  kind?: CrmLead['kind'];
  unit?: string | null;
  phone?: string | null;
  note?: string;
  draftReady?: boolean;
}): CrmLead {
  const initialsFrom = (name: string): string => {
    const parts = name.trim().split(/\s+/);
    return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '#';
  };
  return {
    id: p.id,
    name: p.name,
    initials: initialsFrom(p.name),
    kind: p.kind ?? 'renter',
    source: p.source,
    stage: p.stage,
    status: 'active',
    unit: p.unit ?? null,
    phone: p.phone ?? null,
    note: p.note ?? '',
    reason: { kind: 'inquiry', text: '', at: null },
    needsYou: Boolean(p.draftReady),
    calendarLabel: null,
    movedAt: null,
  };
}

const CRM_EXTRA_LEADS: CrmLead[] = [
  crmDemoLead({ id: 'demo-l-marcus', name: 'Marcus Webb', stage: 'new', source: 'apartments', unit: 'Oakline 7', note: 'Move-in Aug 1', draftReady: true }),
  crmDemoLead({ id: 'demo-l-noah', name: 'Noah Kim', stage: 'new', source: 'zillow', unit: 'Maple Court 9', note: 'Budget ~$1,800, wants 2 bed', draftReady: true }),
  crmDemoLead({ id: 'demo-l-carla', name: 'Carla Gray', stage: 'new', source: 'referral', unit: 'Riverside 5C', note: 'Referred by a current tenant' }),
  crmDemoLead({ id: 'demo-l-tara', name: 'Tara Nguyen', stage: 'contacted', source: 'sms', unit: 'Riverside 5C', phone: '+17045550190', note: 'Asked about parking' }),
  crmDemoLead({ id: 'demo-l-omar', name: 'Omar Haddad', stage: 'contacted', source: 'zillow', unit: 'Oakline 3', note: 'Replied, comparing units' }),
  crmDemoLead({ id: 'demo-l-chris', name: 'Chris Gale', stage: 'scheduled', source: 'referral', unit: 'Oakline 3', note: 'Tour Sunday 11am' }),
  crmDemoLead({ id: 'demo-l-ben', name: 'Ben Lowe', stage: 'applied', source: 'zillow', unit: 'Riverside 2A', note: 'Application submitted' }),
  crmDemoLead({ id: 'demo-l-ada', name: 'Ada Gomez', stage: 'leased', source: 'zillow', unit: 'Maple Court 4B', note: 'Lease signed' }),
  crmDemoLead({ id: 'demo-l-rob', name: 'Rob Tan', stage: 'leased', source: 'referral', unit: 'Oakline 3', note: 'Lease signed' }),
];

/** Full demo lead pool = the board's 6 + the extras. Single source for the
 *  overview counts + the stage lists. */
function crmDemoPool(): CrmLead[] {
  return [...demoCrmBoard().leads, ...CRM_EXTRA_LEADS];
}

const CRM_STAGE_LABELS: Record<CrmStage, string> = {
  new: 'New',
  contacted: 'Contacted',
  scheduled: 'Scheduled',
  applied: 'Applied',
  leased: 'Leased',
};
const CRM_STAGE_ORDER: CrmStage[] = ['new', 'contacted', 'scheduled', 'applied', 'leased'];

/** Demo overview: per-stage counts + 3 recent per stage + analytics —
 *  derived from the pool so demo stays internally consistent. */
export function demoCrmOverview(): CrmOverview {
  const pool = crmDemoPool();
  const stages = CRM_STAGE_ORDER.map((key) => {
    const inStage = pool.filter((l) => l.stage === key);
    return { key, label: CRM_STAGE_LABELS[key], count: inStage.length, recent: inStage.slice(0, 3) };
  });
  const total = pool.length;
  const leased = pool.filter((l) => l.stage === 'leased').length;
  return {
    stages,
    metrics: {
      new_this_month: pool.filter((l) => l.stage === 'new').length + 22,
      new_prev_month: 30,
      replies_this_month: 28,
      active: total - leased,
      leased,
      total,
    },
    cold_count: demoColdLeads().count,
  };
}

/** Demo going-cold leads — a stable subset of the pool (active, not leased)
 *  to illustrate the "needs a nudge" surface. */
export function demoColdLeads(): CrmColdLeads {
  const ids = new Set(['demo-lead-elena', 'demo-l-omar', 'demo-l-chris']);
  const leads = crmDemoPool().filter((l) => ids.has(l.id));
  return { leads, count: leads.length, threshold_days: 3 };
}

/** Demo stage list: leads in one stage, optionally filtered by query. */
export function demoStageLeads(stage: CrmStage, q?: string): CrmStageLeads {
  let leads = crmDemoPool().filter((l) => l.stage === stage);
  if (q && q.trim()) {
    const needle = q.trim().toLowerCase();
    leads = leads.filter(
      (l) =>
        l.name.toLowerCase().includes(needle) ||
        (l.unit ?? '').toLowerCase().includes(needle) ||
        (l.phone ?? '').includes(needle) ||
        l.note.toLowerCase().includes(needle),
    );
  }
  return { leads, has_more: false };
}

// NOTE: Settings/connectors already ships its own demo seed
// (``demoSeed`` in services/connectors.ts), so demo connectors are NOT
// defined here — that would be a second, divergent source of truth.
