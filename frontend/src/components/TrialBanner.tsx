/**
 * The one place outside Settings that knows about the subscription.
 *
 * Until this shipped, nothing in the app mentioned the trial. A 14-day clock
 * that is only visible if you go looking for it is a clock nobody reads — the
 * first a customer hears about it is the day the gate refuses them, which is
 * the worst possible moment to introduce the subject.
 *
 * ⚠ IT RENDERS NOTHING ON A HEALTHY PAID PLAN. A permanent bar is chrome tax
 * on every screen forever, and a banner that is always there stops being read
 * — so it earns its row only while there is something to act on: a trial
 * running out, a refusal, or a failed payment.
 *
 * ⚠ The blocked and past-due copy is the SERVER'S, verbatim (gotcha 73). This
 * component must never compose its own explanation from `plan` + `status`: the
 * gate's own words are what the user is entitled to read, and a second author
 * is how a banner and the refusal it describes drift apart.
 *
 * ⚠ AND THAT RULE WAS BROKEN HERE, which is why `status.notice` now exists.
 * The past-due branch composed its own sentence — *"Occupella keeps working
 * while your bank retries"* — which was true when written and became FALSE the
 * moment `gate.py` grew a `PAYMENT_GRACE_DAYS` cutoff: it promises unbounded
 * continuity while the gate now stops at 21 days, so the first a customer
 * hears of the deadline is the refusal. Gotcha 70's shape exactly (a change
 * that invalidates a claim has to go and find it), and the reason the local
 * branches below are a FALLBACK for an older backend rather than the primary
 * path. A countdown this component computes cannot see `past_due_since`, so it
 * could never have shown the real number.
 */

import { useEffect, useState } from 'react';

import { fetchBillingStatus, type BillingStatus } from '../services/billing';
import { useStore } from '../stores/store';

/** Below this, the trial is the most important thing on the screen. */
const URGENT_DAYS = 3;

export function daysLeft(iso: string | null, now: number = Date.now()): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return null;
  return ms <= 0 ? 0 : Math.ceil(ms / 86_400_000);
}

type Tone = 'urgent' | 'notice';

/** What the bar should say, or null for "say nothing". Pure, so the decision
 *  is testable without rendering — and so the "healthy paid plan shows
 *  nothing" rule is a property rather than a side effect of the markup. */
export function bannerFor(
  status: BillingStatus | null,
  now: number = Date.now(),
): { text: string; tone: Tone; cta: string } | null {
  if (!status) return null;

  // The gate's own words win over anything computed here.
  if (status.blockMessage) {
    return { text: status.blockMessage, tone: 'urgent', cta: 'Choose a plan' };
  }

  // Then the server's own WARNING, which is the only thing that can state the
  // real deadline — a payment grace window is `past_due_since` plus a constant
  // this bundle does not have.
  if (status.notice) {
    return {
      text: status.notice.message,
      tone: status.notice.severity === 'critical' ? 'urgent' : 'notice',
      cta: status.notice.action ?? 'Choose a plan',
    };
  }

  // ── Below here: FALLBACKS for a backend deployed before `notice` shipped.
  // They are deliberately vaguer than the server's copy, because a client
  // cannot know the deadline and a made-up one is worse than none.

  if (status.pastDue) {
    return {
      // ⚠ States the problem and NOT a promise about how long service lasts.
      // The old copy promised continuity "while your bank retries"; this build
      // cannot see the grace window, so the honest version says what is wrong
      // and what fixes it, and leaves the deadline to whoever knows it.
      text: "Your last payment didn't go through — update the card to avoid an interruption.",
      tone: 'urgent',
      cta: 'Update payment',
    };
  }

  if (status.plan === 'trial') {
    const days = daysLeft(status.trialEndsAt, now);
    if (days === null) return null;
    if (days === 0) {
      return { text: 'Your trial ends today.', tone: 'urgent', cta: 'Choose a plan' };
    }
    return {
      text: `${days} day${days === 1 ? '' : 's'} left in your trial.`,
      tone: days <= URGENT_DAYS ? 'urgent' : 'notice',
      cta: 'Choose a plan',
    };
  }

  // Paid and healthy — nothing to say, so take up no room.
  return null;
}

export function TrialBanner() {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const openSettings = useStore((s) => s.openSettings);

  useEffect(() => {
    let cancelled = false;
    fetchBillingStatus()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      // Billing is not load-bearing for the app shell. A failed read shows no
      // bar rather than an error strip across every screen.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const banner = bannerFor(status);
  if (!banner) return null;

  const urgent = banner.tone === 'urgent';
  return (
    <div
      className="flex flex-wrap items-center justify-center gap-x-2 gap-y-1 border-b px-4 py-1.5 text-[13px]"
      style={{
        borderColor: 'var(--line)',
        background: urgent ? 'var(--iris-soft)' : 'var(--canvas-raised)',
        color: 'var(--ink)',
      }}
    >
      <span>{banner.text}</span>
      <button
        type="button"
        onClick={() => openSettings('billing')}
        className="font-medium underline underline-offset-2"
        style={{ color: 'var(--iris-press)' }}
      >
        {banner.cta}
      </button>
    </div>
  );
}
