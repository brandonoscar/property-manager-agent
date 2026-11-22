/**
 * Generative action buttons disappear once the user moves on.
 *
 * The rule (docs/decisions.md 2026-09-30): a next step, a planner chip or a
 * form the assistant generated for one moment goes away as soon as the user
 * takes their next action, by clicking one of them or by typing. The founder
 * clicked "Check vendor capacity", and it was still on screen, clickable,
 * above the answer to the question it had just asked.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ConversationThread, SPENT_AFTER_USER_TURN } from './ConversationThread';
import type { ConversationEvent } from './types';

const ev = (type: string, data: Record<string, unknown> = {}): ConversationEvent =>
  ({ type, data }) as ConversationEvent;

const answer = (id: string, text: string): ConversationEvent[] => [
  ev('assistant_message_start', { turn_id: id }),
  ev('assistant_message_token', { turn_id: id, content: text }),
  ev('assistant_message_end', { turn_id: id }),
];

const nextSteps = ev('next_steps', {
  steps: [{ label: 'Check vendor capacity', prompt: 'Check vendor capacity' }],
  note_hint: 'Note that 61 work orders are still New',
});

describe('spent generative actions', () => {
  it('shows the next steps under the latest answer', () => {
    render(<ConversationThread events={[...answer('a1', 'Here is the list.'), nextSteps]} />);
    expect(screen.getByTestId(/^next-steps-\d+-step$/)).toHaveTextContent('Check vendor capacity');
    expect(screen.getByTestId(/^next-steps-\d+-note$/)).toBeInTheDocument();
  });

  it('hides them once the user sends anything after them', () => {
    render(
      <ConversationThread
        variant="chat"
        events={[
          ...answer('a1', 'Here is the list.'),
          nextSteps,
          ev('user_bubble', { content: 'Check vendor capacity' }),
        ]}
      />,
    );
    expect(screen.queryByTestId(/^next-steps-\d+-root$/)).toBeNull();
    // The answer they belonged to stays.
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Here is the list.');
  });

  it('hides a form and a chip row the same way', () => {
    render(
      <ConversationThread
        events={[
          ev('suggestion_chip', { action_index: 0, label: 'Assign Apex' }),
          ev('form', { fields: [{ name: 'unit', label: 'Which unit?' }] }),
          ev('user_bubble', { content: 'never mind' }),
        ]}
      />,
    );
    expect(screen.queryByTestId('suggestion-row')).toBeNull();
    expect(screen.queryByTestId(/^form-card-\d+-root$/)).toBeNull();
    expect(screen.queryAllByRole('button', { name: 'Assign Apex' })).toHaveLength(0);
  });

  it('never hides a confirm gate, which answers a write the backend is holding', () => {
    render(
      <ConversationThread
        events={[
          ev('confirm_gate', { action_index: 0, summary: 'Confirm: send the notice' }),
          ev('user_bubble', { content: 'something else' }),
        ]}
      />,
    );
    expect(screen.getByText('Confirm: send the notice')).toBeInTheDocument();
    expect(SPENT_AFTER_USER_TURN.has('confirm')).toBe(false);
  });

  it('keeps the new answer’s own next steps', () => {
    render(
      <ConversationThread
        events={[
          nextSteps,
          ev('user_bubble', { content: 'Check vendor capacity' }),
          ...answer('a2', 'Apex has 14 open.'),
          ev('next_steps', { steps: [{ label: 'Reassign two', prompt: 'Reassign two' }] }),
        ]}
      />,
    );
    const steps = screen.getAllByTestId(/^next-steps-\d+-step$/);
    expect(steps).toHaveLength(1);
    expect(steps[0]).toHaveTextContent('Reassign two');
  });
});
