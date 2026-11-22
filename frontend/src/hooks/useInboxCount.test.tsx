/**
 * The Inbox badge — the count, and the two ways a count lies.
 *
 * The Inbox is the notification surface, which is why there is no bell. What
 * was missing is that it only spoke to people standing on it: the page says
 * "4 new items need your attention" and the rail said nothing.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

const listInboxRows = vi.hoisted(() => vi.fn());
vi.mock('../services/inbox', () => ({ listInboxRows }));
vi.mock('../services/auth', () => ({ demoMode: false }));
vi.mock('../services/demoData', () => ({ demoInboxRows: () => [] }));

import { useStore } from '../stores/store';
import { INBOX_COUNT_POLL_MS, useInboxCount } from './useInboxCount';

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ inboxCount: null });
  listInboxRows.mockResolvedValue([{ id: '1' }, { id: '2' }, { id: '3' }]);
});

describe('useInboxCount', () => {
  it('publishes what is waiting', async () => {
    renderHook(() => useInboxCount(true));
    await waitFor(() => expect(useStore.getState().inboxCount).toBe(3));
  });

  it('asks for the UNFILTERED list', async () => {
    // `listInboxRows(true)` is the "mine" view. A badge fed by it would
    // under-report for anybody who had ever clicked that rail chip — a wrong
    // number indistinguishable from a right one.
    renderHook(() => useInboxCount(true));
    await waitFor(() => expect(listInboxRows).toHaveBeenCalled());
    expect(listInboxRows).toHaveBeenCalledWith(false);
  });

  it('keeps the last known count when a fetch fails', async () => {
    // ⚠ NOT zero. Zero is a claim — "your inbox is clear" — and a network
    // blip has not earned it. Telling somebody their inbox emptied itself is
    // worse than telling them nothing.
    //
    // ⚠ Needs FAKE TIMERS, and the first version of this test did not have
    // them: it swapped in a rejecting mock and awaited a microtask, but the
    // next poll is 90 seconds away, so the failure path never ran and the
    // assertion passed on the count the FIRST (successful) fetch had already
    // stored. A mutation that reported 0 on failure survived it. The test was
    // blind, not the code.
    vi.useFakeTimers();
    try {
      renderHook(() => useInboxCount(true));
      await vi.waitFor(() => expect(useStore.getState().inboxCount).toBe(3));
      listInboxRows.mockRejectedValue(new Error('offline'));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INBOX_COUNT_POLL_MS + 100);
      });
      expect(listInboxRows).toHaveBeenCalledTimes(2);
      expect(useStore.getState().inboxCount).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears when signed out rather than leaving the last person\'s number', async () => {
    const { rerender } = renderHook(({ on }) => useInboxCount(on), {
      initialProps: { on: true },
    });
    await waitFor(() => expect(useStore.getState().inboxCount).toBe(3));
    rerender({ on: false });
    expect(useStore.getState().inboxCount).toBeNull();
    expect(listInboxRows).toHaveBeenCalledTimes(1);
  });

  it('does not fetch at all while disabled', () => {
    renderHook(() => useInboxCount(false));
    expect(listInboxRows).not.toHaveBeenCalled();
  });
});
