/**
 * A run's output belongs to the thread that ASKED, not the one on screen.
 *
 * Every stream mutator used to write to `activeThreadId`, read fresh inside
 * the store each time an event arrived. A run is minutes long and switching
 * thread is one click, so the two are routinely different — and the failure
 * is silent by construction: the text that lands is a real, correct answer,
 * just to a question the reader is not looking at.
 *
 * The 2026-09-06 demo take is the record of it: the thread titled "Which
 * leads have gone quiet?" rendered a complete, accurate answer about 200 E
 * Colfax Ave — the previous question's. Nothing about that reads as an error.
 *
 * ⚠ These tests RUN the store. The routing bug is a runtime data-flow fault;
 * an AST guard on the call site (which also exists, below) can only see that
 * an argument is passed, never where it ends up.
 */

import { beforeEach, describe, expect, it } from 'vitest';

// ⚠ `?raw` (Vite) rather than `node:fs` — the frontend tsconfig has no node
// types, and a test file tsc rejects still reports GREEN under vitest
// (gotcha 72), so the guard would silently never run.
import submitSource from '../hooks/useSubmitChatTask.ts?raw';
import watchSource from '../hooks/useWatchThread.ts?raw';

import { useStore } from './store';

const OTHER = 't-other';
const RUN = 't-run';

/** Two threads, each with a blank agent bubble, with the WRONG one selected. */
function twoThreads() {
  useStore.setState({
    threads: [
      {
        id: RUN,
        title: 'the question that was asked',
        messages: [{ id: 'a-run', role: 'agent', content: '', timestamp: 0, events: [] }],
        status: 'running',
        createdAt: 0,
      },
      {
        id: OTHER,
        title: 'a thread the user clicked into',
        messages: [{ id: 'a-other', role: 'agent', content: '', timestamp: 0, events: [] }],
        status: 'idle',
        createdAt: 0,
      },
    ],
    // The user has navigated AWAY from the running thread — the whole point.
    activeThreadId: OTHER,
    streamingText: '',
    pendingConfirmation: null,
  });
}

const thread = (id: string) => useStore.getState().threads.find((t) => t.id === id)!;
const lastText = (id: string) => thread(id).messages[thread(id).messages.length - 1].content;

describe('stream mutators route by the RUN thread, not the active one', () => {
  beforeEach(twoThreads);

  it('tokens land in the thread that asked', () => {
    useStore.getState().appendToken('Aaron Tillman is ', RUN);
    useStore.getState().appendToken('$3,900 past due.', RUN);

    expect(lastText(RUN)).toBe('Aaron Tillman is $3,900 past due.');
    // The demo defect, stated as an assertion: the visible thread must be
    // untouched by a run it did not start.
    expect(lastText(OTHER)).toBe('');
  });

  it('the terminal summary lands in the thread that asked', () => {
    useStore.getState().finishAgentResponse('the whole answer', RUN);

    expect(lastText(RUN)).toBe('the whole answer');
    expect(thread(RUN).status).toBe('done');
    expect(lastText(OTHER)).toBe('');
    expect(thread(OTHER).status).toBe('idle');
  });

  it('an error lands in the thread that asked', () => {
    useStore.getState().setError('That question needed more work than fits in one turn.', RUN);

    expect(lastText(RUN)).toContain('needed more work');
    expect(thread(RUN).status).toBe('error');
    // ⚠ Both halves matter. An error written into the WRONG thread also marks
    // that thread failed, so a healthy conversation the user is reading turns
    // red because of a run somewhere else.
    expect(lastText(OTHER)).toBe('');
    expect(thread(OTHER).status).toBe('idle');
  });

  it('events land in the thread that asked', () => {
    useStore
      .getState()
      .addEvent({ type: 'narration', data: { content: 'checked Buildium' }, timestamp: 0 }, RUN);

    expect(thread(RUN).messages[0].events).toHaveLength(1);
    expect(thread(OTHER).messages[0].events).toHaveLength(0);
  });

  it('a confirm gate marks the RUN thread waiting, not the visible one', () => {
    useStore.getState().addEvent(
      {
        type: 'confirm_gate',
        data: { confirm_id: 'c1', summary: 'Add a note to work order 11316' },
        timestamp: 0,
      },
      RUN,
    );

    expect(thread(RUN).status).toBe('waiting_confirm');
    expect(thread(OTHER).status).toBe('idle');
  });

  it('omitting the thread id still falls back to the active thread', () => {
    // The back-compat path every other caller relies on. Removing it would
    // break `useUploadDocument` and the Inbox silently.
    useStore.getState().appendToken('typed into whatever is open', undefined);
    expect(lastText(OTHER)).toBe('typed into whatever is open');
  });
});

describe('the reply accumulates on the message, not on a store-wide buffer', () => {
  beforeEach(twoThreads);

  it('selecting another thread mid-run does not truncate the running answer', () => {
    // ⚠ This is a SECOND bug the same shape hid. `appendToken` rebuilt the
    // bubble from `streamingText` every token, and `setActiveThread` clears that
    // buffer — so one click mid-answer replaced everything already streamed
    // with only what arrived after the click.
    useStore.getState().appendToken('The first half of the answer. ', RUN);
    useStore.getState().setActiveThread(OTHER);
    useStore.getState().appendToken('The second half.', RUN);

    expect(lastText(RUN)).toBe('The first half of the answer. The second half.');
  });

  it('a thinking event still resets the bubble so pre-tool prose is not glued on', () => {
    // The reset is deliberate (the thought moves to the activity trail); it
    // must survive the switch to per-message accumulation.
    useStore.getState().appendToken('Let me check occupancy', RUN);
    useStore
      .getState()
      .addEvent({ type: 'thinking', data: { content: 'Let me check occupancy' }, timestamp: 0 }, RUN);
    useStore.getState().appendToken('All 50 units are occupied.', RUN);

    expect(lastText(RUN)).toBe('All 50 units are occupied.');
  });
});

