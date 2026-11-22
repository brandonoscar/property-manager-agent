/**
 * The fact itself: when do we say this device's sign-in is not accepted?
 *
 * ⚠ These tests drive the module directly and therefore CANNOT see whether
 * anything calls it — the "complete, tested and wired to nothing" failure
 * this repo keeps shipping (gotchas 62/66/69/103/121/136). The wiring is
 * pinned in `api.sessionRejected.test.ts`, which drives the real `apiFetch`.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CONSECUTIVE_REJECTIONS,
  isSessionRejected,
  onSessionRejectedChange,
  reportCredentialAccepted,
  reportCredentialRejected,
  resetSessionRejected,
} from './sessionRejected';

beforeEach(() => {
  resetSessionRejected();
});

describe('the bar', () => {
  it('stays quiet below it', () => {
    for (let i = 0; i < CONSECUTIVE_REJECTIONS - 1; i += 1) {
      reportCredentialRejected();
    }
    // ⚠ The distinguishing input for "is there a bar at all". A module that
    // announced on the first rejection passes every other test in this file.
    expect(isSessionRejected()).toBe(false);
  });

  it('says so once it is met', () => {
    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) {
      reportCredentialRejected();
    }
    expect(isSessionRejected()).toBe(true);
  });

  it('is more than one', () => {
    // The bar's whole purpose. A token can expire between `getAccessToken()`
    // reading it and the server verifying it, and the next request refreshes
    // and succeeds — so one 401 is a race, not a verdict.
    expect(CONSECUTIVE_REJECTIONS).toBeGreaterThan(1);
  });
});

describe('an accepted request', () => {
  it('clears an announced rejection', () => {
    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();
    expect(isSessionRejected()).toBe(true);

    reportCredentialAccepted();

    expect(isSessionRejected()).toBe(false);
  });

  it('resets the count outright, not by one', () => {
    // One accepted request answers "is the credential good right now", so a
    // decrement would leave a loaded counter that announces again on the very
    // next single 401.
    //
    // ⚠ THE COUNT HAS TO GO WELL ABOVE THE BAR FIRST, and the mutation
    // battery is what found that. An earlier cut stopped one short of the bar
    // before accepting — where `max(0, 1 - 1)` and `= 0` are the same number,
    // so a decrementing version passed it (gotcha 58: check the mutant can
    // change any output before believing the test covers it). This is the
    // real shape anyway: a rejected credential fails EVERY request, so by the
    // time one succeeds the counter is deep.
    for (let i = 0; i < CONSECUTIVE_REJECTIONS + 2; i += 1) reportCredentialRejected();
    reportCredentialAccepted();
    reportCredentialRejected();

    expect(isSessionRejected()).toBe(false);
  });
});

describe('subscribers', () => {
  it('are told on a change and not on a repeat', () => {
    const seen: boolean[] = [];
    onSessionRejectedChange((r) => seen.push(r));

    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();
    reportCredentialRejected();
    reportCredentialRejected();

    // One emission for the transition, none for the rejections that follow.
    expect(seen).toEqual([true]);
  });

  it('are told when it clears', () => {
    const seen: boolean[] = [];
    onSessionRejectedChange((r) => seen.push(r));

    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();
    reportCredentialAccepted();
    reportCredentialAccepted();

    expect(seen).toEqual([true, false]);
  });

  it('a throwing subscriber does not stop the others', () => {
    // The signal is an enhancement and must never break the request that
    // produced it (gotcha 7).
    const good = vi.fn();
    onSessionRejectedChange(() => {
      throw new Error('boom');
    });
    onSessionRejectedChange(good);

    expect(() => {
      for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();
    }).not.toThrow();
    expect(good).toHaveBeenCalledWith(true);
  });

  it('stop hearing after unsubscribe', () => {
    const seen: boolean[] = [];
    const off = onSessionRejectedChange((r) => seen.push(r));
    off();

    for (let i = 0; i < CONSECUTIVE_REJECTIONS; i += 1) reportCredentialRejected();

    expect(seen).toEqual([]);
  });
});
