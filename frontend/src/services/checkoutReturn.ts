/**
 * What happens when somebody comes BACK from Stripe Checkout.
 *
 * ⚠ NOTHING DID. `create_checkout` sends `success_url={base}?billing=success`
 * and `cancel_url={base}?billing=cancelled`, and a grep of the whole frontend
 * for either string found no reader. So a customer typed their card into
 * Stripe, was redirected into the app, and the app said nothing at all — while
 * very likely still showing "Free trial", because the plan is only written when
 * the `customer.subscription.created` webhook lands, which is asynchronous and
 * routinely a beat behind the redirect.
 *
 * That is the worst possible moment for silence. The customer has just paid,
 * has no receipt from us, and the one screen they would check still says they
 * are on a trial. The support ticket writes itself, and the natural next action
 * — click the plan again — used to start a SECOND subscription (the server now
 * refuses that with a 409).
 *
 * ⚠ THE MESSAGE SAYS THE LAG OUT LOUD rather than pretending it is instant.
 * Claiming the plan is live when our own row may not say so yet is a promise
 * this code cannot keep; "it can take a moment to show up here" is true, and it
 * stops the refresh-and-panic loop.
 */

export type CheckoutOutcome = 'success' | 'cancelled';

/** The query parameter `billing/checkout` puts on both return URLs. */
export const CHECKOUT_PARAM = 'billing';

/**
 * Read the outcome out of a URL search string, if there is one.
 *
 * Split out from the DOM so it can be driven directly — the alternative is a
 * test that has to fake `history` to assert a string comparison.
 */
export function checkoutOutcome(search: string): CheckoutOutcome | null {
  const value = new URLSearchParams(search).get(CHECKOUT_PARAM);
  return value === 'success' || value === 'cancelled' ? value : null;
}

/**
 * Strip the parameter from the address bar without a navigation.
 *
 * ⚠ Load-bearing, not tidiness. Left in place, every refresh re-announces a
 * purchase that happened once — and worse, a customer who bookmarks or shares
 * that URL gets "you're subscribed" on a page they may not have paid for.
 * `replaceState` keeps the history entry so Back still behaves.
 */
export function clearCheckoutParam(): void {
  if (typeof window === 'undefined' || !window.history?.replaceState) return;
  const url = new URL(window.location.href);
  if (!url.searchParams.has(CHECKOUT_PARAM)) return;
  url.searchParams.delete(CHECKOUT_PARAM);
  window.history.replaceState(
    window.history.state,
    '',
    `${url.pathname}${url.search}${url.hash}`,
  );
}

/** What to say for each outcome. Exported so the copy is testable without
 *  rendering, and so there is one place it lives. */
export const CHECKOUT_MESSAGE: Record<CheckoutOutcome, { title: string; description: string }> = {
  success: {
    title: 'Payment received — thank you.',
    // ⚠ Names the lag. Our billing row is written by Stripe's webhook, not by
    // the redirect, so the plan genuinely can still read as the old one for a
    // few seconds. Saying so is what stops the second checkout attempt.
    description:
      'Your plan can take a moment to show up here. Settings → Billing has your ' +
      'invoices and payment method.',
  },
  cancelled: {
    // Not an error and not a nag: they chose to stop. Acknowledging it is what
    // distinguishes "you cancelled" from "something broke on the way back".
    title: 'Checkout cancelled — nothing was charged.',
    description: 'Your plan is unchanged.',
  },
};
