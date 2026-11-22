/**
 * The Today strip shows a handful of cards, not a wall of them.
 *
 * Founder direction 2026-08-30, from a screenshot of nine identical rows.
 * Measured the same morning: one company held **16 open reminder cards with
 * ONE distinct title**, another held **55 with 55 distinct titles**. Grouping
 * (`bills_due`) fixes the first — 16 → 1, verified against live rows — and
 * only takes the second from 55 to 50. So the cap is not belt-and-braces; it
 * is the half that handles genuinely-different work piling up.
 *
 * ⚠ COLLAPSED, NOT DROPPED, and both directions are pinned. Every card is
 * still fetched, still real, still dismissible; the tail is one click away
 * with its exact count. Suppressing an overdue bill so the strip looks tidy
 * is the failure mode this product treats as its worst — a real obligation
 * that silently never reaches the user.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import { REMINDER_VISIBLE_LIMIT, TodayCard } from './TodayCard';
import type { Reminder } from '../services/reminders';

// Network calls only — see the note in TodayCard.test.tsx.
vi.mock('../services/reminders', async (importActual) => ({
  ...(await importActual<typeof import('../services/reminders')>()),
  fetchReminders: vi.fn(),
  dismissReminder: vi.fn(),
}));
vi.mock('../services/morningBrief', () => ({
  fetchMorningBrief: vi.fn(async () => []),
  isBriefDismissedToday: () => true,
  dismissBriefForToday: vi.fn(),
}));
vi.mock('../services/noticed', () => ({ fetchNoticed: vi.fn(async () => null) }));

import { dismissReminder, fetchReminders } from '../services/reminders';

const card = (i: number): Reminder => ({
  id: `r${i}`,
  producer: 'bill_due',
  entity_key: `vendor:${i}|due:2026-07-21`,
  status: 'open',
  severity: 'high',
  title: `Vendor ${i} — 3 unpaid bills, $795.00 total`,
  body: 'All due 2026-07-21. OVERDUE.',
  link_path: null,
  payload: {},
  snoozed_until: null,
  first_fired_at: null,
  last_fired_at: null,
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(dismissReminder).mockResolvedValue(card(0));
});

describe('the reminder cap', () => {
  it('shows every card when there are few enough', async () => {
    vi.mocked(fetchReminders).mockResolvedValue([card(0), card(1), card(2)]);
    render(<TodayCard />);
    await waitFor(() => expect(screen.getByTestId('inbox-today-reminder-2')).toBeTruthy());
    // No "more" row when nothing is hidden — an affordance that opens an
    // empty tail is noise.
    expect(screen.queryByTestId('inbox-today-reminder-more')).toBeNull();
  });

  it('collapses the tail and names its exact size', async () => {
    vi.mocked(fetchReminders).mockResolvedValue(Array.from({ length: 16 }, (_, i) => card(i)));
    render(<TodayCard />);
    await waitFor(() => expect(screen.getByTestId('inbox-today-reminder-more')).toBeTruthy());

    expect(screen.getByTestId(`inbox-today-reminder-${REMINDER_VISIBLE_LIMIT - 1}`)).toBeTruthy();
    expect(screen.queryByTestId(`inbox-today-reminder-${REMINDER_VISIBLE_LIMIT}`)).toBeNull();
    // The COUNT is the promise that nothing was thrown away.
    expect(screen.getByTestId('inbox-today-reminder-more').textContent).toContain('11 more');
  });

  it('reveals the whole tail on click — nothing was discarded', async () => {
    vi.mocked(fetchReminders).mockResolvedValue(Array.from({ length: 16 }, (_, i) => card(i)));
    render(<TodayCard />);
    await waitFor(() => expect(screen.getByTestId('inbox-today-reminder-more')).toBeTruthy());
    fireEvent.click(screen.getByTestId('inbox-today-reminder-more'));

    expect(screen.getByTestId('inbox-today-reminder-15')).toBeTruthy();
    expect(screen.queryByTestId('inbox-today-reminder-more')).toBeNull();
  });

  it('pulls the next card up when a visible one is dismissed', async () => {
    // ⚠ The visible slice is recomputed from state, never snapshotted. A
    // snapshot leaves a gap where the dismissed card was and the count stops
    // matching what is on screen.
    vi.mocked(fetchReminders).mockResolvedValue(Array.from({ length: 16 }, (_, i) => card(i)));
    render(<TodayCard />);
    await waitFor(() => expect(screen.getByTestId('inbox-today-reminder-more')).toBeTruthy());

    fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-0'));

    await waitFor(() =>
      expect(screen.getByTestId('inbox-today-reminder-more').textContent).toContain('10 more'),
    );
    // Still a full fold — the sixth card moved up into it.
    expect(screen.getByTestId(`inbox-today-reminder-${REMINDER_VISIBLE_LIMIT - 1}`)).toBeTruthy();
  });

  it('says "item needs" for a tail of one', async () => {
    vi.mocked(fetchReminders).mockResolvedValue(
      Array.from({ length: REMINDER_VISIBLE_LIMIT + 1 }, (_, i) => card(i)),
    );
    render(<TodayCard />);
    await waitFor(() => expect(screen.getByTestId('inbox-today-reminder-more')).toBeTruthy());
    expect(screen.getByTestId('inbox-today-reminder-more').textContent).toContain(
      '1 more item needs',
    );
  });

  it('keeps the limit in the founder-stated range', () => {
    // "there should only be 3-5 Occupella noticed reminders a day". A drift to
    // 20 would leave every test above passing while the strip filled up again.
    expect(REMINDER_VISIBLE_LIMIT).toBeGreaterThanOrEqual(3);
    expect(REMINDER_VISIBLE_LIMIT).toBeLessThanOrEqual(5);
  });
});
