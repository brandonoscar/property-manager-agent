/**
 * Unit tests for the conversation primitives + the foldEvents
 * reducer (PR #70). One test per component plus a focused test for
 * the event-type → render mapping in ``ConversationThread``.
 *
 * jsdom + RTL via the same setup the ActionChainModal tests use —
 * we mock motion/react to a passthrough to avoid the reduced-motion
 * warning the existing suite already lives with.
 */

import { describe, expect, it, vi } from 'vitest';
import { act, render, screen, fireEvent } from '@testing-library/react';

import { AssistantBubble } from './AssistantBubble';
import { NarrationLine } from './NarrationLine';
import { SuggestionRow } from './SuggestionRow';
import { ConfirmGate } from './ConfirmGate';
import { ConversationThread, foldEvents } from './ConversationThread';
import { CARD_EVENT_TYPES } from './types';
import type {
  ConfirmGateData,
  ConversationEvent,
  SuggestionChipData,
} from './types';

/** Open a trail that auto-collapsed because the answer already landed.
 *
 * ⚠ The trail folds once anything renders after it — expanded is right WHILE
 * it works, clutter under a finished answer. These tests render a COMPLETE
 * sequence, so they are looking at the collapsed state by construction. */
function expandTrail() {
  const toggle = screen.getByTestId('tool-pill').querySelector('button');
  if (toggle) fireEvent.click(toggle);
}

describe('AssistantBubble', () => {
  it('renders content text', () => {
    render(<AssistantBubble content="Hello, world." streaming={false} />);
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Hello, world.');
  });

  it('shows a cursor while streaming', () => {
    const { container } = render(
      <AssistantBubble content="In progress…" streaming />,
    );
    // The cursor is the only ``aria-hidden`` sibling of the text node
    // inside the bubble. Its presence is the streaming affordance.
    const cursor = container.querySelector('[aria-hidden="true"]');
    expect(cursor).not.toBeNull();
  });

  it('hides the cursor when not streaming', () => {
    const { container } = render(<AssistantBubble content="Done." streaming={false} />);
    const cursor = container.querySelector('[aria-hidden="true"]');
    expect(cursor).toBeNull();
  });
});

describe('NarrationLine', () => {
  it('renders the content as italic muted text', () => {
    render(<NarrationLine content="Reading the lease record" />);
    const line = screen.getByTestId('narration-line');
    expect(line).toHaveTextContent('Reading the lease record');
    // Class list should include the italic utility class so the
    // typography rule is enforced.
    expect(line.className).toContain('italic');
  });
});

