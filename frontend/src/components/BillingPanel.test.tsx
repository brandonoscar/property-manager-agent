/**
 * BillingPanel — the prices on the buttons that start a charge.
 *
 * Until 2026-09-02 this file's component carried its own table:
 *
 *     starter: { price: '$199 / user / month', blurb: '400 questions per user…' }
 *     pro:     { price: '$500 / month',        blurb: '…1,200 questions a month.' }
 *
 * hand-copied from `billing/plans.py`, with nothing holding the two in
 * agreement — while that module's docstring *invites* re-derivation before a
 * price change. So the drift was invited on one side and invisible on the
 * other, and the cost is not cosmetic: the card reads $199, the button opens
 * a Stripe Checkout at whatever the server now charges, and the customer has
 * a billing dispute.
 *
 * ⚠ Every fixture below uses prices and turn counts that are DELIBERATELY NOT
 * the shipped ones. A test written with the real figures passes against a
 * reintroduced hardcode by coincidence, which is the direction that discards
 * a working guard (gotcha 69's route-level lesson).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

vi.mock('../services/billing', () => ({
  fetchBillingStatus: vi.fn(),
  // ⚠ Needed even though nothing here asserts on it. A bare-object factory
  // replaces the WHOLE module, so an export it omits is `undefined` and the
  // panel's call to it throws — 21 unhandled errors alongside 28 green tests,
  // which is a shape that reads as passing. Default: Stripe said nothing, so
  // the panel falls back to its own estimate (what every existing assertion
  // below was written against).
  fetchUpcomingInvoice: vi.fn(async () => ({
    totalUsd: null,
    at: null,
    discounted: false,
    cardExpiresFirst: false,
    cardExpiry: null,
  })),
  startCheckout: vi.fn(),
  changePlan: vi.fn(),
  openBillingPortal: vi.fn(),
}));

// Seats are DERIVED from the roster now, so the roster is a dependency of the
// price shown on a button that starts a charge. Left unmocked it would hit the
// real API client and every per-seat assertion would be measuring a fetch.
vi.mock('../services/company', () => ({
  listMembers: vi.fn(),
}));

import {
  changePlan,
  fetchBillingStatus,
  fetchUpcomingInvoice,
  openBillingPortal,
  startCheckout,
  type BillingStatus,
  type PlanOption,
  type UpcomingInvoice,
} from '../services/billing';
import { listMembers, type Member } from '../services/company';
import { BillingPanel, nextChargeLabel } from './BillingPanel';
import { useStore } from '../stores/store';

const statusMock = vi.mocked(fetchBillingStatus);
const memberMock = vi.mocked(listMembers);

/** N people on the team. Only the length is read, so the shape is minimal. */
function members(n: number): Member[] {
  return Array.from({ length: n }, (_, i) => ({
    user_id: `u${i}`,
    email: `u${i}@example.com`,
    display_name: null,
    role: 'member' as const,
    is_primary_admin: false,
    is_self: i === 0,
  }));
}

// Not $199/$500 and not 400/1200 — see the file header.
//
// ⚠ `features` states what the fixture MEANS, never a catch-all. STARTER
// carries none and PRO carries 'crm', which is the shipped packaging — a
// fixture that granted everything would make the "not included" row on a
// cheaper plan untestable, which is gotcha 67's "a fixture named live that is
// actually dark" pointed the other way.
const STARTER: PlanOption = {
  key: 'starter',
  label: 'Starter',
  priceUsd: 243,
  perSeat: true,
  turnsIncluded: 675,
  // Not 5 and not 50 — a fixture reusing a shipped cap cannot tell "read the
  // server's number" from "remembered ours", which is how the trial's real
  // cap of 5 would go untested.
  maxSeats: 7,
  tagline: 'For one desk.',
  highlights: ['Ask about the portfolio'],
  features: [],
};
const PRO: PlanOption = {
  key: 'pro',
  label: 'Pro',
  priceUsd: 811,
  perSeat: false,
  turnsIncluded: 2450,
  maxSeats: 31,
  tagline: 'For a team.',
  highlights: ['Everything in Starter'],
  features: ['crm'],
};

