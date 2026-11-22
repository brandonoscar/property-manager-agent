/**
 * The "this device's sign-in is no longer accepted" strip, the one click that
 * fixes it, and the fact that something renders it.
 *
 * ⚠ The mount check is not padding. A perfectly correct banner that no page
 * renders is this repo's recurring failure — complete, unit-tested, and
 * wired to nothing — and a behavioural test of the component structurally
 * cannot see it.
 */

import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls: string[] = [];

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), message: vi.fn() },
}));

vi.mock('../services/auth', async (importActual) => ({
  ...(await importActual<typeof import('../services/auth')>()),
  performSignOut: vi.fn(async () => {
    calls.push('performSignOut');
  }),
}));

vi.mock('../services/sessionEnd', async (importActual) => ({
  ...(await importActual<typeof import('../services/sessionEnd')>()),
  rememberReturnView: vi.fn((view: string) => {
    calls.push(`rememberReturnView:${view}`);
  }),
}));

// Vite's ?raw gives the file's text without needing node types in this
// tsconfig — the mechanism ServiceBanner.test.tsx already uses for App.tsx.
import appSource from '../App.tsx?raw';
import { SESSION_REJECTED_MESSAGE, SessionBanner } from './SessionBanner';
import {
  CONSECUTIVE_REJECTIONS,
  reportCredentialAccepted,
  reportCredentialRejected,
  resetSessionRejected,
} from '../services/sessionRejected';
import { performSignOut } from '../services/auth';
import { useStore } from '../stores/store';

function reject(): void {
  act(() => {
    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();
  });
}

beforeEach(() => {
  calls.length = 0;
  resetSessionRejected();
  vi.mocked(performSignOut).mockClear();
});

describe('when the credential is still accepted', () => {
  it('renders nothing at all', () => {
    render(<SessionBanner />);
    expect(screen.queryByTestId('session-banner')).toBeNull();
  });

  it('renders nothing below the bar', () => {
    render(<SessionBanner />);
    act(() => {
      for (let i = 0; i < CONSECUTIVE_REJECTIONS - 1; i += 1) reportCredentialRejected();
    });
    expect(screen.queryByTestId('session-banner')).toBeNull();
  });
});

describe('when it is not', () => {
  it('appears for a rejection raised after mount', () => {
    render(<SessionBanner />);
    reject();
    expect(screen.getByTestId('session-banner')).toBeInTheDocument();
  });

  it('appears for a rejection that began BEFORE mount', () => {
    // ⚠ The initial read, not the subscription. Listeners are told about
    // CHANGES only, so a banner that trusted the bus alone would stay silent
    // through a rejection raised while the user was on another screen — and
    // every request had already failed by then.
    reject();
    render(<SessionBanner />);
    expect(screen.getByTestId('session-banner')).toBeInTheDocument();
  });

  it('goes away when a request is accepted again', () => {
    render(<SessionBanner />);
    reject();
    expect(screen.queryByTestId('session-banner')).not.toBeNull();

    act(() => reportCredentialAccepted());

    expect(screen.queryByTestId('session-banner')).toBeNull();
  });

  it('does not sign anybody out by itself', () => {
    // The message promises what they typed is still on screen. Clearing the
    // session on the banner's own initiative would break that promise on
    // evidence that could be two unlucky requests.
    render(<SessionBanner />);
    reject();
    expect(performSignOut).not.toHaveBeenCalled();
  });
});

describe('the button', () => {
  it('remembers where they were BEFORE signing them out', async () => {
    // ⚠ ORDER is the property, and a presence check cannot see it.
    // `performSignOut` marks the sign-out intentional, which is what
    // suppresses the involuntary path that would otherwise have remembered
    // the view — so a `rememberReturnView` call placed after it runs against
    // a store that has already been cleared.
    useStore.getState().setView('properties');
    render(<SessionBanner />);
    reject();

    await userEvent.click(screen.getByRole('button', { name: /sign in again/i }));

    expect(calls).toEqual(['rememberReturnView:properties', 'performSignOut']);
  });

  it('signs out even when the view cannot be remembered', async () => {
    // sessionStorage throws in a private window. Losing the return view is a
    // smaller failure than a button that does nothing.
    const sessionEnd = await import('../services/sessionEnd');
    vi.mocked(sessionEnd.rememberReturnView).mockImplementationOnce(() => {
      throw new Error('blocked site data');
    });
    render(<SessionBanner />);
    reject();

    await userEvent.click(screen.getByRole('button', { name: /sign in again/i }));

    expect(performSignOut).toHaveBeenCalled();
  });
});

describe('the sentence', () => {
  it('claims no cause it cannot know', () => {
    // The client cannot tell a revoked session from an expired one from a
    // signature it could not verify. Guessing would be right often and wrong
    // sometimes, on a strip whose whole job is to be believed.
    const text = SESSION_REJECTED_MESSAGE.toLowerCase();
    expect(text).not.toMatch(/expired/);
    expect(text).not.toMatch(/another device/);
    expect(text).not.toMatch(/revoked/);
  });

  it('promises the return this component actually keeps', () => {
    expect(SESSION_REJECTED_MESSAGE.toLowerCase()).toMatch(/back to this screen/);
  });
});

describe('the wiring', () => {
  it('App renders it', () => {
    expect(appSource).toMatch(/<SessionBanner\s*\/>/);
  });
});
