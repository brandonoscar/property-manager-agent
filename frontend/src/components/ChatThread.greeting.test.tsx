/**
 * The empty chat opens on a greeting (founder, 2026-09-30), using a first
 * name only when the account carries one. The strings and the name rule are
 * pinned in lib/chatGreeting.test.ts; this pins that the screen renders them.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { User } from '@supabase/supabase-js';

vi.mock('./GettingStartedCard', () => ({ GettingStartedCard: () => null }));
vi.mock('./SyncProgressBanner', () => ({ SyncProgressBanner: () => null }));
vi.mock('../services/agent', () => ({ runAgent: vi.fn(), cancelAgent: vi.fn() }));
vi.mock('../services/auth', () => ({ getAccessToken: vi.fn(async () => 'tok') }));

import { useStore } from '../stores/store';
import { ChatThread } from './ChatThread';
import { greetings } from '../lib/chatGreeting';

const asUser = (metadata: Record<string, unknown>) =>
  ({ id: 'u1', email: 'team@acme.com', user_metadata: metadata }) as unknown as User;

afterEach(() => {
  vi.restoreAllMocks();
  useStore.setState({ user: null, activeThreadId: null, threads: [] });
});

describe('empty chat greeting', () => {
  it('greets by first name when the account has one', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.1); // index 1: "Hello, …"
    useStore.setState({ user: asUser({ full_name: 'Dana Whitfield' }), activeThreadId: null, threads: [] });
    render(<ChatThread />);
    expect(screen.getByTestId('chat-greeting')).toHaveTextContent('Hello, Dana');
  });

  it('uses the nameless form otherwise, never the email', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.1);
    useStore.setState({ user: asUser({ tos_version: '1' }), activeThreadId: null, threads: [] });
    render(<ChatThread />);
    const text = screen.getByTestId('chat-greeting').textContent ?? '';
    expect(text).toBe('Hello');
    expect(text).not.toMatch(/team/i);
  });

  it('draws one of the fifteen', () => {
    useStore.setState({ user: null, activeThreadId: null, threads: [] });
    render(<ChatThread />);
    const text = screen.getByTestId('chat-greeting').textContent;
    const hour = new Date().getHours();
    expect(greetings(hour).map((g) => g.plain)).toContain(text);
  });
});
