/**
 * Typing while the agent works.
 *
 * The composer was ``disabled`` mid-run, so a thought arriving during a
 * three-minute turn had nowhere to go — you waited, or you stopped the turn.
 * Now it queues and sends as a follow-up when the turn ends.
 *
 * ⚠ TWO PROPERTIES, and the second is the one that makes this honest:
 *   - the words are never lost — queued, visible, and cancellable
 *   - the queue is NOT injected into the running turn, and every surface
 *     says so. The run is already dispatched; there is no channel into it.
 *     A user who adds context, believes the agent read it, and gets an answer
 *     that ignored it has been misled by the UI rather than helped.
 *
 * ⚠ A store test cannot see this. ``queueMessage`` / ``takeQueuedMessages``
 * are correct in isolation and were, throughout — what had to be pinned is
 * that the COMPONENT drains them, once, into the right thread (gotcha 62:
 * pair every behavioural test of a helper with a guard on its call site).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

const { toastInfo } = vi.hoisted(() => ({ toastInfo: vi.fn() }));
vi.mock('sonner', () => ({
  toast: { info: toastInfo, error: vi.fn(), success: vi.fn() },
}));
vi.mock('../services/agent', () => ({ runAgent: vi.fn(), cancelAgent: vi.fn() }));
vi.mock('../services/auth', () => ({ getAccessToken: vi.fn(async () => 'tok') }));

import { runAgent } from '../services/agent';
import { useStore } from '../stores/store';
import { InputBar } from './InputBar';

const runAgentMock = runAgent as unknown as ReturnType<typeof vi.fn>;

// ⚠ The exact input, not a regex over the composer container. A loose
// matcher can resolve to the wrapper div, whose `.disabled` is `undefined`,
// and the disabled-state assertions then measure nothing.
const composer = () => screen.getByTestId('chat-composer-input') as HTMLInputElement;

function type(text: string) {
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.keyDown(composer(), { key: 'Enter' });
}

beforeEach(() => {
  toastInfo.mockReset();
  runAgentMock.mockReset();
  runAgentMock.mockResolvedValue(new AbortController());
  useStore.setState({
    isRunning: false,
    companyId: 'c1',
    activeThreadId: null,
    threads: [],
    queuedMessages: [],
    composerDraft: '',
  });
});
afterEach(() => {
  useStore.setState({ isRunning: false, activeThreadId: null, threads: [], queuedMessages: [] });
});

describe('typing while a turn runs', () => {
  it('accepts the keystroke instead of refusing it', () => {
    // The old composer was `disabled`, which is the defect: there was
    // nowhere for a mid-turn thought to go at all.
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    render(<InputBar />);
    expect(composer().disabled).toBe(false);
  });

  it('queues the message, shows it, and does not send it into the running turn', async () => {
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    render(<InputBar />);

    type('also check the vendor');

    await waitFor(() =>
      expect(useStore.getState().queuedMessages.map((q) => q.text)).toEqual([
        'also check the vendor',
      ]),
    );
    // ⚠ NOT injected. Nothing was dispatched.
    expect(runAgentMock).not.toHaveBeenCalled();
    // ⚠ And it is VISIBLE. A message the user typed and can no longer see is
    // indistinguishable from one we dropped.
    expect(screen.getByTestId('queued-message')).toHaveTextContent('also check the vendor');
  });

  it('says the queue sends LATER, never that the agent is reading it', async () => {
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    render(<InputBar />);
    type('one more thing');

    await waitFor(() => expect(screen.getByTestId('queued-messages')).toBeTruthy());
    const strip = screen.getByTestId('queued-messages').textContent ?? '';
    expect(strip.toLowerCase()).toMatch(/follow-up|finishes/);
    // The failure this wording exists to prevent.
    expect(strip.toLowerCase()).not.toMatch(/reading|added to (the )?(current|this) (turn|run)/);
  });

  it('sends the queue as ONE follow-up when the turn ends', async () => {
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    render(<InputBar />);

    type('also check the vendor');
    type('and the invoice');
    await waitFor(() => expect(useStore.getState().queuedMessages).toHaveLength(2));

    // The turn finishes.
    await act(async () => {
      useStore.setState({ isRunning: false });
    });

    // ONE turn, not two: the user was adding context to a single thought, and
    // N separate runs would each pay the full model cost for a fragment.
    await waitFor(() => expect(runAgentMock).toHaveBeenCalledTimes(1));
    expect(runAgentMock.mock.calls[0][0].task).toBe('also check the vendor\nand the invoice');
    expect(useStore.getState().queuedMessages).toEqual([]);
  });

  it('drains exactly once — the effect re-runs and must not double-send', async () => {
    // ⚠ ``submitTask`` is async and flips ``isRunning`` on a later tick. An
    // effect that re-ran while the queue still held its items would send them
    // twice; the drain has to TAKE before awaiting (gotcha 25 in React).
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    const { rerender } = render(<InputBar />);
    type('go');
    await waitFor(() => expect(useStore.getState().queuedMessages).toHaveLength(1));

    await act(async () => {
      useStore.setState({ isRunning: false });
    });
    rerender(<InputBar />);
    rerender(<InputBar />);

    await waitFor(() => expect(runAgentMock).toHaveBeenCalledTimes(1));
    expect(runAgentMock).toHaveBeenCalledTimes(1);
  });

  it('never files a follow-up in the thread it was not written for', async () => {
    // ⚠ ``isRunning`` is GLOBAL, so the user can queue on thread A and switch
    // to B before the turn ends. Draining into whatever is active would put
    // their words in the wrong conversation — quietly, and permanently.
    const a = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: a });
    render(<InputBar />);
    type('about thread A');
    await waitFor(() => expect(useStore.getState().queuedMessages).toHaveLength(1));

    const b = useStore.getState().createThread();
    await act(async () => {
      useStore.setState({ isRunning: false, activeThreadId: b });
    });

    // Nothing sent into B, and A's words are still held rather than dropped.
    expect(runAgentMock).not.toHaveBeenCalled();
    expect(useStore.getState().queuedMessages.map((q) => q.text)).toEqual(['about thread A']);
    // ...and B's composer does not show someone else's queued text.
    expect(screen.queryByTestId('queued-message')).toBeNull();
  });

  it('lets the user take a queued message back', async () => {
    const threadId = useStore.getState().createThread();
    useStore.setState({ isRunning: true, activeThreadId: threadId });
    render(<InputBar />);
    type('never mind this');
    await waitFor(() => expect(screen.getByTestId('queued-message')).toBeTruthy());

    fireEvent.click(screen.getByLabelText('Remove queued message'));

    await waitFor(() => expect(useStore.getState().queuedMessages).toEqual([]));
    await act(async () => {
      useStore.setState({ isRunning: false });
    });
    expect(runAgentMock).not.toHaveBeenCalled();
  });
});
