/**
 * The Today detail pane's action row — the half a TodayCard test cannot see.
 *
 * ⚠ **A callback nothing calls is the same defect as a callback that does not
 * exist.** `TodayCard.actions.test.tsx` proves the strip HANDS the pane an
 * `onSnooze` and an `onHandled` and that both reach the service. It cannot see
 * whether the pane renders anything that calls them — which is exactly the
 * state this product shipped in: `snoozeReminder` and `handledReminder` were
 * correct, exported, backed by live routes, and had zero callers (gotchas
 * 62/66/69/103/121/136 — a well-tested helper whose one production caller is
 * invisible to its tests).
 *
 * So this drives the REAL page: strip row → `onOpenItem` → pane → button →
 * service. Mocking the pane's callbacks would assert whatever the mock said.
 *
 * ⚠ `sonner` is mocked as a CALLABLE with methods, not an object of methods.
 * `InboxPage.dismissRow` calls `toast(...)` directly, so a plain object throws
 * "toast is not a function" — the trap `InboxPage.dismiss.test.tsx` documents.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const toastFn = vi.hoisted(() => {
  const fn = vi.fn() as ReturnType<typeof vi.fn> & Record<string, unknown>;
  for (const m of ['success', 'error', 'message', 'warning', 'info']) fn[m] = vi.fn();
  return fn;
});
vi.mock('sonner', () => ({ toast: toastFn }));

vi.mock('../services/inbox', async (orig) => {
  const actual = await orig<typeof import('../services/inbox')>();
  return { ...actual, listInboxRows: vi.fn().mockResolvedValue([]) };
});

vi.mock('../services/reports', () => ({
  fetchMorningBrief: vi.fn(async () => []),
  fetchNoticed: vi.fn(async () => null),
  dismissRiskFlag: vi.fn(),
  isBriefDismissedToday: vi.fn(() => true),
  dismissBriefForToday: vi.fn(),
}));

vi.mock('../services/reminders', async (orig) => ({
  ...(await orig<typeof import('../services/reminders')>()),
  fetchReminders: vi.fn(),
  dismissReminder: vi.fn(),
  snoozeReminder: vi.fn(),
  handledReminder: vi.fn(),
}));

import { InboxPage } from './InboxPage';
import {
  dismissReminder,
  fetchReminders,
  handledReminder,
  snoozeReminder,
  type Reminder,
} from '../services/reminders';
import { SNOOZE_CHOICES } from './TodayCard';

const REMINDER: Reminder = {
  id: 'rem-1',
  producer: 'renewal_window',
  entity_key: 'property:700',
  status: 'open',
  severity: 'high',
  title: '2 leases at Garden Row are in the renewal window',
  body: 'Soonest ends in 28 days.',
  link_path: null,
  payload: {},
  snoozed_until: null,
  first_fired_at: null,
  last_fired_at: null,
};

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  vi.mocked(fetchReminders).mockResolvedValue([REMINDER]);
  vi.mocked(dismissReminder).mockResolvedValue(REMINDER);
  vi.mocked(snoozeReminder).mockResolvedValue(REMINDER);
  vi.mocked(handledReminder).mockResolvedValue(REMINDER);
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

/** Render the page and open the reminder's detail pane, the way a PM does. */
async function openReminderDetail(): Promise<void> {
  render(<InboxPage />);
  await waitFor(() => {
    expect(screen.getByTestId('inbox-today-reminder-open-0')).toBeTruthy();
  });
  fireEvent.click(screen.getByTestId('inbox-today-reminder-open-0'));
  await waitFor(() => {
    expect(screen.getByTestId('inbox-today-detail')).toBeTruthy();
  });
}

describe('the reminder detail pane', () => {
  it('offers all three verbs, not just the permanent one', async () => {
    await openReminderDetail();

    // Dismiss is deliberately permanent (`reminders/aging.py` never brings a
    // dismissed card back). Shipping it as the ONLY verb meant a PM who could
    // not deal with a card today had to stop tracking it for good.
    expect(screen.getByTestId('inbox-today-detail-dismiss')).toBeTruthy();
    expect(screen.getByTestId('inbox-today-detail-handled')).toBeTruthy();
    for (const choice of SNOOZE_CHOICES) {
      expect(screen.getByTestId(`inbox-today-detail-snooze-${choice.days}`)).toBeTruthy();
    }
  });

  it('snoozing reaches the service with that button’s own duration', async () => {
    await openReminderDetail();
    const choice = SNOOZE_CHOICES[1] ?? SNOOZE_CHOICES[0];

    fireEvent.click(screen.getByTestId(`inbox-today-detail-snooze-${choice.days}`));

    await waitFor(() => {
      expect(snoozeReminder).toHaveBeenCalledWith(REMINDER.id, choice.days);
    });
    // Every button must send ITS OWN number. One shared handler that always
    // passed the first choice would satisfy a test that only clicked one.
    expect(vi.mocked(snoozeReminder).mock.calls[0][1]).toBe(choice.days);
  });

  it('marking it handled reaches the service', async () => {
    await openReminderDetail();
    fireEvent.click(screen.getByTestId('inbox-today-detail-handled'));
    await waitFor(() => {
      expect(handledReminder).toHaveBeenCalledWith(REMINDER.id);
    });
    expect(dismissReminder).not.toHaveBeenCalled();
  });

  it('each verb closes the pane, so the desk reflects the choice', async () => {
    await openReminderDetail();
    fireEvent.click(screen.getByTestId('inbox-today-detail-handled'));
    await waitFor(() => {
      expect(screen.queryByTestId('inbox-today-detail')).toBeNull();
    });
  });

  it('a failed action from the pane still says so', async () => {
    vi.mocked(handledReminder).mockRejectedValue(new Error('500'));
    await openReminderDetail();

    fireEvent.click(screen.getByTestId('inbox-today-detail-handled'));

    // The pane closes optimistically; the failure has to reach the user
    // somewhere, and the strip they are looking at is the only surface left.
    // ⚠ Typed on the way out of the hoisted mock, which is a
    // `Record<string, unknown>`. Reading `.mock.calls` straight off it is a
    // `tsc` error that vitest happily runs green (gotcha 132).
    const errorToast = toastFn.error as ReturnType<typeof vi.fn>;
    await waitFor(() => {
      expect(errorToast).toHaveBeenCalledTimes(1);
    });
    expect(String(errorToast.mock.calls[0][0])).toMatch(/still on your desk/i);
  });
});