function status(over: Partial<BillingStatus> = {}): BillingStatus {
  return {
    plan: 'trial',
    planLabel: 'Free trial',
    status: 'trialing',
    seats: 1,
    turnsUsed: 12,
    turnsAllowed: 150,
    turnsRemaining: 138,
    trialEndsAt: null,
    periodEnd: null,
    pastDue: false,
    // A trialling company has no subscription to re-price, so every plan
    // button opens Checkout. Tests that mean the other thing override it.
    canChangePlan: false,
    block: null,
    blockMessage: null,
    enforced: true,
    // A never-paid trial: no Stripe customer, so no portal to open. Every
    // PAYING case overrides this — a fixture has to say which state it
    // means (gotchas 67/72/73), and `false` here is what makes
    // `test_a_cancelled_customer_can_still_reach_their_invoices` meaningful.
    hasBillingAccount: false,
    features: ['crm'],
    plans: [STARTER, PRO],
    // The trial's own seat terms. ⚠ NOT one of the plans above: `currentPlan`
    // exists precisely because the trial is absent from the purchasable list,
    // so a fixture that reused STARTER here would make that distinction — the
    // whole reason for the field — untestable.
    currentPlan: {
      key: 'trial',
      label: 'Free trial',
      priceUsd: 0,
      perSeat: true,
      turnsIncluded: 150,
      maxSeats: 5,
      tagline: '',
      highlights: [],
      features: [],
    },
    // Null = nothing to warn about. See the note in TrialBanner.test.tsx.
    notice: null,
    ...over,
  };
}

async function renderPanel(over: Partial<BillingStatus> = {}, role: 'admin' | 'member' = 'admin') {
  statusMock.mockResolvedValue(status(over));
  act(() => {
    useStore.getState().setCompanyRole(role);
  });
  await act(async () => {
    render(<BillingPanel />);
  });
  await waitFor(() => expect(statusMock).toHaveBeenCalled());
}

beforeEach(() => {
  vi.clearAllMocks();
  memberMock.mockResolvedValue(members(1));
  // ⚠ Resolve, not undefined. The panel awaits this and then re-reads status;
  // an unmocked default returns undefined, the await passes, and the refresh
  // assertion would measure a promise nobody made.
  vi.mocked(changePlan).mockResolvedValue({ plan: 'pro', seats: 1 });
});

describe('the price the customer reads comes from the server', () => {
  // ⚠ BY LABEL, not by text. The card renders the figure and its unit as two
  // nodes so the price can carry the type hierarchy, and the pair carries one
  // `aria-label` for exactly this reason — a reader hears the whole phrase
  // and a test can assert on the whole phrase. `getByText` would need a
  // function matcher and would stop proving the two belong together.
  it('renders the per-seat plan at the served price and allowance', async () => {
    await renderPanel();
    expect(await screen.findByLabelText('$243 per person, per month')).toBeInTheDocument();
    expect(screen.getByText('675 answers each, every month')).toBeInTheDocument();
  });

  it('renders the flat plan at the served price and allowance', async () => {
    await renderPanel();
    expect(await screen.findByLabelText('$811 per month')).toBeInTheDocument();
    expect(
      screen.getByText('2,450 answers a month, shared across the team'),
    ).toBeInTheDocument();
  });

  it('shows no shipped price anywhere — a local table would leak one', async () => {
    await renderPanel();
    // The guard that actually catches a revert: the real figures must not
    // appear when the server did not send them.
    for (const stale of ['$199 per person, per month', '$500 per month']) {
      expect(screen.queryByText(stale)).toBeNull();
      expect(screen.queryByLabelText(stale)).toBeNull();
    }
  });

  it('says who each plan is for, in the server\'s words', async () => {
    // Three prices and no reason to pick one is what this panel was before
    // 2026-09-04. The copy is server-owned like every number beside it.
    await renderPanel();
    expect(await screen.findByText('For one desk.')).toBeInTheDocument();
    expect(screen.getByText('For a team.')).toBeInTheDocument();
    expect(screen.getByText('Ask about the portfolio')).toBeInTheDocument();
  });
});