describe('the callers name their thread', () => {
  // ⚠ AST guards. The tests above prove the STORE routes correctly; nothing
  // in them can see a caller that stops passing the id and silently falls
  // back to `activeThreadId` — which is the exact defect, restored (gotcha 62).

  it('useSubmitChatTask passes the run thread to every stream mutator', () => {
    for (const call of [
      'appendToken(event.data.content as string, runThreadId)',
      'finishAgentResponse(event.data.summary as string, runThreadId)',
      'setError(event.data.message as string, runThreadId)',
      'addEvent(event, runThreadId)',
      'addUserMessage(task, runThreadId)',
      'startAgentResponse(runThreadId)',
    ]) {
      expect(submitSource).toContain(call);
    }
  });

  it('useWatchThread passes the watched thread to every stream mutator', () => {
    expect(watchSource).toContain('appendToken(event.data.content as string, threadId)');
    expect(watchSource).toContain('addEvent(event, threadId)');
  });
});

describe('the SYNTHESIZE footer is receipts, not prose', () => {
  beforeEach(twoThreads);

  const ANSWER = 'No — all 50 units across your 50 properties are currently occupied.';
  const FOOTER =
    '\n\nSources: Buildium (portfolio occupancy snapshot)\n' +
    'Confidence: likely — the data is internally consistent\n' +
    'Unverified: turnover in the last 5 days';

  it('the bubble drops the footer once the disclosure arrives', () => {
    // What the demo take showed: the three contract lines streamed as tokens
    // and stayed in the reply, with the "How I got this" panel rendered under
    // them — the receipts printed twice, once as scaffolding.
    useStore.getState().appendToken(ANSWER + FOOTER, RUN);
    useStore
      .getState()
      .addEvent({ type: 'disclosure', data: { answer: ANSWER }, timestamp: 0 }, RUN);

    expect(lastText(RUN)).toBe(ANSWER);
    expect(lastText(RUN)).not.toContain('Confidence:');
  });

  it('the panel still renders — the event is kept, not consumed', () => {
    useStore.getState().appendToken(ANSWER + FOOTER, RUN);
    useStore
      .getState()
      .addEvent({ type: 'disclosure', data: { answer: ANSWER }, timestamp: 0 }, RUN);

    const events = thread(RUN).messages[thread(RUN).messages.length - 1].events;
    expect(events.some((e) => e.type === 'disclosure')).toBe(true);
  });

  it('declines when the stripped text is not a prefix of what is on screen', () => {
    // ⚠ The direction that matters. `answer` comes from `final_output` and the
    // bubble holds streamed deltas; those legitimately diverge (gotcha 56).
    // Swapping on a mismatch would replace a good answer with a different one,
    // which is far worse than leaving a footer visible.
    useStore.getState().appendToken('An entirely different streamed answer.', RUN);
    useStore
      .getState()
      .addEvent({ type: 'disclosure', data: { answer: ANSWER }, timestamp: 0 }, RUN);

    expect(lastText(RUN)).toBe('An entirely different streamed answer.');
  });

  it('an empty or missing answer never blanks the bubble', () => {
    useStore.getState().appendToken(ANSWER, RUN);
    useStore.getState().addEvent({ type: 'disclosure', data: { answer: '' }, timestamp: 0 }, RUN);
    useStore.getState().addEvent({ type: 'disclosure', data: {}, timestamp: 0 }, RUN);

    expect(lastText(RUN)).toBe(ANSWER);
  });

  it('an answer with no footer is left byte-for-byte alone', () => {
    // A short reply legitimately has no footer, so `strip_synthesis_footer`
    // returns it unchanged and the lengths match — the guard must not treat
    // "equal" as "shorter" and start rewriting identical text.
    useStore.getState().appendToken(ANSWER, RUN);
    useStore
      .getState()
      .addEvent({ type: 'disclosure', data: { answer: ANSWER }, timestamp: 0 }, RUN);

    expect(lastText(RUN)).toBe(ANSWER);
  });
});

describe('a pending confirm gate survives a thread switch', () => {
  beforeEach(twoThreads);

  const gate = () => useStore.getState().pendingConfirmation;

  const raise = (thread: string) =>
    useStore.getState().addEvent(
      {
        type: 'confirm_gate',
        data: { confirm_id: 'c1', summary: 'Add a note to work order 11316' },
        timestamp: 0,
      },
      thread,
    );

  it('the gate records the thread whose run raised it', () => {
    raise(RUN);
    expect(gate()?.threadId).toBe(RUN);
  });

  it('switching threads does not destroy it', () => {
    // ⚠ The defect. `setActiveThread` cleared `pendingConfirmation`, so a
    // glance at another conversation removed the only way to approve. The
    // backend gate then waited out its 300s and denied by default: the write
    // the user asked for silently did not happen, with nothing on screen to
    // say so.
    raise(RUN);
    useStore.getState().setActiveThread(OTHER);
    useStore.getState().setActiveThread(RUN);

    expect(gate()?.confirmId).toBe('c1');
  });

  it('starting a NEW turn still abandons it', () => {
    // The other direction, and it must keep working: a stale card must not
    // re-render under a fresh question. The backend times the unanswered
    // gate out safely (deny-by-default).
    raise(RUN);
    useStore.getState().addUserMessage('a different question', RUN);

    expect(gate()).toBeNull();
  });
});
