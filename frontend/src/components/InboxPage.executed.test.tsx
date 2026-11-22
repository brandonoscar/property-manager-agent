/**
 * Once you pick an option, the other options are gone.
 *
 * ⚠ **The swap existed in the JSX and nothing pinned it.** `SuggestedWorkflow`
 * renders `<ExecutedOutcome>` instead of `<ActionCardList>` the moment
 * `executed_action_index` is set — one action per item, enforced server-side
 * with a 409, so re-offering the alternatives beside a completed one would be
 * a lie. But the only tests touching that field checked the queue FILTER (an
 * executed row leaves the active list), which is a different question in a
 * different component. Deleting the ternary left every one of them green while
 * the rejected options stayed on screen under "Completed" (founder report,
 * 2026-09-08).
 *
 * ⚠ Both directions, because a card that ALWAYS shows the outcome and never
 * the options passes a one-sided test just as happily — and that version of
 * the bug is worse: nobody could act on an inbox item at all.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { __test__ } from './InboxPage';
import type { InboxRow } from '../services/inbox';

const { SuggestedWorkflow } = __test__;

function row(overrides: Partial<InboxRow> = {}): InboxRow {
  return {
    id: 'row-x',
    event_name: 'task_created',
    event_time: '2026-09-08T12:00:00Z',
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
      summary: 'summary',
      actions: [
        { tool_id: 'a', label: 'Raise the priority', intent: 'update', parameters: {} },
        { tool_id: 'b', label: 'Message the tenant', intent: 'notify', parameters: {} },
        { tool_id: 'c', label: 'Assign a vendor', intent: 'assign', parameters: {} },
      ],
      freeform_placeholder: 'Type…',
    },
    ...overrides,
  } as InboxRow;
}

describe('picking one of the suggested actions', () => {
  it('offers every option while none has been taken', () => {
    render(
      <SuggestedWorkflow
        row={row()}
        onReplan={() => undefined}
        busy={false}
        onExecuted={() => undefined}
      />,
    );
    expect(screen.getByTestId('inbox-action-cards')).toBeTruthy();
    // Three actions → a tab each, so the alternatives are reachable.
    expect(screen.getByTestId('inbox-action-tab-2')).toBeTruthy();
    expect(screen.queryByTestId('inbox-executed-outcome')).toBeNull();
  });

  it('erases the alternatives once one has run', () => {
    render(
      <SuggestedWorkflow
        row={row({
          executed_action_index: 0,
          executed_at: '2026-09-08T12:05:00Z',
          execution_result: { task_id: 43091 },
        })}
        onReplan={() => undefined}
        busy={false}
        onExecuted={() => undefined}
      />,
    );
    expect(screen.getByTestId('inbox-executed-outcome')).toBeTruthy();
    // ⚠ The whole list, not just the chosen card. A completed item that still
    // shows "Message the tenant" as an available button invites a second
    // action the server will refuse with a 409 — the user reads that refusal
    // as a broken product rather than as the rule working.
    expect(screen.queryByTestId('inbox-action-cards')).toBeNull();
    expect(screen.queryByTestId('inbox-action-tab-0')).toBeNull();
    expect(screen.queryByText('Message the tenant')).toBeNull();
    expect(screen.queryByText('Assign a vendor')).toBeNull();
  });

  it('keeps the reply box open after an action has run', () => {
    // Erasing the OPTIONS must not erase the conversation. "That is not quite
    // what I wanted" is the most likely next thing somebody types, and the
    // freeform input is the only way to say it.
    render(
      <SuggestedWorkflow
        row={row({ executed_action_index: 1, executed_at: '2026-09-08T12:05:00Z' })}
        onReplan={() => undefined}
        onFreeformSubmit={async () => undefined}
        busy={false}
        onExecuted={() => undefined}
      />,
    );
    expect(screen.getByTestId('inbox-workflow-freeform-input')).toBeTruthy();
  });
});
