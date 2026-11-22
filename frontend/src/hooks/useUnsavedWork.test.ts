/**
 * The close-the-tab warning.
 *
 * Two properties matter and they pull in opposite directions: it has to fire
 * when there is work to lose, and it has to be ABSENT otherwise — not merely
 * silent. A `beforeunload` listener disables the browser's back/forward cache
 * for the whole page, so a handler that is always attached and returns early
 * behaves identically in a test and costs every back-navigation in the app a
 * full reload in real use.
 */

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useUnsavedWork } from './useUnsavedWork';

let added: string[] = [];
let removed: string[] = [];

beforeEach(() => {
  added = [];
  removed = [];
  vi.spyOn(window, 'addEventListener').mockImplementation(((type: string) => {
    added.push(type);
  }) as typeof window.addEventListener);
  vi.spyOn(window, 'removeEventListener').mockImplementation(((type: string) => {
    removed.push(type);
  }) as typeof window.removeEventListener);
});

afterEach(() => vi.restoreAllMocks());

describe('when there is nothing to lose', () => {
  it('attaches NO listener at all', () => {
    // ⚠ Not "attaches one that does nothing". See the file note: an
    // always-attached handler kills the bfcache for every navigation.
    renderHook(() => useUnsavedWork(false));
    expect(added).not.toContain('beforeunload');
  });
});

describe('when there is work to lose', () => {
  it('attaches the listener', () => {
    renderHook(() => useUnsavedWork(true));
    expect(added).toContain('beforeunload');
  });

  it('removes it once the work is saved', () => {
    const { rerender } = renderHook(({ dirty }) => useUnsavedWork(dirty), {
      initialProps: { dirty: true },
    });
    expect(added).toContain('beforeunload');
    rerender({ dirty: false });
    expect(removed).toContain('beforeunload');
  });

  it('removes it on unmount', () => {
    renderHook(() => useUnsavedWork(true)).unmount();
    expect(removed).toContain('beforeunload');
  });
});

describe('the handler itself', () => {
  it('both cancels the event and sets returnValue', () => {
    // Neither alone is enough across browsers: preventDefault is the modern
    // spec, `returnValue` is what older WebKit and Firefox key on. Capture
    // the real handler and call it rather than asserting on the source.
    let handler: ((e: BeforeUnloadEvent) => void) | null = null;
    vi.spyOn(window, 'addEventListener').mockImplementation(((
      type: string,
      fn: EventListenerOrEventListenerObject,
    ) => {
      if (type === 'beforeunload') handler = fn as typeof handler;
    }) as typeof window.addEventListener);

    renderHook(() => useUnsavedWork(true));
    expect(handler).toBeTruthy();

    const event = { preventDefault: vi.fn(), returnValue: undefined } as unknown as
      BeforeUnloadEvent;
    handler!(event);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(event.returnValue).toBe('');
  });
});
