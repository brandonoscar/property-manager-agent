/**
 * An action the server marked `unavailable_reason` is not offered.
 *
 * The server marks a planned action that writes to a PMS this workspace is
 * not on (`tool_catalog.mark_unavailable`), e.g. a Buildium task on a Rentvine
 * card: it can only fail. The executor addresses an action by its POSITION in
 * the stored list, so the card hides the marked one and keeps every other
 * action's original index; dropping it from the list instead would make the
 * next button run a different action than the one on screen (gotcha 183).
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const executeInboxAction = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ result: { summary: 'Done' } }),
);
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../services/inbox', async (orig) => {
  const actual = await orig<typeof import('../services/inbox')>();
  return { ...actual, executeInboxAction };
});

import { __test__ } from './InboxPage';
import type { InboxRow } from '../services/inbox';

const { SuggestedWorkflow } = __test__;

const NOT_HERE =
  'This writes to Buildium, and this workspace is connected to Rentvine. Make this change in Rentvine.';

function row(marked: boolean[]): InboxRow {
  const labels = ['Raise the priority', 'Add a Buildium note', 'Assign a vendor'];
  return {
    id: 'row-x',
    event_name: 'Risk.FlagRaised',
    event_time: '2026-10-03T12:00:00Z',
    status: 'done',
    last_error: null,
    enrichment: null,
    executed_action_index: null,
    planner_status: 'succeeded',
    planner_error: null,
    summary: { status: 'succeeded', text: null, error: null, attempts: 0, last_attempted_at: null },
    action_surface: {
      schema_version: 1,
      summary: 'summary',
      actions: labels.map((label, i) => ({
        tool_id: `tool_${i}`,
        label,
        intent: label,
        parameters: {},
        unavailable_reason: marked[i] ? NOT_HERE : null,
      })),
      freeform_placeholder: 'Type…',
    },
  } as InboxRow;
}

function renderCard(marked: boolean[]) {
  render(
    <SuggestedWorkflow
      row={row(marked)}
      onReplan={() => undefined}
      busy={false}
      onExecuted={() => undefined}
    />,
  );
}

describe('an action the server marked unavailable', () => {
  it('is hidden, and the others keep their original index', () => {
    renderCard([false, true, false]);
    expect(screen.getByTestId('inbox-action-card-0')).toBeTruthy();
    expect(screen.queryByTestId('inbox-action-card-1')).toBeNull();
    expect(screen.getByTestId('inbox-action-card-2')).toBeTruthy();
    expect(screen.getByTestId('inbox-action-tabs-counter').textContent).toBe('1 of 2');
    expect(screen.queryByText('Add a Buildium note')).toBeNull();
  });

  it('runs the action on screen, not the one after the hidden one', async () => {
    renderCard([false, true, false]);
    fireEvent.click(screen.getByTestId('inbox-action-tab-1'));
    fireEvent.click(screen.getByTestId('inbox-action-card-execute-2'));
    await waitFor(() => expect(executeInboxAction).toHaveBeenCalled());
    expect(executeInboxAction.mock.calls[0][0]).toBe('row-x');
    expect(executeInboxAction.mock.calls[0][1]).toBe(2);
  });

  it('leaves no action list when every action is marked', () => {
    renderCard([true, true, true]);
    expect(screen.queryByTestId('inbox-action-cards')).toBeNull();
  });

  it('changes nothing when no action is marked', () => {
    renderCard([false, false, false]);
    expect(screen.getByTestId('inbox-action-tabs-counter').textContent).toBe('1 of 3');
  });
});