describe('SuggestionRow', () => {
  const chips: SuggestionChipData[] = [
    {
      action_index: 0,
      label: 'Set priority to High',
      parameter_patch: { priority: 'High' },
      is_destructive: false,
    },
    {
      action_index: 1,
      label: 'Delete task',
      parameter_patch: {},
      is_destructive: true,
    },
  ];

  it('renders one button per chip', () => {
    render(<SuggestionRow chips={chips} onPick={() => undefined} />);
    const rendered = screen.getAllByTestId('suggestion-chip');
    expect(rendered.map((el) => el.textContent)).toEqual([
      'Set priority to High',
      'Delete task',
    ]);
  });

  it('fires onPick with the chip data on click', () => {
    const onPick = vi.fn();
    render(<SuggestionRow chips={chips} onPick={onPick} />);
    fireEvent.click(screen.getAllByTestId('suggestion-chip')[0]);
    expect(onPick).toHaveBeenCalledWith(chips[0]);
  });

  it('disables chips when busy', () => {
    render(<SuggestionRow chips={chips} onPick={() => undefined} disabled />);
    for (const el of screen.getAllByTestId('suggestion-chip')) {
      expect((el as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it('renders nothing when given an empty chip list', () => {
    const { container } = render(<SuggestionRow chips={[]} onPick={() => undefined} />);
    expect(container.querySelector('[data-testid="suggestion-row"]')).toBeNull();
  });
});

describe('ConfirmGate', () => {
  const data: ConfirmGateData = {
    action_index: 0,
    summary: 'Confirm: raise priority to High',
    payload_preview: { priority: 'High' },
    confirm_label: 'Execute',
    cancel_label: 'Cancel',
  };

  it('renders the summary and both buttons', () => {
    render(<ConfirmGate data={data} onConfirm={() => undefined} onCancel={() => undefined} />);
    expect(screen.getByTestId('confirm-gate')).toHaveTextContent('Confirm: raise priority to High');
    expect(screen.getByTestId('confirm-gate-confirm')).toHaveTextContent('Execute');
    expect(screen.getByTestId('confirm-gate-cancel')).toHaveTextContent('Cancel');
  });

  it('reveals the payload preview when "Show raw" is clicked', () => {
    render(<ConfirmGate data={data} onConfirm={() => undefined} onCancel={() => undefined} />);
    expect(screen.queryByTestId('confirm-gate-raw')).toBeNull();
    fireEvent.click(screen.getByTestId('confirm-gate-show-raw'));
    expect(screen.getByTestId('confirm-gate-raw')).toHaveTextContent('"priority": "High"');
  });

  it('fires onConfirm / onCancel on the respective buttons', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmGate data={data} onConfirm={onConfirm} onCancel={onCancel} />);
    fireEvent.click(screen.getByTestId('confirm-gate-confirm'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('confirm-gate-cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('disables both buttons when busy', () => {
    render(
      <ConfirmGate data={data} onConfirm={() => undefined} onCancel={() => undefined} busy />,
    );
    expect((screen.getByTestId('confirm-gate-confirm') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('confirm-gate-cancel') as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('foldEvents', () => {
  it('collapses consecutive assistant tokens into one bubble per turn_id', () => {
    const events: ConversationEvent[] = [
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Hello' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: ', world' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    const folded = foldEvents(events);
    expect(folded).toHaveLength(1);
    const turn = folded[0];
    expect(turn.type).toBe('turn');
    if (turn.type === 'turn') {
      expect(turn.content).toBe('Hello, world');
      expect(turn.streaming).toBe(false);
    }
  });

  it('emits a still-streaming turn when end is missing', () => {
    const events: ConversationEvent[] = [
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'In progress' } },
    ];
    const folded = foldEvents(events);
    expect(folded).toHaveLength(1);
    const turn = folded[0];
    expect(turn.type).toBe('turn');
    if (turn.type === 'turn') {
      expect(turn.streaming).toBe(true);
    }
  });

  it('groups consecutive suggestion_chip events into one row', () => {
    const events: ConversationEvent[] = [
      {
        type: 'suggestion_chip',
        data: { action_index: 0, label: 'A', parameter_patch: {}, is_destructive: false },
      },
      {
        type: 'suggestion_chip',
        data: { action_index: 1, label: 'B', parameter_patch: {}, is_destructive: false },
      },
    ];
    const folded = foldEvents(events);
    expect(folded).toHaveLength(1);
    const suggestions = folded[0];
    expect(suggestions.type).toBe('suggestions');
    if (suggestions.type === 'suggestions') {
      expect(suggestions.chips.map((c) => c.label)).toEqual(['A', 'B']);
    }
  });

  it('renders narration between turns', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Reading the lease' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    const folded = foldEvents(events);
    // Narration folds into the activity trail on every surface now.
    expect(folded.map((i) => i.type)).toEqual(['tool_pill', 'turn']);
  });

  it('renders a confirm gate after suggestions', () => {
    const events: ConversationEvent[] = [
      {
        type: 'suggestion_chip',
        data: { action_index: 0, label: 'A', parameter_patch: {}, is_destructive: false },
      },
      {
        type: 'confirm_gate',
        data: {
          action_index: 0,
          summary: 'Confirm A',
          payload_preview: {},
          confirm_label: 'Execute',
          cancel_label: 'Cancel',
        },
      },
    ];
    const folded = foldEvents(events);
    expect(folded.map((i) => i.type)).toEqual(['suggestions', 'confirm']);
  });

  it('ignores unknown event types instead of crashing', () => {
    const events = [
      { type: 'unknown_type' as never, data: {} },
      { type: 'narration', data: { content: 'still here' } },
    ];
    const folded = foldEvents(events as ConversationEvent[]);
    expect(folded).toHaveLength(1);
    expect(folded[0].type).toBe('tool_pill');
  });

  it('folds a map event into a map render item (maps #539)', () => {
    const events: ConversationEvent[] = [
      {
        type: 'map',
        data: {
          url: '/api/v1/maps/static?markers=42.36,-71.06',
          caption: 'Near Boston',
          markers: [{ lat: 42.36, lng: -71.06 }],
        },
      },
    ];
    const folded = foldEvents(events);
    expect(folded).toHaveLength(1);
    expect(folded[0].type).toBe('map');
  });

  it('drops a map event with no url', () => {
    const events: ConversationEvent[] = [
      { type: 'map', data: { url: '', markers: [] } },
    ];
    expect(foldEvents(events)).toHaveLength(0);
  });
});

describe('ConversationThread', () => {
  it('renders nothing when given no events', () => {
    render(<ConversationThread events={[]} />);
    expect(screen.getByTestId('conversation-thread')).toBeInTheDocument();
    expect(screen.queryByTestId('assistant-bubble')).toBeNull();
  });

  it('renders the full sequence: narration → bubble → suggestions → confirm', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Reading the lease' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      {
        type: 'assistant_message_token',
        data: { turn_id: 't1', content: 'Summary text' },
      },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
      {
        type: 'suggestion_chip',
        data: { action_index: 0, label: 'Approve', parameter_patch: {}, is_destructive: false },
      },
      {
        type: 'confirm_gate',
        data: {
          action_index: 0,
          summary: 'Confirm approval',
          payload_preview: {},
          confirm_label: 'Execute',
          cancel_label: 'Cancel',
        },
      },
    ];
    render(<ConversationThread events={events} />);
    expandTrail();
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('Reading the lease');
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Summary text');
    expect(screen.getByTestId('suggestion-chip')).toHaveTextContent('Approve');
    expect(screen.getByTestId('confirm-gate')).toHaveTextContent('Confirm approval');
  });

  it('shows the thinking indicator when thinking=true', () => {
    render(<ConversationThread events={[]} thinking />);
    expect(screen.getByTestId('thinking-indicator')).toBeInTheDocument();
  });

  it('renders a "recalled from memory" line on memory_recall', () => {
    render(
      <ConversationThread
        events={[{ type: 'memory_recall', data: { results_found: 1 } }]}
      />,
    );
    expect(screen.getByTestId('memory-recall-line')).toHaveTextContent(
      'Recalled from memory',
    );
  });

  it('includes the count when more than one note was recalled', () => {
    render(
      <ConversationThread
        events={[{ type: 'memory_recall', data: { results_found: 3 } }]}
      />,
    );
    expect(screen.getByTestId('memory-recall-line')).toHaveTextContent(
      'Recalled 3 notes from memory',
    );
  });

  it('routes suggestion clicks to onPickSuggestion', () => {
    const onPick = vi.fn();
    const events: ConversationEvent[] = [
      {
        type: 'suggestion_chip',
        data: { action_index: 0, label: 'Pick me', parameter_patch: { x: 1 }, is_destructive: false },
      },
    ];
    render(<ConversationThread events={events} onPickSuggestion={onPick} />);
    fireEvent.click(screen.getByTestId('suggestion-chip'));
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ action_index: 0, label: 'Pick me' }),
    );
  });
});

// ── PR #75 — variant prop, UserBubble, and the "Used N tools" pill ──

describe('ConversationThread (PR #75 chat variant)', () => {
  it('renders a right-aligned iris-purple UserBubble for user_bubble events when variant="chat"', () => {
    // PR #129 — UserBubble now wraps the iris pill in a column with
    // a "You" role label + an avatar on the right. The right-
    // alignment is still flex justify-end on the outermost element;
    // the iris background is now on the pill inside the column,
    // which is the SECOND child after the role label.
    const events: ConversationEvent[] = [
      { type: 'user_bubble', data: { content: 'who is delinquent' } },
    ];
    render(<ConversationThread events={events} variant="chat" />);
    const bubble = screen.getByTestId('user-bubble');
    expect(bubble).toHaveTextContent('who is delinquent');
    expect(bubble.className).toContain('justify-end');
    // Find the iris-pill element inside (the descendant whose
    // background is var(--iris)). Walk the subtree rather than
    // pinning structural depth so future chrome tweaks (extra
    // wrappers) don't break this contract.
    const irisPill = Array.from(
      bubble.querySelectorAll<HTMLElement>('div, span'),
    ).find((el) => el.style.background === 'var(--iris)');
    expect(irisPill).toBeDefined();
    // Dark label on the filled-iris pill (AA contrast: white failed at 3.87:1;
    // var(--canvas) is 5.1:1 on purple / 7.4:1 on the incoming blue).
    expect(irisPill?.style.color).toBe('var(--canvas)');
    // Avatar + "You" role label are both present in the chat
    // variant (the new chrome).
    expect(screen.getByTestId('user-avatar')).toBeInTheDocument();
    expect(bubble.textContent).toContain('You');
  });

  it('folds narration steps into one activity trail (with the step lines) in chat variant', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: '→ Buildium Agent' } },
      { type: 'narration', data: { content: 'Calling buildium list leases' } },
      { type: 'narration', data: { content: 'Got the result from buildium list leases' } },
      { type: 'narration', data: { content: 'Calling buildium get rent roll' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={events} variant="chat" />);
    expandTrail();
    const pill = screen.getByTestId('tool-pill');
    // Summary count + the actual steps (trail is expanded by default), incl.
    // the specialist that was routed to.
    expect(pill).toHaveTextContent('4 steps');
    expect(pill).toHaveTextContent('→ Buildium Agent');
    expect(pill).toHaveTextContent('Calling buildium get rent roll');
    // The individual NarrationLine primitives must NOT render in chat — the
    // trail is the entire replacement.
    expect(screen.queryByTestId('narration-line')).toBeNull();
    expect(screen.getByTestId('assistant-bubble')).toHaveTextContent('Done.');
  });

  it('shows the model reasoning in the trail, beside the step it explains', () => {
    // ⚠ A nested specialist consult publishes NOTHING between its start and
    // its result, and that is most of a long turn (gotcha 37). The model's
    // working-out is the only signal that exists in that window — without it
    // the user is watching a spinner and cannot tell slow from broken.
    const events: ConversationEvent[] = [
      { type: 'reasoning', data: { content: 'The lease ends in March, so renewal is open.' } },
      { type: 'narration', data: { content: 'Looking up the lease' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={events} variant="chat" />);
    expandTrail();
    const pill = screen.getByTestId('tool-pill');
    expect(pill).toHaveTextContent('The lease ends in March');
    expect(pill).toHaveTextContent('Looking up the lease');
    // ⚠ Stream ORDER, not grouped: the thought explains the step after it.
    const rows = screen.getAllByTestId(/^trail-(step|thought)$/);
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'trail-thought',
      'trail-step',
    ]);
    // ⚠ AND IT IS NOT A STEP. A model thinking for ninety seconds would
    // otherwise report dozens of "steps" it never took, and a count nobody
    // can trust is worse than no count.
    expect(pill).toHaveTextContent('1 step');
    expect(pill).not.toHaveTextContent('2 steps');
  });

  it('puts the restatement above the trail, at readable size', () => {
    // ⚠ The FIRST thing on screen, and addressed to the USER. Folding it into
    // the steps pill at 11.5px behind a collapse toggle would defeat its one
    // job — filling the seconds after Enter, which is where "is this broken?"
    // gets decided.
    const events: ConversationEvent[] = [
      { type: 'restatement', data: { content: 'Checking whether 4B is behind on rent.' } },
      { type: 'narration', data: { content: 'Checking Buildium' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    expect(foldEvents(events).map((i) => i.type)).toEqual([
      'restatement',
      'tool_pill',
      'turn',
    ]);
    render(<ConversationThread events={events} variant="chat" />);
    expect(screen.getByTestId('restatement-line')).toHaveTextContent(
      'Checking whether 4B is behind on rent.',
    );
    // Not inside the pill.
    expect(screen.getByTestId('tool-pill')).not.toHaveTextContent('behind on rent');
  });

  it('shows a specialist\'s nested steps under its consult row', () => {
    // ⚠ THE LONGEST SILENT WINDOW. Out on the parent stream a whole consult
    // is ONE tool row that sits there for ninety seconds; gotcha 37 measured
    // that this is most of a long turn (195 of 232 tool calls were nested
    // agent runs). These are the steps happening inside it.
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Checking Buildium' } },
      { type: 'substep', data: { content: 'Listing the open work orders' } },
      { type: 'substep', data: { content: 'Looking up the property' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={events} variant="chat" />);
    expandTrail();
    const pill = screen.getByTestId('tool-pill');
    // ⚠ ONE step. The ORCHESTRATOR consulted one specialist; reporting the
    // specialist's fan-out as the parent's own work would turn every consult
    // into "9 steps" and make the summary worthless.
    expect(pill).toHaveTextContent('1 step');
    expect(pill).not.toHaveTextContent('3 steps');

    // ⚠ FOLDED by default, and reachable in one click. A live turn put forty
    // GMAIL_FETCH_EMAILS substeps on screen at once, which is a debugger's
    // view: it says what the machine did and nothing about what it worked
    // out, and it buries the line worth reading a page above the fold. They
    // are still the transparency promise, so hiding them outright would be
    // worse than showing too many — hence a count, not a deletion.
    expect(screen.queryAllByTestId('trail-substep')).toHaveLength(0);
    expect(pill).toHaveTextContent('2 lookups');

    fireEvent.click(screen.getByTestId('trail-substep-toggle'));
    expect(screen.getAllByTestId('trail-substep')).toHaveLength(2);
    expect(pill).toHaveTextContent('Listing the open work orders');
  });

  it('renders a reasoning-only turn without claiming any steps', () => {
    // The model thought and answered without calling anything. "1 step"
    // would be an invented tool call, and "0 steps" — while true — reads as
    // a failure rather than as what happened.
    render(
      <ConversationThread
        events={[{ type: 'reasoning', data: { content: 'I already know this one.' } }]}
        variant="chat"
      />,
    );
    const pill = screen.getByTestId('tool-pill');
    expect(pill).toHaveTextContent('Thinking');
    expect(pill).not.toHaveTextContent('step');
    expect(screen.getByTestId('trail-thought')).toHaveTextContent('I already know this one.');
  });

  it('uses singular "1 step" when only one narration event arrives', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Calling foo' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={events} variant="chat" />);
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('1 step');
  });

  it('folds the trail on the inbox surface too, keeping its own bubble style', () => {
    // ⚠ This used to assert the OPPOSITE: the inbox kept "one NarrationLine
    // per event and no tool pill". That split was reasonable while the inbox
    // was a card you read, and stopped being reasonable the moment typing
    // turned an inbox item into a conversation — the same run that put forty
    // GMAIL_FETCH_EMAILS rows in chat put forty italic lines down the inbox
    // pane, and every trail fix (folding, promoted reasoning, the live status
    // row) was inert there. A fix bound to one surface is not a fix.
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Reading the lease' } },
      { type: 'narration', data: { content: 'Cross-checking ledger' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'OK' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={events} />);
    expandTrail();
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('Reading the lease');
    expect(screen.queryAllByTestId('narration-line')).toHaveLength(0);
    // ⚠ The bubble styling is still per-variant — `variant` was only ever
    // doing two jobs and this is the one it keeps.
    expect(screen.getByTestId('assistant-bubble').getAttribute('data-variant')).toBe('inbox');
  });

  it('Inbox variant ignores user_bubble events gracefully', () => {
    // The Inbox path never emits ``user_bubble``, but if one ever
    // arrived we still want the renderer to handle it without
    // crashing. It renders a UserBubble regardless of variant —
    // variant only controls the assistant-side styling + pill
    // collapsing.
    const events: ConversationEvent[] = [
      { type: 'user_bubble', data: { content: 'stray' } },
    ];
    render(<ConversationThread events={events} />);
    expect(screen.getByTestId('user-bubble')).toHaveTextContent('stray');
  });

  it('passes confirm_id through onConfirm callback', () => {
    const onConfirm = vi.fn();
    const events: ConversationEvent[] = [
      {
        type: 'confirm_gate',
        data: {
          action_index: 0,
          summary: 'Send email?',
          payload_preview: {},
          confirm_label: 'Send',
          cancel_label: 'Cancel',
          confirm_id: 'confirm-abc-123',
        },
      },
    ];
    render(<ConversationThread events={events} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByTestId('confirm-gate-confirm'));
    expect(onConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ confirm_id: 'confirm-abc-123' }),
    );
  });
});

describe('the live status row says what it is doing', () => {
  // ⚠ "Thinking" was the first thing on screen and often the only thing for
  // a minute and a half. It is true, useless, and indistinguishable from a
  // hang — and the trail already knew the current step, it was just being
  // kept out of the one line people actually watch.

  it('shows the current step rather than the word Thinking', () => {
    const events: ConversationEvent[] = [
      { type: 'reasoning', data: { content: 'Checking whether all leases are being enforced' } },
      { type: 'narration', data: { content: 'Checking Buildium' } },
    ];
    render(<ConversationThread events={events} variant="chat" thinking />);
    const indicator = screen.getByTestId('thinking-indicator');
    expect(indicator).toHaveTextContent('Checking Buildium');
    expect(indicator).not.toHaveTextContent('Thinking');
  });

  it('says nothing at all before there is anything to say', () => {
    // ⚠ This test previously asserted the OPPOSITE — that the empty moment
    // falls back to the word "Thinking" — on the reasoning that it was the one
    // point where the old copy was honest. It is honest and it is still the
    // first thing a user sees on every single turn, which is what made it read
    // as boilerplate. The animated dots already say a turn is running; a word
    // that adds nothing to them is noise, so the row carries the dots alone
    // until a real step exists to name.
    render(<ConversationThread events={[]} variant="chat" thinking />);
    const indicator = screen.getByTestId('thinking-indicator');
    expect(indicator).toBeInTheDocument();
    expect(indicator).not.toHaveTextContent('Thinking');
    expect(indicator.textContent?.trim()).toBe('');
  });

  it('drops the routing arrow — it is a label, not something being done', () => {
    const events: ConversationEvent[] = [
      { type: 'narration', data: { content: '→ Buildium Agent' } },
    ];
    render(<ConversationThread events={events} variant="chat" thinking />);
    expect(screen.getByTestId('thinking-indicator')).toHaveTextContent('Buildium Agent');
    expect(screen.getByTestId('thinking-indicator').textContent).not.toContain('→');
  });
});

describe('the turn has a clock, and the trail gets out of the way', () => {
  it('shows elapsed seconds once a turn is genuinely slow', () => {
    // ⚠ p50 for a chat turn is ~40s and the tail runs into minutes. At that
    // length the words alone cannot separate SLOW from STUCK — a number that
    // keeps moving can, and a frozen one is itself the signal.
    vi.useFakeTimers();
    try {
      render(<ConversationThread events={[]} variant="chat" thinking />);
      // Held back at first: a counter on a two-second turn is nervous
      // furniture.
      expect(screen.queryByTestId('thinking-elapsed')).toBeNull();
      act(() => {
        vi.advanceTimersByTime(6000);
      });
      expect(screen.getByTestId('thinking-elapsed')).toHaveTextContent('6s');
    } finally {
      vi.useRealTimers();
    }
  });

  it('collapses the trail once the answer has landed, and stays open while working', () => {
    const working: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Checking Buildium' } },
    ];
    const { unmount } = render(<ConversationThread events={working} variant="chat" />);
    // Nothing after it yet — expanded is the whole point of a trail.
    expect(screen.getByTestId('trail-step')).toBeInTheDocument();
    unmount();

    const finished: ConversationEvent[] = [
      ...working,
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={finished} variant="chat" />);
    expect(screen.queryByTestId('trail-step')).toBeNull();
    expect(screen.getByTestId('tool-pill')).toHaveTextContent('1 step');
  });

  it('a manual toggle wins over the auto-collapse', () => {
    // ⚠ Why `manual` is nullable rather than a plain boolean: someone who
    // opened a finished trail to read it must not have it shut again, and a
    // `false` default would collapse a running turn.
    const finished: ConversationEvent[] = [
      { type: 'narration', data: { content: 'Checking Buildium' } },
      { type: 'assistant_message_start', data: { turn_id: 't1' } },
      { type: 'assistant_message_token', data: { turn_id: 't1', content: 'Done.' } },
      { type: 'assistant_message_end', data: { turn_id: 't1' } },
    ];
    render(<ConversationThread events={finished} variant="chat" />);
    expect(screen.queryByTestId('trail-step')).toBeNull();
    fireEvent.click(screen.getByTestId('tool-pill').querySelector('button')!);
    expect(screen.getByTestId('trail-step')).toHaveTextContent('Checking Buildium');
  });
});

// --------------------------------------------------------------------------
// A card payload that is not the shape the branch expects must cost THE CARD,
// never the answer.
//
// ⚠ Written after Sentry PYTHON-FASTAPI-3M (2026-09-07) took chat down in
// production. Every card branch unwrapped with `'data' in payload` against a
// value TYPED as an object; `in` throws `TypeError` on a primitive, and the
// React error boundary replaced the whole conversation with "Occupella hit an
// error". The payload was a JSON STRING, because `tool_result` is a jsonb
// column and asyncpg returns jsonb as text with no codec registered.
//
// The backend decodes it now. This side is the second layer, and it is not
// redundant: the throw is catastrophic and the guard is one call.
// --------------------------------------------------------------------------

describe('card payloads that are not objects', () => {
  const STEPS = { steps: [{ label: 'Fix it', prompt: 'fix the thing' }] };

  it('does not throw when a card arrives as JSON text', () => {
    // The exact production shape: the payload serialised, not parsed.
    const events: ConversationEvent[] = [
      { type: 'assistant_message_start', data: { turn_id: 'T' } },
      { type: 'assistant_message_token', data: { turn_id: 'T', content: 'Here is what I found.' } },
      { type: 'assistant_message_end', data: { turn_id: 'T' } },
      { type: 'next_steps', data: JSON.stringify(STEPS) },
    ] as unknown as ConversationEvent[];

    expect(() => foldEvents(events)).not.toThrow();
    // …and the string is real JSON, so the card still renders.
    expect(foldEvents(events).some((i) => i.type === 'next_steps')).toBe(true);
  });

  it('drops a card whose payload is unusable and keeps the prose', () => {
    // ⚠ Both directions. A guard that throws loses the answer; a guard that
    // waves anything through hands a renderer something it cannot draw.
    for (const bad of [null, 42, 'not json at all', ['a', 'b'], undefined]) {
      const events = [
        { type: 'assistant_message_start', data: { turn_id: 'T' } },
        { type: 'assistant_message_token', data: { turn_id: 'T', content: 'The answer survives.' } },
        { type: 'assistant_message_end', data: { turn_id: 'T' } },
        { type: 'next_steps', data: bad },
      ] as unknown as ConversationEvent[];

      let items: ReturnType<typeof foldEvents> = [];
      expect(() => (items = foldEvents(events))).not.toThrow();
      expect(items.some((i) => i.type === 'next_steps')).toBe(false);
      expect(JSON.stringify(items)).toContain('The answer survives.');
    }
  });

  it('unwraps {data: …} but keeps a payload whose own field is called data', () => {
    // ⚠ `'data' in payload` was the wrong question: a card carrying
    // `data: null` or a scalar `data` unwrapped to something undrawable.
    const wrapped = [
      { type: 'next_steps', data: { data: STEPS } },
    ] as unknown as ConversationEvent[];
    expect(foldEvents(wrapped).some((i) => i.type === 'next_steps')).toBe(true);

    const scalarData = [
      { type: 'next_steps', data: { ...STEPS, data: 'a note' } },
    ] as unknown as ConversationEvent[];
    expect(foldEvents(scalarData).some((i) => i.type === 'next_steps')).toBe(true);
  });

  // ⚠ A MINIMAL RENDERABLE PAYLOAD PER CARD TYPE, keyed off CARD_EVENT_TYPES
  // itself rather than a hand-written list.
  //
  // The list this replaced was hardcoded, named ten branches, and OMITTED
  // `disclosure` — the one branch that still carried the defect. A fan-out
  // guard written from a hand-typed set cannot see the member it forgot to
  // type, which is gotcha 45's rule (read the corpus) arriving inside the
  // fix for gotcha 25 (the fan-out). The exhaustiveness test below is what
  // makes the omission impossible to repeat.
  const RENDERABLE: Record<string, unknown> = {
    tabular_data: { columns: ['A'], rows: [['1']] },
    artifacts: { artifacts: [{ name: 'r.pdf', url: 'https://x/r.pdf' }] },
    email_draft: { to: ['a@b.c'], variants: [{ body: 'B' }] },
    detail_card: { title: 'T', fields: [{ label: 'L', value: 'V' }] },
    chart: {
      points: [
        { label: 'A', value: 1 },
        { label: 'B', value: 2 },
      ],
    },
    schedule: { slots: [{ label: 'T', when: '2026-09-10T10:00:00Z' }] },
    checklist: { items: [{ label: 'Do it' }] },
    map: { url: 'https://maps.example/static.png' },
    form: { fields: [{ name: 'n', label: 'L', type: 'text' }] },
    disclosure: {
      answer: 'Unit 4B owes $2,400.',
      reasoning: {
        sources: ['buildium: $2,400 past due'],
        confidence: 'high',
        confidence_reason: 'mirror data',
        unverified: [],
        footer_well_formed: true,
        specialist_trail: ['buildium_agent'],
      },
    },
    next_steps: { steps: [{ label: 'Fix it', prompt: 'fix the thing' }] },
  };

  // ⚠ The rendered ITEM type is not always the EVENT type — `tabular_data`
  // becomes `tabular`. Asserting `items.some(i => i.type === event.type)`
  // would report that branch as silently dropping every payload, which is
  // gotcha 28's producer/consumer boundary: two names for one thing, and a
  // guard that reads only one side measures the wrong half.
  const RENDERED_AS: Record<string, string> = { tabular_data: 'tabular' };
  const renderedType = (eventType: string) => RENDERED_AS[eventType] ?? eventType;

  it('has a fixture for every card type, so a new one cannot skip this file', () => {
    // The staleness half. Without it, adding an eleventh CARD_EVENT_TYPE
    // reopens exactly the gap `disclosure` sat in — silently.
    expect(Object.keys(RENDERABLE).sort()).toEqual([...CARD_EVENT_TYPES].sort());
  });

  it('every card branch RENDERS a JSON-string payload, not merely survives it', () => {
    // ⚠ BOTH DIRECTIONS, and the second is the one that was missing.
    //
    // The old assertion was `not.toThrow()` alone — which `disclosure` passed
    // the whole time, because `parseDisclosure` returned `null` for a string
    // instead of crashing. So the branch failed SAFE and SILENT: on every
    // hydrated thread the receipts panel simply was not there, and a missing
    // panel reads identically to a turn that never emitted one. A guard that
    // only asks "did it crash" cannot tell those apart.
    for (const type of CARD_EVENT_TYPES) {
      const events = [
        { type, data: JSON.stringify(RENDERABLE[type]) },
      ] as unknown as ConversationEvent[];

      let items: ReturnType<typeof foldEvents> = [];
      expect(() => (items = foldEvents(events)), `${type} threw on JSON text`).not.toThrow();
      expect(
        items.some((i) => i.type === renderedType(type)),
        `${type} silently dropped a well-formed JSON-string payload — ` +
          'this is the hydration path (tool_result is jsonb, asyncpg returns text)',
      ).toBe(true);
    }
  });

  it('every card branch drops an unusable payload without throwing', () => {
    for (const type of CARD_EVENT_TYPES) {
      const events = [{ type, data: 'plainly a string' }] as unknown as ConversationEvent[];
      let items: ReturnType<typeof foldEvents> = [];
      expect(() => (items = foldEvents(events)), `${type} threw on a string payload`).not.toThrow();
      expect(items.some((i) => i.type === renderedType(type)), `${type} rendered junk`).toBe(false);
    }
  });
});
