/**
 * The failure this guards against is silent: a new view is added to the store
 * and nobody names it, so its tab reads "Occupella" exactly as every tab did
 * before — indistinguishable from the bug being fixed. `Record<Union, string>`
 * makes that a compile error, and this reads BOTH sides so a rename in the
 * store cannot drift from the map.
 */

import { describe, expect, it } from 'vitest';

import { APP_NAME, SIGN_IN_TITLE, titleFor, VIEW_TITLES } from './useDocumentTitle';

describe('tab titles', () => {
  it('names the view first, because a narrow tab truncates from the right', () => {
    expect(titleFor('inbox')).toBe('Inbox · Occupella');
    expect(titleFor('inbox').indexOf('Inbox')).toBe(0);
  });

  it('falls back to the bare product name rather than rendering "undefined · Occupella"', () => {
    expect(titleFor(null)).toBe(APP_NAME);
    expect(titleFor(undefined)).toBe(APP_NAME);
  });

  it('gives every view a DISTINCT title — the whole point is telling tabs apart', () => {
    const titles = Object.values(VIEW_TITLES);
    expect(new Set(titles).size).toBe(titles.length);
  });

  it('never leaves a view unnamed', () => {
    for (const [view, label] of Object.entries(VIEW_TITLES)) {
      expect(label.trim(), `view "${view}" has no title`).not.toBe('');
      expect(titleFor(view as keyof typeof VIEW_TITLES)).not.toBe(APP_NAME);
    }
  });

  it('covers exactly the store\'s view union — a stale key is as bad as a missing one', () => {
    // Mirrors the union in stores/store.ts. If that union changes, the Record
    // type fails to compile AND this list disagrees — two signals, not one.
    expect(Object.keys(VIEW_TITLES).sort()).toEqual(
      ['chat', 'crm', 'dev-runs', 'inbox', 'properties', 'search', 'settings'].sort(),
    );
  });

  it('names the sign-in screen when nobody is signed in', () => {
    expect(SIGN_IN_TITLE).toBe('Sign in · Occupella');
  });
});
