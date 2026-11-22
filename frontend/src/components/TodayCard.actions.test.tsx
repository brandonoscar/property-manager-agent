/**
 * The reminder ACTION surface — the three verbs and what happens when one
 * fails.
 *
 * WHAT WAS WRONG
 * --------------
 * Found 2026-09-16 by reading the strip's call sites rather than by any test
 * failing. Two defects, one theme.
 *
 * **1. A failed action was swallowed.** Both dismiss call sites read
 * `setReminders(filter out) ; void dismissReminder(id).catch(() => {})`. That
 * is a real write to `helixis_reminders.status`, so a 500, a timeout or an
 * expired token removed the card, told the user nothing, and the card came
 * back on the next load. An action that appears to work and did not.
 *
 * ⚠ `InboxPage.dismissRow` — one screen away, in the same file as the detail
 * pane — already reasons this out for the strictly LESS destructive case (that
 * dismissal is localStorage-only and reversible) and still gives it an undo
 * toast: "a destructive-feeling action that cannot be taken back is one people
 * stop using, which quietly costs the feature rather than the row." The rule
 * was written down and never reached this caller (gotchas 25/67).
 *
 * **2. Two of the three verbs were unreachable.** `snoozeReminder` and
 * `handledReminder` shipped in `services/reminders.ts`, were named in its
 * docstring ("the three user actions"), had live backend routes — and had ZERO
 * production callers (gotcha 62's `comms_triage` shape). So:
 *   - "remind me in 2 days" was impossible, and the only verb a PM had for a
 *     card they could not deal with today was the PERMANENT one;
 *   - nothing could ever set `handled_at`, which makes gotcha 88's whole
 *     handled-cooldown ladder — suppress, reopen when the condition still
 *     holds, and render "Marked handled N days ago — still open" — unreachable
 *     in production. That is gotcha 98's shape: shipped, correct, never once
 *     able to fire.
 *
 * WHY THE RESTORE IS POSITIONAL
 * -----------------------------
 * The strip caps at `REMINDER_VISIBLE_LIMIT`, so a card restored by appending
 * can land in the collapsed tail and read as still gone — which is exactly the
 * failure the restore exists to make visible.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const toastError = vi.hoisted(() => vi.fn());
const toastSuccess = vi.hoisted(() => vi.fn());
vi.mock('sonner', () => ({ toast: { error: toastError, success: toastSuccess } }));

vi.mock('../services/reports', () => ({
  fetchMorningBrief: vi.fn(async () => []),
  fetchNoticed: vi.fn(async () => null),
  dismissRiskFlag: vi.fn(),
  isBriefDismissedToday: vi.fn(() => true),
  dismissBriefForToday: vi.fn(),
}));
// Mock the NETWORK calls only — `reminderDeskNote` is a pure formatter and
// stubbing it would make the rendered rows assert whatever the stub said.
vi.mock('../services/reminders', async (importActual) => ({
  ...(await importActual<typeof import('../services/reminders')>()),
  fetchReminders: vi.fn(),
  dismissReminder: vi.fn(),
  snoozeReminder: vi.fn(),
  handledReminder: vi.fn(),
}));

import {
  dismissReminder,
  fetchReminders,
  handledReminder,
  snoozeReminder,
  type Reminder,
} from '../services/reminders';
import { titleCase } from '../lib/text';
import { SNOOZE_CHOICES, TodayCard, type TodayDetailItem } from './TodayCard';

function card(id: string, title: string): Reminder {
  return {
    id,
    producer: 'renewal_window',
    entity_key: `property:${id}`,
    status: 'open',
    severity: 'normal',
    title,
    body: null,
    link_path: null,
    payload: {},
    snoozed_until: null,
    first_fired_at: null,
    last_fired_at: null,
  };
}

const first = card('rem-1', 'First card');
const middle = card('rem-2', 'Middle card');
const last = card('rem-3', 'Last card');

beforeEach(() => {
  vi.clearAllMocks();
  // ⚠ `clearAllMocks` clears CALLS and keeps IMPLEMENTATIONS, so the test that
  // makes `toast.success` throw would leak that into every test after it —
  // and the symptom would be a later test failing for a reason nothing in it
  // mentions. `mockReset` is safe here precisely because these two have no
  // default implementation worth preserving.
  toastSuccess.mockReset();
  toastError.mockReset();
  vi.mocked(fetchReminders).mockResolvedValue([first, middle, last]);
  vi.mocked(dismissReminder).mockResolvedValue(middle);
  vi.mocked(snoozeReminder).mockResolvedValue(middle);
  vi.mocked(handledReminder).mockResolvedValue(middle);
});

async function renderStrip(onOpenItem?: (item: TodayDetailItem) => void) {
  render(<TodayCard onOpenItem={onOpenItem} />);
  await waitFor(() => {
    expect(screen.queryByTestId('inbox-today-loading')).toBeNull();
  });
}

/**
 * The rendered card titles, top to bottom.
 *
 * ⚠ The strip renders `titleCase(rem.title)`, so the DOM says "Middle Card"
 * and not "Middle card". My first cut matched the fixture's own spelling and
 * therefore matched NOTHING — which made the success-path assertion
 * (`.some(...)` is false) pass vacuously, for a predicate that could never be
 * true either way (gotchas 38/51: a one-sided check passes against a predicate
 * that matches nothing). `MIDDLE` is derived from the fixture through the real
 * formatter so the two cannot drift again.
 */
