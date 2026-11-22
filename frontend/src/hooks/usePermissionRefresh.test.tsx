/**
 * A 403 tells the client its cached role is stale, and the three screens that
 * cache one listen for it.
 *
 * ⚠ **This file is HALF the guard.** It proves the primitive works. That the
 * three screens actually USE it is checked in
 * `tests/unit/test_permission_refresh_wiring.py` — a hook that is correct and
 * called by nothing is this repo's documented failure (gotcha 62), and no
 * behavioural test of a helper can see it.
 *
 * ⚠ The wiring scan lives in PYTEST, not here, following the precedent of
 * `test_roster_invite_pending.py`: reading the filesystem from a vitest file
 * needs `@types/node`, which this tsconfig does not carry — and vitest does
 * not typecheck, so it would have gone green while `tsc` stayed red
 * (gotcha 72).
 */

import { renderHook, act } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { notifyPermissionDenied } from '../services/api';
import { usePermissionRefresh } from './usePermissionRefresh';

describe('the subscription', () => {
  it('runs the loader when a permission-denied is announced', () => {
    const reload = vi.fn();
    renderHook(() => usePermissionRefresh(reload));
    expect(reload).not.toHaveBeenCalled(); // subscribing must not fetch

    act(() => {
      fireDenied();
    });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('stops listening once the screen unmounts', () => {
    const reload = vi.fn();
    const { unmount } = renderHook(() => usePermissionRefresh(reload));
    unmount();

    act(() => {
      fireDenied();
    });
    expect(reload).not.toHaveBeenCalled();
  });

  it('calls the LATEST loader, not the one captured on mount', () => {
    // ⚠ The reason `reload` is held in a ref. A caller passing an inline arrow
    // gets a new function every render; without the ref this would either
    // re-subscribe on every render or call a stale closure — the same
    // stale-capture defect gotcha 44 records in useSubmitChatTask.
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => usePermissionRefresh(fn), {
      initialProps: { fn: first },
    });
    rerender({ fn: second });

    act(() => {
      fireDenied();
    });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('one listener throwing does not stop the others', () => {
    // A 403 is being turned into the error the person will read. A listener
    // that throws here must not replace it with its own stack.
    const boom = vi.fn(() => {
      throw new Error('listener exploded');
    });
    const ok = vi.fn();
    renderHook(() => usePermissionRefresh(boom));
    renderHook(() => usePermissionRefresh(ok));

    expect(() =>
      act(() => {
        fireDenied();
      }),
    ).not.toThrow();
    expect(ok).toHaveBeenCalledTimes(1);
  });
});

/**
 * Fires the REAL signal — the same function `apiFetch` calls on a 403 — so a
 * regression that stops announcing 403s fails these tests. A hand-rolled fake
 * notifier would keep passing through exactly that break.
 */
function fireDenied(): void {
  notifyPermissionDenied();
}
