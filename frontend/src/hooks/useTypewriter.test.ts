/**
 * The answer types itself out instead of arriving in slabs.
 *
 * ⚠ Why this lives in the frontend at all: `StreamScrubber` cannot publish per
 * token. It only cuts where an identifier cannot straddle the seam, which is a
 * safety property — two cleverer designs shipped exactly that corruption. Its
 * docstring names this layer as the place for the typing feel.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { useTypewriter } from './useTypewriter';

/**
 * ⚠ Every test STATES the motion preference it means, and none of them inherit
 * the environment's. jsdom implements `matchMedia` and answers `matches: true`
 * for `(prefers-reduced-motion: reduce)` — measured, not assumed — so a test
 * that leaves it alone runs the reduced-motion branch while reading as a test
 * of the animation, and reports "reveals everything at once" for a hook that
 * is behaving correctly. That is what these six did before this helper existed.
 */
function setReducedMotion(reduce: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduce,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

describe('useTypewriter', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setReducedMotion(false);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('reveals a chunk progressively rather than all at once', () => {
    const text = 'a'.repeat(200);
    const { result } = renderHook(() => useTypewriter(text, true));
    expect(result.current.length).toBeLessThan(text.length);
    act(() => void vi.advanceTimersByTime(32));
    const early = result.current.length;
    expect(early).toBeGreaterThan(0);
    expect(early).toBeLessThan(text.length);
    act(() => void vi.advanceTimersByTime(2000));
    expect(result.current).toBe(text);
  });

  it('always finishes — a backlog can never strand text unrevealed', () => {
    // ⚠ The failure that matters. A fixed characters-per-tick that is too slow
    // leaves the tail of a long answer permanently behind the stream; draining
    // a FRACTION of the backlog self-corrects.
    const text = 'x'.repeat(5000);
    const { result } = renderHook(() => useTypewriter(text, true));
    act(() => void vi.advanceTimersByTime(5000));
    expect(result.current).toBe(text);
  });

  it('shows a settled turn in full, immediately', () => {
    // Re-reading history must not replay an animation.
    const text = 'The answer, already finished.';
    const { result } = renderHook(() => useTypewriter(text, false));
    expect(result.current).toBe(text);
  });

  it('finishes instantly the moment streaming stops', () => {
    const text = 'y'.repeat(400);
    const { result, rerender } = renderHook(
      ({ streaming }: { streaming: boolean }) => useTypewriter(text, streaming),
      { initialProps: { streaming: true } },
    );
    act(() => void vi.advanceTimersByTime(16));
    expect(result.current.length).toBeLessThan(text.length);
    rerender({ streaming: false });
    expect(result.current).toBe(text);
  });

  it('restarts cleanly when a shorter turn reuses the hook', () => {
    // ⚠ Without this the new turn shows NOTHING until its text overtakes the
    // old length — a blank bubble that looks like a hang.
    const { result, rerender } = renderHook(
      ({ text }: { text: string }) => useTypewriter(text, true),
      { initialProps: { text: 'a'.repeat(300) } },
    );
    act(() => void vi.advanceTimersByTime(3000));
    expect(result.current.length).toBe(300);
    rerender({ text: 'short' });
    expect(result.current).toBe('short');

    // ⚠ The half that actually needs the reset, and the reason asserting only
    // the line above proves nothing: `slice` clamps on its own, so the SHORT
    // turn reads correctly either way. The defect shows on the NEXT delta — a
    // revealed counter still parked at 300 dumps the whole of a 120-character
    // continuation the instant it arrives, and the new turn never animates.
    rerender({ text: 'short'.padEnd(120, '!') });
    expect(result.current.length).toBeLessThan(120);
  });

  it('honours prefers-reduced-motion by showing everything at once', () => {
    // The other direction of the same switch. Without this test the branch that
    // jsdom was accidentally running would have no coverage at all once it is
    // stubbed off — and it is a real accessibility setting, not a test artefact.
    setReducedMotion(true);
    const text = 'z'.repeat(400);
    const { result } = renderHook(() => useTypewriter(text, true));
    expect(result.current).toBe(text);
  });

  it('never shows text the source does not have', () => {
    // Slices the live content rather than keeping a copy, so a corrected turn
    // cannot leave a stale answer on screen.
    const { result, rerender } = renderHook(
      ({ text }: { text: string }) => useTypewriter(text, true),
      { initialProps: { text: 'The balance is $4,500 and rising fast' } },
    );
    act(() => void vi.advanceTimersByTime(3000));
    rerender({ text: 'The balance is $4,500 and rising fast today' });
    expect('The balance is $4,500 and rising fast today').toContain(result.current);
  });
});