describe('what a plan unlocks comes from `features`, not from its name', () => {
  it('marks Leasing included on the plan that carries it', async () => {
    await renderPanel();
    const rows = await screen.findAllByText(/Leasing pipeline/);
    // One row per card — the capability is stated on every plan, not only
    // where it is missing. A line that appears only on the cheap plan reads
    // as a footnote; the reason somebody upgrades is watching it tick over.
    expect(rows).toHaveLength(2);
  });

  it('says NOT INCLUDED in words, not only with an icon', async () => {
    // The icon is aria-hidden, so without this the two states are
    // indistinguishable to a screen reader — identical text, different glyph.
    await renderPanel();
    await screen.findByText('Starter');
    const starter = within(screen.getByTestId('plan-card-starter'));
    const pro = within(screen.getByTestId('plan-card-pro'));
    expect(starter.getByText(/Leasing pipeline/).previousSibling).toHaveTextContent(
      'Not included:',
    );
    expect(pro.getByText(/Leasing pipeline/).previousSibling).toHaveTextContent('Included:');
  });

  it('does not decide inclusion from the plan key', async () => {
    // The mutation this catches: a card that hardcodes `key === 'starter'`
    // instead of reading `features`. Here the CHEAP plan carries Leasing and
    // the dear one does not, so a name-based rule gets both backwards while a
    // rule that counts rows gets both right.
    await renderPanel({
      plans: [
        { ...STARTER, features: ['crm'] },
        { ...PRO, features: [] },
      ],
    });
    await screen.findByText('Starter');
    const starter = within(screen.getByTestId('plan-card-starter'));
    const pro = within(screen.getByTestId('plan-card-pro'));
    expect(starter.getByText(/Leasing pipeline/).previousSibling).toHaveTextContent('Included:');
    expect(pro.getByText(/Leasing pipeline/).previousSibling).toHaveTextContent(
      'Not included:',
    );
  });
});