function visibleTitles(): string[] {
  return screen
    .getAllByTestId(/^inbox-today-reminder-open-\d+$/)
    .map((el) => el.textContent ?? '');
}

const MIDDLE = titleCase(middle.title);

function middleIsOnTheDesk(): boolean {
  return visibleTitles().some((t) => t.includes(MIDDLE));
}

describe('a reminder action that fails', () => {
  it('puts the card back and says so, rather than reporting a success', async () => {
    vi.mocked(dismissReminder).mockRejectedValue(new Error('500'));
    await renderStrip();

    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-1'));

    // The optimistic removal still happens — the card goes immediately.
    await waitFor(() => {
      expect(toastError).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(middleIsOnTheDesk()).toBe(true);
    });
    // The message names what is true NOW ("still on your desk"), because that
    // is the recovery: the card the user thought they had cleared is back.
    expect(String(toastError.mock.calls[0][0])).toMatch(/still on your desk/i);
  });

  it('restores it AT ITS OLD POSITION, not at the end', async () => {
    vi.mocked(dismissReminder).mockRejectedValue(new Error('500'));
    await renderStrip();
    expect(visibleTitles().map((t) => t.slice(0, 5))).toEqual(['First', 'Middl', 'Last ']);

    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-1'));

    await waitFor(() => {
      expect(visibleTitles()).toHaveLength(3);
    });
    // Appending would reorder the desk. On a full strip it could also push the
    // restored card into the collapsed tail, where it reads as still gone.
    expect(visibleTitles().map((t) => t.slice(0, 5))).toEqual(['First', 'Middl', 'Last ']);
  });

  /**
   * ⚠ This pins "the restore puts back EXACTLY ONE copy", which is real and
   * catches a restore that inserts twice.
   *
   * It does NOT cover the `prev.some(...)` guard beside it, and an earlier
   * draft of this test claimed it did. That guard handles a reload landing
   * between the removal and the failure, and `fetchReminders` runs once on
   * mount with no poll — so the component cannot produce that input, and a
   * mutation dropping the guard is ALIVE for a reason about the component
   * rather than about this file (gotcha 58). The guard's own comment now says
   * so; pretending otherwise here would have been a distinction that does not
   * exist, pinned by a test nobody could falsify.
   */
  it('restores exactly one copy of the card', async () => {
    let releaseFailure: (reason: Error) => void = () => {};
    vi.mocked(dismissReminder).mockReturnValue(
      new Promise((_resolve, reject) => {
        releaseFailure = reject;
      }) as Promise<Reminder>,
    );
    await renderStrip();

    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-1'));
    await waitFor(() => {
      expect(visibleTitles()).toHaveLength(2);
    });
    releaseFailure(new Error('500'));

    await waitFor(() => {
      expect(toastError).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(visibleTitles()).toHaveLength(3);
    });
    const restored = visibleTitles().filter((t) => t.includes(MIDDLE));
    expect(restored).toHaveLength(1);
  });

  it('still removes the card when the write succeeds', async () => {
    await renderStrip();
    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-1'));

    await waitFor(() => {
      expect(visibleTitles()).toHaveLength(2);
    });
    // ⚠ Paired with a POSITIVE assertion on purpose. On its own, "the middle
    // card is gone" also passes when the matcher matches nothing at all —
    // which is precisely how the first cut of this file passed while being
    // blind (gotchas 38/51).
    expect(middleIsOnTheDesk()).toBe(false);
    expect(visibleTitles().some((t) => t.includes(titleCase(first.title)))).toBe(true);
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe('the two verbs that had no caller', () => {
  it('hands the detail pane a snooze that reaches the service with the chosen days', async () => {
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    expect(opened).toHaveLength(1);

    const days = SNOOZE_CHOICES[0].days;
    opened[0].onSnooze?.(days);

    await waitFor(() => {
      expect(snoozeReminder).toHaveBeenCalledWith('rem-2', days);
    });
    await waitFor(() => {
      expect(middleIsOnTheDesk()).toBe(false);
    });
  });

  it('hands the detail pane a handled action that reaches the service', async () => {
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    opened[0].onHandled?.();

    await waitFor(() => {
      expect(handledReminder).toHaveBeenCalledWith('rem-2');
    });
  });

  it('applies the same restore-and-say-so rule to snooze and handled', async () => {
    // The whole point of one shared helper: a verb added later cannot get the
    // failure handling wrong, because it does not implement it.
    vi.mocked(snoozeReminder).mockRejectedValue(new Error('500'));
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    opened[0].onSnooze?.(SNOOZE_CHOICES[0].days);

    await waitFor(() => {
      expect(toastError).toHaveBeenCalledTimes(1);
    });
    expect(String(toastError.mock.calls[0][0])).toMatch(/snooze/i);
    await waitFor(() => {
      expect(visibleTitles().map((t) => t.slice(0, 5))).toEqual(['First', 'Middl', 'Last ']);
    });
  });
});

describe('what the user is told when it worked', () => {
  /**
   * ⚠ All three verbs look IDENTICAL from the outside — the card vanishes.
   * That correctly describes a dismiss and misdescribes the other two, which
   * promise a return. So the rule is: say something only when the outcome is
   * not what the vanishing implies.
   */
  it('names the day a snoozed card comes back, from the SERVER’s own answer', async () => {
    const until = '2026-09-24T12:00:00.000Z';
    vi.mocked(snoozeReminder).mockResolvedValue({ ...middle, snoozed_until: until });
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    opened[0].onSnooze?.(SNOOZE_CHOICES[0].days);

    await waitFor(() => {
      expect(toastSuccess).toHaveBeenCalledTimes(1);
    });
    // Derived from the response, never recomputed as `now + days` here — the
    // route builds `snoozed_until` itself, and a second source of truth would
    // let this screen name a different day from the one the sweep uses.
    const expected = new Date(until).toLocaleDateString(undefined, {
      weekday: 'long',
      month: 'short',
      day: 'numeric',
    });
    expect(String(toastSuccess.mock.calls[0][0])).toContain(expected);
  });

  it('says a handled card can come back, because that is the whole difference', async () => {
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    opened[0].onHandled?.();

    await waitFor(() => {
      expect(toastSuccess).toHaveBeenCalledTimes(1);
    });
    // Silence here would let a user read "handled" as "gone for good", which
    // `reminders/aging.py` explicitly does not mean.
    expect(String(toastSuccess.mock.calls[0][0])).toMatch(/comes back/i);
  });

  it('stays silent on dismiss, where vanishing IS the whole truth', async () => {
    await renderStrip();
    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-1'));

    await waitFor(() => {
      expect(visibleTitles()).toHaveLength(2);
    });
    // A toast on every action is three interruptions that teach the user to
    // dismiss all of them, including the two that carry real information.
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it('a broken notification never reports a successful write as failed', async () => {
    // ⚠ The defect a mutation battery found in the first cut of this feature.
    // `.then(...).catch(...)` routes a throw from the SUCCESS handler into the
    // failure handler, so a formatter or a toast that blew up restored the
    // card and told the user the write had failed — on a write that landed.
    // The notification is an enhancement and must never be load-bearing
    // (gotcha 7).
    toastSuccess.mockImplementation(() => {
      throw new TypeError('toast.success is not a function');
    });
    const opened: TodayDetailItem[] = [];
    await renderStrip((item) => opened.push(item));

    fireEvent.click(screen.getByTestId('inbox-today-reminder-open-1'));
    opened[0].onHandled?.();

    await waitFor(() => {
      expect(middleIsOnTheDesk()).toBe(false);
    });
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe('the snooze choices', () => {
  it('offers the product default first', () => {
    // `SnoozeIn.days` is `Field(default=2, ...)` and the route docstring reads
    // "remind me in 2 days" — so the short option is the product's own stated
    // default rather than a number picked to fill a button.
    expect(SNOOZE_CHOICES[0].days).toBe(2);
  });

  it('every choice is a positive number inside the backend cap', () => {
    // `gt=0, le=MAX_SNOOZE_DAYS` — a value outside that is a 422 the user
    // would only meet in production. The exact cap is pinned against the
    // Python constant by `test_snooze_choices_are_inside_the_backend_cap.py`,
    // which can read both sides; this is the shape check.
    expect(SNOOZE_CHOICES.length).toBeGreaterThan(0);
    for (const choice of SNOOZE_CHOICES) {
      expect(choice.days).toBeGreaterThan(0);
      expect(Number.isFinite(choice.days)).toBe(true);
      expect(choice.label.trim()).not.toBe('');
    }
  });
});
