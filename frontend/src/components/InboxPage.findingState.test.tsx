/**
 * A risk card whose problem was already fixed, as it actually reaches a reader.
 *
 * The label helper is covered in `services/findingState.test.ts`. This file
 * exists because of a MUTATION that survived that one: replacing the row's
 * render with `{false && (...)}` left every label test green, because computing
 * the right sentence and never showing it is indistinguishable from computing
 * it correctly if nothing renders. Gotchas 62/66/69's shape — a well-tested
 * helper whose one caller quietly ignores it.
 *
 * ⚠ `sonner` is mocked as a CALLABLE with methods, matching the neighbouring
 * dismiss test. A plain object of methods throws "toast is not a function" the
 * moment anything in this page dismisses a row.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const toastFn = vi.hoisted(() => {
  const fn = vi.fn() as ReturnType<typeof vi.fn> & Record<string, unknown>;
  for (const m of ['success', 'error', 'message', 'warning', 'info']) fn[m] = vi.fn();
  return fn;
});
vi.mock('sonner', () => ({ toast: toastFn }));

const ROWS = vi.hoisted(() => [
  {
    id: 'row-healed',
    event_name: 'Risk.FlagRaised',
    event_time: '2026-09-01T10:00:00Z',
    status: 'done' as const,
    last_error: null,
    summary_text: 'Rent charged below the lease',
    executed_action_index: null,
    finding_state: {
      state: 'all_resolved',
      total: 4,
      open: 0,
      resolved: 4,
      dismissed: 0,
      unknown: 0,
    },
  },
  {
    id: 'row-partly',
    event_name: 'Risk.FlagRaised',
    event_time: '2026-09-02T10:00:00Z',
    status: 'done' as const,
    last_error: null,
    summary_text: 'Possible duplicate vendor bill',
    executed_action_index: null,
    finding_state: {
      state: 'partly_resolved',
      total: 4,
      open: 2,
      resolved: 2,
      dismissed: 0,
      unknown: 0,
    },
  },
  {
    id: 'row-open',
    event_name: 'Risk.FlagRaised',
    event_time: '2026-09-03T10:00:00Z',
    status: 'done' as const,
    last_error: null,
    summary_text: 'No rent collected this month',
    executed_action_index: null,
    finding_state: { state: 'open', total: 2, open: 2, resolved: 0, dismissed: 0, unknown: 0 },
  },
  {
    // Every other card type: the backend omits the key entirely.
    id: 'row-buildium',
    event_name: 'Task.Created',
    event_time: '2026-09-04T10:00:00Z',
    status: 'done' as const,
    last_error: null,
    summary_text: 'Leaking tap at 1 Example St',
    executed_action_index: null,
  },
]);

vi.mock('../services/inbox', async (orig) => {
  const actual = await orig<typeof import('../services/inbox')>();
  return { ...actual, listInboxRows: vi.fn().mockResolvedValue(ROWS) };
});

import { InboxPage } from './InboxPage';

afterEach(cleanup);

describe('a risk card whose findings have moved', () => {
  it('tells the reader the problem was already fixed', async () => {
    render(<InboxPage />);
    await waitFor(() => expect(screen.getAllByTestId('inbox-row-item')).toHaveLength(4));
    expect(screen.getByText('All 4 resolved since this was raised')).toBeTruthy();
  });

  it('gives the remainder when only some were fixed', async () => {
    render(<InboxPage />);
    await waitFor(() => expect(screen.getAllByTestId('inbox-row-item')).toHaveLength(4));
    expect(screen.getByText('2 of 4 resolved since this was raised')).toBeTruthy();
  });

  it('says nothing on the cards that are still accurate', async () => {
    // ⚠ The twin, and the direction that decides whether this survives real
    // use. A note on every row is one people stop reading, and then it is
    // absent exactly when it matters (gotcha 52). Exactly two of the four
    // rows have moved, so exactly two notes may appear.
    render(<InboxPage />);
    await waitFor(() => expect(screen.getAllByTestId('inbox-row-item')).toHaveLength(4));
    expect(screen.getAllByTestId('inbox-row-finding-state')).toHaveLength(2);
  });
});
