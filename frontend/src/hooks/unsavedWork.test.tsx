/**
 * Leaving a page with typed work on it, by every route out of it.
 *
 * `useUnsavedWork` guarded `beforeunload` — a tab close, a reload. That is one
 * way to leave and it was the only one covered. An IN-APP navigation unloads
 * no document, so clicking Chat in the rail with a half-filled carrier
 * registration on screen — legal name, EIN, address, an authorised
 * representative, none of it autosaved — threw it away in silence, while the
 * hook attached to that very form made it look handled.
 *
 * ⚠ URL sync made it WIDER on the same day it was found. Back and Forward now
 * change the view, so the browser's own buttons became a way to lose the form
 * that had not existed the day before. The popstate case is therefore not a
 * nicety here; it is the half this session added.
 *
 * ⚠ `window.confirm` on purpose, and this is the app's only use of it. A store
 * action is synchronous and a React modal is not — returning "did they agree"
 * out of `setView` is exactly what lets Back/Forward put the address bar back
 * on a refusal. A custom dialog would make all ~20 call sites await a promise,
 * which is how the nineteenth ends up unguarded.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

import { useStore } from '../stores/store';
import { useUnsavedWork } from './useUnsavedWork';

const realConfirm = window.confirm;

beforeEach(() => {
  useStore.setState({ view: 'chat', settingsSection: 'account', unsavedWork: false });
});
afterEach(() => {
  window.confirm = realConfirm;
});

/** Answer the prompt, and record that it was asked. */
function answer(yes: boolean) {
  const spy = vi.fn(() => yes);
  window.confirm = spy as unknown as typeof window.confirm;
  return spy;
}

describe('navigating away from unsaved work', () => {
  it('does not ask when there is nothing to lose', () => {
    const spy = answer(false);
    expect(useStore.getState().setView('inbox')).toBe(true);
    expect(useStore.getState().view).toBe('inbox');
    // ⚠ A prompt that fires when nothing is at stake is one people learn to
    // click through — and then it is not there for the registration either.
    expect(spy).not.toHaveBeenCalled();
  });

  it('keeps the person where they are when they say no', () => {
    useStore.setState({ unsavedWork: true });
    const spy = answer(false);
    expect(useStore.getState().setView('inbox')).toBe(false);
    expect(useStore.getState().view).toBe('chat');
    expect(spy).toHaveBeenCalled();
  });

  it('lets them go when they say yes', () => {
    useStore.setState({ unsavedWork: true });
    answer(true);
    expect(useStore.getState().setView('inbox')).toBe(true);
    expect(useStore.getState().view).toBe('inbox');
  });

  it('guards the Settings sections too, which is where the form lives', () => {
    // The carrier registration is a Settings panel, so section-to-section is
    // the navigation most likely to destroy it — and it does not go through
    // `setView` at all.
    useStore.setState({ view: 'settings', settingsSection: 'connectors', unsavedWork: true });
    answer(false);
    expect(useStore.getState().openSettings('billing')).toBe(false);
    expect(useStore.getState().settingsSection).toBe('connectors');
  });

  it('never challenges a navigation to where you already are', () => {
    // ⚠ Clicking the current page is a no-op, and prompting for it would make
    // the warning look broken. Both actions check first.
    useStore.setState({ view: 'inbox', unsavedWork: true });
    const spy = answer(false);
    expect(useStore.getState().setView('inbox')).toBe(true);
    useStore.setState({ view: 'settings', settingsSection: 'team' });
    expect(useStore.getState().openSettings('team')).toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });

  it('allows the move when there is no confirm to ask with', () => {
    // Some embedded and automated contexts have no `window.confirm`. Blocking
    // navigation forever because we could not ask is worse than the loss this
    // prevents — and it would strand somebody with no way out of the page.
    useStore.setState({ unsavedWork: true });
    (window as unknown as { confirm?: unknown }).confirm = undefined;
    expect(useStore.getState().setView('inbox')).toBe(true);
  });
});

describe('the flag itself', () => {
  it('is published while there is work, and cleared when there is not', () => {
    const { rerender } = renderHook(({ dirty }) => useUnsavedWork(dirty), {
      initialProps: { dirty: true },
    });
    expect(useStore.getState().unsavedWork).toBe(true);

    // ⚠ The clearing half is what a save depends on. Published only on `true`,
    // the flag sticks and every navigation is challenged until a reload —
    // which is why this lives in its own effect rather than after the early
    // return in the `beforeunload` one.
    rerender({ dirty: false });
    expect(useStore.getState().unsavedWork).toBe(false);
  });

  it('clears on unmount, because the form is gone', () => {
    const { unmount } = renderHook(() => useUnsavedWork(true));
    expect(useStore.getState().unsavedWork).toBe(true);
    unmount();
    expect(useStore.getState().unsavedWork).toBe(false);
  });

  it('separates "a tab close loses it" from "a view change loses it"', () => {
    // ⚠ The composer's draft lives in the STORE, so navigating away does not
    // touch it — only closing the tab does. One boolean answering both
    // questions would prompt on every click of the rail with text in the box,
    // which is exactly how a warning becomes one people click through, and
    // then it is not there for the carrier registration either (gotcha 67's
    // shape: a flag read as answering a question it was not asked).
    const { rerender } = renderHook(
      ({ lost }: { lost: boolean }) => useUnsavedWork(true, { lostOnNavigation: lost }),
      { initialProps: { lost: false } },
    );
    expect(useStore.getState().unsavedWork).toBe(false);

    // The staged FILE is local state and really is lost, so it flips back on.
    rerender({ lost: true });
    expect(useStore.getState().unsavedWork).toBe(true);
  });

  it('guards a new caller by default', () => {
    // ⚠ The default is the safe direction on purpose: an unguarded form loses
    // somebody's work silently, while an over-eager prompt is visible and gets
    // reported. A caller opts OUT, with a reason.
    renderHook(() => useUnsavedWork(true));
    expect(useStore.getState().unsavedWork).toBe(true);
  });

  it('still warns on a TAB CLOSE even when navigation is safe', () => {
    // The half that must not be lost in the split: a draft that survives a
    // view change still dies with the tab.
    const add = vi.spyOn(window, 'addEventListener');
    const { unmount } = renderHook(() =>
      useUnsavedWork(true, { lostOnNavigation: false }),
    );
    expect(add).toHaveBeenCalledWith('beforeunload', expect.any(Function));
    expect(useStore.getState().unsavedWork).toBe(false);
    unmount();
    add.mockRestore();
  });

  it('still attaches the tab-close warning', () => {
    // The half that already worked has to keep working: this hook now does
    // two jobs and a change to one must not quietly drop the other.
    const add = vi.spyOn(window, 'addEventListener');
    const { unmount } = renderHook(() => useUnsavedWork(true));
    expect(add).toHaveBeenCalledWith('beforeunload', expect.any(Function));
    unmount();
    add.mockRestore();
  });
});
