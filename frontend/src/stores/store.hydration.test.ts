/**
 * Reloading a thread must bring the cards back.
 *
 * Cards live on the live SSE stream. Until 2026-09-01 nothing wrote them
 * down, so a refresh replayed the prose and dropped the form, the table, the
 * chart and the next-step chips. For a FORM that is a dead end: the assistant
 * said "fill that in", is waiting, and was told not to re-ask in prose — so
 * the user is left staring at a sentence about a form that is not there.
 */

import { describe, expect, it, beforeEach } from 'vitest';

import { useStore } from './store';
import { CARD_HISTORY_PREFIX } from '../components/conversation/types';
import type { ChatMessage as PersistedChatMessage } from '../services/chat';

const THREAD = 't-1';

function row(over: Partial<PersistedChatMessage>): PersistedChatMessage {
  return {
    id: 'r',
    role: 'assistant',
    content: '',
    created_at: '2026-09-01T00:00:00Z',
    ...over,
  } as PersistedChatMessage;
}

function cardRow(type: string, data: Record<string, unknown>, id = `c-${type}`) {
  return row({ id, role: 'tool', tool_name: `${CARD_HISTORY_PREFIX}${type}`, tool_result: data });
}

beforeEach(() => {
  useStore.setState({
    threads: [{ id: THREAD, title: 't', messages: [], status: 'idle' }] as never,
    activeThreadId: THREAD,
  });
});

const messages = () => useStore.getState().threads[0].messages;

describe('hydrateThreadMessages', () => {
  it('restores a card onto the turn it belongs to', () => {
    useStore.getState().hydrateThreadMessages(THREAD, [
      row({ id: 'u1', role: 'user', content: 'make a task for the leaky faucet' }),
      cardRow('form', { title: 'New task', fields: [{ name: 'unit', label: 'Unit' }] }),
      row({ id: 'a1', role: 'assistant', content: 'Fill that in and I will take it from there.' }),
    ]);

    expect(messages().map((m) => m.role)).toEqual(['user', 'agent']);
    const events = messages()[1].events;
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('form');
    expect((events[0].data as { title: string }).title).toBe('New task');
  });

  it('keeps several cards on one turn, in stored order', () => {
    useStore.getState().hydrateThreadMessages(THREAD, [
      cardRow('tabular_data', { rows: [] }, 'c1'),
      cardRow('next_steps', { steps: [] }, 'c2'),
      row({ id: 'a1', content: 'Here you go.' }),
    ]);
    expect(messages()[0].events.map((e) => e.type)).toEqual(['tabular_data', 'next_steps']);
  });

  it('does not carry a card across into the next turn', () => {
    // ⚠ A card buffered before a USER message belongs to a turn that already
    // ended. Carrying it forward re-renders the previous answer's form under
    // the next reply — a stale form is worse than a missing one, because it
    // looks live and submitting it answers a question nobody asked again.
    useStore.getState().hydrateThreadMessages(THREAD, [
      cardRow('form', { title: 'First' }, 'c1'),
      row({ id: 'a1', content: 'first answer' }),
      row({ id: 'u2', role: 'user', content: 'something else' }),
      row({ id: 'a2', content: 'second answer' }),
    ]);
    const [first, , second] = messages();
    expect(first.events.map((e) => e.type)).toEqual(['form']);
    expect(second.events).toEqual([]);
  });

  it('does not attach an orphaned card to a LATER answer', () => {
    // ⚠ The input that actually distinguishes the reset, and the one the
    // first version of the test above missed. A turn can emit a card and then
    // die before its assistant row lands — a deadline, an error — leaving the
    // card orphaned. The user asks again; without clearing on the USER row
    // that stale form attaches to the NEXT answer and renders as if the
    // assistant just asked for it. A live-looking form nobody asked for is
    // worse than a missing one: submitting it sends an answer to a dead turn.
    useStore.getState().hydrateThreadMessages(THREAD, [
      cardRow('form', { title: 'Orphan' }, 'c1'),
      row({ id: 'u1', role: 'user', content: 'never mind, something else' }),
      row({ id: 'a1', content: 'sure, here you go' }),
    ]);
    const [user, answer] = messages();
    expect(user.events).toEqual([]);
    expect(answer.events).toEqual([]);
  });

  it('drops ordinary tool rows, which have nothing to replay', () => {
    useStore.getState().hydrateThreadMessages(THREAD, [
      row({ id: 't1', role: 'tool', tool_name: 'buildium_list_tasks', tool_result: { rows: 3 } }),
      row({ id: 'a1', content: 'three tasks' }),
    ]);
    expect(messages()).toHaveLength(1);
    expect(messages()[0].events).toEqual([]);
  });

  it('ignores a card row with no payload rather than rendering an empty card', () => {
    useStore.getState().hydrateThreadMessages(THREAD, [
      row({ id: 'c1', role: 'tool', tool_name: `${CARD_HISTORY_PREFIX}form`, tool_result: null }),
      row({ id: 'a1', content: 'answer' }),
    ]);
    expect(messages()[0].events).toEqual([]);
  });

  it('never attaches a card to a user bubble', () => {
    useStore.getState().hydrateThreadMessages(THREAD, [
      cardRow('form', { title: 'X' }),
      row({ id: 'u1', role: 'user', content: 'hello' }),
    ]);
    expect(messages()[0].events).toEqual([]);
  });
});
