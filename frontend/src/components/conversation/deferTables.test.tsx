/**
 * A finished table must not un-render itself when the model starts talking
 * again.
 *
 * One answer arrives as several turns — `turn → tool_pill → turn → tool_pill
 * → turn` — because the model narrates between its own tool calls. The
 * deferral rule ("hold a table while the sentence above it is still being
 * written") was applied to EVERY table whenever ANY turn was streaming, so
 * each new narration collapsed the tables already on screen back into 92px
 * grey skeletons and then popped them out again.
 *
 * That is the flicker in the 2026-09-06 demo recording, and it is the worst
 * kind of visual bug: content the user has already read visibly disappearing.
 *
 * ⚠ Tested through `foldEvents`, which is what the component actually calls.
 * `deferTablesWhileStreaming` is module-private, and exporting it to test it
 * would prove the helper is correct while saying nothing about whether the
 * pipeline uses it that way (gotcha 62's shape).
 */

import { describe, expect, it } from 'vitest';

import { foldEvents } from './ConversationThread';
import type { ConversationEvent } from './types';

const start = (id: string): ConversationEvent =>
  ({ type: 'assistant_message_start', data: { turn_id: id } }) as ConversationEvent;
const token = (id: string, content: string): ConversationEvent =>
  ({ type: 'assistant_message_token', data: { turn_id: id, content } }) as ConversationEvent;
const end = (id: string): ConversationEvent =>
  ({ type: 'assistant_message_end', data: { turn_id: id } }) as ConversationEvent;
const table = (): ConversationEvent =>
  ({
    type: 'tabular_data',
    data: { columns: [{ key: 'a', label: 'A' }], rows: [{ a: 1 }] },
  }) as ConversationEvent;

const tables = (events: ConversationEvent[]) =>
  foldEvents(events).filter((i) => i.type === 'tabular') as { deferred?: boolean }[];

describe('tables are deferred only while the prose ABOVE them is unfinished', () => {
  it('a table below the live turn is held behind the skeleton', () => {
    // The rule the deferral exists for: a completed table under a
    // half-written sentence reads as an answer that jumped ahead of itself.
    const items = tables([start('t1'), token('t1', 'Here is what I found'), table()]);

    expect(items).toHaveLength(1);
    expect(items[0].deferred).toBe(true);
  });

  it('a table ABOVE a newly-opened turn keeps rendering', () => {
    // ⚠ THE DEFECT. Turn 1 finished and its table rendered; the model then
    // starts narrating again, which used to yank that table back into a grey
    // box. The prose it belongs to is complete — nothing about a later turn
    // makes an earlier table premature.
    const items = tables([
      start('t1'),
      token('t1', 'Open work orders by property:'),
      end('t1'),
      table(),
      start('t2'),
      token('t2', 'Two of those are stalled'),
    ]);

    expect(items).toHaveLength(1);
    expect(items[0].deferred).toBeFalsy();
  });

  it('both rules hold in the same answer', () => {
    // The realistic shape: one settled table above, one still-arriving below.
    const items = tables([
      start('t1'),
      token('t1', 'Open work orders by property:'),
      end('t1'),
      table(),
      start('t2'),
      token('t2', 'And this month by category:'),
      table(),
    ]);

    expect(items).toHaveLength(2);
    expect(items[0].deferred).toBeFalsy();
    expect(items[1].deferred).toBe(true);
  });

  it('nothing is deferred once the run finishes', () => {
    // The skeleton is bounded by the run: ChatThread leaves the last turn
    // open only while `isRunning`. If this ever fails, a dead run leaves grey
    // boxes on screen permanently.
    const items = tables([start('t1'), token('t1', 'Here it is'), table(), end('t1')]);

    expect(items).toHaveLength(1);
    expect(items[0].deferred).toBeFalsy();
  });
});
