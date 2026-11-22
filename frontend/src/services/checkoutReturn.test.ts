/**
 * Coming back from Stripe with something to show for it.
 *
 * ⚠ THE GAP THIS CLOSES. `create_checkout` has always sent
 * `success_url={base}?billing=success`, and a grep of the whole frontend found
 * NO reader for that string. A customer typed their card into Stripe, was
 * redirected into the app, and the app said nothing — while very likely still
 * showing "Free trial", because our billing row is written by the webhook and
 * not by the redirect.
 */

import { describe, expect, it, beforeEach } from 'vitest';
import {
  CHECKOUT_MESSAGE,
  CHECKOUT_PARAM,
  checkoutOutcome,
  clearCheckoutParam,
} from './checkoutReturn';

describe('reading the outcome', () => {
  it('recognises both outcomes the server actually sends', () => {
    // ⚠ The literals are the SERVER'S. `billing.py` writes `?billing=success`
    // and `?billing=cancelled` — a client that looked for anything else would
    // silently never fire, which is the state this file exists to leave.
    expect(checkoutOutcome('?billing=success')).toBe('success');
    expect(checkoutOutcome('?billing=cancelled')).toBe('cancelled');
  });

  it('reads it alongside other query parameters', () => {
    expect(checkoutOutcome('?demo=0&billing=success&x=1')).toBe('success');
  });

  it('is null for anything else, so an ordinary page load says nothing', () => {
    expect(checkoutOutcome('')).toBeNull();
    expect(checkoutOutcome('?demo=0')).toBeNull();
    // Not a value we send. Announcing a purchase off an arbitrary string would
    // let any link congratulate somebody on a payment they never made.
    expect(checkoutOutcome('?billing=yes')).toBeNull();
    expect(checkoutOutcome('?billing=')).toBeNull();
  });
});

describe('clearing the parameter', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/');
  });

  it('removes it so a refresh does not re-announce the purchase', () => {
    window.history.replaceState({}, '', '/?billing=success');
    clearCheckoutParam();
    expect(window.location.search).toBe('');
    expect(checkoutOutcome(window.location.search)).toBeNull();
  });

  it('leaves every other parameter alone', () => {
    // Dropping `demo` here would sign somebody out of the demo on their way
    // back from Stripe.
    window.history.replaceState({}, '', '/?demo=0&billing=success&keep=1');
    clearCheckoutParam();
    const params = new URLSearchParams(window.location.search);
    expect(params.get('demo')).toBe('0');
    expect(params.get('keep')).toBe('1');
    expect(params.has(CHECKOUT_PARAM)).toBe(false);
  });

  it('is a no-op when there is nothing to clear', () => {
    window.history.replaceState({}, '', '/?demo=0');
    clearCheckoutParam();
    expect(window.location.search).toBe('?demo=0');
  });
});

describe('what it says', () => {
  it('names the lag instead of claiming the plan is already live', () => {
    // ⚠ Our row is written by the webhook, not the redirect, so the plan
    // genuinely can still read as the old one for a few seconds. Claiming
    // otherwise is a promise this code cannot keep — and the refresh-and-panic
    // loop it causes ends in a second checkout attempt.
    expect(CHECKOUT_MESSAGE.success.description).toMatch(/take a moment/i);
  });

  it('says plainly that a cancelled checkout charged nothing', () => {
    expect(CHECKOUT_MESSAGE.cancelled.title).toMatch(/nothing was charged/i);
  });
});
