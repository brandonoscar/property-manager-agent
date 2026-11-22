/**
 * The two mistakes this module exists to prevent, stated as tests:
 *
 *   1. A DELIBERATE sign-out reported to the user as "your session ended" —
 *      which is a lie, and one that makes the product look unstable at the
 *      exact moment they chose to leave.
 *   2. An INVOLUNTARY sign-out reported as nothing at all — indistinguishable
 *      from a crash that binned their work.
 *
 * Both are silent in production, so both need a test that can observe them.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  consumeSignOutIntent,
  markIntentionalSignOut,
  rememberReturnView,
  takeReturnView,
} from './sessionEnd';

beforeEach(() => {
  // Consume any leftover intent so tests cannot leak into one another —
  // the flag is module scope by design (see the module docstring).
  consumeSignOutIntent();
  window.sessionStorage.clear();
});

describe('telling the two sign-outs apart', () => {
  it('reports an unmarked sign-out as involuntary — the default that matters', () => {
    expect(consumeSignOutIntent()).toBe(false);
  });

  it('reports a marked one as deliberate', () => {
    markIntentionalSignOut();
    expect(consumeSignOutIntent()).toBe(true);
  });

  it('CONSUMES the intent, so a later session death is still reported honestly', () => {
    // The failure this guards: click Sign out, sign back in, then get revoked
    // from another device — and hear nothing, because the earlier click is
    // still standing.
    markIntentionalSignOut();
    expect(consumeSignOutIntent()).toBe(true);
    expect(consumeSignOutIntent()).toBe(false);
  });
});

describe('coming back to where you were', () => {
  it('round-trips the view', () => {
    rememberReturnView('inbox');
    expect(takeReturnView()).toBe('inbox');
  });

  it('is spent once — a stale return view would yank a later sign-in off course', () => {
    rememberReturnView('properties');
    expect(takeReturnView()).toBe('properties');
    expect(takeReturnView()).toBeNull();
  });

  it('returns null when nothing was remembered, rather than an empty string', () => {
    expect(takeReturnView()).toBeNull();
  });

  it('uses sessionStorage, not localStorage — a return view must die with the tab', () => {
    rememberReturnView('crm');
    expect(window.sessionStorage.getItem('occupella.returnView')).toBe('crm');
    expect(window.localStorage.getItem('occupella.returnView')).toBeNull();
  });
});

describe('storage that throws does not break sign-out', () => {
  it('swallows a write failure — a private window must still be able to sign out', () => {
    const setItem = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('blocked');
      });
    expect(() => rememberReturnView('inbox')).not.toThrow();
    setItem.mockRestore();
  });

  it('swallows a read failure and reports no return view', () => {
    const getItem = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('blocked');
      });
    expect(takeReturnView()).toBeNull();
    getItem.mockRestore();
  });
});
