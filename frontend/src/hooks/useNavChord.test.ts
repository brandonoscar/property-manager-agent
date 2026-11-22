/**
 * `g`-chord navigation, and specifically the one that now deep-links.
 *
 * Team Activity moved off the sidebar into Settings (2026-09-02). The muscle
 * memory for `g a` is older than the layout, so the chord follows the
 * destination rather than being retired — which means it is the only chord
 * that resolves to a view AND a section.
 *
 * ⚠ This file exists because removing the sidebar item and rewiring the chord
 * broke nothing: 648 tests passed against a chord that could have gone
 * nowhere. Neither the chord nor the settings deep-link had any coverage at
 * all, so the whole path was free to regress silently.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';

import { useNavChord } from './useNavChord';
import { useStore } from '../stores/store';

vi.mock('../services/analytics', () => ({
  captureNavChordUsed: vi.fn(),
  captureViewChanged: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { dismiss: vi.fn() }) }));

function press(key: string): void {
  window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
}

/** `g` then the follow-up, inside the 800ms window. */
function chord(key: string): void {
  press('g');
  press(key);
}

beforeEach(() => {
  useStore.setState({ view: 'chat', settingsSection: 'account' });
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => vi.useRealTimers());

const state = () => useStore.getState();

describe('useNavChord', () => {
  it('sends `g a` to Team Activity inside Settings', () => {
    renderHook(() => useNavChord());
    chord('a');
    expect(state().view).toBe('settings');
    expect(state().settingsSection).toBe('activity');
  });

  it('still moves when Settings is ALREADY open', () => {
    // ⚠ The case the same-view early return would swallow. Pressing `g a`
    // from the Account tab has to land on Team Activity — otherwise the
    // chord silently does nothing for anyone already in Settings, which is
    // exactly where they are most likely to press it.
    useStore.setState({ view: 'settings', settingsSection: 'account' });
    renderHook(() => useNavChord());
    chord('a');
    expect(state().settingsSection).toBe('activity');
  });

  it('leaves the plain view chords alone', () => {
    renderHook(() => useNavChord());
    chord('i');
    expect(state().view).toBe('inbox');
    chord('c');
    expect(state().view).toBe('chat');
  });

  it('opens Settings on `g s` without hijacking the section', () => {
    useStore.setState({ view: 'chat', settingsSection: 'activity' });
    renderHook(() => useNavChord());
    chord('s');
    expect(state().view).toBe('settings');
    // `g s` means "settings", not "settings, and forget where I was".
    expect(state().settingsSection).toBe('activity');
  });

  it('ignores an unrecognised follow-up key', () => {
    renderHook(() => useNavChord());
    chord('z');
    expect(state().view).toBe('chat');
  });

  it('does not fire without the leading g', () => {
    renderHook(() => useNavChord());
    press('a');
    expect(state().view).toBe('chat');
  });

  it('does not hijack typing in an input', () => {
    renderHook(() => useNavChord());
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
    expect(state().view).toBe('chat');
    input.remove();
  });
});
