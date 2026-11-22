/**
 * The subscription bar in the app chrome.
 *
 * The decision of WHAT to say is a pure function (`bannerFor`), so most of
 * this file tests it directly — the rule that matters ("a healthy paid plan
 * shows nothing") is a property of that decision, not of the markup, and
 * testing it through a render would make it hostage to layout.
 *
 * ⚠ The rendering tests still exist, because a correct decision nothing
 * mounts is the `comms_triage` failure (gotcha 62): a helper can be perfect
 * and have no caller.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/billing', () => ({
  fetchBillingStatus: vi.fn(),
}));

import { fetchBillingStatus, type BillingStatus } from '../services/billing';
import { TrialBanner, bannerFor, daysLeft } from './TrialBanner';
// Vite's ?raw gives the file's text without needing node types in this tsconfig.
import appSource from '../App.tsx?raw';

const NOW = Date.parse('2026-09-03T12:00:00Z');
const inDays = (n: number) => new Date(NOW + n * 86_400_000).toISOString();

function status(over: Partial<BillingStatus> = {}): BillingStatus {
  return {
    plan: 'trial',
    planLabel: 'Free trial',
    status: 'trialing',
    seats: 1,
    turnsUsed: 5,
    turnsAllowed: 150,
    turnsRemaining: 145,
    trialEndsAt: inDays(9),
    periodEnd: null,
    pastDue: false,
    // Never subscribed — nothing to switch. Stated rather than defaulted:
    // this banner's subject is a trial that has not paid yet.
    canChangePlan: false,
    block: null,
    blockMessage: null,
    enforced: true,
    // A never-paid trial — this banner's whole subject.
    hasBillingAccount: false,
    features: ['crm'],
    plans: [],
    // ⚠ Null MEANS "an older backend did not send it" — the state this banner
    // must survive, since it says nothing about seats or price. A populated
    // plan here would be testing a field this component never reads.
    currentPlan: null,
    // ⚠ Null, not omitted. `vitest` does not typecheck (gotcha 72), so a
    // fixture missing a required field reports green here and is rejected by
    // `tsc` in CI — and the default has to state which case it MEANS: null is
    // "the server had nothing to warn about", which is the healthy account
    // most of these cases are about.
    notice: null,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('it stays out of the way when there is nothing to say', () => {
  it('renders nothing on a healthy paid plan', () => {
    // ⚠ The rule that keeps this from becoming permanent chrome. A bar that
    // is always present stops being read, so it must earn its row.
    expect(bannerFor(status({ plan: 'pro', planLabel: 'Pro', status: 'active' }), NOW)).toBeNull();
  });

  it('renders nothing before billing has loaded', () => {
    expect(bannerFor(null, NOW)).toBeNull();
  });

  it('renders nothing on a trial with no end date on file', () => {
    // Unknown must not render as urgent (gotcha 55). A trial whose end we
    // cannot read gets no countdown rather than an invented one.
    expect(bannerFor(status({ trialEndsAt: null }), NOW)).toBeNull();
  });
});

describe('the trial countdown', () => {
  it('counts the days left', () => {
    expect(bannerFor(status({ trialEndsAt: inDays(9) }), NOW)?.text).toBe(
      '9 days left in your trial.',
    );
  });

  it('says day, singular, at one', () => {
    expect(bannerFor(status({ trialEndsAt: inDays(0.5) }), NOW)?.text).toBe(
      '1 day left in your trial.',
    );
  });

  it('says the trial ends today rather than "0 days left"', () => {
    expect(bannerFor(status({ trialEndsAt: inDays(-1) }), NOW)?.text).toBe(
      'Your trial ends today.',
    );
  });

  it('escalates tone only in the last stretch', () => {
    // Two branches of one rule need two tests (gotcha 74). Amber all fortnight
    // teaches people to ignore it; amber never is the same as no warning.
    expect(bannerFor(status({ trialEndsAt: inDays(9) }), NOW)?.tone).toBe('notice');
    expect(bannerFor(status({ trialEndsAt: inDays(2) }), NOW)?.tone).toBe('urgent');
  });
});

describe("a refusal is quoted, never re-authored", () => {
  it("renders the server's blockMessage verbatim", () => {
    // Gotcha 73: the banner and the refusal it explains must not have two
    // authors. Whatever the gate refuses with is what the user reads.
    const message = 'Your 14-day trial has ended. Settings → Billing to pick a plan.';
    expect(bannerFor(status({ block: 'trial_expired', blockMessage: message }), NOW)?.text).toBe(
      message,
    );
  });

  it('lets the block outrank the countdown', () => {
    // Both conditions true at once: the refusal is the one that matters.
    const message = 'You are out of questions for this period.';
    const out = bannerFor(status({ trialEndsAt: inDays(5), blockMessage: message }), NOW);
    expect(out?.text).toBe(message);
    expect(out?.tone).toBe('urgent');
  });

  it("renders the server's payment warning verbatim, deadline and all", () => {
    // ⚠ THIS TEST USED TO ASSERT /keeps working/ — the old copy promised
    // service "while your bank retries", which was true until gate.py grew
    // PAYMENT_GRACE_DAYS and then described a continuity the gate had stopped
    // offering. A test pinning a claim the product no longer keeps is not
    // coverage; it is the defect with a green tick (gotchas 67/68).
    //
    // Only the server can state the remaining days: they are `past_due_since`
    // plus a constant this bundle does not have.
    const out = bannerFor(
      status({
        plan: 'pro',
        status: 'past_due',
        pastDue: true,
        notice: {
          reason: 'payment_failed',
          severity: 'critical',
          message:
            "Your last payment didn't go through. Occupella keeps working for 20 more days, then pauses until the card is updated.",
          action: 'Update payment method',
        },
      }),
      NOW,
    );
    expect(out?.text).toMatch(/20 more days/);
    expect(out?.tone).toBe('urgent');
    expect(out?.cta).toBe('Update payment method');
  });

  it('states the problem without inventing a deadline when the server sends none', () => {
    // The fallback path — a backend older than `notice`. It must still warn,
    // and must NOT make up a number it cannot know.
    const out = bannerFor(
      status({ plan: 'pro', status: 'past_due', pastDue: true, notice: null }),
      NOW,
    );
    expect(out?.text).toMatch(/didn't go through/i);
    expect(out?.text).not.toMatch(/\d+ (more )?days?/);
    expect(out?.cta).toBe('Update payment');
  });

  it('lets the server notice outrank the local trial countdown', () => {
    // Both true at once. The server's sentence wins, because it is the one
    // that knows which deadline is actually being enforced.
    const out = bannerFor(
      status({
        trialEndsAt: inDays(2),
        notice: {
          reason: 'trial_ending',
          severity: 'critical',
          message: 'Your free trial ends tomorrow.',
          action: 'Choose a plan',
        },
      }),
      NOW,
    );
    expect(out?.text).toBe('Your free trial ends tomorrow.');
  });
});

describe('it is actually mounted and wired', () => {
  // ⚠ The pure-function tests above pass NOW explicitly; the MOUNTED
  // component reads the real clock. So `inDays(4)` was four days after a
  // frozen 2026-09-03 and only read as "4 days left" until real time caught
  // up with it -- on the fifth day the banner said "ends today" and this
  // failed, on a file nobody had touched. A test whose fixture is relative to
  // a hardcoded date has to freeze the clock it is measured against.
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the countdown and offers a way to act on it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    vi.mocked(fetchBillingStatus).mockResolvedValue(status({ trialEndsAt: inDays(4) }));
    render(<TrialBanner />);
    expect(await screen.findByText(/left in your trial/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Choose a plan' })).toBeInTheDocument();
  });

  it('shows no bar at all when billing cannot be read', async () => {
    // Billing is not load-bearing for the app shell. A failed read must not
    // put an error strip across every screen in the product.
    vi.mocked(fetchBillingStatus).mockRejectedValue(new Error('down'));
    const { container } = render(<TrialBanner />);
    await waitFor(() => expect(vi.mocked(fetchBillingStatus)).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });
});

describe('it is in the app shell', () => {
  it('is mounted by App', () => {
    // ⚠ Every test above renders the component directly, so all of them pass
    // against an App that never mounts it — a helper that is perfect and has
    // no caller (gotcha 62). This is the only assertion that can see that.
    //
    // A source scan rather than an AST walk: JSX needs a TS-aware parser that
    // vitest does not carry, and `<TrialBanner` is a single indivisible token
    // that no formatter splits — unlike the call-with-arguments shapes gotcha
    // 40 warns about, where a reflow puts the callee and the literal on
    // different lines.
    expect(appSource).toMatch(/<TrialBanner\s*\/>/);
    expect(appSource).toMatch(/import\s*\{\s*TrialBanner\s*\}/);
  });
});

describe('daysLeft', () => {
  it('rounds up, so a partial day still counts as a day', () => {
    expect(daysLeft(inDays(0.1), NOW)).toBe(1);
  });

  it('floors at zero rather than going negative', () => {
    expect(daysLeft(inDays(-5), NOW)).toBe(0);
  });

  it('returns null for an unparseable date instead of NaN', () => {
    expect(daysLeft('not-a-date', NOW)).toBeNull();
  });
});
