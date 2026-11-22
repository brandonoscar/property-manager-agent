/**
 * The composer wraps and grows instead of scrolling sideways.
 *
 * A one-line `<input>` pushed the start of a long prompt off the left edge
 * (founder screenshot, 2026-09-30). It is a textarea now: Enter sends,
 * Shift+Enter adds a line, and Enter that picks an IME candidate does
 * neither.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() } }));
vi.mock('../services/agent', () => ({ runAgent: vi.fn(), cancelAgent: vi.fn() }));
vi.mock('../services/auth', () => ({ getAccessToken: vi.fn(async () => 'tok') }));

import { runAgent } from '../services/agent';
import { useStore } from '../stores/store';
import { InputBar } from './InputBar';

const runAgentMock = runAgent as unknown as ReturnType<typeof vi.fn>;
const field = () => screen.getByTestId('chat-composer-input');
// The send path awaits the auth token before it calls runAgent, so "did not
// send" must be asserted after that await could have happened, or it
// measures nothing (a mutant removing the IME guard survived exactly so).
const settle = () => new Promise((r) => setTimeout(r, 20));

beforeEach(() => {
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

describe.each([
  ['landing composer', true],
  ['thread composer', false],
])('%s', (_name, embedded) => {
  it('is a textarea, so a long prompt wraps', () => {
    render(<InputBar embedded={embedded} />);
    expect(field().tagName).toBe('TEXTAREA');
    expect(field()).toHaveAttribute('rows', '1');
  });

  it('Shift+Enter adds a line and does not send', async () => {
    render(<InputBar embedded={embedded} />);
    fireEvent.change(field(), { target: { value: 'line one' } });
    fireEvent.keyDown(field(), { key: 'Enter', shiftKey: true });
    await settle();
    expect(runAgentMock).not.toHaveBeenCalled();
    expect(useStore.getState().composerDraft).toBe('line one');
  });

  it('Enter while an IME is composing does not send', async () => {
    render(<InputBar embedded={embedded} />);
    fireEvent.change(field(), { target: { value: 'にほん' } });
    fireEvent.keyDown(field(), { key: 'Enter', isComposing: true });
    await settle();
    expect(runAgentMock).not.toHaveBeenCalled();
  });

  it('Enter sends and clears the box', async () => {
    render(<InputBar embedded={embedded} />);
    fireEvent.change(field(), { target: { value: 'Who is behind on rent?' } });
    fireEvent.keyDown(field(), { key: 'Enter' });
    await vi.waitFor(() => expect(runAgentMock).toHaveBeenCalledTimes(1));
    expect(runAgentMock.mock.calls[0][0].task).toBe('Who is behind on rent?');
    expect(useStore.getState().composerDraft).toBe('');
  });
});