describe('seats are derived from the roster, never typed', () => {
  it('offers no seat input at all', async () => {
    // ⚠ THIS IS THE POINT OF THE REWRITE. The old panel let an admin type any
    // number and billed Stripe that quantity, with nothing checking it against
    // reality — buy one seat, invite five people. A stepper reappearing is a
    // billing defect, not a UI preference.
    await renderPanel();
    await screen.findByText('Starter');
    expect(screen.queryByLabelText('Seats')).toBeNull();
    expect(screen.queryByRole('spinbutton')).toBeNull();
  });

  it('checks out with the team size the roster reports', async () => {
    // The load-bearing assertion: the quantity Stripe is asked to bill is the
    // number of people actually on the team.
    memberMock.mockResolvedValue(members(4));
    await renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Starter' }));
    await waitFor(() =>
      expect(vi.mocked(startCheckout)).toHaveBeenCalledWith('starter', 4),
    );
  });

  it('shows the arithmetic before the button that charges for it', async () => {
    // Taking the control away means showing the count. A per-person button
    // that does not say how many people it counted is asking for approval of
    // a number the user cannot see.
    memberMock.mockResolvedValue(members(4));
    await renderPanel();
    expect(await screen.findByText(/4 people on your team · \$972 per month/)).toBeInTheDocument();
  });

  it('bills a FLAT plan for one regardless of team size', async () => {
    // Driven by the DATA, not the plan name: Pro is perSeat:false, so a
    // four-person team must still check out at quantity 1.
    //
    // ⚠ ON TRIAL, and that is now load-bearing rather than incidental. This
    // case used to render with `plan: 'starter'` — a company that ALREADY PAYS
    // — and assert it reached `startCheckout`. That was the double-charge: a
    // second Checkout gives one customer two live subscriptions. The quantity
    // rule under test is unchanged; the state that exercises it is a first
    // purchase. A paying customer's route is pinned separately below.
    memberMock.mockResolvedValue(members(4));
    await renderPanel({ plan: 'trial', planLabel: 'Free trial' });
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Pro' }));
    await waitFor(() => expect(vi.mocked(startCheckout)).toHaveBeenCalledWith('pro', 1));
  });

  it('a paying customer CHANGES plan and never starts a second checkout', async () => {
    // ⚠ THE DOUBLE-CHARGE. Checkout CREATES a subscription and Stripe will
    // happily give one customer two: they pay both, and the resulting
    // `subscription.created` webhook overwrites our stored subscription id, so
    // the first bills forever with nothing in this app able to see it.
    memberMock.mockResolvedValue(members(4));
    // ⚠ `canChangePlan` STATED, not inherited. It is the server's answer to
    // "is there a subscription to re-price", and this test's whole subject is
    // a customer who has one — a fixture that left it at the base `false`
    // would send them to Checkout and pass for the wrong reason.
    await renderPanel({ plan: 'starter', planLabel: 'Starter', canChangePlan: true });
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Pro' }));
    await waitFor(() => expect(vi.mocked(changePlan)).toHaveBeenCalledWith('pro'));
    expect(vi.mocked(startCheckout)).not.toHaveBeenCalled();
  });

  it('a customer whose subscription is DEAD gets checkout, not a doomed switch', async () => {
    // ⚠ THE CUSTOMER THIS FIELD EXISTS FOR, and the one input no other test
    // here supplies. They picked Starter, the first payment expired, and the
    // row still says `plan: 'starter'` — so `onTrial` was false and the panel
    // pushed `changePlan`, which re-prices a subscription that never charged
    // and comes back as "Could not change the plan with Stripe". Checkout is
    // the open door: `gate.py` tells this customer to pick a plan again, and
    // the server's 409 no longer refuses them for "already having" one.
    //
    // Every other test here has `canChangePlan` agreeing with `!onTrial`, so a
    // panel that reads either one passes all of them. This is the pair that
    // disagrees (gotchas 38/51 — the distinguishing input is the whole test).
    memberMock.mockResolvedValue(members(4));
    await renderPanel({
      plan: 'starter',
      planLabel: 'Starter',
      status: 'incomplete_expired',
      canChangePlan: false,
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Pro' }));
    await waitFor(() => expect(vi.mocked(startCheckout)).toHaveBeenCalled());
    expect(vi.mocked(changePlan)).not.toHaveBeenCalled();
  });

  it('heads the section "Choose a plan" when there is nothing to switch', async () => {
    // The label has to agree with what the button does, or it promises one
    // action and the click performs the other.
    memberMock.mockResolvedValue(members(4));
    await renderPanel({
      plan: 'starter',
      planLabel: 'Starter',
      status: 'incomplete_expired',
      canChangePlan: false,
    });
    expect(await screen.findByText('Choose a plan')).toBeInTheDocument();
    expect(screen.queryByText('Change plan')).not.toBeInTheDocument();
  });

  it('re-reads the server after a switch rather than trusting the response', async () => {
    // The webhook is the only writer of our billing row, so the response body
    // says what we ASKED for. Patching local state from it would show a plan
    // the customer might not be on.
    memberMock.mockResolvedValue(members(4));
    // ⚠ `canChangePlan` STATED, not inherited. It is the server's answer to
    // "is there a subscription to re-price", and this test's whole subject is
    // a customer who has one — a fixture that left it at the base `false`
    // would send them to Checkout and pass for the wrong reason.
    await renderPanel({ plan: 'starter', planLabel: 'Starter', canChangePlan: true });
    const before = vi.mocked(fetchBillingStatus).mock.calls.length;
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Pro' }));
    await waitFor(() =>
      expect(vi.mocked(fetchBillingStatus).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it('will not start a per-person checkout while the count is unknown', async () => {
    // Failing to count must not silently bill for one person (gotcha 55's
    // decline-to-assert, pointed at money). The button stays disabled.
    memberMock.mockRejectedValue(new Error('roster unavailable'));
    await renderPanel();
    const button = await screen.findByRole('button', { name: 'Choose Starter' });
    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(true));
    expect(vi.mocked(startCheckout)).not.toHaveBeenCalled();
  });
});

describe('an empty catalog declines to price rather than guessing', () => {
  it('says it could not load the plans instead of rendering a remembered price', async () => {
    // An older backend sends no `plans`. Rendering a figure this build happens
    // to remember is exactly the drift the change removes, so the honest
    // degradation is to show none (gotcha 55's decline-to-assert).
    await renderPanel({ plans: [] });
    expect(await screen.findByText(/Couldn't load the current plans/)).toBeInTheDocument();
    expect(screen.queryByText('Choose')).toBeNull();
  });
});

describe('what a member sees', () => {
  it('shows the meter but no purchasable plans', async () => {
    await renderPanel({}, 'member');
    expect(await screen.findByText('An admin on your team can change the plan.')).toBeInTheDocument();
    expect(screen.queryByText('$243 per person, per month')).toBeNull();
    // The meter stays visible on purpose — a member who can see 12 / 150
    // tells their admin instead of filing a support ticket.
    expect(screen.getByText('12 of 150')).toBeInTheDocument();
  });
});

describe('a paying customer can find the way out', () => {
  it('names cancelling on the portal link, not just payment and invoices', async () => {
    // This link IS the cancellation path — there is no in-app cancel — so the
    // label is the only thing that tells a customer it exists. "Manage payment
    // method and invoices" reads as a billing-details link and makes the exit
    // look absent, which is a dark pattern rather than a wording preference.
    await renderPanel({ plan: 'pro', planLabel: 'Pro', status: 'active', hasBillingAccount: true });
    const link = await screen.findByRole('button', { name: /cancel/i });
    expect(link).toBeInTheDocument();
  });

  it('offers it to a paying customer and not on the trial', async () => {
    // Two branches of one rule need two tests (gotcha 74): asserting only that
    // the link renders says nothing about the plan it renders FOR. On trial
    // there is no subscription to manage and the portal has nothing to show.
    await renderPanel({ plan: 'trial' });
    expect(screen.queryByRole('button', { name: /cancel/i })).toBeNull();
  });

  it('still reaches invoices AFTER cancelling, when plan has fallen back to trial', async () => {
    // ⚠ THE DEFECT, and the reason this stopped keying on `plan`.
    // `customer.subscription.deleted` stores `plan = 'trial'` deliberately, so
    // a company that paid for a year and stopped was indistinguishable from
    // one that never paid — and this link, the only route to their invoices
    // and payment history, disappeared at the moment they most needed it and a
    // chargeback was most likely.
    await renderPanel({
      plan: 'trial',
      planLabel: 'Free trial',
      status: 'canceled',
      hasBillingAccount: true,
    });
    expect(await screen.findByRole('button', { name: /cancel/i })).toBeInTheDocument();
  });

  it('opens the portal rather than starting a checkout', async () => {
    // The label promising cancellation has to reach the portal. Wired to
    // startCheckout it would send someone trying to LEAVE into a purchase.
    await renderPanel({ plan: 'pro', planLabel: 'Pro', status: 'active', hasBillingAccount: true });
    (await screen.findByRole('button', { name: /cancel/i })).click();
    await waitFor(() => expect(vi.mocked(openBillingPortal)).toHaveBeenCalled());
    expect(vi.mocked(startCheckout)).not.toHaveBeenCalled();
  });
});

describe("the block banner is the server's own words", () => {
  it('renders blockMessage verbatim rather than composing one', async () => {
    // Gotcha 73: two booleans cannot separate "your trial ended" from "you're
    // out of questions", and those need opposite actions from the user.
    const message = 'Your 14-day trial has ended. Settings → Billing to pick a plan.';
    await renderPanel({ block: 'trial_expired', blockMessage: message });
    expect(await screen.findByText(message)).toBeInTheDocument();
  });
});

describe('nextChargeLabel', () => {
  // ⚠ "Resets <date>" was the ONLY thing on this page about the renewal, and
  // it describes the METER. A three-seat Pro customer read "$199 / person /
  // month", a "3 people" pill and a reset date, and had to do the
  // multiplication themselves to learn they pay $597 — then guess that the
  // reset date is also the charge date. Both facts were already on the
  // payload; neither was ever said.
  // ⚠ Built here rather than reused from PRO: that fixture is deliberately
  // `perSeat: false` at an unrealistic $811, so a test that assumed it was
  // per-seat would assert production's numbers against a fixture chosen to
  // NOT be them. The multiplication is the subject, so the plan under test has
  // to state which kind it is.
  const PER_SEAT = { ...PRO, key: 'pro' as const, perSeat: true, priceUsd: 199 };
  const FLAT = { ...PRO, key: 'scale' as const, perSeat: false, priceUsd: 500 };

  const paying = (over: Partial<BillingStatus> = {}) =>
    status({
      plan: 'pro',
      planLabel: 'Pro',
      status: 'active',
      seats: 3,
      periodEnd: '2026-10-04T00:00:00Z',
      hasBillingAccount: true,
      currentPlan: PER_SEAT,
      ...over,
    });

  it('multiplies a per-seat plan by the seats Stripe is billing', () => {
    expect(nextChargeLabel(paying())).toContain('$597');
  });

  it('does NOT multiply a flat plan', () => {
    // Scale's own promise is that adding people does not change the bill, so
    // multiplying here would contradict the card one section above.
    expect(nextChargeLabel(paying({ currentPlan: FLAT }))).toContain('$500');
  });

  it('says nothing on a trial', () => {
    // Naming a charge that is not coming is a false statement about somebody's
    // money — the one direction of error that reaches an invoice.
    expect(nextChargeLabel(status())).toBeNull();
  });

  it('says nothing after a cancellation, even though period_end is still set', () => {
    // ⚠ `canceled` keeps a period_end — it is the paid-THROUGH date. Calling
    // that a charge tells someone who cancelled that money is about to leave
    // their account.
    expect(nextChargeLabel(paying({ status: 'canceled' }))).toBeNull();
  });

  it('still names the charge while a card is failing', () => {
    // `past_due` is served for a grace window and Stripe is still retrying, so
    // there genuinely is a charge coming.
    expect(nextChargeLabel(paying({ status: 'past_due' }))).toContain('$597');
  });

  it('says nothing when the bundle is too old to carry a plan', () => {
    expect(nextChargeLabel(paying({ currentPlan: null }))).toBeNull();
  });

  it('says nothing without a period end', () => {
    expect(nextChargeLabel(paying({ periodEnd: null }))).toBeNull();
  });
});

// ── the next charge comes from Stripe, not from arithmetic ──────────
//
// ⚠ The panel computed "Next charge $X" as list price × seats, which knew
// nothing about DISCOUNTS or PRORATION. That was correct only while nobody
// could have a coupon — and enabling promotion codes on Checkout is exactly
// what made it possible to overstate somebody's bill. A confident wrong number
// about money is the one error that reaches an invoice.

/** An UpcomingInvoice with only the fields a test cares about.
 *
 *  ⚠ Exists because adding two fields to `UpcomingInvoice` broke four inline
 *  literals at once — caught by `tsc`, never by `vitest`, which does not
 *  typecheck (gotcha 72). The defaults are the QUIET state: no discount, no
 *  card warning, so a test that says nothing about the card is asserting on a
 *  panel that shows none.
 */
function invoiceOf(over: Partial<UpcomingInvoice> = {}): UpcomingInvoice {
  return {
    totalUsd: null,
    at: null,
    discounted: false,
    cardExpiresFirst: false,
    cardExpiry: null,
    ...over,
  };
}

describe('nextChargeLabel with a real invoice', () => {
  const paying = status({
    plan: 'pro',
    planLabel: 'Pro',
    status: 'active',
    seats: 3,
    periodEnd: '2026-10-04T00:00:00Z',
    hasBillingAccount: true,
    currentPlan: { ...PRO, perSeat: true, priceUsd: 199 },
  });

  it("uses Stripe's figure over the panel's own multiplication", () => {
    // 3 x $199 = $597. Stripe says $448.50 because a coupon is applied. The
    // discounted number is the one a customer will actually be charged.
    const label = nextChargeLabel(paying, {
      totalUsd: 448.5,
      at: '2026-10-04T00:00:00Z',
      discounted: true,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
    expect(label).toContain('448.5');
    expect(label).not.toContain('597');
  });

  it("uses Stripe's DATE too, which can differ mid plan-change", () => {
    const label = nextChargeLabel(paying, invoiceOf({ totalUsd: 100, at: '2026-11-20T00:00:00Z' }));
    expect(label).toContain(new Date('2026-11-20T00:00:00Z').toLocaleDateString());
  });

  it('falls back to its own estimate when Stripe said nothing', () => {
    // ⚠ Null is UNKNOWN, never zero. No subscription, a Stripe outage and a
    // 404 all arrive this way, and "$0.00 due" would be a false statement.
    // The estimate is what this panel showed before the invoice existed and
    // is right for everyone without a coupon.
    for (const invoice of [
      null,
      undefined,
      invoiceOf(),
    ]) {
      expect(nextChargeLabel(paying, invoice), String(invoice)).toContain('597');
    }
  });

  it('still says nothing at all when there is no charge coming', () => {
    // The invoice must not resurrect a label the status rules suppress —
    // telling somebody who cancelled that money is about to leave.
    const cancelled = { ...paying, status: 'canceled' } as BillingStatus;
    expect(
      nextChargeLabel(cancelled, invoiceOf({ totalUsd: 448.5, discounted: true })),
    ).toBeNull();
  });

  it('does not let a zero total read as an estimate', () => {
    // $0.00 is a real answer — a fully-discounted period — and it must render
    // as zero rather than falling through to list price. `typeof === number`
    // is what separates it from null; a truthiness check would not.
    const label = nextChargeLabel(paying, invoiceOf({ totalUsd: 0, discounted: true }));
    expect(label).toContain('$0');
    expect(label).not.toContain('597');
  });
});

describe('the panel asks for the invoice', () => {
  it('fetches it, and a failure there does not fail the panel', async () => {
    // ⚠ Pairs the behavioural test with proof the panel CALLS it (gotcha 62):
    // a client function nothing invokes is the `comms_triage` failure.
    const base = status();
    statusMock.mockResolvedValue(base);
    memberMock.mockResolvedValue(members(1));
    vi.mocked(fetchUpcomingInvoice).mockResolvedValue({
      totalUsd: null,
      at: null,
      discounted: false,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
    render(<BillingPanel />);
    await waitFor(() => expect(fetchUpcomingInvoice).toHaveBeenCalled());
    // The panel still rendered its plan, which is the half that must survive.
    expect(await screen.findByText(base.planLabel)).toBeInTheDocument();
  });

  it("RENDERS Stripe's number, not just fetches it", async () => {
    // ⚠ Found by a mutation, not by review: discarding the fetched result
    // while still calling the function passed every test above, because the
    // label tests call `nextChargeLabel` directly and the call-site test only
    // asserted the call. Fetching a figure and then showing a different one is
    // exactly the defect this whole change removes.
    statusMock.mockResolvedValue(
      status({
        plan: 'pro',
        planLabel: 'Pro',
        status: 'active',
        seats: 3,
        periodEnd: '2026-10-04T00:00:00Z',
        hasBillingAccount: true,
        currentPlan: { ...PRO, perSeat: true, priceUsd: 199 },
      }),
    );
    memberMock.mockResolvedValue(members(3));
    vi.mocked(fetchUpcomingInvoice).mockResolvedValue({
      totalUsd: 448.5,
      at: '2026-10-04T00:00:00Z',
      discounted: true,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
    render(<BillingPanel />);
    // 3 x $199 = $597 is what the panel would compute for itself.
    expect(await screen.findByText(/448\.5/)).toBeInTheDocument();
    expect(screen.queryByText(/597/)).toBeNull();
  });
});

describe('the card that will not survive the next charge', () => {
  // The one billing failure nobody was warned about: the renewal declines,
  // the row goes past_due, and the existing notice finally speaks AFTER the
  // money did not move. Stripe knew the expiry the whole time.
  //
  // ⚠ It renders HERE and not in the standing banner, because that banner is
  // driven by `notice_for`, which reads only the DB row — putting a Stripe
  // call behind it would land on `/billing/status`, which every page fetches
  // (gotcha 37). This is the screen somebody opened to look at their bill.
  const paying = () =>
    status({
      plan: 'pro',
      planLabel: 'Pro',
      status: 'active',
      seats: 1,
      periodEnd: '2026-10-04T00:00:00Z',
      hasBillingAccount: true,
      currentPlan: { ...PRO, perSeat: true, priceUsd: 199 },
    });

  it('warns, and names the expiry in the form printed on the card', async () => {
    statusMock.mockResolvedValue(paying());
    memberMock.mockResolvedValue(members(1));
    vi.mocked(fetchUpcomingInvoice).mockResolvedValue(
      invoiceOf({
        totalUsd: 199,
        at: '2026-10-04T00:00:00Z',
        cardExpiresFirst: true,
        cardExpiry: '09/2026',
      }),
    );
    render(<BillingPanel />);
    expect(await screen.findByText(/09\/2026/)).toBeInTheDocument();
    // The sentence has to say what happens if they do nothing — a bare
    // "card expires 09/2026" is a fact, not a reason to act.
    expect(screen.getByText(/decline/i)).toBeInTheDocument();
  });

  it('says NOTHING when the card outlives the charge', async () => {
    // ⚠ The direction a one-sided test would miss (gotcha 38). A panel that
    // warned everybody would pass the test above, and a false alarm about
    // somebody's money is how a warning gets ignored when it is real.
    statusMock.mockResolvedValue(paying());
    memberMock.mockResolvedValue(members(1));
    vi.mocked(fetchUpcomingInvoice).mockResolvedValue(
      invoiceOf({ totalUsd: 199, at: '2026-10-04T00:00:00Z' }),
    );
    render(<BillingPanel />);
    // ⚠ Anchor on the NEXT-CHARGE line, not the plan name: 'Pro' renders both
    // as the current plan and as a card in the picker, so it is ambiguous —
    // and this line is what proves the invoice actually resolved, which is
    // the state the absence assertion below has to be made against.
    await screen.findByText(/Next charge/);
    expect(screen.queryByText(/expires/i)).toBeNull();
    expect(screen.queryByText(/decline/i)).toBeNull();
  });

  it('says nothing when the server could not read the card', async () => {
    // `cardExpiresFirst: false` covers "fine" AND "cannot say", and the panel
    // must render the same thing for both — the server is what decides, and
    // it declines to assert on an unreadable card (gotchas 55/57).
    statusMock.mockResolvedValue(paying());
    memberMock.mockResolvedValue(members(1));
    vi.mocked(fetchUpcomingInvoice).mockResolvedValue(
      invoiceOf({ totalUsd: 199, at: '2026-10-04T00:00:00Z', cardExpiry: '09/2026' }),
    );
    render(<BillingPanel />);
    // ⚠ Anchor on the NEXT-CHARGE line, not the plan name: 'Pro' renders both
    // as the current plan and as a card in the picker, so it is ambiguous —
    // and this line is what proves the invoice actually resolved, which is
    // the state the absence assertion below has to be made against.
    await screen.findByText(/Next charge/);
    expect(screen.queryByText(/09\/2026/)).toBeNull();
  });
});
