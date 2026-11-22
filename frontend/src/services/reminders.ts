/**
 * Inbox reminders — neglect-aware desk cards.
 *
 * Mirrors `/api/v1/reminders`. Cards are produced server-side by the
 * worker sweep; the client only lists them and applies the three user
 * actions (snooze / dismiss / handled). Demo mode serves canned cards.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export interface Reminder {
  id: string;
  producer: string;
  entity_key: string;
  status: string;
  severity: 'low' | 'normal' | 'high';
  title: string;
  body: string | null;
  link_path: string | null;
  payload: Record<string, unknown>;
  snoozed_until: string | null;
  first_fired_at: string | null;
  last_fired_at: string | null;
  /**
   * How loud this card has become. The server (`reminders/aging.py`) raises it
   * for the time the card has sat there, so the client never re-derives the
   * ladder — two copies of a threshold is how the two ends drift while both
   * look right.
   *
   * Optional because a backend older than this field is a 200 with the key
   * absent, and reminders are an enhancement rather than load-bearing.
   */
  escalation?: number;
  /** When somebody last claimed they had dealt with this outside Helixis. */
  handled_at?: string | null;
}

const DAY_MS = 86_400_000;

function wholeDaysSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const days = Math.floor((Date.now() - Date.parse(iso)) / DAY_MS);
  // `Date.parse` of junk gives NaN, and every comparison against NaN is
  // false — so `days >= 1` rejects a malformed timestamp on its own, and a
  // future one too.
  //
  // ⚠ There WAS a `Number.isFinite(days) &&` in front of this, and a comment
  // explaining why it had to come first. A mutation deleting it changed no
  // output for any input: `Date.parse` returns NaN or a finite number, never
  // ±Infinity, so the range check already covers it. That is the code being
  // redundant rather than the test being blind (gotcha 58) — keeping it would
  // have told the next reader a distinction exists where none does. The
  // malformed-date tests stay; they pin behaviour that is real.
  return days >= 1 ? days : null;
}

/**
 * The one line that tells a PM this card is not new.
 *
 * Two facts, and the handled one wins because it is the more surprising:
 * "you told us this was dealt with and it still is not" is the thing a person
 * needs to know, and it is precisely the case that used to be invisible —
 * `handled` silenced a card permanently and nothing ever said so.
 *
 * Returns null for an ordinary fresh card, so the strip is unchanged for the
 * cards that are behaving.
 */
export function reminderDeskNote(
  rem: Pick<Reminder, 'escalation' | 'first_fired_at' | 'handled_at'>,
): string | null {
  const handledDays = wholeDaysSince(rem.handled_at);
  if (handledDays !== null) {
    return `Marked handled ${handledDays} day${handledDays === 1 ? '' : 's'} ago — still open`;
  }
  // The DECISION to say anything is the server's; the client only renders the
  // number. Re-deriving the thresholds here would put the ladder in two
  // languages and let them disagree silently.
  if (!rem.escalation || rem.escalation < 1) return null;
  const days = wholeDaysSince(rem.first_fired_at);
  return days === null ? null : `On your desk ${days} day${days === 1 ? '' : 's'}`;
}

const DEMO_REMINDERS: Reminder[] = [
  {
    id: 'demo-rem-1',
    producer: 'renewal_window',
    entity_key: 'property:700',
    status: 'open',
    severity: 'high',
    title: '2 leases at Garden Row are in the renewal window',
    body: 'Soonest ends in 28 days. No renewal offer has been started.',
    link_path: '/properties/700',
    payload: {},
    snoozed_until: null,
    first_fired_at: new Date(Date.now() - 86_400_000).toISOString(),
    last_fired_at: new Date().toISOString(),
  },
  {
    id: 'demo-rem-2',
    producer: 'lead_cold',
    entity_key: 'lead:demo',
    status: 'open',
    severity: 'normal',
    title: 'Jordan Reyes is going cold',
    body: 'No touch in 3+ days · interested in Maple Court 4B. A quick nudge keeps them warm.',
    link_path: '/crm',
    payload: {},
    snoozed_until: null,
    first_fired_at: new Date(Date.now() - 3_600_000).toISOString(),
    last_fired_at: new Date().toISOString(),
  },
];

export async function fetchReminders(): Promise<Reminder[]> {
  if (demoMode) return DEMO_REMINDERS;
  const res = await apiFetch('/api/v1/reminders');
  const body = (await res.json()) as { reminders?: Reminder[] } | null;
  // A 200 with an unexpected body must degrade to "no reminders", not
  // poison the strip's state — an undefined here crashed the whole
  // Inbox tree (reminders.length on undefined) and made every webhook
  // card unreachable. Reminders are an enhancement, never load-bearing.
  return Array.isArray(body?.reminders) ? body.reminders : [];
}

async function act(id: string, action: string, body?: unknown): Promise<Reminder> {
  const res = await apiFetch(`/api/v1/reminders/${id}/${action}`, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });
  return res.json();
}

export async function snoozeReminder(id: string, days: number): Promise<Reminder> {
  if (demoMode) {
    return {
      ...DEMO_REMINDERS[0],
      id,
      status: 'snoozed',
      snoozed_until: new Date(Date.now() + days * 86_400_000).toISOString(),
    };
  }
  return act(id, 'snooze', { days });
}

export async function dismissReminder(id: string): Promise<Reminder> {
  if (demoMode) return { ...DEMO_REMINDERS[0], id, status: 'dismissed' };
  return act(id, 'dismiss');
}

export async function handledReminder(id: string, note = ''): Promise<Reminder> {
  if (demoMode) return { ...DEMO_REMINDERS[0], id, status: 'handled' };
  return act(id, 'handled', { note });
}
