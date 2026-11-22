/**
 * A typed message must never vanish.
 *
 * Live probe, 2026-08-26: `send_dropped: true` on EVERY run — the first
 * message after clicking "New thread" was swallowed, and the probe only got
 * through because it retries. A real user types, presses Enter, and their
 * words disappear with no error at all.
 *
 * The cause was a stale closure. Zustand's `set` is synchronous but React's
 * re-render is not, so between a state change and the commit that rebuilds
 * this callback there is a window where the handler React already holds closes
 * over the PREVIOUS values. A keystroke landing in that window ran a stale
 * copy, whose `if (!task || isRunning) return` bailed silently — after the
 * composer had already cleared the box.
 *
 * Two properties, and the second is what makes the first observable:
 *   - volatile state is read at CALL time, not captured at render time
 *   - a refusal is never silent
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

// ⚠ vi.mock is hoisted above every top-level binding, so the spy has to be
// created inside vi.hoisted or the factory references a dead temporal zone.
const { toastInfo, toastError } = vi.hoisted(() => ({ toastInfo: vi.fn(), toastError: vi.fn() }));
vi.mock('sonner', () => ({
  toast: { info: toastInfo, error: toastError, success: vi.fn() },
}));
vi.mock('../services/agent', () => ({ runAgent: vi.fn() }));
vi.mock('../services/auth', () => ({ getAccessToken: vi.fn(async () => 'tok') }));

import { runAgent } from '../services/agent';
import { getAccessToken } from '../services/auth';
import { SUPPORT_EMAIL } from '../lib/support';
import { useStore } from '../stores/store';
import { TaskNotSent, useSubmitChatTask } from './useSubmitChatTask';

const runAgentMock = runAgent as unknown as ReturnType<typeof vi.fn>;
const tokenMock = getAccessToken as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  toastInfo.mockReset();
  toastError.mockReset();
  tokenMock.mockReset();
  tokenMock.mockResolvedValue('tok');
  runAgentMock.mockReset();
  runAgentMock.mockResolvedValue(new AbortController());
  useStore.setState({
    isRunning: false,
    companyId: 'c1',
    activeThreadId: null,
    threads: [],
    queuedMessages: [],
  });
});
afterEach(() => {
  useStore.setState({ isRunning: false, activeThreadId: null, threads: [], queuedMessages: [] });
});

describe('useSubmitChatTask', () => {
  it('reads isRunning at CALL time, not from the render closure', async () => {
    const { result } = renderHook(() => useSubmitChatTask());

    // The store changes AFTER the hook rendered — exactly the window the bug
    // lived in. The callback React is holding was built when isRunning was
    // false; it must not act on that stale value.
    useStore.setState({ isRunning: true });

    await result.current('hello');
    // Not sent into the running turn...
    expect(runAgentMock).not.toHaveBeenCalled();
    // ...and not lost either. The words are QUEUED, which is the whole
    // change: a thought arriving during a three-minute turn used to have
    // nowhere to go.
    expect(useStore.getState().queuedMessages.map((q) => q.text)).toEqual(['hello']);
  });

  it('queues rather than injecting, and says so', async () => {
    const { result } = renderHook(() => useSubmitChatTask());
    useStore.setState({ isRunning: true });

    await result.current('also check the vendor');

    // ⚠ The message must say FOLLOW-UP. The run is already dispatched and
    // cannot see this text; a user who believes it landed, and gets an
    // answer that ignored it, has been misled by the UI rather than helped.
    expect(toastInfo).toHaveBeenCalled();
    const calls = toastInfo.mock.calls;
    const said = String(calls[calls.length - 1]?.[0] ?? '').toLowerCase();
    expect(said).toContain('queued');
    expect(said).toMatch(/follow-up|finish/);
  });

  it('keeps the words when a TEAMMATE owns the thread — refused, not queued', async () => {
    // ⚠ A watched thread is driven by someone else's run. Queuing would
    // silently attach your words to the end of their turn, so this branch
    // refuses — but it must THROW so the composer restores the text. It
    // used to `return` after the box had already cleared, which is exactly
    // the message-loss defect the isRunning branch above was fixed for.
    const { result } = renderHook(() => useSubmitChatTask());
    const threadId = useStore.getState().createThread();
    useStore.setState((st) => ({
      threads: st.threads.map((t) =>
        t.id === threadId ? { ...t, status: 'watching' as const } : t,
      ),
    }));

    await expect(result.current('mine')).rejects.toBeInstanceOf(TaskNotSent);
    expect(runAgentMock).not.toHaveBeenCalled();
    expect(useStore.getState().queuedMessages).toEqual([]);
  });

  it('still sends normally when the agent is idle', async () => {
    const { result } = renderHook(() => useSubmitChatTask());
    await result.current('what is the rent');

    expect(runAgentMock).toHaveBeenCalledTimes(1);
    expect(runAgentMock.mock.calls[0][0].task).toBe('what is the rent');
  });

  it('creates a thread when there is none, and sends into THAT thread', async () => {
    // The "no active thread" state: the composer renders, accepts text, and
    // Enter used to clear it into nothing.
    const { result } = renderHook(() => useSubmitChatTask());
    await result.current('first message ever');

    const sessionId = runAgentMock.mock.calls[0][0].sessionId;
    expect(sessionId).toBeTruthy();
    expect(useStore.getState().activeThreadId).toBe(sessionId);
    // And the user's words are actually in the thread, not lost.
    const thread = useStore.getState().threads.find((t) => t.id === sessionId);
    expect(thread?.messages.some((m) => m.content === 'first message ever')).toBe(true);
  });

  it('sends into a thread created after the hook rendered', async () => {
    const { result } = renderHook(() => useSubmitChatTask());

    // Clicking "New thread" mutates the store synchronously; React has not
    // committed yet. The stale closure would have used the OLD id (or null).
    const fresh = useStore.getState().createThread();
    await result.current('into the new thread');

    expect(runAgentMock.mock.calls[0][0].sessionId).toBe(fresh);
  });

  it('keeps the words when the session has expired', async () => {
    // ⚠ This branch and the one below used to `return`, after InputBar had
    // already cleared the box — so they lost the message exactly the way the
    // `isRunning` bug did. Gotcha 44's fix reached the `watching` branch and
    // stopped one exit short of these two, in the same callback.
    const { result } = renderHook(() => useSubmitChatTask());
    tokenMock.mockResolvedValue(null);

    await expect(result.current('what is the rent')).rejects.toBeInstanceOf(TaskNotSent);
    expect(runAgentMock).not.toHaveBeenCalled();
    // Not queued either — a queued message would be sent later under a
    // session that is gone, which is a refusal wearing a success's clothes.
    expect(useStore.getState().queuedMessages).toEqual([]);
  });

  it('keeps the words when no workspace resolved', async () => {
    const { result } = renderHook(() => useSubmitChatTask());
    useStore.setState({ companyId: null });

    await expect(result.current('what is the rent')).rejects.toBeInstanceOf(TaskNotSent);
    expect(runAgentMock).not.toHaveBeenCalled();
    expect(useStore.getState().queuedMessages).toEqual([]);
  });

  it('tells a workspace-less user something they can actually do', async () => {
    // ⚠ THE PRODUCT HALF, and it is the reason this branch was worth
    // touching at all. "No company selected." names a state with no control
    // attached: there is no company picker for somebody with zero companies,
    // so the instruction could not be carried out by anyone who read it
    // (gotchas 102/106/118/133). Reload IS actionable — App.tsx re-runs
    // `ensureCompanyProvisioned()` on mount, which re-queries and bootstraps.
    const { result } = renderHook(() => useSubmitChatTask());
    useStore.setState({ companyId: null });

    await expect(result.current('what is the rent')).rejects.toBeInstanceOf(TaskNotSent);

    // ⚠ Index arithmetic, not `.at(-1)` — this tsconfig's lib predates it and
    // `tsc --noEmit` refuses it while vitest runs it happily (gotcha 72).
    const errs = toastError.mock.calls;
    const said = String(errs[errs.length - 1]?.[0] ?? '');
    expect(said.toLowerCase()).toContain('reload');
    expect(said).toContain(SUPPORT_EMAIL);
    // ⚠ Both directions (gotchas 38/51). A message that merely ADDED the
    // word "reload" to the old sentence would pass the check above while
    // still pointing at a picker that does not exist.
    expect(said.toLowerCase()).not.toContain('select');
  });

  it('an empty message is a no-op, not a refusal', async () => {
    const { result } = renderHook(() => useSubmitChatTask());
    await expect(result.current('   ')).resolves.toBeUndefined();
    expect(runAgentMock).not.toHaveBeenCalled();
    expect(toastInfo).not.toHaveBeenCalled();
  });
});
