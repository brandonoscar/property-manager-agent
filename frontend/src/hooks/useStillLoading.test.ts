/**
 * PR #117 Phase D — useStillLoading time-bounded skeleton hook.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { useStillLoading } from './useStillLoading';


describe('useStillLoading', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns false immediately when loading just started', () => {
    const { result } = renderHook(({ l }) => useStillLoading(l, 2000), {
      initialProps: { l: true },
    });
    expect(result.current).toBe(false);
  });

  it('flips to true after the threshold while loading stays truthy', () => {
    const { result } = renderHook(({ l }) => useStillLoading(l, 2000), {
      initialProps: { l: true },
    });
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current).toBe(true);
  });

  it('resets to false when loading flips to falsy', () => {
    const { result, rerender } = renderHook(
      ({ l }) => useStillLoading(l, 2000),
      { initialProps: { l: true } },
    );
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current).toBe(true);

    rerender({ l: false });
    expect(result.current).toBe(false);
  });

  it('returns false when loading is initially false', () => {
    const { result } = renderHook(({ l }) => useStillLoading(l, 2000), {
      initialProps: { l: false },
    });
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(false);
  });

  it('respects a custom threshold', () => {
    const { result } = renderHook(({ l }) => useStillLoading(l, 500), {
      initialProps: { l: true },
    });
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(result.current).toBe(false);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(true);
  });
});
