/**
 * A question typed in the Inbox renders like chat: steps, then the answer.
 *
 * The Inbox stream carries the agent runner's raw events (`token`,
 * `tool_start`, `done`, ...), which `foldEvents` does not draw. Before
 * `foldInboxStream` the Inbox showed the user's message, "Recalled 5 notes
 * from memory", and then nothing, for a run that had answered.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ConversationThread } from './ConversationThread';
import { answerIsStreaming, foldInboxStream } from './runnerFold';
import type { ConversationEvent } from './types';

const ev = (type: string, data: Record<string, unknown> = {}): ConversationEvent =>
  ({ type, data }) as ConversationEvent;

const RUN: ConversationEvent[] = [
  ev('user_bubble', { content: 'who is overloaded?' }),
  ev('memory_recall', { results_found: 5 }),
  ev('tool_start', { tool: 'vendor_load', description: 'Counting open work orders', call_id: 'c1' }),
  ev('tool_result', { tool: 'vendor_load', call_id: 'c1', status: 'ok' }),
  ev('token', { content: 'Apex Plumbing has 14 open work orders.' }),
  ev('done', { summary: 'Apex Plumbing has 14 open work orders.' }),
];

describe('foldInboxStream', () => {
  it('renders the steps and the answer of a finished run', () => {
    render(<ConversationThread events={foldInboxStream(RUN, false)} />);
    expect(screen.getByTestId('memory-recall-line')).toHaveTextContent('Recalled 5 notes');
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('1 step');
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent(
      'Apex Plumbing has 14 open work orders.',
    );
  });

  it('draws nothing of the answer through the old path, which is the defect', () => {
    // Pins WHY the fold exists: the raw stream alone renders no answer.
    render(<ConversationThread events={RUN} />);
    expect(screen.queryByTestId('assistant-bubble')).toBeNull();
    expect(screen.queryByTestId('tool-pill')).toBeNull();
  });

  it('falls back to the done summary when no token applied', () => {
    const events = [
      ev('user_bubble', { content: 'q' }),
      ev('done', { summary: 'Nothing is overdue.' }),
    ];
    render(<ConversationThread events={foldInboxStream(events, false)} />);
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Nothing is overdue.');
  });

  it('keeps a live run streaming and leaves its open call pending', () => {
    const live = [
      ev('user_bubble', { content: 'q' }),
      ev('tool_start', { tool: 'lookup', description: 'Looking up the lease', call_id: 'c9' }),
    ];
    const out = foldInboxStream(live, true);
    const narration = out.find((e) => e.type === 'narration');
    expect(narration?.data.content).toBe('Looking up the lease…');
    // No end event: the answer bubble is still open.
    expect(out.some((e) => e.type === 'assistant_message_end')).toBe(false);
  });

  it('resolves an unanswered call once the run is over, and says so', () => {
    const over = [
      ev('tool_start', { tool: 'lookup', description: 'Looking up the lease', call_id: 'c9' }),
      ev('done', { summary: 'x' }),
    ];
    const narration = foldInboxStream(over, true).find((e) => e.type === 'narration');
    expect(narration?.data.content).toBe('Looking up the lease — no result');
  });

  it('passes planner events through untouched', () => {
    const planner = [
      ev('narration', { content: 'Reading the work order' }),
      ev('assistant_message_start', { turn_id: 'p1' }),
      ev('assistant_message_token', { turn_id: 'p1', content: 'Here is the plan.' }),
      ev('assistant_message_end', { turn_id: 'p1' }),
    ];
    expect(foldInboxStream(planner, false)).toEqual(planner);
  });

  it('does not open a bubble for a subscription failure', () => {
    // `subscribeInboxThread` turns a thrown fetch into `{type: 'error'}` with
    // `String(err)` as the message. That is not a sentence for the user.
    const lost = ev('error', { message: 'TypeError: Failed to fetch', connection: true });
    expect(foldInboxStream([lost], false)).toEqual([]);
    // Nor inside a run: it would replace the answer with a stack-trace line.
    const mid = foldInboxStream([ev('token', { content: 'Apex has' }), lost], true);
    expect(JSON.stringify(mid)).not.toContain('Failed to fetch');
  });

  it('shows a run refused before its first step', () => {
    const events = [
      ev('user_bubble', { content: 'q' }),
      ev('error', { message: "You've used today's questions. They reset at midnight." }),
    ];
    render(<ConversationThread events={foldInboxStream(events, true)} />);
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent("You've used today's questions");
  });

  it('shows a runner error inside a run instead of an empty bubble', () => {
    const events = [
      ev('user_bubble', { content: 'q' }),
      ev('tool_start', { tool: 'lookup', call_id: 'c1' }),
      ev('error', { message: 'Buildium did not answer in time. Try again in a minute.' }),
    ];
    render(<ConversationThread events={foldInboxStream(events, true)} />);
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Buildium did not answer');
  });

  it('keeps a card that arrived inside the run with that run', () => {
    const events = [
      ev('user_bubble', { content: 'q' }),
      ev('token', { content: 'Here are two options.' }),
      ev('next_steps', { steps: [{ label: 'Call Apex', prompt: 'Call Apex' }] }),
      ev('done', { summary: '' }),
    ];
    render(<ConversationThread events={foldInboxStream(events, false)} />);
    expect(screen.getByTestId(/^next-steps-\d+-step$/)).toHaveTextContent('Call Apex');
  });
});

describe('answerIsStreaming', () => {
  it('is true only while an answer has text and no end', () => {
    expect(answerIsStreaming(foldInboxStream(RUN.slice(0, 5), true))).toBe(true);
    expect(answerIsStreaming(foldInboxStream(RUN, true))).toBe(false);
    expect(answerIsStreaming(foldInboxStream(RUN.slice(0, 3), true))).toBe(false);
  });

  it('keeps the dots when the only user message was retracted (a 409)', () => {
    expect(answerIsStreaming([])).toBe(false);
  });
});
