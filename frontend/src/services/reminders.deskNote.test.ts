/**
 * The line that tells a PM a reminder card is not new.
 *
 * WHY IT EXISTS
 * The server (`reminders/aging.py`) raises a card's `escalation` for the time
 * it has sat untouched, and expires a `handled` claim whose condition still
 * holds. Both were invisible in the strip: a card ignored for six weeks
 * rendered identically to one raised this morning, and a card that came back
 * after somebody said they had dealt with it said nothing about that.
 *
 * WHAT IS PINNED, AND THE DIRECTION THAT MATTERS
 * Both ways round. A note that always renders is not a signal — it is noise on
 * every card, which is how a control gets switched off. So the tests assert
 * the silence as hard as the sentence.
 *
 * ⚠ The client MUST NOT re-derive the ladder's thresholds. The decision to say
 * anything comes from the server's `escalation`; only the number is computed
 * here. Two copies of "7 days" in two languages is how the ends drift while
 * both keep looking right.
 */

import { describe, expect, it } from 'vitest';

import { reminderDeskNote } from './reminders';

const DAY = 86_400_000;
const daysAgo = (n: number) => new Date(Date.now() - n * DAY).toISOString();

describe('reminderDeskNote', () => {
  it('says nothing about a card the server has not escalated', () => {
    expect(
      reminderDeskNote({ escalation: 0, first_fired_at: daysAgo(90), handled_at: null }),
    ).toBeNull();
  });

  it('stays silent when the field is missing entirely', () => {
    // A backend older than `escalation` answers 200 with the key absent.
    // Reminders are an enhancement — an unknown must render as nothing, not
    // as `undefined day(s)`.
    expect(reminderDeskNote({ first_fired_at: daysAgo(90), handled_at: null })).toBeNull();
  });

  it('reports the age once the server has escalated the card', () => {
    expect(
      reminderDeskNote({ escalation: 1, first_fired_at: daysAgo(9), handled_at: null }),
    ).toBe('On your desk 9 days');
  });

  it('does not re-derive the threshold — escalation is the server’s decision', () => {
    // Two days old and escalated: possible, because a producer can raise
    // escalation from the entity's own facts. The client renders what it is
    // told rather than second-guessing it against a day count of its own.
    expect(
      reminderDeskNote({ escalation: 2, first_fired_at: daysAgo(2), handled_at: null }),
    ).toBe('On your desk 2 days');
  });

  it('singularises one day', () => {
    expect(
      reminderDeskNote({ escalation: 1, first_fired_at: daysAgo(1), handled_at: null }),
    ).toBe('On your desk 1 day');
  });

  it('says nothing for a card raised today', () => {
    // Under a day rounds to 0, and "On your desk 0 days" is worse than
    // silence — it reads as a bug in front of the user.
    expect(
      reminderDeskNote({ escalation: 1, first_fired_at: daysAgo(0), handled_at: null }),
    ).toBeNull();
  });

  it('leads with the handled claim, which is the surprising fact', () => {
    // This is the case that used to be invisible: `handled` silenced a card
    // permanently, so a claim that turned out to be wrong was never revisited.
    expect(
      reminderDeskNote({ escalation: 2, first_fired_at: daysAgo(60), handled_at: daysAgo(31) }),
    ).toBe('Marked handled 31 days ago — still open');
  });

  it('reports the handled claim even when nothing escalated', () => {
    // The two facts are independent. Gating the handled note on `escalation`
    // would hide it exactly when a producer had not raised the card itself.
    expect(
      reminderDeskNote({ escalation: 0, first_fired_at: daysAgo(2), handled_at: daysAgo(45) }),
    ).toBe('Marked handled 45 days ago — still open');
  });

  it('survives a malformed timestamp instead of rendering NaN', () => {
    // `Date.parse` of junk is NaN, and `NaN < 1` is FALSE — so a naive
    // ordering of the guard prints "On your desk NaN days" to a customer.
    expect(
      reminderDeskNote({ escalation: 2, first_fired_at: 'not-a-date', handled_at: null }),
    ).toBeNull();
    expect(
      reminderDeskNote({ escalation: 2, first_fired_at: null, handled_at: 'not-a-date' }),
    ).toBeNull();
  });

  it('ignores a timestamp in the future rather than counting backwards', () => {
    expect(
      reminderDeskNote({
        escalation: 2,
        first_fired_at: new Date(Date.now() + 5 * DAY).toISOString(),
        handled_at: null,
      }),
    ).toBeNull();
  });
});
