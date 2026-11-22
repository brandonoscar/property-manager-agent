/**
 * A refused send puts the words back in the box.
 *
 * ⚠ THE HALF THE HOOK TESTS STRUCTURALLY CANNOT SEE. `useSubmitChatTask`
 * throws `TaskNotSent` on every refusal it does not queue, and
 * `useSubmitChatTask.test.tsx` pins that thoroughly — but the throw buys
 * nothing unless the COMPOSER catches it, and `InputBar` clears the input
 * BEFORE awaiting the send. Delete the `catch (err) { if (err instanceof
 * TaskNotSent) setInput(task) }` block and every hook test stays green while
 * the user's typed message disappears with a toast as the only trace. That is
 * gotchas 62/66/69/103/121's shape: a well-tested helper whose one production
 * caller is invisible to its tests.
 *
 * The refusal driven here is the workspace-less one, because it is the likely
 * one — `companyId` resolves asynchronously on boot (App.tsx awaits
 * `ensureCompanyProvisioned()` before `setCompanies`), and one failed request
 * on a bad connection leaves it null for the whole session.
 *
 * ⚠ It goes through the REAL hook, deliberately. Mocking `useSubmitChatTask`
 * to throw would assert that the component handles a rejection somebody
 * invented, not that the two halves shipped here agree about which refusals
 * throw (gotcha 28's both-sides rule).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: toastError, success: vi.fn() },
}));
vi.mock('../services/agent', () => ({ runAgent: vi.fn(), cancelAgent: vi.fn() }));
vi.mock('../services/auth', () => ({ getAccessToken: vi.fn(async () => 'tok') }));

import { runAgent } from '../services/agent';
import { useStore } from '../stores/store';
import { InputBar } from './InputBar';

const runAgentMock = runAgent as unknown as ReturnType<typeof vi.fn>;

const composer = () => screen.getByTestId('chat-composer-input') as HTMLInputElement;

const TYPED = 'is the Colfax lease up for renewal?';

beforeEach(() => {
  toastError.mockReset();
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

describe('a refused send', () => {
  it('restores the typed message instead of swallowing it', async () => {
    useStore.setState({ companyId: null });
    render(<InputBar />);

    fireEvent.change(composer(), { target: { value: TYPED } });
    fireEvent.keyDown(composer(), { key: 'Enter' });

    // The refusal is real: nothing was dispatched.
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(runAgentMock).not.toHaveBeenCalled();

    // …and the words are back where the user left them, character for
    // character. Anything less and they have to retype a sentence the
    // product never told them it had dropped.
    await waitFor(() => expect(composer().value).toBe(TYPED));
  });

  it('still clears the box on a send that actually goes', async () => {
    // ⚠ The other direction (gotchas 38/51). A composer that restored on
    // EVERY path would leave the sent message sitting in the box, so the
    // next Enter sends it twice — a duplicate the user cannot take back,
    // which is the failure InputBar's own staged-file comment is about.
    render(<InputBar />);

    fireEvent.change(composer(), { target: { value: TYPED } });
    fireEvent.keyDown(composer(), { key: 'Enter' });

    await waitFor(() => expect(runAgentMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(composer().value).toBe(''));
  });
});
