/**
 * One reply, not six messages.
 *
 * The model narrates between its own tool calls now, so an answer arrives as
 * several assistant turns with the activity trail between them. Two things had
 * to change for that to read as one thing being written rather than a chat log:
 * the bubble lost its surface, and a continuing turn drops the avatar and the
 * "Occupella" badge.
 *
 * ⚠ The badge suppression is exactly the kind of change that is right in one
 * direction and wrong in the other. Suppressing it everywhere loses the role
 * marker entirely; suppressing it nowhere is the six-badge stack. Both
 * directions are pinned below.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { AssistantBubble } from './AssistantBubble';
import { continuationTurns } from './ConversationThread';

type Item = { type: string; turnId?: string };

const items = (...types: string[]): Item[] =>
  types.map((t, i) => (t === 'turn' ? { type: t, turnId: `t${i}` } : { type: t }));

describe('continuationTurns', () => {
  it('leaves the first turn after the user alone', () => {
    // user → trail → turn. The trail comes FIRST when the model calls a tool
    // before saying anything, so "previous item is a pill" cannot be the whole
    // test -- that would strip the badge off the opening turn of every reply.
    const marks = continuationTurns(items('user', 'tool_pill', 'turn') as never);
    expect(marks.has(2)).toBe(false);
  });

  it('marks every turn after the first in one reply', () => {
    const marks = continuationTurns(
      items('user', 'turn', 'tool_pill', 'turn', 'tool_pill', 'turn') as never,
    );
    expect([...marks].sort()).toEqual([3, 5]);
  });

  it('gives the badge back after the user speaks again', () => {
    const marks = continuationTurns(items('user', 'turn', 'user', 'turn') as never);
    expect(marks.size).toBe(0);
  });

  it('treats a card between turns as a real break', () => {
    // A table or a confirm gate is a change of subject on the page, not the
    // model drawing breath -- the badge re-establishes who is speaking after it.
    const marks = continuationTurns(items('user', 'turn', 'tabular', 'turn') as never);
    expect(marks.size).toBe(0);
  });
});

describe('the chat bubble is prose, not a container', () => {
  it('renders no surface behind the text', () => {
    const { container } = render(
      <AssistantBubble content="Rent is due on the 1st." streaming={false} variant="chat" />,
    );
    // The box was `background: var(--canvas-elevated)` + `1px solid var(--line)`
    // on the text wrapper. Assert on the STYLE rather than a class name: the
    // border came from an inline style and a class-name check would pass
    // against a re-introduced one.
    const boxed = Array.from(container.querySelectorAll<HTMLElement>('div')).filter(
      (el) =>
        el.style.background === 'var(--canvas-elevated)' &&
        el.style.border.includes('var(--line)'),
    );
    expect(boxed).toHaveLength(0);
    expect(screen.getByText('Rent is due on the 1st.')).toBeTruthy();
  });

  it('names the speaker once, at the top of a reply', () => {
    render(<AssistantBubble content="Checking Buildium." streaming variant="chat" />);
    expect(screen.getByTestId('assistant-avatar')).toBeTruthy();
    expect(screen.getByText('Occupella')).toBeTruthy();
  });

  it('drops the badge on a continuation but keeps the column', () => {
    render(
      <AssistantBubble content="Nothing there." streaming variant="chat" continuation />,
    );
    expect(screen.queryByTestId('assistant-avatar')).toBeNull();
    expect(screen.queryByText('Occupella')).toBeNull();
    // ⚠ The spacer is the point. Dropping the avatar element outright would
    // shift every continuing paragraph 30px left of the first one.
    expect(screen.getByTestId('assistant-avatar-spacer')).toBeTruthy();
  });

  it('still shows the cursor while a continuation streams', () => {
    // The cursor is what makes it read as typing rather than as posted
    // messages -- it must survive the chrome removal.
    const { container } = render(
      <AssistantBubble content="Checking…" streaming variant="chat" continuation />,
    );
    const cursor = Array.from(container.querySelectorAll<HTMLElement>('span')).filter((el) =>
      el.style.animation.includes('pulse-dot'),
    );
    expect(cursor.length).toBe(1);
  });

  it('leaves the inbox variant untouched', () => {
    render(<AssistantBubble content="Inbox prose." streaming={false} variant="inbox" />);
    expect(screen.queryByTestId('assistant-avatar')).toBeNull();
    expect(screen.queryByText('Occupella')).toBeNull();
  });
});
