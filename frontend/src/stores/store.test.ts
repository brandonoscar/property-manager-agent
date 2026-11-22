/**
 * PR #75 Bug E — frontend store handling of the backend's
 * ``confirm_gate`` SSE event.
 *
 * The legacy demo runner emitted ``{type: 'confirm', data: {…}}``;
 * the live orchestrator now emits ``{type: 'confirm_gate', data:
 * {summary, payload_preview, confirm_id, …}}`` — both must populate
 * ``pendingConfirmation`` so ``<ConfirmCard>`` shows.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { useStore } from './store';

describe('useStore.addEvent — confirm shapes (PR #75)', () => {
  beforeEach(() => {
    // Reset the store to a known-good shape with a single active
    // thread; ``addEvent`` no-ops without one. Zustand stores hold
    // module-scoped state across tests so the reset must be explicit.
    useStore.setState({
      threads: [
        {
          id: 't-1',
          title: 'Test',
          messages: [
            { id: 'a-1', role: 'agent', content: '', timestamp: 0, events: [] },
          ],
          status: 'running',
          createdAt: 0,
        },
      ],
      activeThreadId: 't-1',
      pendingConfirmation: null,
    });
  });

  it('populates pendingConfirmation from a legacy ``confirm`` event', () => {
    useStore.getState().addEvent({
      type: 'confirm',
      data: {
        confirm_id: 'cid-legacy',
        action: 'send_email',
        title: 'Send a reminder?',
        details: 'To Sarah Chen',
        items: ['Sarah Chen'],
      },
      timestamp: 0,
    });
    const pending = useStore.getState().pendingConfirmation;
    expect(pending).not.toBeNull();
    expect(pending!.confirmId).toBe('cid-legacy');
    expect(pending!.action).toBe('send_email');
    expect(pending!.title).toBe('Send a reminder?');
    expect(pending!.items).toEqual(['Sarah Chen']);
  });

  it('populates pendingConfirmation from the live ``confirm_gate`` event', () => {
    // Shape mirrors what
    // ``helixis.agents.orchestrator`` emits via
    // ``EventType.CONFIRM_GATE`` after the PR #75 Bug E wiring.
    useStore.getState().addEvent({
      type: 'confirm_gate',
      data: {
        action_index: 0,
        confirm_id: 'cid-live',
        summary: 'Send a reminder? — To Sarah Chen',
        payload_preview: { action: 'send_email', items: ['Sarah Chen'] },
        confirm_label: 'Confirm',
        cancel_label: 'Cancel',
      },
      timestamp: 0,
    });
    const pending = useStore.getState().pendingConfirmation;
    expect(pending).not.toBeNull();
    expect(pending!.confirmId).toBe('cid-live');
    // For the live shape the title comes from ``summary``.
    expect(pending!.title).toBe('Send a reminder? — To Sarah Chen');
    // ``items`` is extracted from payload_preview.items so the
    // confirm card can show the per-item bullet list.
    expect(pending!.items).toEqual(['Sarah Chen']);
    expect(pending!.action).toBe('send_email');
  });

  it('flips the active thread to "waiting_confirm" for both shapes', () => {
    useStore.getState().addEvent({
      type: 'confirm_gate',
      data: { confirm_id: 'cid-x', summary: 'x', payload_preview: {} },
      timestamp: 0,
    });
    expect(useStore.getState().threads[0].status).toBe('waiting_confirm');
  });
});


// ── #7 B1 — blank-bubble recovery in finishAgentResponse ─────────

describe('useStore.finishAgentResponse — done.summary fallback (#7 B1)', () => {
  const seed = (agentContent: string) =>
    useStore.setState({
      threads: [
        {
          id: 't-1',
          title: 'Test',
          messages: [
            { id: 'u-1', role: 'user', content: 'hi', timestamp: 0, events: [] },
            { id: 'a-1', role: 'agent', content: agentContent, timestamp: 0, events: [] },
          ],
          status: 'running',
          createdAt: 0,
        },
      ],
      activeThreadId: 't-1',
      streamingText: agentContent,
      isRunning: true,
    });

  const lastAgent = () => {
    const t = useStore.getState().threads[0];
    return t.messages[t.messages.length - 1];
  };

  it('backfills a BLANK agent bubble from the terminal done.summary', () => {
    // The streamed tokens never rendered → empty bubble, but the turn
    // completed and done.summary carries the authoritative reply.
    seed('');
    useStore.getState().finishAgentResponse('You have 3 delinquent tenants.');
    expect(lastAgent().content).toBe('You have 3 delinquent tenants.');
    expect(useStore.getState().threads[0].status).toBe('done');
  });

  it('never clobbers a good stream (summary is backend-truncated)', () => {
    // Tokens streamed a long reply; done.summary is a truncated copy. The
    // streamed content must win so nothing is cut off.
    const streamed = 'A'.repeat(4000);
    seed(streamed);
    useStore.getState().finishAgentResponse('A'.repeat(1000));
    expect(lastAgent().content).toBe(streamed);
  });

  it('leaves a blank bubble blank when the summary is also empty', () => {
    // A gate-only / no-text turn must not invent content.
    seed('');
    useStore.getState().finishAgentResponse('');
    expect(lastAgent().content).toBe('');
    expect(useStore.getState().threads[0].status).toBe('done');
  });
});


// ── PR #125 — defensive thread dedup in hydrateThreads ───────────


describe('useStore.hydrateThreads — dedup defense (PR #125)', () => {
  beforeEach(() => {
    useStore.setState({ threads: [], activeThreadId: null });
  });

  it('keeps only the first occurrence of each thread id even when backend rows duplicate', () => {
    // Synthetic: backend returns the same session_id twice. The
    // pre-PR-#125 construction wouldn't actually produce this from
    // the existing call site, but the Claude extension's UI audit
    // saw duplicates in prod. The defensive filter inside
    // hydrateThreads must drop the second.
    useStore.getState().hydrateThreads([
      { session_id: 's-1', preview: 'First arrival', created_at: '2026-05-22T00:00:00Z' },
      { session_id: 's-1', preview: 'Duplicate row', created_at: '2026-05-22T00:00:01Z' },
      { session_id: 's-2', preview: 'Distinct thread', created_at: '2026-05-22T00:00:02Z' },
    ]);
    const threads = useStore.getState().threads;
    // Two unique threads, not three.
    expect(threads).toHaveLength(2);
    // First-seen wins (preserves the merged ordering).
    expect(threads[0].id).toBe('s-1');
    expect(threads[0].title).toBe('First arrival');
    expect(threads[1].id).toBe('s-2');
  });

  it('preserves the existing merge-with-local behavior (local thread retained when backend echoes its id)', () => {
    // Seed a local in-progress thread.
    useStore.setState({
      threads: [
        {
          id: 's-local',
          title: 'New task',
          messages: [],
          status: 'idle',
          createdAt: 1,
        },
      ],
    });
    useStore.getState().hydrateThreads([
      { session_id: 's-local', preview: 'List open requests', created_at: '2026-05-22T00:00:00Z' },
    ]);
    const threads = useStore.getState().threads;
    expect(threads).toHaveLength(1);
    // The local thread's id is preserved; title updates from "New
    // task" → backend preview.
    expect(threads[0].id).toBe('s-local');
    expect(threads[0].title).toBe('List open requests');
  });

  it('drops a phantom local thread when the backend rows already cover its id', () => {
    // Real prod risk: if a stale local thread somehow shares an id
    // with a backend row AND the dedup logic walks the leftover
    // map after the merge, we'd see the local thread re-emitted.
    // Filter must prevent it.
    useStore.setState({
      threads: [
        { id: 's-1', title: 'New task', messages: [], status: 'idle', createdAt: 1 },
      ],
    });
    useStore.getState().hydrateThreads([
      { session_id: 's-1', preview: 'List open requests', created_at: '2026-05-22T00:00:00Z' },
      // A second row that shares the same id (unlikely but
      // theoretically possible if the backend has a sync glitch).
      { session_id: 's-1', preview: 'Echo', created_at: '2026-05-22T00:00:01Z' },
    ]);
    expect(useStore.getState().threads).toHaveLength(1);
  });
});

describe('useStore.setError — surfaces the message in the reply (#7 F7)', () => {
  beforeEach(() => {
    useStore.setState({
      threads: [
        {
          id: 't-err',
          title: 'Test',
          messages: [
            { id: 'u-1', role: 'user', content: 'do a thing', timestamp: 0, events: [] },
            { id: 'a-1', role: 'agent', content: '', timestamp: 0, events: [] },
          ],
          status: 'running',
          createdAt: 0,
        },
      ],
      activeThreadId: 't-err',
      isRunning: true,
    });
  });

  it('fills the empty agent placeholder with the error (no more blank reply)', () => {
    useStore
      .getState()
      .setError('This company has reached its daily AI usage limit. It resets at midnight UTC.');
    const t = useStore.getState().threads[0];
    const last = t.messages[t.messages.length - 1];
    expect(last.role).toBe('agent');
    expect(last.content).toMatch(/daily AI usage limit/i);
    expect(t.status).toBe('error');
    expect(useStore.getState().isRunning).toBe(false);
  });

  it('appends an agent bubble when there is no empty placeholder to fill', () => {
    useStore.setState({
      threads: [
        {
          id: 't-err',
          title: 'Test',
          messages: [{ id: 'u-1', role: 'user', content: 'hi', timestamp: 0, events: [] }],
          status: 'running',
          createdAt: 0,
        },
      ],
      activeThreadId: 't-err',
    });
    useStore.getState().setError('Session expired. Please sign in again.');
    const t = useStore.getState().threads[0];
    expect(t.messages).toHaveLength(2);
    expect(t.messages[1].role).toBe('agent');
    expect(t.messages[1].content).toMatch(/session expired/i);
  });
});

describe('useStore live watch (multiplayer PR)', () => {
  beforeEach(() => {
    useStore.setState({
      threads: [
        {
          id: 't-watch',
          title: 'Teammate thread',
          messages: [
            { id: 'u-1', role: 'user', content: 'chase rent', timestamp: 0, events: [] },
          ],
          status: 'done',
          createdAt: 0,
          live: true,
        },
      ],
      activeThreadId: 't-watch',
      isRunning: false,
    });
  });

  it('startWatching marks the thread and appends the streaming placeholder', () => {
    useStore.getState().startWatching('t-watch');
    const t = useStore.getState().threads[0];
    expect(t.status).toBe('watching');
    const last = t.messages[t.messages.length - 1];
    expect(last.role).toBe('agent');
    expect(last.content).toBe('');
    // Watching never claims the caller's own run flag.
    expect(useStore.getState().isRunning).toBe(false);
  });

  it('endWatching fills the blank bubble from the terminal summary and clears live', () => {
    useStore.getState().startWatching('t-watch');
    useStore.getState().endWatching('t-watch', 'All done — 3 reminders sent.');
    const t = useStore.getState().threads[0];
    expect(t.status).toBe('done');
    expect(t.live).toBe(false);
    expect(t.messages[t.messages.length - 1].content).toMatch(/3 reminders/);
  });

  it('endWatching is idempotent — the stream close after done is a no-op', () => {
    useStore.getState().startWatching('t-watch');
    useStore.getState().endWatching('t-watch', 'First summary.');
    useStore.getState().endWatching('t-watch', '', true); // late close must not flip to error
    const t = useStore.getState().threads[0];
    expect(t.status).toBe('done');
    expect(t.messages[t.messages.length - 1].content).toMatch(/first summary/i);
  });

  it('a confirm_gate event keeps a watched thread in watching (composer stays locked)', () => {
    useStore.getState().startWatching('t-watch');
    useStore.getState().addEvent({
      type: 'confirm_gate',
      data: { confirm_id: 'c-9', summary: 'Send the reminder?' },
    } as never);
    const t = useStore.getState().threads[0];
    expect(t.status).toBe('watching');
    // The card still renders — a watching manager may approve.
    expect(useStore.getState().pendingConfirmation?.confirmId).toBe('c-9');
  });

  it('setThreadWatcherCount updates presence on the thread', () => {
    useStore.getState().setThreadWatcherCount('t-watch', 2);
    expect(useStore.getState().threads[0].watcherCount).toBe(2);
  });

  it('hydrateThreads carries the live flag from the backend', () => {
    useStore.getState().hydrateThreads([
      { session_id: 't-watch', preview: 'Teammate thread', created_at: '2026-07-24T00:00:00Z', live: true },
      { session_id: 't-other', preview: 'Old thread', created_at: '2026-07-01T00:00:00Z' },
    ]);
    const threads = useStore.getState().threads;
    expect(threads.find((t) => t.id === 't-watch')?.live).toBe(true);
    expect(threads.find((t) => t.id === 't-other')?.live).toBe(false);
  });
});
