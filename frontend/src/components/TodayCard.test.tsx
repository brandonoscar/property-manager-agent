/**
 * TodayCard — the Inbox "Today" attention strip (2026-08-15 audit,
 * debt #2: morning brief / reminders were built server-side but
 * rendered nowhere).
 *
 * Pins:
 *  - brief + reminder sections render from their services;
 *  - a brief row opens in-pane detail via onOpenItem (never navigates);
 *  - detector findings do NOT render as their own strip rows anymore —
 *    the risk→Inbox bridge delivers them as real inbox rows; the digest
 *    survives only as the brief's "risk_flags" rollup findings;
 *  - each dismissal path calls the right service and removes the row
 *    (brief per-day as a group — the service's own model);
 *  - a quiet morning (all empty) renders NOTHING — no invented urgency;
 *  - a failing backend degrades silently (error = quiet, never an
 *    error card in front of the inbox).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/reports', () => ({
  fetchMorningBrief: vi.fn(),
  fetchNoticed: vi.fn(),
  dismissRiskFlag: vi.fn(),
  isBriefDismissedToday: vi.fn(() => false),
  dismissBriefForToday: vi.fn(),
}));
// ⚠ Mock the NETWORK calls only. `reminderDeskNote` is a pure formatter, and
// stubbing it would make every render test here assert nothing about the line
// the user actually reads — the strip would render whatever the stub said.
vi.mock('../services/reminders', async (importActual) => ({
  ...(await importActual<typeof import('../services/reminders')>()),
  fetchReminders: vi.fn(),
  dismissReminder: vi.fn(),
}));

import {
  dismissBriefForToday,
  fetchMorningBrief,
  fetchNoticed,
  isBriefDismissedToday,
  type MorningBriefItem,
  type NoticedDigest,
} from '../services/reports';
import { dismissReminder, fetchReminders, type Reminder } from '../services/reminders';
import { useStore } from '../stores/store';
import { TodayCard } from './TodayCard';

const briefItem: MorningBriefItem = {
  key: 'delinquency',
  count: 3,
  amount: 4020,
  label: '$4,020 owed across 3 leases',
  detail: 'Ask who is behind on rent to see who needs the nudge.',
  view: 'properties',
};

const riskBriefItem: MorningBriefItem = {
  key: 'risk_flags',
  count: 2,
  amount: 650,
  label: '2 hidden risks in the books',
  detail: 'Occupella noticed things worth a look.',
  view: 'inbox',
};

const digest: NoticedDigest = {
  generated_at: 'now',
  urgent: [
    {
      id: 'flag-urgent',
      rule_key: 'duplicate_vendor_bill',
      severity: 'critical',
      title: 'Possible duplicate vendor bill',
      detail: 'Two $450 bills from Apex Plumbing this week.',
      amount: 450,
      property_id: null,
      entity_type: null,
      entity_id: null,
      related: [],
    },
  ],
  noticed: [
    {
      id: 'flag-1',
      rule_key: 'rent_under_charged',
      severity: 'warning',
      title: 'Unit 4B rent below the lease amount',
      detail: 'Charged $1,750 against a $1,850 lease.',
      amount: 100,
      property_id: null,
      entity_type: null,
      entity_id: null,
      related: [],
    },
  ],
  overflow_count: 0,
  delivered_count: 2,
  miscalibrated: [],
};

const reminder: Reminder = {
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

const emptyDigest: NoticedDigest = {
  generated_at: '',
  urgent: [],
  noticed: [],
  overflow_count: 0,
  delivered_count: 0,
  miscalibrated: [],
};

const fetchBriefMock = vi.mocked(fetchMorningBrief);
const fetchNoticedMock = vi.mocked(fetchNoticed);
const fetchRemindersMock = vi.mocked(fetchReminders);
const dismissedTodayMock = vi.mocked(isBriefDismissedToday);

beforeEach(() => {
  vi.clearAllMocks();
  dismissedTodayMock.mockReturnValue(false);
  fetchBriefMock.mockResolvedValue([briefItem]);
  fetchNoticedMock.mockResolvedValue(digest);
  fetchRemindersMock.mockResolvedValue([reminder]);
  vi.mocked(dismissReminder).mockResolvedValue(reminder);
  // Reset navigation state between tests (module-level Zustand store).
  useStore.getState().setView('chat');
});

async function renderSettled(onOpenItem?: (item: unknown) => void) {
  render(<TodayCard onOpenItem={onOpenItem as never} />);
  await waitFor(() => {
    expect(screen.queryByTestId('inbox-today-loading')).toBeNull();
  });
}

describe('TodayCard sections', () => {
  it('shows a skeleton while loading, then brief + reminders', async () => {
    render(<TodayCard />);
    expect(screen.getByTestId('inbox-today-loading')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('inbox-today')).toBeInTheDocument();
    });
    // Input is lowercase (webhook casing); the render is headline-cased.
    expect(screen.getByText('$4,020 Owed Across 3 Leases')).toBeInTheDocument();
    expect(
      screen.getByText('2 Leases at Garden Row Are in the Renewal Window'),
    ).toBeInTheDocument();
    // Findings NO LONGER render as their own strip rows — the risk→Inbox
    // bridge delivers them as real inbox rows in the full format.
    expect(screen.queryByText('Possible Duplicate Vendor Bill')).toBeNull();
    expect(screen.queryByTestId('inbox-today-noticed-0')).toBeNull();
  });

  it('a brief row opens IN-PANE detail — never a page navigation', async () => {
    const onOpenItem = vi.fn();
    await renderSettled(onOpenItem);
    fireEvent.click(screen.getByTestId('inbox-today-brief-0'));
    // The Inbox receives the item to show in its detail pane…
    expect(onOpenItem).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'brief', title: '$4,020 Owed Across 3 Leases' }),
    );
    // …and the app view NEVER changes (the earlier navigation behavior
    // bounced users out of the Inbox — founder-reported regression).
    expect(useStore.getState().view).toBe('chat');
  });

  it("the brief's risk_flags rollup carries the open findings into the pane", async () => {
    fetchBriefMock.mockResolvedValue([riskBriefItem]);
    const onOpenItem = vi.fn();
    await renderSettled(onOpenItem);
    fireEvent.click(screen.getByTestId('inbox-today-brief-0'));
    expect(onOpenItem).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'brief',
        findings: [
          expect.objectContaining({ title: 'Possible Duplicate Vendor Bill' }),
          expect.objectContaining({ title: 'Unit 4B Rent Below the Lease Amount' }),
        ],
      }),
    );
  });

  it('"Hide for today" dismisses the brief as a group, keeping reminders', async () => {
    await renderSettled();
    fireEvent.click(screen.getByTestId('inbox-today-hide-brief'));
    expect(dismissBriefForToday).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('inbox-today-brief-0')).toBeNull();
    expect(screen.getByTestId('inbox-today-reminder-0')).toBeInTheDocument();
  });

  it('an already-dismissed brief stays hidden while reminders render', async () => {
    dismissedTodayMock.mockReturnValue(true);
    await renderSettled();
    expect(screen.queryByTestId('inbox-today-brief-0')).toBeNull();
    expect(screen.getByTestId('inbox-today-reminder-0')).toBeInTheDocument();
  });

  it('dismissing a reminder calls the reminders service and removes the row', async () => {
    await renderSettled();
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-today-reminder-dismiss-0'));
    });
    expect(dismissReminder).toHaveBeenCalledWith('rem-1');
    expect(screen.queryByTestId('inbox-today-reminder-0')).toBeNull();
  });
});

describe('TodayCard quiet states', () => {
  it('renders nothing on a quiet morning (all sections empty)', async () => {
    fetchBriefMock.mockResolvedValue([]);
    fetchNoticedMock.mockResolvedValue(emptyDigest);
    fetchRemindersMock.mockResolvedValue([]);
    const { container } = render(<TodayCard />);
    await waitFor(() => {
      expect(screen.queryByTestId('inbox-today-loading')).toBeNull();
    });
    expect(container.firstChild).toBeNull();
  });

  it('findings alone render nothing — they live in the inbox rows now', async () => {
    fetchBriefMock.mockResolvedValue([]);
    fetchRemindersMock.mockResolvedValue([]);
    // fetchNoticed still returns open flags…
    fetchNoticedMock.mockResolvedValue(digest);
    const { container } = render(<TodayCard />);
    await waitFor(() => {
      expect(screen.queryByTestId('inbox-today-loading')).toBeNull();
    });
    // …but with no brief and no reminders the strip stays empty.
    expect(container.firstChild).toBeNull();
  });

  it('degrades silently when every fetch fails — never blocks the inbox', async () => {
    fetchBriefMock.mockRejectedValue(new Error('backend down'));
    fetchNoticedMock.mockRejectedValue(new Error('backend down'));
    fetchRemindersMock.mockRejectedValue(new Error('backend down'));
    const { container } = render(<TodayCard />);
    await waitFor(() => {
      expect(screen.queryByTestId('inbox-today-loading')).toBeNull();
    });
    expect(container.firstChild).toBeNull();
  });

  it('a failing noticed fetch leaves brief + reminders rendering', async () => {
    fetchNoticedMock.mockRejectedValue(new Error('detector sweep down'));
    await renderSettled();
    expect(screen.getByTestId('inbox-today-brief-0')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-today-reminder-0')).toBeInTheDocument();
  });

  // ── the age ladder has to be VISIBLE, or it is a number nobody sees ──
  //
  // `reminderDeskNote` is unit-tested in reminders.deskNote.test.ts. These
  // two say the strip actually CALLS it — a correct helper nothing renders is
  // the same defect as no helper (the `comms_triage` shape: complete,
  // unit-tested, and wired to nothing).

  it('marks a card the server has escalated for sitting there', async () => {
    fetchRemindersMock.mockResolvedValue([
      {
        ...reminder,
        escalation: 2,
        first_fired_at: new Date(Date.now() - 24 * 86_400_000).toISOString(),
      },
    ]);
    await renderSettled();
    expect(screen.getByTestId('inbox-today-reminder-age-0')).toHaveTextContent(
      'On your desk 24 days',
    );
  });

  it('says nothing extra on an ordinary fresh card', async () => {
    // The other direction, and the one that keeps the marker meaningful: a
    // note on every row is decoration, and decoration is what people learn to
    // stop reading.
    await renderSettled();
    expect(screen.getByTestId('inbox-today-reminder-0')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-today-reminder-age-0')).toBeNull();
  });
});
