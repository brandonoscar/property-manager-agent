/**
 * The boot screen holds long enough to be seen, and never longer than it has
 * to be.
 *
 * ⚠ **The two directions fail differently and both are silent.** Too short and
 * the launch is a white flash nobody registers — the state the founder asked
 * to fix. Too eager and a warm reload that previously showed nothing gains
 * 700ms of splash, which is a latency regression wearing a polish costume. A
 * test that only asserts "it stays up" catches the first and ships the second.
 */

import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BOOT_MIN_MS, BootScreen, useMinimumVisible } from './BootScreen';

/** Renders the hook's answer as text so the assertions read as behaviour. */
function Probe({ active, minMs = BOOT_MIN_MS }: { active: boolean; minMs?: number }) {
  return <span data-testid="held">{useMinimumVisible(active, minMs) ? 'booting' : 'ready'}</span>;
}

const state = () => screen.getByTestId('held').textContent;

describe('useMinimumVisible', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('keeps the screen up after the work finishes, until the floor is reached', () => {
    const { rerender } = render(<Probe active />);
    expect(state()).toBe('booting');

    // Auth resolves almost immediately — the case that used to flash.
    act(() => {
      vi.advanceTimersByTime(50);
    });
    rerender(<Probe active={false} />);
    expect(state()).toBe('booting');

    // ⚠ Advance PART of the floor and assert it is still up. Without this the
    // test passes against a hold of zero length — the mutant that reinstates
    // the flash — because the first assertion runs before any timer fires and
    // the second runs after every timer has. Measured: `setTimeout(…, 0)`
    // survived the version of this test that skipped straight to the end.
    act(() => {
      vi.advanceTimersByTime(BOOT_MIN_MS / 2);
    });
    expect(state()).toBe('booting');

    act(() => {
      vi.advanceTimersByTime(BOOT_MIN_MS);
    });
    expect(state()).toBe('ready');
  });

  it('never pads a session check that was already slower than the floor', () => {
    const { rerender } = render(<Probe active />);
    act(() => {
      vi.advanceTimersByTime(BOOT_MIN_MS * 3);
    });
    rerender(<Probe active={false} />);
    // ⚠ Released on the SAME tick. A floor implemented as "wait minMs after
    // the work finishes" would pass every other test in this file and add
    // 700ms to exactly the launches that were already the slowest.
    expect(state()).toBe('ready');
  });

  it('does not appear at all when there was nothing to wait for', () => {
    render(<Probe active={false} />);
    expect(state()).toBe('ready');
    act(() => {
      vi.advanceTimersByTime(BOOT_MIN_MS * 2);
    });
    expect(state()).toBe('ready');
  });

  it('does not come back over a working app when auth re-checks', () => {
    // A token refresh mid-session flips `isAuthLoading` again. Putting the
    // splash back over a page somebody is reading is worse than showing
    // nothing — the latch is what stops it.
    const { rerender } = render(<Probe active />);
    act(() => {
      vi.advanceTimersByTime(BOOT_MIN_MS + 10);
    });
    rerender(<Probe active={false} />);
    expect(state()).toBe('ready');

    rerender(<Probe active />);
    // `active` still forces it — App only passes `isAuthLoading`, so this is
    // the honest answer — but the HOLD does not re-arm behind it.
    rerender(<Probe active={false} />);
    act(() => {
      vi.advanceTimersByTime(10);
    });
    expect(state()).toBe('ready');
  });
});

describe('BootScreen', () => {
  it('names the product rather than showing a bare spinner', () => {
    // The whole point of the change: a 12px circle on white says nothing about
    // whose software is starting.
    render(<BootScreen />);
    expect(screen.getByTestId('boot-screen')).toBeTruthy();
    expect(screen.getByText('occupella')).toBeTruthy();
    expect(screen.getByRole('status', { name: /loading occupella/i })).toBeTruthy();
  });
});
