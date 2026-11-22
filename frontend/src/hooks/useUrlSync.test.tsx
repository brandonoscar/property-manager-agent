/**
 * URL ↔ view, exercised rather than read.
 *
 * jsdom has a real History API and the store is real, so unlike layout this
 * whole hook is observable here — including the mount-order bug that made
 * every deep link land on Chat, which no source guard could have seen.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useStore } from '../stores/store';
import { useUrlSync } from './useUrlSync';

const at = (path: string) => window.history.replaceState(null, '', path);
const path = () => window.location.pathname;

beforeEach(() => {
  at('/');
  useStore.setState({ view: 'chat', settingsSection: 'account', unsavedWork: false });
});

describe('arriving at a URL', () => {
  it('opens the view the link names', () => {
    at('/leasing');
    renderHook(() => useUrlSync());
    expect(useStore.getState().view).toBe('crm');
    expect(path()).toBe('/leasing');
  });

  it('does NOT push the default path over the link that was opened', () => {
    // The bug this exists for. Effects run in declaration order, so the read
    // effect has already called setView by the time the push effect runs —
    // but `view` in the push effect is closed over the MOUNT render, which
    // still says `chat`. It pushed `/` over `/settings/billing`, and every
    // deep link landed on Chat. Measured in a real browser before it was
    // caught here.
    at('/settings/billing');
    renderHook(() => useUrlSync());
    expect(path()).toBe('/settings/billing');
    expect(useStore.getState().settingsSection).toBe('billing');
  });

  it('normalises without adding a history step', () => {
    at('/settings');
    const before = window.history.length;
    renderHook(() => useUrlSync());
    expect(path()).toBe('/settings/account');
    expect(window.history.length).toBe(before);
  });

  it('leaves an unknown path alone rather than relocating the person', () => {
    at('/nonsense');
    renderHook(() => useUrlSync());
    expect(useStore.getState().view).toBe('chat');
  });
});

describe('navigating inside the app', () => {
  it('puts each view in the address bar', () => {
    renderHook(() => useUrlSync());
    act(() => useStore.getState().setView('inbox'));
    expect(path()).toBe('/inbox');
    act(() => useStore.getState().setView('properties'));
    expect(path()).toBe('/operations');
    act(() => useStore.getState().openSettings('team'));
    expect(path()).toBe('/settings/team');
  });

  it('adds a history entry per navigation, so Back has somewhere to go', () => {
    renderHook(() => useUrlSync());
    const before = window.history.length;
    act(() => useStore.getState().setView('inbox'));
    expect(window.history.length).toBe(before + 1);
  });

  it('does not push when the view is set to what it already is', () => {
    // Otherwise clicking the tab you are on stacks duplicate entries and Back
    // appears to do nothing several times in a row.
    at('/inbox');
    renderHook(() => useUrlSync());
    const before = window.history.length;
    act(() => useStore.getState().setView('inbox'));
    expect(window.history.length).toBe(before);
  });
});

describe('Back and Forward', () => {
  it('moves the app when the browser moves the URL', () => {
    renderHook(() => useUrlSync());
    act(() => useStore.getState().setView('inbox'));
    act(() => {
      // What the browser does on Back: the URL changes, then popstate fires.
      window.history.replaceState(null, '', '/operations');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(useStore.getState().view).toBe('properties');
  });

  it('does not push a second entry for the move Back just made', () => {
    // The guard is a COMPARISON, not a flag: popstate sets the store, the
    // store change re-runs the push effect, and the path it computes is
    // already the current location — so it stays quiet. A flag would need
    // clearing at exactly the right moment, and getting that wrong is how a
    // router either duplicates entries or eats a real navigation.
    renderHook(() => useUrlSync());
    act(() => useStore.getState().setView('inbox'));
    const after = window.history.length;
    act(() => {
      window.history.replaceState(null, '', '/operations');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(window.history.length).toBe(after);
    expect(path()).toBe('/operations');
  });

  it('puts the address bar BACK when unsaved work is kept', () => {
    // ⚠ URL sync is what made Back a way to lose a half-filled form, so this
    // is the half that arrived with the feature. The browser has already
    // changed the URL by the time popstate fires, and the push effect only
    // runs when the VIEW changes — which, on a refusal, it does not. Without
    // the restore the bar says /operations while the app shows the form the
    // person asked to keep, and the next Back lands somewhere neither of them
    // expects.
    const realConfirm = window.confirm;
    window.confirm = (() => false) as unknown as typeof window.confirm;
    try {
      renderHook(() => useUrlSync());
      act(() => useStore.getState().setView('inbox'));
      useStore.setState({ unsavedWork: true });
      act(() => {
        window.history.replaceState(null, '', '/operations');
        window.dispatchEvent(new PopStateEvent('popstate'));
      });
      expect(useStore.getState().view).toBe('inbox');
      expect(path()).toBe('/inbox');
    } finally {
      window.confirm = realConfirm;
    }
  });

  it('lets Back through when the person agrees to lose the work', () => {
    const realConfirm = window.confirm;
    window.confirm = (() => true) as unknown as typeof window.confirm;
    try {
      renderHook(() => useUrlSync());
      act(() => useStore.getState().setView('inbox'));
      useStore.setState({ unsavedWork: true });
      act(() => {
        window.history.replaceState(null, '', '/operations');
        window.dispatchEvent(new PopStateEvent('popstate'));
      });
      expect(useStore.getState().view).toBe('properties');
      expect(path()).toBe('/operations');
    } finally {
      window.confirm = realConfirm;
    }
  });

  it('ignores a popstate to a path it does not recognise', () => {
    renderHook(() => useUrlSync());
    act(() => useStore.getState().setView('crm'));
    act(() => {
      window.history.replaceState(null, '', '/nonsense');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(useStore.getState().view).toBe('crm');
  });
});

describe('the query string', () => {
  it('survives a navigation', () => {
    // `?checkout=success` is read at the root and cleared by its own effect.
    // A push that dropped it would swallow the one acknowledgement a customer
    // gets after paying.
    at('/?checkout=success');
    renderHook(() => useUrlSync());
    act(() => useStore.getState().setView('inbox'));
    expect(window.location.search).toBe('?checkout=success');
  });
});
