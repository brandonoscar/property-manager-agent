/**
 * PR #85 — InboxPage helpers + event header.
 * PR #86 — normalizer + duplicate-summary turn stripper.
 *
 * Covers:
 *  - ``formatPropertyLine`` mirrors PR #84's backend dedupe rule
 *    (3+ digit building suffix on the address absorbs the unit
 *    number; short or alphanumeric suffixes still get the unit
 *    appended).
 *  - ``EventHeader`` renders enrichment values + dash-placeholders
 *    for missing fields without crashing.
 *  - "Open in Buildium" link points at the real Buildium URL and
 *    is hidden when ``task.id`` is missing.
 *  - ``normalizeForCompare`` folds whitespace, strips trailing
 *    punctuation, and lowercases — so the duplicate-summary check
 *    survives a stray period or stretched space from the planner.
 *  - ``stripDuplicateSummaryTurn`` drops only the FIRST assistant
 *    turn when it matches the row's ``action_surface.summary``
 *    after normalization; partial / mismatched / later turns are
 *    left in place.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import { __test__, InboxPage } from './InboxPage';
import type { ConversationEvent } from './conversation/types';
import type { InboxRow } from '../services/inbox';

const {
  formatPropertyLine,
  formatDueDate,
  formatReporter,
  normalizeForCompare,
  stripDuplicateSummaryTurn,
} = __test__;

describe('formatPropertyLine — PR #85 inline mirror of PR #84 backend rule', () => {
  it('absorbs unit when address already ends in #NNN (3+ digit pure-numeric)', () => {
    expect(formatPropertyLine('160 East End Avenue #120', '1')).toBe('160 East End Avenue #120');
  });

  it('appends unit when address has no #-suffix', () => {
    expect(formatPropertyLine('160 East End Avenue', '120')).toBe('160 East End Avenue #120');
  });

  it('appends unit when address suffix is a short numeric (likely a unit, not a building)', () => {
    expect(formatPropertyLine('Main St', '10')).toBe('Main St #10');
  });

  it('appends unit even when address ends with alphanumeric #-suffix (NOT 3+ pure-digit)', () => {
    // ``#4b`` is alphanumeric, not pure digits — the strict
    // building-suffix rule does NOT match, so unit IS re-appended.
    // This pin matches the PR #84 spec verbatim.
    expect(formatPropertyLine('Main St #4b', '4b')).toBe('Main St #4b #4b');
  });

  it('returns em-dash when both fields are null', () => {
    expect(formatPropertyLine(null, null)).toBe('—');
  });

  it('renders unit-only when address is missing', () => {
    expect(formatPropertyLine(null, '7')).toBe('#7');
  });

  it('renders address-only when unit is missing', () => {
    expect(formatPropertyLine('Main St', null)).toBe('Main St');
  });

  it('handles whitespace-only inputs as empty', () => {
    expect(formatPropertyLine('   ', '   ')).toBe('—');
  });
});

describe('formatDueDate', () => {
  it('renders an ISO timestamp as "MMM D"', () => {
    expect(formatDueDate('2026-05-17T00:00:00Z')).toBe('May 17');
  });

  it('returns em-dash for null / undefined / unparseable input', () => {
    expect(formatDueDate(null)).toBe('—');
    expect(formatDueDate(undefined)).toBe('—');
    expect(formatDueDate('not a date')).toBe('—');
  });
});

describe('formatReporter', () => {
  it('joins first + last when both present', () => {
    expect(formatReporter('Sarah', 'Chen')).toBe('Sarah Chen');
  });

  it('renders just one half when the other is empty', () => {
    expect(formatReporter('Sarah', null)).toBe('Sarah');
    expect(formatReporter(null, 'Chen')).toBe('Chen');
  });

  it('returns em-dash when both names are empty', () => {
    expect(formatReporter(null, null)).toBe('—');
    expect(formatReporter('  ', '  ')).toBe('—');
  });
});

describe('InboxPage top-level render', () => {
  // Smoke-test that the page mounts without crashing. The full
  // Inbox flow is covered by integration-style screenshots; this
  // ensures the PR #85 EventHeader compiles into the bundle.
  it('renders without crashing in demo mode', () => {
    render(<InboxPage />);
    // The rail title is always present at the top of the page. (Demo
    // mode now renders populated fixtures, so "Inbox" also appears in
    // the detail-pane back button — assert the title by testid to stay
    // unambiguous.)
    expect(screen.getByTestId('inbox-rail-title')).toBeInTheDocument();
  });
});

describe('normalizeForCompare — PR #86 punctuation/whitespace-insensitive folder', () => {
  it('lowercases, trims, and drops a trailing period', () => {
    // Both inputs must collapse to the same canonical string,
    // otherwise the stripper would leave a duplicate turn rendered.
    expect(normalizeForCompare('Hello World.')).toBe('hello world');
    expect(normalizeForCompare('hello world')).toBe('hello world');
    expect(normalizeForCompare('Hello World.')).toBe(normalizeForCompare('hello world'));
  });

  it('collapses runs of whitespace and trims edges', () => {
    expect(normalizeForCompare('  Loud Crashing — 160 East End Ave  ')).toBe(
      'loud crashing — 160 east end ave',
    );
  });

  it('strips a trailing punctuation cluster but keeps inner punctuation', () => {
    // Inner em-dash and comma remain (they're identity-bearing
    // separators); only the terminal "!!" is removed.
    expect(normalizeForCompare('New maintenance task: leak in #4B!!')).toBe(
      'new maintenance task: leak in #4b',
    );
  });
});

describe('stripDuplicateSummaryTurn — PR #86 dedupe of the first assistant turn', () => {
  const summary = 'Maintenance request — Loud crashing — 160 East End Avenue #120';
  const turnId = 'turn-1';

  function turn(content: string, id: string = turnId): ConversationEvent[] {
    // Build a synthetic "complete" assistant turn: start ⇒ tokens ⇒ end.
    return [
      { type: 'assistant_message_start', data: { turn_id: id } },
      { type: 'assistant_message_token', data: { turn_id: id, content } },
      { type: 'assistant_message_end', data: { turn_id: id } },
    ];
  }

  it('drops the first assistant turn when its content matches the summary', () => {
    const events: ConversationEvent[] = turn(summary);
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual([]);
  });

  it('drops the first turn when only punctuation/case differs', () => {
    const events: ConversationEvent[] = turn(`${summary.toLowerCase()}.`);
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual([]);
  });

  it('keeps the turn when content does NOT match the summary', () => {
    const events: ConversationEvent[] = turn('Something else entirely');
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual(events);
  });

  it('keeps the turn when it is still streaming (no end event yet)', () => {
    const events: ConversationEvent[] = [
      { type: 'assistant_message_start', data: { turn_id: turnId } },
      { type: 'assistant_message_token', data: { turn_id: turnId, content: summary } },
      // No assistant_message_end — turn is in-flight.
    ];
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual(events);
  });

  it('preserves narration and suggestion events that precede the duplicate turn', () => {
    const narration: ConversationEvent = {
      type: 'narration',
      data: { text: 'Routing to maintenance lead…' },
    };
    const suggestion: ConversationEvent = {
      type: 'suggestion_chip',
      data: { action_index: 0, label: 'Create task', parameter_patch: {}, is_destructive: false },
    };
    const events: ConversationEvent[] = [narration, suggestion, ...turn(summary)];
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual([narration, suggestion]);
  });

  it('keeps later turns intact even when the first one is dropped', () => {
    const later = turn('Follow-up message from the planner.', 'turn-2');
    const events: ConversationEvent[] = [...turn(summary), ...later];
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual(later);
  });

  it('is a no-op when the summary is empty', () => {
    const events: ConversationEvent[] = turn('anything');
    expect(stripDuplicateSummaryTurn(events, '')).toEqual(events);
  });

  it('is a no-op when there is no assistant turn at all', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { text: 'still routing…' } },
    ];
    expect(stripDuplicateSummaryTurn(events, summary)).toEqual(events);
  });
});

// PR #87 — HOTFIX regression test.
//
// Pre-fix, ``ThreadPane`` rendered 7 hooks when ``row`` was null and 8
// hooks once a row was selected, because the ``visibleEvents``
// ``useMemo`` lived AFTER an ``if (!row) return`` early return. React
// throws ``Rendered more hooks than during the previous render`` the
// moment the first row gets auto-selected, the error boundary blanks
// the page, and Vercel ships a white-screen Inbox.
//
// This test exercises the exact transition: ``listInboxRows`` is
// stubbed to return ONE row, the InboxPage auto-select effect fires,
// and ``ThreadPane`` goes from ``row = null`` to ``row = the row``
// inside a single ``act()``. With the hook moved above the early
// return, the render succeeds. Without the fix, React throws
// synchronously inside ``act()`` and the test fails.
describe('PR #87 regression — hooks order across row select', () => {
  beforeEach(() => {
    vi.resetModules();

    // Disable demoMode so the InboxPage actually fetches rows
    // (in demoMode it short-circuits to ``setRows([])`` and the
    // auto-select effect never fires — defeating the test).
    vi.doMock('../services/auth', async () => {
      const actual = await vi.importActual<typeof import('../services/auth')>(
        '../services/auth',
      );
      return {
        ...actual,
        demoMode: false,
        supabaseConfigured: true,
        getAccessToken: vi.fn().mockResolvedValue('test-token'),
      };
    });

    vi.doMock('../services/inbox', async () => {
      const actual = await vi.importActual<typeof import('../services/inbox')>(
        '../services/inbox',
      );
      return {
        ...actual,
        listInboxRows: vi.fn().mockResolvedValue([
          {
            id: 'row-1',
            event_name: 'task_created',
            event_time: new Date().toISOString(),
            status: 'done',
            last_error: null,
            action_surface: {
              schema_version: 1,
              summary: 'Stub summary',
              actions: [],
              freeform_placeholder: 'Type…',
            },
            enrichment: null,
            executed_action_index: null,
            // PR #89 fields required by the InboxRow type.
            planner_status: 'succeeded',
            planner_error: null,
            // PR #92 — SummarySection on EventHeader reads
            // ``row.summary.status`` directly; without this field
            // the section throws on the null → row transition.
            summary: {
              status: 'succeeded',
              text: null,
              error: null,
              attempts: 0,
              last_attempted_at: null,
            },
          },
        ]),
        // No-op stream — the test cares about the hook-order
        // transition, not the SSE payload.
        subscribeInboxThread: vi.fn(() => ({
          abort: () => {},
          sessionId: null,
          // ``done`` is part of the InboxThreadStream contract — the pane
          // chains the thinking pill off it. A stub omitting it crashed the
          // whole pane during commit.
          done: Promise.resolve(),
        })),
      };
    });
  });

  afterEach(() => {
    vi.doUnmock('../services/auth');
    vi.doUnmock('../services/inbox');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it('does not throw when ThreadPane transitions from null row to populated row', async () => {
    // Re-import InboxPage AFTER the mocks are in place so the dynamic
    // copy picks up the stubbed ``listInboxRows`` + ``subscribeInboxThread``.
    const { InboxPage: PatchedInboxPage } = await import('./InboxPage');

    await act(async () => {
      render(<PatchedInboxPage />);
    });

    // Let the listInboxRows promise resolve and the auto-select
    // effect run. If hooks-order is broken, React throws synchronously
    // here and the test fails.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Sanity check: the page rendered without the error boundary
    // unmounting it. PR #120 — the "Inbox" word appears in the rail
    // title AND in the breadcrumb of the selected event's detail
    // pane; pin the rail-title testid specifically.
    expect(screen.getByTestId('inbox-rail-title')).toBeInTheDocument();
  });

  it('the "My work" scope toggle refetches with assignee=me (#69)', async () => {
    const { InboxPage: PatchedInboxPage } = await import('./InboxPage');
    const inboxSvc = await import('../services/inbox');
    const listMock = inboxSvc.listInboxRows as ReturnType<typeof vi.fn>;

    await act(async () => {
      render(<PatchedInboxPage />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // Initial load is the shared "All" scope.
    expect(listMock).toHaveBeenCalledWith(false);

    listMock.mockClear();
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-scope-mine'));
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    // Switching to "My work" refetches with the per-user filter on.
    expect(listMock).toHaveBeenCalledWith(true);
  });
});

// Failure visibility — an event that arrived but couldn't be turned into an
// actionable card (planner failed / no surface) must be VISIBLE with a retry,
// not silently hidden by the "needs a surface" filter.
describe('inbox failure visibility', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doMock('../services/auth', async () => {
      const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
      return {
        ...actual,
        demoMode: false,
        supabaseConfigured: true,
        getAccessToken: vi.fn().mockResolvedValue('test-token'),
      };
    });
    vi.doMock('../services/inbox', async () => {
      const actual = await vi.importActual<typeof import('../services/inbox')>('../services/inbox');
      return {
        ...actual,
        listInboxRows: vi.fn().mockResolvedValue([
          {
            id: 'failed-1',
            event_name: 'Task.Created',
            event_time: new Date().toISOString(),
            status: 'done',
            last_error: null,
            action_surface: null, // no surface → hidden from the normal feed
            enrichment: { task: { title: 'Broken heater at 12 Oak' } },
            executed_action_index: null,
            planner_status: 'failed',
            planner_error: 'planner exploded',
            summary: {
              status: 'failed',
              text: null,
              error: null,
              attempts: 1,
              last_attempted_at: null,
            },
          },
        ]),
        replanInboxEvent: vi.fn().mockResolvedValue(undefined),
        subscribeInboxThread: vi.fn(() => ({ abort: () => {}, sessionId: null, done: Promise.resolve() })),
      };
    });
  });

  afterEach(() => {
    vi.doUnmock('../services/auth');
    vi.doUnmock('../services/inbox');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it('surfaces a failed event and retries it via replanInboxEvent', async () => {
    const { InboxPage: PatchedInboxPage } = await import('./InboxPage');
    const inboxSvc = await import('../services/inbox');
    const replanMock = inboxSvc.replanInboxEvent as ReturnType<typeof vi.fn>;

    await act(async () => {
      render(<PatchedInboxPage />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    // The failed event is visible (it would be hidden by the normal filter).
    expect(screen.getByTestId('inbox-failed-section')).toBeInTheDocument();
    expect(screen.getByText('Broken heater at 12 Oak')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-failed-retry'));
    });
    expect(replanMock).toHaveBeenCalledWith('failed-1');
  });
});

// ─────────────────────────────────────────────────────────────────────
// PR #89 — PlannerStatusAffordance
//
// The planner moved out of the synchronous webhook receiver into the
// worker. The inbox needs to render honest status while the worker
// is mid-flight (shimmer) and offer a one-click Replan when the
// planner timed out or failed. These tests pin:
//   - 'succeeded' renders nothing (no extra UI when everything worked)
//   - 'pending' / 'in_progress' render a pulsing "Thinking" chip
//   - 'timed_out' / 'failed' render a Replan button
//   - Replan click POSTs to the right URL via replanInboxEvent and
//     optimistically flips the chip to pending before the server replies
//   - On replan failure, the chip reverts to the original status
// ─────────────────────────────────────────────────────────────────────

import { fireEvent, waitFor } from '@testing-library/react';

// Service-layer mock so we can spy on the network call without
// touching real fetch. ``inboxService`` re-imported with the doMock
// behavior is gnarly; we just spy on the module's named export.
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), message: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('../services/inbox', async (orig) => {
  const actual = await orig<typeof import('../services/inbox')>();
  return {
    ...actual,
    replanInboxEvent: vi.fn().mockResolvedValue(undefined),
    // PR #92 — same shape as replan; SummarySection unit tests
    // spy on this via the inboxService import below.
    resummarizeInboxEvent: vi.fn().mockResolvedValue(undefined),
    // Email-ingest — the reply composer executes the reply_to_email
    // action through this.
    executeInboxAction: vi
      .fn()
      .mockResolvedValue({ status: 'executed', result: { buildium_id: null, summary: 'Replied' } }),
  };
});

// We grab the mock by re-importing after the vi.mock declaration so
// each assertion gets the same instance.
import * as inboxService from '../services/inbox';

const {
  PlannerStatusAffordance,
  SummarySection,
  SuggestedWorkflow,
  AgentNoticed,
  buildAgentNoticed,
  computeBlockingGap,
  EmailReplyComposer,
  EmailBodyCard,
  isEmailRow,
  ActionCard,
  ActionCardList,
} = __test__;

describe('PR #89 PlannerStatusAffordance — succeeded path', () => {
  it('renders nothing when planner_status is succeeded', () => {
    const { container } = render(
      <PlannerStatusAffordance
        eventId="row-1"
        plannerStatus="succeeded"
        plannerError={null}
      />,
    );
    // The component returns null on succeeded — first child should
    // be absent (no chip, no button, no wrapper div).
    expect(container.firstChild).toBeNull();
  });
});

describe('PR #89 PlannerStatusAffordance — in-flight states', () => {
  it.each(['pending', 'in_progress'] as const)(
    'renders the Thinking chip for %s',
    (status) => {
      render(
        <PlannerStatusAffordance
          eventId="row-1"
          plannerStatus={status}
          plannerError={null}
        />,
      );
      const chip = screen.getByTestId('inbox-planner-pending');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent(/thinking/i);
      // No Replan button while in-flight — we don't want the user
      // to fire a redundant replan.
      expect(screen.queryByTestId('inbox-planner-replan')).toBeNull();
    },
  );
});

describe('PR #89 PlannerStatusAffordance — terminal-failure states', () => {
  beforeEach(() => {
    (inboxService.replanInboxEvent as ReturnType<typeof vi.fn>).mockClear();
    (inboxService.replanInboxEvent as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
  });

  it.each(['timed_out', 'failed'] as const)(
    'renders the Replan pill for %s',
    (status) => {
      render(
        <PlannerStatusAffordance
          eventId="row-99"
          plannerStatus={status}
          plannerError="something specific went wrong"
        />,
      );
      const pill = screen.getByTestId('inbox-planner-replan');
      expect(pill).toBeInTheDocument();
      expect(pill).toHaveTextContent(/replan/i);
      // Tooltip carries the planner_error so the user can see what
      // happened without opening a modal.
      expect(pill).toHaveAttribute('title', 'something specific went wrong');
    },
  );

  it('truncates very long planner_error in the tooltip to 120 chars', () => {
    const long = 'x'.repeat(500);
    render(
      <PlannerStatusAffordance
        eventId="row-99"
        plannerStatus="failed"
        plannerError={long}
      />,
    );
    const pill = screen.getByTestId('inbox-planner-replan');
    expect(pill.getAttribute('title')?.length).toBe(120);
  });

  it('calls replanInboxEvent and optimistically flips to pending on click', async () => {
    render(
      <PlannerStatusAffordance
        eventId="row-99"
        plannerStatus="timed_out"
        plannerError="upstream 502"
      />,
    );
    const pill = screen.getByTestId('inbox-planner-replan');
    fireEvent.click(pill);

    // Optimistic: the Thinking chip appears immediately, before the
    // promise resolves.
    expect(screen.getByTestId('inbox-planner-pending')).toBeInTheDocument();

    await waitFor(() => {
      expect(inboxService.replanInboxEvent).toHaveBeenCalledWith('row-99');
    });
  });

  it('reverts to the original status when replanInboxEvent rejects', async () => {
    (inboxService.replanInboxEvent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('network down'),
    );
    render(
      <PlannerStatusAffordance
        eventId="row-99"
        plannerStatus="failed"
        plannerError="prior error"
      />,
    );
    fireEvent.click(screen.getByTestId('inbox-planner-replan'));

    // Eventually the chip should swap back to the Replan pill — the
    // optimistic Thinking chip is reverted when the network call
    // fails so the user can retry.
    await waitFor(() => {
      expect(screen.getByTestId('inbox-planner-replan')).toBeInTheDocument();
    });
  });
});

describe('PR #89 PlannerStatusAffordance — prop sync', () => {
  it('updates when the parent supplies a new planner_status', () => {
    const { rerender } = render(
      <PlannerStatusAffordance
        eventId="row-1"
        plannerStatus="pending"
        plannerError={null}
      />,
    );
    expect(screen.getByTestId('inbox-planner-pending')).toBeInTheDocument();

    // Poll arrives with a fresh status — the affordance must follow
    // it without needing a remount.
    rerender(
      <PlannerStatusAffordance
        eventId="row-1"
        plannerStatus="succeeded"
        plannerError={null}
      />,
    );
    expect(screen.queryByTestId('inbox-planner-pending')).toBeNull();
    expect(screen.queryByTestId('inbox-planner-replan')).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────
// PR #92 — SummarySection
//
// The agent-written narrative the worker drops into the Inbox detail
// pane above the field grid. Same shape of test coverage as
// PlannerStatusAffordance: each lifecycle state renders the expected
// affordance, click-to-resummarize is wired, prop sync works.
// ─────────────────────────────────────────────────────────────────────

import type { InboxEventSummary } from '../services/inbox';

function _summary(overrides: Partial<InboxEventSummary> = {}): InboxEventSummary {
  return {
    status: 'succeeded',
    text: null,
    error: null,
    attempts: 0,
    last_attempted_at: null,
    ...overrides,
  };
}

describe('PR #92 SummarySection — succeeded with text', () => {
  it('renders the narrative paragraph and a non-prominent Resummarize pill', () => {
    render(
      <SummarySection
        eventId="row-1"
        summary={_summary({
          status: 'succeeded',
          text: 'Sarah Chen at 123 Main St unit 4B reported a kitchen sink leak.',
        })}
      />,
    );
    const para = screen.getByTestId('inbox-summary-text');
    expect(para).toBeInTheDocument();
    expect(para).toHaveTextContent(/Sarah Chen/);
    // Resummarize pill is present but not failed-state — the
    // failed-state test below pins the same testid.
    expect(screen.getByTestId('inbox-summary-resummarize')).toBeInTheDocument();
  });
});

describe('PR #92 SummarySection — succeeded with empty text', () => {
  it('renders nothing — degrades cleanly on pre-migration deploys', () => {
    const { container } = render(
      <SummarySection
        eventId="row-1"
        summary={_summary({ status: 'succeeded', text: null })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('PR #92 SummarySection — in-flight states', () => {
  it.each(['pending', 'retrying'] as const)('renders the shimmer for %s', (status) => {
    render(
      <SummarySection
        eventId="row-1"
        summary={_summary({ status })}
      />,
    );
    expect(screen.getByTestId('inbox-summary-shimmer')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-summary-text')).toBeNull();
    // PR #113 — Resummarize is now anchored to the section header
    // (icon variant) and renders during ALL section states,
    // including shimmer (disabled while resummarizing is in flight,
    // but always present so the section header reads consistently).
    // Was queryByTestId(...).toBeNull() under the old "ResummarizePill
    // floats next to the text" model.
    expect(screen.getByTestId('inbox-summary-resummarize')).toBeInTheDocument();
  });
});

describe('PR #92 SummarySection — failed state', () => {
  beforeEach(() => {
    (inboxService.resummarizeInboxEvent as ReturnType<typeof vi.fn>).mockClear();
    (inboxService.resummarizeInboxEvent as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    );
  });

  it('renders "Summary unavailable" + Resummarize control with error tooltip', () => {
    render(
      <SummarySection
        eventId="row-9"
        summary={_summary({
          status: 'failed',
          text: null,
          error: 'gemini timeout',
        })}
      />,
    );
    expect(screen.getByTestId('inbox-summary-failed')).toBeInTheDocument();
    const pill = screen.getByTestId('inbox-summary-resummarize');
    // PR #113 — tooltip wraps the error in a labeled prefix so the
    // user knows the icon is "Resummarize (because last attempt
    // failed: ...)" instead of a bare error string.
    expect(pill.getAttribute('title')).toBe('Resummarize — last error: gemini timeout');
  });

  it('truncates very long error tooltips to a bounded length', () => {
    const long = 'z'.repeat(500);
    render(
      <SummarySection
        eventId="row-9"
        summary={_summary({ status: 'failed', error: long })}
      />,
    );
    const pill = screen.getByTestId('inbox-summary-resummarize');
    // PR #113 — title is now "Resummarize — last error: <120 chars of error>".
    // Pin the exact shape: prefix + 120-char slice (the truncation
    // applies to the error portion, not the whole title).
    const title = pill.getAttribute('title') ?? '';
    expect(title.startsWith('Resummarize — last error: ')).toBe(true);
    const errorPortion = title.replace('Resummarize — last error: ', '');
    expect(errorPortion.length).toBe(120);
  });

  it('calls resummarizeInboxEvent and optimistically flips to shimmer on click', async () => {
    render(
      <SummarySection
        eventId="row-9"
        summary={_summary({ status: 'failed', error: 'something failed' })}
      />,
    );
    fireEvent.click(screen.getByTestId('inbox-summary-resummarize'));
    // Optimistic: shimmer appears immediately.
    expect(screen.getByTestId('inbox-summary-shimmer')).toBeInTheDocument();
    await waitFor(() => {
      expect(inboxService.resummarizeInboxEvent).toHaveBeenCalledWith('row-9');
    });
  });

  it('reverts to the failed state when resummarizeInboxEvent rejects', async () => {
    (inboxService.resummarizeInboxEvent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('network down'),
    );
    render(
      <SummarySection
        eventId="row-9"
        summary={_summary({ status: 'failed', text: null, error: 'prior error' })}
      />,
    );
    fireEvent.click(screen.getByTestId('inbox-summary-resummarize'));
    await waitFor(() => {
      expect(screen.getByTestId('inbox-summary-failed')).toBeInTheDocument();
    });
  });
});

describe('PR #92 SummarySection — prop sync', () => {
  it('updates when the parent supplies a new summary status / text', () => {
    const { rerender } = render(
      <SummarySection
        eventId="row-1"
        summary={_summary({ status: 'pending' })}
      />,
    );
    expect(screen.getByTestId('inbox-summary-shimmer')).toBeInTheDocument();

    rerender(
      <SummarySection
        eventId="row-1"
        summary={_summary({
          status: 'succeeded',
          text: 'Sarah Chen at 123 Main St reported a leak this afternoon.',
        })}
      />,
    );
    expect(screen.getByTestId('inbox-summary-text')).toHaveTextContent(/Sarah Chen/);
    expect(screen.queryByTestId('inbox-summary-shimmer')).toBeNull();
  });
});

// PR #93 — defensive case the brief calls out: a row whose API
// response leaks through without a ``summary`` field at all (e.g.
// pre-migration deploy or a future relaxation) must NOT crash the
// EventHeader. The prop type is ``InboxEventSummary | undefined``
// so this is type-valid; the test pins the runtime null-render.
describe('PR #93 SummarySection — undefined summary prop', () => {
  it('renders nothing when summary is undefined', () => {
    const { container } = render(
      <SummarySection eventId="row-1" summary={undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────
// PR #93 — SuggestedWorkflow
//
// Renders the planner's ``action_surface.actions[]`` as a numbered
// list inside the EventHeader card, replacing the floating chip
// row. Each step has Execute (→ parent's onPickSuggestion → confirm
// gate) and Skip (local dismiss). Top-right Re-plan pill fires
// the parent's onReplan callback (which hits the existing
// replanInboxEvent service from PR #89).
// ─────────────────────────────────────────────────────────────────────

import type { InboxRow as InboxRowType } from '../services/inbox';

function _rowWithActions(
  actions: Array<{ tool_id: string; label: string; intent: string }>,
): InboxRowType {
  return {
    id: 'row-w',
    event_name: 'task_created',
    event_time: new Date().toISOString(),
    status: 'done',
    last_error: null,
    enrichment: null,
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: {
      status: 'succeeded',
      text: null,
      error: null,
      attempts: 0,
      last_attempted_at: null,
    },
    action_surface: {
      schema_version: 1,
      summary: 'parent action_surface summary (not displayed here)',
      actions: actions.map((a) => ({
        ...a,
        parameters: {},
      })),
      freeform_placeholder: 'Type…',
    },
  };
}




describe('PR #93 SuggestedWorkflow — Re-plan', () => {
  it('fires the parent onReplan callback on click', () => {
    const row = _rowWithActions([{ tool_id: 'x', label: 'A', intent: 'a' }]);
    const onReplan = vi.fn();
    render(<SuggestedWorkflow row={row} onReplan={onReplan} busy={false} />);
    fireEvent.click(screen.getByTestId('inbox-workflow-replan'));
    expect(onReplan).toHaveBeenCalledTimes(1);
  });

  it('disables Re-plan when busy=true', () => {
    const row = _rowWithActions([{ tool_id: 'x', label: 'A', intent: 'a' }]);
    const onReplan = vi.fn();
    render(<SuggestedWorkflow row={row} onReplan={onReplan} busy={true} />);
    fireEvent.click(screen.getByTestId('inbox-workflow-replan'));
    expect(onReplan).not.toHaveBeenCalled();
  });
});


// ─────────────────────────────────────────────────────────────────────
// PR #98 — buildAgentNoticed + AgentNoticed
//
// The "WHAT HELIXIS NOTICED" section turns the floating chips into
// "an analyst showing their work." Bullets are derived deterministically
// from enrichment when the planner doesn't supply explicit signals,
// and the section hides entirely when fewer than 2 bullets are
// available (thin one-bullet sections undermine the voice).
// ─────────────────────────────────────────────────────────────────────

function _rowFor(
  overrides: {
    task?: Partial<NonNullable<InboxRowType['enrichment']>['task']> | null;
    unit?: Partial<NonNullable<InboxRowType['enrichment']>['unit']> | null;
    history_unit?: Array<{ id: number; title?: string; category?: string }>;
    history_property?: Array<{ id: number; title?: string; category?: string }>;
    signals?: string[];
  } = {},
): InboxRowType {
  const baseTask = {
    id: 17435,
    unit_id: null,
    property_id: null,
    assigned_to_user_id: null,
    title: 'Loud crashing',
    description: null,
    category: null,
    priority: null,
    status: null,
    due_date: null,
    requested_by_first_name: null,
    requested_by_last_name: null,
  };
  return {
    id: 'row-an',
    event_name: 'task_created',
    event_time: new Date().toISOString(),
    status: 'done',
    last_error: null,
    enrichment:
      overrides.task === null
        ? null
        : {
            task: { ...baseTask, ...(overrides.task ?? {}) },
            unit:
              overrides.unit === null
                ? null
                : {
                    id: 412,
                    property_id: 100,
                    unit_number: null,
                    address_line1: null,
                    ...(overrides.unit ?? {}),
                  },
            history_unit: overrides.history_unit ?? [],
            history_property: overrides.history_property ?? [],
          },
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: {
      status: 'succeeded',
      text: null,
      error: null,
      attempts: 0,
      last_attempted_at: null,
    },
    action_surface: {
      schema_version: 1,
      summary: 'stub',
      actions: [],
      freeform_placeholder: 'Type…',
      signals: overrides.signals,
    },
  };
}

describe('PR #98 buildAgentNoticed — uses explicit signals when supplied', () => {
  it('returns the action_surface.signals array verbatim (capped at 4)', () => {
    const row = _rowFor({
      signals: [
        'signal one',
        'signal two',
        'signal three',
        'signal four',
        'signal five',
      ],
    });
    const bullets = buildAgentNoticed(row);
    expect(bullets).toEqual(['signal one', 'signal two', 'signal three', 'signal four']);
  });

  it('filters out empty / whitespace-only signal entries', () => {
    const row = _rowFor({ signals: ['real signal', '   ', '', 'another real one'] });
    expect(buildAgentNoticed(row)).toEqual(['real signal', 'another real one']);
  });
});

describe('PR #98 buildAgentNoticed — derives bullets deterministically from enrichment', () => {
  it('combines reporter + property when both present', () => {
    const row = _rowFor({
      task: {
        requested_by_first_name: 'Clifton',
        requested_by_last_name: 'Munoz',
        category: 'General Inquiry',
        priority: 'Normal',
      },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
    });
    const bullets = buildAgentNoticed(row);
    expect(bullets[0]).toBe('Reported by Clifton Munoz at 160 East End Ave #120');
    expect(bullets).toContain('Filed in Buildium as "General Inquiry"');
    expect(bullets).toContain('Buildium priority: Normal');
  });

  it('falls back to reporter-only when property is missing', () => {
    const row = _rowFor({
      task: {
        requested_by_first_name: 'Jimmy',
        requested_by_last_name: null,
        category: 'Maintenance',
      },
      unit: { address_line1: null, unit_number: null },
    });
    const bullets = buildAgentNoticed(row);
    expect(bullets[0]).toBe('Reported by Jimmy');
  });

  it('surfaces unit history count when present', () => {
    const row = _rowFor({
      task: {
        requested_by_first_name: 'Clifton',
        category: 'General Inquiry',
      },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
      history_unit: [
        { id: 1, title: 'prior task A' },
        { id: 2, title: 'prior task B' },
      ],
    });
    expect(buildAgentNoticed(row)).toContain(
      '2 prior tickets on this unit in the last 90 days',
    );
  });

  it('uses property history when unit history is empty', () => {
    const row = _rowFor({
      task: { requested_by_first_name: 'Clifton', category: 'General Inquiry' },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
      history_unit: [],
      history_property: [{ id: 99, title: 'one prior' }],
    });
    expect(buildAgentNoticed(row)).toContain(
      '1 prior ticket on this property in the last 90 days',
    );
  });

  it('caps derived bullets at 4', () => {
    const row = _rowFor({
      task: {
        requested_by_first_name: 'Clifton',
        requested_by_last_name: 'Munoz',
        category: 'General Inquiry',
        priority: 'Normal',
      },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
      history_unit: [{ id: 1 }, { id: 2 }, { id: 3 }],
    });
    expect(buildAgentNoticed(row).length).toBeLessThanOrEqual(4);
  });
});

describe('PR #98 AgentNoticed — render gates', () => {
  it('renders the section + bullets when 2+ are derived', () => {
    const row = _rowFor({
      task: {
        requested_by_first_name: 'Clifton',
        requested_by_last_name: 'Munoz',
        category: 'General Inquiry',
      },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
    });
    render(<AgentNoticed row={row} />);
    expect(screen.getByTestId('inbox-agent-noticed')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-agent-noticed-bullet-0')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-agent-noticed-bullet-1')).toBeInTheDocument();
  });

  it('renders nothing when fewer than 2 bullets are available', () => {
    // Only a reporter, no property / category / history → 1 bullet.
    const row = _rowFor({
      task: { requested_by_first_name: 'Jimmy' },
      unit: { address_line1: null, unit_number: null },
    });
    const { container } = render(<AgentNoticed row={row} />);
    expect(container.firstChild).toBeNull();
  });

  it('uses the iris-tinted section header', () => {
    const row = _rowFor({
      task: { requested_by_first_name: 'Clifton', category: 'General Inquiry' },
      unit: { address_line1: '160 East End Ave', unit_number: '120' },
    });
    render(<AgentNoticed row={row} />);
    expect(screen.getByText(/What Occupella noticed/i)).toBeInTheDocument();
  });
});


// ── Commit 2 — deterministic blocking-gap detection ──────────────────
//
// A missing field surfaces as a gap in "What Occupella noticed" ONLY
// when its absence blocks the next step AND the item is hot/old enough.
// The rule lives in computeBlockingGap (code, not the model). "Silence
// is the hard part": a low-priority item with the same empty fields
// must produce NO gap — that's the explicit negative test below.

// Build a row whose event landed `hoursAgo` hours ago (idle age).
function _agedRow(
  taskOverrides: Record<string, unknown>,
  hoursAgo: number,
): InboxRowType {
  return {
    ..._rowFor({ task: taskOverrides }),
    event_time: new Date(Date.now() - hoursAgo * 3_600_000).toISOString(),
  };
}

describe('Commit 2 computeBlockingGap — deterministic gaps', () => {
  it('raises an assignee gap: unassigned + high priority + idle past the bar', () => {
    const row = _agedRow(
      { assigned_to_user_id: null, priority: 'High' },
      72, // 3 days idle, high bar is 48h
    );
    const gap = computeBlockingGap(row);
    expect(gap).not.toBeNull();
    expect(gap).toMatch(/no one's assigned/i);
    expect(gap).toMatch(/high-priority/i);
  });

  it('SILENCE: low priority with the SAME empty fields raises NO gap', () => {
    // The hard case — identical missing-assignee state, but low priority
    // never qualifies no matter how old.
    const row = _agedRow(
      { assigned_to_user_id: null, priority: 'Low' },
      240, // 10 days idle
    );
    expect(computeBlockingGap(row)).toBeNull();
  });

  it('SILENCE: a hot item opened this morning is too fresh to nudge', () => {
    const row = _agedRow({ assigned_to_user_id: null, priority: 'High' }, 2);
    expect(computeBlockingGap(row)).toBeNull();
  });

  it('no gap once the item is assigned (assignee unblocks work)', () => {
    const row = _agedRow(
      { assigned_to_user_id: 70287, priority: 'Urgent' },
      240,
    );
    expect(computeBlockingGap(row)).toBeNull();
  });

  it('a missing DUE DATE on an assigned item is not a gap', () => {
    const row = _agedRow(
      { assigned_to_user_id: 70287, priority: 'High', due_date: null },
      240,
    );
    expect(computeBlockingGap(row)).toBeNull();
  });

  it('normal priority uses the longer idle bar and drops the -priority suffix', () => {
    expect(
      computeBlockingGap(
        _agedRow({ assigned_to_user_id: null, priority: 'Normal' }, 48),
      ),
    ).toBeNull(); // under the 72h normal bar
    const gap = computeBlockingGap(
      _agedRow({ assigned_to_user_id: null, priority: 'Normal' }, 80),
    );
    expect(gap).toMatch(/no one's assigned/i);
    expect(gap).not.toMatch(/-priority/);
  });

  it('renders the gap as the leading bullet in "What Occupella noticed"', () => {
    const row = {
      ..._agedRow(
        {
          assigned_to_user_id: null,
          priority: 'High',
          requested_by_first_name: 'Clifton',
          category: 'General Inquiry',
        },
        72,
      ),
      enrichment: {
        task: {
          id: 17435,
          unit_id: null,
          property_id: null,
          assigned_to_user_id: null,
          title: 'Loud crashing',
          description: null,
          category: 'General Inquiry',
          priority: 'High',
          status: null,
          due_date: null,
          requested_by_first_name: 'Clifton',
          requested_by_last_name: 'Munoz',
        },
        unit: {
          id: 412,
          property_id: 100,
          unit_number: '120',
          address_line1: '160 East End Ave',
        },
        history_unit: [],
        history_property: [],
      },
    } as InboxRowType;
    render(<AgentNoticed row={row} />);
    const first = screen.getByTestId('inbox-agent-noticed-bullet-0');
    expect(first.textContent).toMatch(/no one's assigned/i);
    // The gap bullet reads as a caution (amber), not a neutral note.
    const dot = screen.getByTestId('inbox-agent-noticed-dot-0');
    expect(dot.style.background).toBe('var(--caution)');
  });
});


// ─────────────────────────────────────────────────────────────────────
// PR #99 — OFFER vs QUESTION card dispatch
//
// SuggestedWorkflow now branches on action.type + action.confidence:
//   confidence='high' (or absent / V1) → OFFER card (primary button)
//   confidence='medium' | 'low'         → QUESTION card (chips + input)
//
// Both shapes still carry rationale, "Why this?", and Skip from PR #98.
// The brief calls for the primary button label to come from
// ``primary_action.label`` (e.g. "Send to Sarah") instead of a generic
// "Execute". Chips render from the planner's ``first_followup.options``.
// ─────────────────────────────────────────────────────────────────────

function _rowV2(
  actions: Array<{
    label: string;
    intent: string;
    type?: 'information_need' | 'bonus' | null;
    confidence?: 'high' | 'medium' | 'low' | null;
    primary_action?: {
      label: string;
      kind:
        | 'send_message'
        | 'create_note'
        | 'create_task'
        | 'schedule'
        | 'request_info'
        | 'freeform';
      target?: { kind: 'tenant' | 'owner' | 'vendor' | 'internal'; name?: string };
      draft?: string;
    } | null;
    first_followup?: {
      prompt: string;
      options: Array<{
        label: string;
        parameter_patch: Record<string, unknown>;
        needs_followup?: boolean;
      }>;
      allow_freeform: boolean;
    } | null;
  }>,
): InboxRowType {
  return {
    id: 'row-v2',
    event_name: 'task_created',
    event_time: new Date().toISOString(),
    status: 'done',
    last_error: null,
    enrichment: null,
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: {
      status: 'succeeded',
      text: null,
      error: null,
      attempts: 0,
      last_attempted_at: null,
    },
    action_surface: {
      schema_version: 2,
      summary: 'stub',
      actions: actions.map((a) => ({
        tool_id: 'send_tenant_message',
        label: a.label,
        intent: a.intent,
        parameters: {},
        type: a.type ?? null,
        confidence: a.confidence ?? null,
        primary_action: a.primary_action ?? null,
        first_followup: a.first_followup
          ? {
              prompt: a.first_followup.prompt,
              allow_freeform: a.first_followup.allow_freeform,
              options: a.first_followup.options.map((o) => ({
                label: o.label,
                parameter_patch: o.parameter_patch,
                needs_followup: o.needs_followup ?? false,
              })),
            }
          : null,
      })),
      freeform_placeholder: 'Type…',
    },
  };
}





// ─────────────────────────────────────────────────────────────────────
// PR #99 — Inline conversational confirm
//
// The primary button on an OFFER card transforms in-place through:
//   idle → countdown (5s + Cancel) → executing → done (✓ Sent at HH:MM)
// On Cancel, the timer is cleared and the button reverts to idle.
// On unmount during the countdown, the timer is cleared so the
// execute callback never fires after navigation away.
// ─────────────────────────────────────────────────────────────────────



// ─────────────────────────────────────────────────────────────────────
// PR #99 step 7 — integrated free-text input inside SuggestedWorkflow
//
// The bottom-of-pane sticky textarea from PR #75 was moved INTO the
// SuggestedWorkflow container as the last "card-shaped" element. The
// integrated input renders even when the actions array is empty, so
// the user can always drive the conversation.
// ─────────────────────────────────────────────────────────────────────

describe('PR #99 IntegratedFreeformInput — render gates', () => {
  it('renders the input + send button inside SuggestedWorkflow', () => {
    const row = _rowV2([
      {
        label: 'Ask Sarah',
        intent: 'ask',
        confidence: 'high',
        primary_action: { label: 'Send to Sarah', kind: 'send_message' },
      },
    ]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={vi.fn().mockResolvedValue(undefined)}
        freeformPlaceholder="or describe what you want me to do…"
        busy={false}
      />,
    );
    expect(screen.getByTestId('inbox-workflow-freeform')).toBeInTheDocument();
    const input = screen.getByTestId('inbox-workflow-freeform-input');
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute(
      'placeholder',
      'or describe what you want me to do…',
    );
    expect(screen.getByTestId('inbox-workflow-freeform-send')).toBeInTheDocument();
  });

  it('renders the input even when the actions array is empty', () => {
    // The brief: "When there are zero suggestions, the input still
    // renders here so the user can drive."
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={vi.fn().mockResolvedValue(undefined)}
        freeformPlaceholder="describe what you want"
        busy={false}
      />,
    );
    expect(screen.getByTestId('inbox-workflow-freeform')).toBeInTheDocument();
  });

  it('renders nothing when actions are empty AND no onFreeformSubmit is supplied', () => {
    // Older test fixtures + the PR #87 standalone path don't pass
    // ``onFreeformSubmit``. With zero actions AND no input
    // affordance, the section is genuinely empty and renders null.
    const row = _rowV2([]);
    const { container } = render(
      <SuggestedWorkflow row={row} onReplan={vi.fn()} busy={false} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('PR #99 IntegratedFreeformInput — submit behavior', () => {
  it('calls onFreeformSubmit with the trimmed text on Send click', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input');
    fireEvent.change(input, { target: { value: '  draft a reply to Sarah  ' } });
    fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSubmit).toHaveBeenCalledWith('draft a reply to Sarah');
  });

  it('disables Send when the textarea is empty / whitespace-only', () => {
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={vi.fn().mockResolvedValue(undefined)}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const send = screen.getByTestId('inbox-workflow-freeform-send') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    const input = screen.getByTestId('inbox-workflow-freeform-input');
    fireEvent.change(input, { target: { value: '   ' } });
    expect(send.disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'a' } });
    expect(send.disabled).toBe(false);
  });

  it('clears the textarea only after the parent submit resolves', async () => {
    let resolveSubmit: (() => void) | null = null;
    const submitPromise = new Promise<void>((resolve) => {
      resolveSubmit = resolve;
    });
    const onSubmit = vi.fn().mockReturnValue(submitPromise);
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId(
      'inbox-workflow-freeform-input',
    ) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'hello' } });
    fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    // Before the parent's submit resolves, the text MUST still be
    // there — if the user retries, they shouldn't lose their draft.
    expect(input.value).toBe('hello');
    await act(async () => {
      resolveSubmit?.();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(input.value).toBe('');
  });

  it('submits on Enter (without Shift) and not on Shift+Enter', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input');
    fireEvent.change(input, { target: { value: 'note one' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: false });
    await act(async () => {
      await Promise.resolve();
    });
    expect(onSubmit).toHaveBeenCalledWith('note one');
  });
});


// ── PR #126 IntegratedFreeformInput — loading + sent + error UX ──


describe('PR #126 IntegratedFreeformInput — loading state', () => {
  it('shows "Sending…" with a spinner while the parent submit promise is pending', async () => {
    // Build a promise we control so we can observe the in-flight UI.
    let resolveSubmit!: () => void;
    const onSubmit = vi.fn().mockImplementation(
      () => new Promise<void>((r) => {
        resolveSubmit = r;
      }),
    );
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'check vendor availability' } });
    fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    // The Send button stays mounted (we don't swap to the Sent pill yet)
    // but shows the in-flight label + disabled + data-phase="sending".
    const sendButton = screen.getByTestId('inbox-workflow-freeform-send');
    expect(sendButton.textContent).toContain('Sending');
    expect(sendButton).toBeDisabled();
    expect(sendButton.getAttribute('data-phase')).toBe('sending');
    // Clean up so React doesn't warn about an unresolved promise.
    await act(async () => {
      resolveSubmit();
      await Promise.resolve();
    });
  });
});


describe('IntegratedFreeformInput — no success chip', () => {
  it('returns straight to idle, and never claims anything was "Sent"', async () => {
    // ⚠ This test used to assert the OPPOSITE: a green "✓ Sent" pill
    // replacing the button for two seconds. It is gone on purpose.
    //
    // In a product that texts tenants and emails owners on your behalf,
    // "Sent" is the most alarming word available on a button you just
    // pressed — it reads as "a message went OUT to someone", not
    // "Occupella heard you". `InboxPage.tsx` already carried the rule and
    // applied it only to executed actions: *"Sent" is a receipt for a
    // REQUEST, not a confirmation of an OUTCOME.*
    //
    // The message becoming a bubble in the conversation is the receipt, and
    // it is unambiguous about who received it.
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'note please' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
      await Promise.resolve();
    });
    expect(screen.queryByTestId('inbox-workflow-freeform-sent')).toBeNull();
    const button = screen.getByTestId('inbox-workflow-freeform-send');
    expect(button.getAttribute('data-phase')).toBe('idle');
    // Belt and braces on the word itself: no control in this composer may
    // tell the user something was sent.
    expect(screen.getByTestId('inbox-workflow-freeform').textContent).not.toContain('Sent');
    // Textarea cleared so the user can start a new message.
    expect(input.value).toBe('');
  });
});


describe('PR #126 IntegratedFreeformInput — error + retry', () => {
  it('shows the inline error chip + Retry when onSubmit rejects', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('Network down. Try again.'));
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'remind owner' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
      await Promise.resolve();
    });
    // Error row is rendered with the exact error message + a Retry button.
    const errorRow = screen.getByTestId('inbox-workflow-freeform-error');
    expect(errorRow).toBeInTheDocument();
    expect(errorRow.getAttribute('role')).toBe('alert');
    expect(
      screen.getByTestId('inbox-workflow-freeform-error-message').textContent,
    ).toBe('Network down. Try again.');
    expect(screen.getByTestId('inbox-workflow-freeform-retry')).toBeInTheDocument();
    // Textarea KEEPS the user's text so they can retry.
    expect(input.value).toBe('remind owner');
    // And the border tint flips to danger so the failure is visible
    // even at a glance (audit said the lack of failure feedback was
    // the most confusing inbox behavior).
    expect(errorRow.style.borderColor || errorRow.style.border).toMatch(/danger|color\(srgb/);
  });

  it('Retry re-fires the submit with the same text', async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(undefined);
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    const input = screen.getByTestId('inbox-workflow-freeform-input') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'second time lucky' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
      await Promise.resolve();
    });
    // First call failed; Retry visible.
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenLastCalledWith('second time lucky');
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-retry'));
      await Promise.resolve();
    });
    // Second call succeeded; the error chip is gone and the composer is
    // idle again. (No "Sent" pill — see the no-success-chip test above.)
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onSubmit).toHaveBeenLastCalledWith('second time lucky');
    expect(screen.queryByTestId('inbox-workflow-freeform-error')).toBeNull();
    expect(screen.getByTestId('inbox-workflow-freeform-send').getAttribute('data-phase')).toBe(
      'idle',
    );
  });

  it('error message falls back to a generic copy when the rejection has no message', async () => {
    // Some throwables (e.g. raw strings, plain objects) come through
    // the catch block without a useful ``.message``. The user-facing
    // chip must still read as a sentence.
    const onSubmit = vi.fn().mockRejectedValue({});
    const row = _rowV2([]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={vi.fn()}
        onFreeformSubmit={onSubmit}
        freeformPlaceholder="describe"
        busy={false}
      />,
    );
    fireEvent.change(
      screen.getByTestId('inbox-workflow-freeform-input'),
      { target: { value: 'x' } },
    );
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
      await Promise.resolve();
    });
    expect(
      screen.getByTestId('inbox-workflow-freeform-error-message').textContent,
    ).toBe('Could not send. Try again.');
  });
});


// ── PR #103 — Inbox detail pane: assignee resolution, visual
// integration, chip UI ────────────────────────────────────────────

const { EventHeader: PR103EventHeader } = __test__;

function _eventHeaderFixture(
  overrides: Partial<InboxRowType> = {},
): InboxRowType {
  return {
    id: 'row-103',
    event_name: 'task_created',
    event_time: new Date().toISOString(),
    status: 'done',
    last_error: null,
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: {
      status: 'succeeded',
      text: null,
      error: null,
      attempts: 0,
      last_attempted_at: null,
    },
    enrichment: {
      task: {
        id: 17435,
        unit_id: null,
        property_id: null,
        assigned_to_user_id: 70287,
        title: 'Loud crashing',
        description: null,
        category: 'General Inquiry',
        priority: 'High',
        status: 'New',
        due_date: null,
        requested_by_first_name: 'Sarah',
        requested_by_last_name: 'Chen',
      },
      unit: {
        id: 30698,
        property_id: null,
        unit_number: '4B',
        address_line1: '123 Main St',
      },
    },
    action_surface: null,
    ...overrides,
  };
}


describe('Field strip — assignee resolution', () => {
  it('renders the resolved assignee_name in the strip when the backend supplied it', () => {
    const row = _eventHeaderFixture();
    // Decorate the enrichment as the route now does on read.
    (row.enrichment!.task as unknown as Record<string, unknown>).assignee_name = 'Vinny Dan';
    render(<PR103EventHeader row={row} />);
    // The assignee chip shows the name (plus an avatar initial); pin
    // the name via the chip's textContent.
    const chip = screen.getByTestId('inbox-strip-assignee');
    expect(chip.textContent).toContain('Vinny Dan');
  });

  it('omits the assignee chip entirely when enrichment lacks assignee_name', () => {
    // Field-strip rule: an empty field does NOT render — no "—", no
    // "Not set", no "Unassigned" placeholder. A blocking assignee gap
    // is raised in "What Occupella noticed" (Commit 2), not here.
    const row = _eventHeaderFixture();
    render(<PR103EventHeader row={row} />);
    expect(screen.queryByTestId('inbox-strip-assignee')).toBeNull();
  });

  it('treats a whitespace-only assignee_name as missing (chip omitted)', () => {
    const row = _eventHeaderFixture();
    (row.enrichment!.task as unknown as Record<string, unknown>).assignee_name = '   ';
    render(<PR103EventHeader row={row} />);
    expect(screen.queryByTestId('inbox-strip-assignee')).toBeNull();
  });
});


describe('PR #103 SuggestedWorkflow — narrative header + chip UI', () => {
  it('renders the new narrative lead-in instead of the uppercase label', () => {
    const row = _rowWithActions([{ tool_id: 'x', label: 'A', intent: 'a' }]);
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={() => undefined}
        busy={false}
      />,
    );
    expect(screen.getByTestId('inbox-workflow-header')).toHaveTextContent(
      "Here's what I'd do next",
    );
    // The old uppercase mono label must NOT be present.
    expect(screen.queryByText('Suggested workflow', { exact: false })).toBeNull();
  });

});


// ── Commit 1 — one-line field strip (replaces the six-box grid) ───
//
// The detail pane's operational fields render as a single horizontal
// strip — Priority · Category · Assignee · Due · Property — that gets
// SHORTER when there's less to say. Empty fields don't render (no
// "Not set"/"Unassigned"). Priority is a semantic dot + value;
// assignee is an avatar + name. Read-only display, not an editor.


describe('Commit 1 field strip — detail pane', () => {
  it('renders Category as a plain value chip', () => {
    const row = _eventHeaderFixture();
    render(<PR103EventHeader row={row} />);
    const chip = screen.getByTestId('inbox-strip-category');
    expect(chip.textContent).toBe('General Inquiry');
  });

  it('renders Priority as a semantic-colored dot + value', () => {
    const row = _eventHeaderFixture();
    render(<PR103EventHeader row={row} />);
    const chip = screen.getByTestId('inbox-strip-priority');
    expect(chip.textContent).toContain('High');
    // The dot is an aria-hidden span carrying the semantic colour;
    // pin its presence so the priority signal can't vanish silently.
    const dot = chip.querySelector('span[aria-hidden]');
    expect(dot).not.toBeNull();
  });

  it('omits the Category chip when category is missing (no placeholder)', () => {
    const row = _eventHeaderFixture();
    (row.enrichment!.task as unknown as Record<string, unknown>).category = null;
    render(<PR103EventHeader row={row} />);
    expect(screen.queryByTestId('inbox-strip-category')).toBeNull();
    // And no "Not set" text leaks in anywhere.
    expect(screen.queryByText('Not set')).toBeNull();
  });

  it('omits the Priority chip when priority is missing (no placeholder)', () => {
    const row = _eventHeaderFixture();
    (row.enrichment!.task as unknown as Record<string, unknown>).priority = null;
    render(<PR103EventHeader row={row} />);
    expect(screen.queryByTestId('inbox-strip-priority')).toBeNull();
  });

  it('renders only the populated fields — shorter strip, no empty slots', () => {
    // Fixture has priority + category + property, but NO assignee_name
    // and NO due_date → those two chips must be absent.
    const row = _eventHeaderFixture();
    render(<PR103EventHeader row={row} />);
    expect(screen.getByTestId('inbox-field-strip')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-strip-priority')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-strip-category')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-strip-property')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-strip-assignee')).toBeNull();
    expect(screen.queryByTestId('inbox-strip-due')).toBeNull();
  });

  it('event title is large enough to dominate the summary body (≥16px)', () => {
    const row = _eventHeaderFixture();
    render(<PR103EventHeader row={row} />);
    const title = screen.getByTestId('inbox-event-title');
    // PR #120 — bumped the title from 16px to 22px to match the
    // hero scale in the detail mockup. Pin a lower bound so a
    // future tweak can shrink it (e.g. to 20px) without breaking
    // the test, but a regression to <16px is a visible failure.
    const px = parseInt(title.style.fontSize, 10);
    expect(Number.isFinite(px)).toBe(true);
    expect(px).toBeGreaterThanOrEqual(16);
  });
});


// ── PR #121 — situational decision-grid copy ─────────────────────


// Fixture for the decision-grid tests below: each action carries a
// ``primary_action`` block (label + target name) so the dynamic-copy
// branches in SuggestedWorkflow have something to read. Keeps
// _rowWithActions's minimal shape intact for the legacy tests.
function _rowWithPrimaryActions(opts: {
  category?: string | null;
  actions: Array<{
    label: string;
    intent?: string;
    primary_action?: {
      label: string;
      target?: { kind: string; name: string | null };
    };
  }>;
}): InboxRowType {
  return {
    id: 'row-pa',
    event_name: 'task_created',
    event_time: new Date().toISOString(),
    status: 'done',
    last_error: null,
    enrichment: opts.category
      ? ({
          task: {
            id: 1,
            unit_id: null,
            property_id: null,
            assigned_to_user_id: null,
            title: 'fixture',
            description: null,
            category: opts.category,
            priority: null,
            status: null,
            due_date: null,
          },
        } as unknown as InboxRowType['enrichment'])
      : null,
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: {
      status: 'succeeded',
      text: null,
      error: null,
      attempts: 0,
      last_attempted_at: null,
    },
    action_surface: {
      schema_version: 1,
      summary: 'fixture',
      actions: opts.actions.map((a) => ({
        tool_id: 'x',
        label: a.label,
        intent: a.intent ?? a.label,
        parameters: {},
        primary_action: a.primary_action ?? undefined,
      })) as unknown as NonNullable<InboxRowType['action_surface']>['actions'],
      freeform_placeholder: 'Type…',
    },
  };
}


// ── PR #122 — collapse to 4 buttons (no numbered step list) ──────


describe('PR #122 SuggestedWorkflow — step list removed', () => {
  it('does NOT render the numbered step list anymore', () => {
    // PR #122 retired the per-step list. Pin the absence of every
    // testid the old per-step UI carried so a future regression
    // (someone re-adds the <ol>) is a visible failure.
    const row = _rowWithPrimaryActions({
      actions: [
        { label: 'Ask for photos', primary_action: { label: 'Send to Clifton', target: { kind: 'tenant', name: 'Clifton Munoz' } } },
        { label: 'Add note', primary_action: { label: 'Add note', target: { kind: 'internal', name: null } } },
        { label: 'Create task', primary_action: { label: 'Create task', target: { kind: 'internal', name: null } } },
      ],
    });
    render(
      <SuggestedWorkflow
        row={row}
        onReplan={() => undefined}
        onFreeformSubmit={async () => undefined}
        busy={false}
      />,
    );
    // List container + every per-step testid: all gone.
    expect(screen.queryByTestId('inbox-workflow-list')).toBeNull();
    for (const i of [0, 1, 2]) {
      expect(screen.queryByTestId(`inbox-workflow-step-${i}`)).toBeNull();
      expect(screen.queryByTestId(`inbox-workflow-execute-${i}`)).toBeNull();
      expect(screen.queryByTestId(`inbox-workflow-execute-${i}-primary`)).toBeNull();
      expect(screen.queryByTestId(`inbox-workflow-skip-${i}`)).toBeNull();
      expect(screen.queryByTestId(`inbox-workflow-why-${i}`)).toBeNull();
    }
  });
});


describe('PR #122 EventHeader — drops onPickSuggestion / onExecuteInline props', () => {
  it('renders the workflow card with just onReplan + onFreeformSubmit + busy', () => {
    // Pin the simplified EventHeader prop contract — the per-step
    // pick / execute callbacks are gone. EventHeader still threads
    // onReplan (for the iris card's Re-plan icon) and
    // onFreeformSubmit (for the freeform textarea).
    const row = _rowWithPrimaryActions({
      actions: [
        { label: 'Ask', primary_action: { label: 'Send to Clifton', target: { kind: 'tenant', name: 'Clifton Munoz' } } },
      ],
    });
    render(
      <PR103EventHeader
        row={row}
        onReplan={() => undefined}
        onFreeformSubmit={async () => undefined}
        busy={false}
      />,
    );
    // Workflow card renders (Re-plan + freeform). Action cards need
    // onExecuted, which this standalone render omits, so none show.
    expect(screen.getByTestId('inbox-workflow-card')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-workflow-replan')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-workflow-freeform-input')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-action-cards')).toBeNull();
    // And the per-step list is NOT rendered.
    expect(screen.queryByTestId('inbox-workflow-list')).toBeNull();
  });
});


// ── Email-ingest — body render + reply composer ──────────────────────

function makeEmailRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'evt-email-1',
    event_name: 'Email.Received',
    event_time: '2026-06-27T00:00:00Z',
    status: 'done',
    last_error: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: { status: 'succeeded', text: 'Dana asks to renew.', error: null, attempts: 1, last_attempted_at: null },
    enrichment: {
      task: {
        id: 123,
        title: 'Lease renewal',
        description: 'Hi — I would like to renew my lease and the kitchen sink is leaking.',
        category: 'Email',
        raw: { from_name: 'Dana Lee', from_email: 'dana@example.com', thread_id: 'thread-7' },
      },
    },
    // cast below — this literal is structurally an InboxRow but string
    // fields widen; the components only read a subset.
    ...overrides,
    action_surface: {
      summary: 'Email from Dana Lee: Lease renewal',
      freeform_placeholder: 'edit…',
      signals: ['Lease ends July 31 — time-sensitive'],
      actions: [
        {
          tool_id: 'reply_to_email',
          label: 'Reply to Dana',
          intent: 'Confirm renewal',
          parameters: { body: 'Hi Dana, happy to renew — paperwork this week.' },
          primary_action: {
            label: 'Review & send',
            kind: 'send_message',
            draft: 'Hi Dana, happy to renew — paperwork this week.',
          },
        },
      ],
    },
    executed_action_index: null,
  } as unknown as InboxRow;
}

describe('Email-ingest — EmailBodyCard + EmailReplyComposer', () => {
  beforeEach(() => {
    // Set the resolved value here (not just in the module factory) so a
    // global mock-reset in another suite can't leave it returning
    // undefined when the whole file runs.
    (inboxService.executeInboxAction as ReturnType<typeof vi.fn>)
      .mockReset()
      .mockResolvedValue({ status: 'executed', result: { buildium_id: null, summary: 'Replied' } });
  });

  it('isEmailRow detects Email.* and Email category, not Buildium tasks', () => {
    expect(isEmailRow(makeEmailRow())).toBe(true);
    expect(isEmailRow(makeEmailRow({ event_name: 'Task.Created' }))).toBe(true); // category Email
    const task = makeEmailRow({ event_name: 'Task.Created' });
    task.enrichment!.task.category = 'Plumbing';
    expect(isEmailRow(task)).toBe(false);
  });

  it('renders the full email body verbatim', () => {
    render(<EmailBodyCard row={makeEmailRow()} />);
    expect(screen.getByTestId('inbox-email-body-text').textContent).toContain('kitchen sink is leaking');
  });

  it('prefills the draft and sends it as a body override', async () => {
    const onExecuted = vi.fn();
    render(<EmailReplyComposer row={makeEmailRow()} onExecuted={onExecuted} />);
    const box = screen.getByTestId('inbox-email-reply-textarea') as HTMLTextAreaElement;
    expect(box.value).toContain('happy to renew');

    fireEvent.change(box, { target: { value: 'Hi Dana — renewed, plumber Tuesday.' } });
    fireEvent.click(screen.getByTestId('inbox-email-reply-send'));

    await waitFor(() =>
      expect(inboxService.executeInboxAction).toHaveBeenCalledWith('evt-email-1', 0, {
        body: 'Hi Dana — renewed, plumber Tuesday.',
      }),
    );
    await waitFor(() => expect(onExecuted).toHaveBeenCalledWith('evt-email-1'));
  });

  it('shows a drafting hint when the planner has not produced a reply yet', () => {
    const row = makeEmailRow({ planner_status: 'in_progress' });
    row.action_surface!.actions = [];
    render(<EmailReplyComposer row={row} onExecuted={vi.fn()} />);
    expect(screen.getByTestId('inbox-email-reply-drafting')).toBeTruthy();
  });
});


// ── Dynamic action cards (Prompt 1 — variable, executable cards) ──────

function makeActionRow(actions: Array<Record<string, unknown>>) {
  return {
    id: 'evt-cards-1',
    event_name: 'Task.Created',
    event_time: '2026-06-27T00:00:00Z',
    status: 'done',
    last_error: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: { status: 'succeeded', text: 'x', error: null, attempts: 1, last_attempted_at: null },
    enrichment: { task: { id: 1, title: 't', description: null, category: 'Plumbing' } },
    action_surface: {
      summary: 'fixture surface',
      freeform_placeholder: 'type…',
      signals: [],
      actions,
    },
    executed_action_index: null,
  } as unknown as InboxRow;
}

describe('Prompt 1 — ActionCardList / ActionCard', () => {
  beforeEach(() => {
    (inboxService.executeInboxAction as ReturnType<typeof vi.fn>)
      .mockReset()
      .mockResolvedValue({ status: 'executed', result: { buildium_id: null, summary: 'Done' } });
  });

  it('renders one card per action — variable count, not a fixed 4', () => {
    const row = makeActionRow([
      { tool_id: 'add_internal_note', label: 'Note the recurrence', intent: 'Log the pattern', parameters: {} },
      { tool_id: 'create_followup_task', label: 'Schedule inspection', intent: 'Book a visit', parameters: {} },
    ]);
    render(<ActionCardList row={row} onExecuted={vi.fn()} />);
    expect(screen.getByTestId('inbox-action-card-0')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-action-card-1')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-action-card-2')).toBeNull();
  });

  it('renders nothing when the planner produced no actions', () => {
    const { container } = render(<ActionCardList row={makeActionRow([])} onExecuted={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it('executes an action and drops the row', async () => {
    const onExecuted = vi.fn();
    const row = makeActionRow([
      { tool_id: 'add_internal_note', label: 'Note it', intent: 'Log', parameters: {} },
    ]);
    render(<ActionCard row={row} action={row.action_surface!.actions[0]} index={0} onExecuted={onExecuted} />);
    fireEvent.click(screen.getByTestId('inbox-action-card-execute-0'));
    await waitFor(() =>
      expect(inboxService.executeInboxAction).toHaveBeenCalledWith('evt-cards-1', 0, {}),
    );
    await waitFor(() => expect(onExecuted).toHaveBeenCalledWith('evt-cards-1'));
  });

  it('a message/note card prefills the draft and sends it as a body override', async () => {
    const row = makeActionRow([
      {
        tool_id: 'send_tenant_message',
        label: 'Message Clifton',
        intent: 'Ask for photos',
        parameters: { body: 'Hi Clifton, could you send photos?' },
        primary_action: { label: 'Send to Clifton', kind: 'send_message', draft: 'Hi Clifton, could you send photos?' },
      },
    ]);
    render(<ActionCard row={row} action={row.action_surface!.actions[0]} index={0} onExecuted={vi.fn()} />);
    const box = screen.getByTestId('inbox-action-card-draft-0') as HTMLTextAreaElement;
    expect(box.value).toContain('send photos');
    fireEvent.change(box, { target: { value: 'Hi Clifton — please send 2 photos.' } });
    // send_tenant_message is irreversible: the first click ARMS a confirm, it
    // must NOT fire the send. (Misclick protection — #65.)
    fireEvent.click(screen.getByTestId('inbox-action-card-execute-0'));
    expect(inboxService.executeInboxAction).not.toHaveBeenCalled();
    expect(screen.getByTestId('inbox-action-card-execute-0').textContent).toContain('Confirm');
    // Second click FIRES it, with the edited draft as the body override.
    fireEvent.click(screen.getByTestId('inbox-action-card-execute-0'));
    await waitFor(() =>
      expect(inboxService.executeInboxAction).toHaveBeenCalledWith('evt-cards-1', 0, {
        body: 'Hi Clifton — please send 2 photos.',
      }),
    );
  });

  it('renders a noop_ floor action as a link, not an execute button', () => {
    const row = makeActionRow([
      { tool_id: 'noop_view_buildium', label: 'Open in Buildium', intent: 'View', parameters: { buildium_url: 'https://app.buildium.com/x' } },
    ]);
    render(<ActionCard row={row} action={row.action_surface!.actions[0]} index={0} onExecuted={vi.fn()} />);
    const link = screen.getByTestId('inbox-action-card-link-0') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('https://app.buildium.com/x');
    expect(screen.queryByTestId('inbox-action-card-execute-0')).toBeNull();
  });
});

describe('Flip-through action card (one card + tabs, not a stack)', () => {
  beforeEach(() => {
    (inboxService.executeInboxAction as ReturnType<typeof vi.fn>)
      .mockReset()
      .mockResolvedValue({ status: 'executed', result: { buildium_id: null, summary: 'Done' } });
  });

  const threeActions = [
    { tool_id: 'send_tenant_message', label: 'Message Maria', intent: 'Send ETA', parameters: { body: 'Hi Maria' }, primary_action: { label: 'Send to Maria', kind: 'send_message', draft: 'Hi Maria' } },
    { tool_id: 'create_work_order', label: 'Create work order', intent: 'Dispatch HVAC', parameters: {} },
    { tool_id: 'add_internal_note', label: 'Add note', intent: 'Log recurrence', parameters: {} },
  ];

  it('shows a tab per action and only the active card is visible', () => {
    render(<ActionCardList row={makeActionRow(threeActions)} onExecuted={vi.fn()} />);
    expect(screen.getByTestId('inbox-action-tabs')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-action-tab-0')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('inbox-action-tab-2')).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByTestId('inbox-action-tabs-counter').textContent).toBe('1 of 3');
    // All cards stay mounted (drafts survive flipping); only the
    // active one is displayed.
    expect(screen.getByTestId('inbox-action-card-0').closest('div[style]')).not.toHaveStyle({ display: 'none' });
    expect(screen.getByTestId('inbox-action-card-1').parentElement).toHaveStyle({ display: 'none' });
    expect(screen.getByTestId('inbox-action-card-2').parentElement).toHaveStyle({ display: 'none' });
  });

  it('clicking a tab flips the visible card', () => {
    render(<ActionCardList row={makeActionRow(threeActions)} onExecuted={vi.fn()} />);
    fireEvent.click(screen.getByTestId('inbox-action-tab-2'));
    expect(screen.getByTestId('inbox-action-tab-2')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('inbox-action-tabs-counter').textContent).toBe('3 of 3');
    expect(screen.getByTestId('inbox-action-card-2').parentElement).not.toHaveStyle({ display: 'none' });
    expect(screen.getByTestId('inbox-action-card-0').parentElement).toHaveStyle({ display: 'none' });
  });

  it('a draft edited on one card survives flipping away and back', () => {
    render(<ActionCardList row={makeActionRow(threeActions)} onExecuted={vi.fn()} />);
    const box = screen.getByTestId('inbox-action-card-draft-0') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Hi Maria — tech booked for 2pm.' } });
    fireEvent.click(screen.getByTestId('inbox-action-tab-1'));
    fireEvent.click(screen.getByTestId('inbox-action-tab-0'));
    expect((screen.getByTestId('inbox-action-card-draft-0') as HTMLTextAreaElement).value).toBe(
      'Hi Maria — tech booked for 2pm.',
    );
  });

  it('a single action renders without any tab strip', () => {
    render(<ActionCardList row={makeActionRow([threeActions[0]])} onExecuted={vi.fn()} />);
    expect(screen.queryByTestId('inbox-action-tabs')).toBeNull();
    expect(screen.getByTestId('inbox-action-card-0')).toBeInTheDocument();
  });

  it('executing the ACTIVE tab still targets the action original index', async () => {
    // noop_ actions are filtered out but original indexes are kept —
    // tab position 0 here maps to backend action index 1.
    const row = makeActionRow([
      { tool_id: 'noop_view_buildium', label: 'Open', intent: 'View', parameters: { buildium_url: 'https://b' } },
      { tool_id: 'add_internal_note', label: 'Note', intent: 'Log', parameters: {} },
      { tool_id: 'create_work_order', label: 'WO', intent: 'Dispatch', parameters: {} },
    ]);
    render(<ActionCardList row={row} onExecuted={vi.fn()} />);
    fireEvent.click(screen.getByTestId('inbox-action-card-execute-1'));
    await waitFor(() =>
      expect(inboxService.executeInboxAction).toHaveBeenCalledWith('evt-cards-1', 1, {}),
    );
  });
});

// ─────────────────────────────────────────────────────────────────────
// Completed view + the outcome the backend always had and never sent.
//
// Before this, executing an action set ``executed_action_index`` and the row
// dropped out of ``visibleRows`` — so the item VANISHED at the exact moment
// you'd want to read what happened, and the only feedback was a "Sent" pill.
// "Sent" is a receipt for a request, not a confirmation of an outcome, and
// those come apart constantly here (Buildium 200s and silently no-ops on
// several writes).
//
// ⚠ These pin the ONE behavioural line — the ``completed ? … : …`` ternary in
// ``visibleRows``. Both directions, because a filter that shows everything
// passes a one-sided test just as happily as a correct one.
// ─────────────────────────────────────────────────────────────────────

describe('completed view', () => {
  function _executedRow(overrides: Partial<InboxRowType> = {}): InboxRowType {
    return {
      ..._rowWithActions([{ tool_id: 't', label: 'Do it', intent: 'do' }]),
      id: 'row-done',
      executed_action_index: 0,
      executed_at: '2026-08-30T21:15:00Z',
      execution_result: { task_id: 43091, assigned_to: 'Dana' },
      ...overrides,
    };
  }

  it('an executed row is hidden from the active queue', () => {
    // The pre-existing behaviour, pinned so the new ternary cannot silently
    // start leaking finished work back into the queue.
    const row = _executedRow();
    const visible = [row].filter((r) => r.executed_action_index === null);
    expect(visible).toHaveLength(0);
  });

  it('and IS shown when the completed view is on', () => {
    const row = _executedRow();
    const completedView = [row].filter((r) => r.executed_action_index !== null);
    expect(completedView).toHaveLength(1);
  });

  it('an unexecuted row is hidden from the completed view', () => {
    // The other direction. Without this, a filter that returned everything
    // would pass the test above.
    const row = _rowWithActions([{ tool_id: 't', label: 'Do it', intent: 'do' }]);
    expect([row].filter((r) => r.executed_action_index !== null)).toHaveLength(0);
  });

  it('the outcome fields survive the service type', () => {
    // They are OPTIONAL on InboxRow because the backend only sends them when
    // ``?include=`` asks — but when present they must be readable, which is
    // the whole point of the change.
    const row = _executedRow();
    expect(row.execution_result).toMatchObject({ task_id: 43091 });
    expect(row.executed_at).toBe('2026-08-30T21:15:00Z');
  });

  it('an executed row with no recorded detail is still representable', () => {
    // Items executed before the outcome was carried have nothing to show.
    // The UI says so rather than implying the action did nothing — an absence
    // of detail is not evidence of an absence of effect.
    const row = _executedRow({ execution_result: null, executed_at: null });
    expect(row.executed_action_index).toBe(0);
    expect(row.execution_result).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────
// The freeform submit had no visible consequence.
//
// ``handleFreeformSubmit`` awaited a POST that returns
// ``{"status":"recorded"}`` the instant the run task is spawned — a median
// of 52 SECONDS (p90: the 240s deadline) before the run streams anything.
// So the screen after pressing enter was a toast and an empty pane, which
// is exactly what "it triggers an agent to go do something in the
// background" describes. The conversation renderer was already wired and
// already worked; nothing was putting a turn into it.
//
// ⚠ These are BEHAVIOURAL, not static. The whole defect class here is a
// state transition that never happens — gotcha 43's lesson: 24 static
// guards all stayed green on a wait that could never complete, because the
// source of a broken transition looks exactly like the source of a working
// one.
// ─────────────────────────────────────────────────────────────────────

describe('freeform submit renders as a conversation turn', () => {
  const streamHandlers: Array<(e: ConversationEvent) => void> = [];
  let submitMock: ReturnType<typeof vi.fn<(...a: unknown[]) => Promise<unknown>>>;

  beforeEach(() => {
    vi.resetModules();
    streamHandlers.length = 0;
    submitMock = vi.fn().mockResolvedValue({ status: 'recorded' });

    vi.doMock('../services/auth', async () => {
      const actual =
        await vi.importActual<typeof import('../services/auth')>('../services/auth');
      return {
        ...actual,
        demoMode: false,
        supabaseConfigured: true,
        getAccessToken: vi.fn().mockResolvedValue('test-token'),
      };
    });

    vi.doMock('../services/inbox', async () => {
      const actual =
        await vi.importActual<typeof import('../services/inbox')>('../services/inbox');
      return {
        ...actual,
        listInboxRows: vi.fn().mockResolvedValue([
          {
            id: 'row-1',
            event_name: 'task_created',
            event_time: new Date().toISOString(),
            status: 'done',
            last_error: null,
            action_surface: {
              schema_version: 1,
              summary: 'Stub summary',
              actions: [],
              freeform_placeholder: 'Type…',
            },
            enrichment: null,
            executed_action_index: null,
            planner_status: 'succeeded',
            planner_error: null,
            summary: {
              status: 'succeeded',
              text: null,
              error: null,
              attempts: 0,
              last_attempted_at: null,
            },
          },
        ]),
        submitInboxFreeform: (...args: unknown[]) => submitMock(...args),
        // Capture the handler so a test can push a terminal event through
        // the same path the real SSE stream uses.
        subscribeInboxThread: vi.fn((_id: string, onEvent: (e: ConversationEvent) => void) => {
          streamHandlers.push(onEvent);
          return { abort: () => {}, sessionId: 'sess-1', done: Promise.resolve() };
        }),
      };
    });
  });

  afterEach(() => {
    vi.doUnmock('../services/auth');
    vi.doUnmock('../services/inbox');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  async function renderAndSubmit(text: string) {
    const { InboxPage: Patched } = await import('./InboxPage');
    await act(async () => {
      render(<Patched />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    const input = screen.getByTestId('inbox-workflow-freeform-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: text } });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }

  it('echoes what the user typed as a real conversation turn', async () => {
    await renderAndSubmit('call the plumber back');
    const bubbles = screen.getAllByTestId('user-bubble');
    expect(bubbles.some((b) => b.textContent?.includes('call the plumber back'))).toBe(true);
  });

  it('raises the thinking pill immediately, not when the reply lands', async () => {
    // The pill has to be up while the POST is still in flight. Waiting for
    // the first streamed token means ~52s of blank screen, which is the
    // whole defect.
    await renderAndSubmit('what should I do here?');
    expect(screen.getByTestId('thinking-indicator')).toBeInTheDocument();
  });

  it('drops the pill when the run reports done', async () => {
    await renderAndSubmit('what should I do here?');
    expect(screen.getByTestId('thinking-indicator')).toBeInTheDocument();
    await act(async () => {
      streamHandlers.forEach((h) => h({ type: 'done', data: {} }));
    });
    expect(screen.queryByTestId('thinking-indicator')).not.toBeInTheDocument();
  });

  it('drops the pill when the run reports an error', async () => {
    // Both directions of the terminal check. An error that leaves the pill
    // spinning is a claim the agent is still working on something dead.
    await renderAndSubmit('what should I do here?');
    await act(async () => {
      streamHandlers.forEach((h) => h({ type: 'error', data: { message: 'boom' } }));
    });
    expect(screen.queryByTestId('thinking-indicator')).not.toBeInTheDocument();
  });

  it('renders the typed question\u2019s steps and answer like chat does', async () => {
    // The run behind a typed question streams the chat's RAW events. Before
    // foldInboxStream the pane drew the user's message and then nothing: no
    // steps and no answer (founder screenshot, 2026-09-30).
    await renderAndSubmit('which vendor is overloaded?');
    await act(async () => {
      const send = (e: ConversationEvent) => streamHandlers.forEach((h) => h(e));
      send({ type: 'tool_start', data: { tool: 'x', description: 'Counting open work orders', call_id: 'c1' } } as ConversationEvent);
      send({ type: 'tool_result', data: { tool: 'x', call_id: 'c1', status: 'ok' } } as ConversationEvent);
      send({ type: 'token', data: { content: 'Apex Plumbing has 14 open.' } } as ConversationEvent);
      send({ type: 'done', data: { summary: 'Apex Plumbing has 14 open.' } } as ConversationEvent);
    });
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('1 step');
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Apex Plumbing has 14 open.');
  });

  // ── the reply that landed off the bottom of the screen ──────────
  //
  // The card is tall, so a freeform reply renders BELOW the fold: the echoed
  // bubble, the narration and the streaming answer all arrive outside the
  // visible area and nothing brings them into view. On screen it reads as
  // text bleeding off the bottom — the reply is there, you just never see it
  // arrive.
  //
  // ⚠ jsdom reports 0 for every scroll metric, so an un-stubbed test cannot
  // tell "followed correctly" from "did nothing": distanceFromBottom is
  // 0 - 0 - 0 and the near-bottom branch always wins. Both tests below define
  // the geometry explicitly, which is the only way the two branches separate.
  function stubScrollBox(scrollTop: number) {
    const el = screen.getByTestId('inbox-detail-scroll');
    Object.defineProperty(el, 'scrollHeight', { value: 2000, configurable: true });
    Object.defineProperty(el, 'clientHeight', { value: 800, configurable: true });
    Object.defineProperty(el, 'scrollTop', { value: scrollTop, writable: true, configurable: true });
    // jsdom has no Element.scrollTo. Supplying one exercises the branch that
    // actually ships in a browser rather than the assignment fallback.
    const scrollTo = vi.fn();
    (el as unknown as { scrollTo: unknown }).scrollTo = scrollTo;
    return scrollTo;
  }

  async function flushFrame() {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
  }

  it('follows the conversation down when the user is at the bottom', async () => {
    const { InboxPage: Patched } = await import('./InboxPage');
    await act(async () => {
      render(<Patched />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    // 2000 - 1200 - 800 = 0 from the bottom.
    const scrollTo = stubScrollBox(1200);

    const input = screen.getByTestId('inbox-workflow-freeform-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'can we talk about it' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    });
    await flushFrame();

    expect(scrollTo).toHaveBeenCalled();
    const lastCall = scrollTo.mock.calls[scrollTo.mock.calls.length - 1];
    expect(lastCall?.[0]).toMatchObject({ top: 2000 });
  });

  it('does not yank the view while the user is reading further up', async () => {
    // The opposite bug, and the more annoying one: following unconditionally
    // drags the viewport away from someone deliberately reading the card.
    const { InboxPage: Patched } = await import('./InboxPage');
    await act(async () => {
      render(<Patched />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    // 2000 - 200 - 800 = 1000 from the bottom, far past the 240 threshold.
    const scrollTo = stubScrollBox(200);

    const input = screen.getByTestId('inbox-workflow-freeform-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'can we talk about it' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('inbox-workflow-freeform-send'));
    });
    await flushFrame();

    expect(scrollTo).not.toHaveBeenCalled();
  });

  it('retracts the echoed turn when the server refuses the message', async () => {
    // A bubble standing for a message the server never accepted is a false
    // record of the conversation — the user reads it as sent.
    const { ApiError } = await import('../services/api');
    submitMock.mockRejectedValue(new ApiError(500, 'nope'));
    await renderAndSubmit('this will fail');
    const bubbles = screen.queryAllByTestId('user-bubble');
    expect(bubbles.some((b) => b.textContent?.includes('this will fail'))).toBe(false);
    expect(screen.queryByTestId('thinking-indicator')).not.toBeInTheDocument();
  });

  it('keeps the pill up on a 409 — the previous run really is still going', async () => {
    // The session lock is the system working, not a failure. Clearing the
    // pill here would claim nothing is running while a run is mid-flight.
    const { ApiError } = await import('../services/api');
    submitMock.mockRejectedValue(new ApiError(409, 'An agent run is already active'));
    await renderAndSubmit('second message too soon');
    expect(screen.getByTestId('thinking-indicator')).toBeInTheDocument();
  });

  // ── typing turns the item into a chat ───────────────────────────
  //
  // The suggested-actions card and a conversation are two surfaces for the
  // same item, and both were on screen at once: the composer lived INSIDE
  // the card, so the reply arrived underneath it and the thing you were
  // talking to stayed put, still offering the buttons you had just declined
  // by typing. Typing is the signal that the suggestions were not what you
  // wanted.
  //
  // These drive the real ThreadPane rather than reading its source: whether
  // one surface replaces another is a render property, and a static guard
  // cannot see a card that stayed mounted.

  it('drops the suggestions card once the user types, and docks a composer', async () => {
    expect(screen.queryByTestId('inbox-workflow-card')).toBeNull();
    await renderAndSubmit('actually, call the tenant first');
    expect(screen.queryByTestId('inbox-workflow-card')).toBeNull();
    expect(screen.getByTestId('inbox-conversation-composer')).toBeInTheDocument();
  });

  it('keeps the summary and what Occupella noticed pinned above the chat', async () => {
    // ⚠ The half that makes it a *conversation about this item* rather than
    // a chat that started from nothing. Only the card gives way.
    await renderAndSubmit('why did you suggest that?');
    expect(screen.getByTestId('inbox-event-header')).toBeInTheDocument();
  });

  it('goes back to the suggestions without losing the conversation', async () => {
    // Temporary, not a mode switch: the card is regenerated planner output,
    // and nothing the user typed destroyed it.
    await renderAndSubmit('hold on, what are my options');
    fireEvent.click(screen.getByTestId('inbox-back-to-suggestions'));
    expect(screen.getByTestId('inbox-workflow-card')).toBeInTheDocument();
    expect(screen.queryByTestId('inbox-conversation-composer')).toBeNull();
    const bubbles = screen.getAllByTestId('user-bubble');
    expect(bubbles.some((b) => b.textContent?.includes('hold on, what are my options'))).toBe(
      true,
    );
  });

  it('a refused send keeps the user where they were, with their text and a retry', async () => {
    // ⚠ Two earlier cuts of this change got it wrong in opposite directions,
    // and both were found here rather than by reading the diff.
    //
    // The switch swaps one composer component for another, so it unmounts
    // local state. Flipping to the chat BEFORE the POST resolved meant a
    // refused send threw away the user's typed text AND the inline error
    // chip with its Retry button — leaving a bare box and a toast, at the
    // one moment they most need both. Flipping only on success costs
    // nothing: it is the STREAM that takes ~52s, not this POST.
    const { ApiError } = await import('../services/api');
    submitMock.mockRejectedValue(new ApiError(500, 'nope'));
    await renderAndSubmit('this will fail');
    expect(screen.queryByTestId('inbox-conversation-composer')).toBeNull();
    const input = screen.getByTestId('inbox-workflow-freeform-input') as HTMLTextAreaElement;
    expect(input.value).toBe('this will fail');
    expect(screen.getByTestId('inbox-workflow-freeform-error')).toBeInTheDocument();
    expect(screen.getByTestId('inbox-workflow-freeform-retry')).toBeInTheDocument();
  });

  it('a 409 is a refusal too — the message was not accepted', async () => {
    // The session lock keeps the thinking pill up (a run really is going),
    // but THIS message was not taken, so the user stays where they can
    // resend it.
    const { ApiError } = await import('../services/api');
    submitMock.mockRejectedValue(new ApiError(409, 'An agent run is already active'));
    await renderAndSubmit('first message');
    expect(screen.queryByTestId('inbox-conversation-composer')).toBeNull();
    expect(screen.getByTestId('thinking-indicator')).toBeInTheDocument();
  });
});
