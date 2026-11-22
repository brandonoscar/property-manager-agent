/**
 * Settings → Billing.
 *
 * ⚠ The block banner renders the server's `blockMessage` verbatim and never
 * composes its own from `plan` + `status` + `turnsRemaining`. Gotcha 73: the
 * SMS status endpoint handed the client two booleans, the client guessed which
 * blocker had fired, and the panel's explanation diverged from the refusal it
 * was explaining. Whatever the gate refuses with is what the user reads.
 *
 * ⚠ The meter is visible to every member, not just admins. Someone who can't
 * change the plan still needs to know the account is at 96% — an invisible
 * balance is what makes people churn from anxiety, and a member who can see
 * the number tells their admin instead of filing a support ticket.
 *
 * ⚠ There is NO price table in this file, and reintroducing one is the bug.
 * Until 2026-09-02 it carried `$199 / user / month` and `400 answers per
 * user` hand-copied from `billing/plans.py`, which nothing held in agreement
 * — while that module's docstring invites re-derivation before a price
 * change. The card would have shown the old figure while the button opened a
 * Checkout at the new one: a billing dispute, not a display bug. The server
 * sends the facts; this file only formats the sentence.
 *
 * ⚠ THERE IS NO SEAT STEPPER, and putting one back is a regression. It let an
 * admin type any number and billed Stripe that quantity, with nothing checking
 * it against reality — buy one seat, invite five people. Seats are DERIVED
 * from the team roster, which is the number the product can actually defend.
 * The consequence is that the count must be VISIBLE before the button that
 * charges for it: taking the control away means showing the arithmetic.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Check, ExternalLink, Loader2, Minus } from 'lucide-react';
import { toast } from 'sonner';

import {
  changePlan,
  fetchBillingStatus,
  fetchUpcomingInvoice,
  openBillingPortal,
  startCheckout,
  type BillingStatus,
  type UpcomingInvoice,
  type PaidPlanKey,
  type PlanFeature,
  type PlanOption,
} from '../services/billing';
import { listMembers } from '../services/company';
import { roleAtLeast } from '../lib/roles';
import { useStore } from '../stores/store';
import { count } from '../lib/plural';

// The SENTENCE is local; every NUMBER in it comes from the server. A price
// table lived here until 2026-09-02, hand-copied from `billing/plans.py` —
// so a price change on one side showed the old figure on the card while
// Stripe charged the new one.
// The pricing page's own words (helixis-onboarding Pricing.tsx `unit`), so a
// card here and the card a customer picked on occupella.com say the same.
function priceUnit(p: PlanOption): string {
  return p.perSeat ? 'per person, per month' : 'per month';
}

function planBlurb(p: PlanOption): string {
  const turns = count(p.turnsIncluded, 'answer');
  return p.perSeat
    ? `${turns} each, every month`
    : `${turns} a month, shared across the team`;
}

/**
 * The capabilities a card states in words, in the order they are shown.
 *
 * ⚠ The KEY is the server's (`plans.FEATURE_CRM`) and so is the yes/no — this
 * map only supplies the English. A card must never decide inclusion from the
 * plan's NAME: the whole point of `PlanOption.features` is that the sentence
 * on the card and the refusal from the API come from one set, so moving
 * Leasing to a different rung changes both at once.
 *
 * ⚠ THE MAP CANNOT GO STALE AND A HAND-TYPED ORDER COULD, which is why the
 * order is derived from it. `Record<PlanFeature, string>` is exhaustive, so
 * adding a capability to the union fails the build here until it has English
 * (measured: TS2741 on this object) — while the render list used to be a
 * separate `PlanFeature[]`, which a subset satisfies, so tsc said nothing and
 * the new capability appeared on no plan card at all. Two adjacent lists, one
 * enforced and one not, with the comment's "quiet rather than wrong"
 * reassurance attached to the enforced one (gotchas 48/122). Deriving leaves
 * one place to add a feature. Key order is insertion order for string keys,
 * so the display order is the order the labels are written below.
 */
const FEATURE_LABEL: Record<PlanFeature, string> = {
  crm: 'Leasing pipeline — texts, calls and lead tracking',
};
const FEATURE_ORDER = Object.keys(FEATURE_LABEL) as PlanFeature[];

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return ms <= 0 ? 0 : Math.ceil(ms / 86_400_000);
}

/** Semantic colour is reserved for a real state (DESIGN.md), and "you are
 *  about to lose access" is one. Below 80% the meter is the accent, because a
 *  bar that is amber all month teaches people to ignore it. */
function meterColor(pct: number): string {
  if (pct >= 100) return 'var(--danger)';
  if (pct >= 80) return 'var(--caution)';
  return 'var(--iris)';
}

function Meter({ used, allowed }: { used: number; allowed: number }) {
  const pct = allowed > 0 ? Math.min(100, Math.round((used / allowed) * 100)) : 0;
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-[var(--r-pill)]"
      style={{ background: 'var(--canvas-elevated)' }}
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="Usage this period"
    >
      <div
        className="h-full rounded-[var(--r-pill)] transition-[width] duration-150"
        style={{ width: `${pct}%`, background: meterColor(pct) }}
      />
    </div>
  );
}

function Pill({ children, tone }: { children: React.ReactNode; tone?: 'accent' | 'muted' }) {
  const accent = tone === 'accent';
  return (
    <span
      className="rounded-[var(--r-pill)] px-2 py-0.5 text-[12px] font-medium"
      style={{
        background: accent ? 'var(--iris-soft)' : 'var(--canvas-elevated)',
        color: accent ? 'var(--iris-press)' : 'var(--ink-muted)',
      }}
    >
      {children}
    </span>
  );
}

/** One line of a plan card's "what you get" list.
 *
 *  ⚠ The excluded state is a MUTED DASH, not a red cross. Red is reserved for
 *  a real failure (DESIGN.md's semantic palette); painting an ordinary
 *  packaging decision in the danger colour makes a cheaper plan look broken
 *  rather than smaller. */
function IncludedRow({ included, children }: { included: boolean; children: React.ReactNode }) {
  const Icon = included ? Check : Minus;
  return (
    <li className="flex items-start gap-1.5 text-[12px]">
      <Icon
        aria-hidden
        className="mt-[2px] h-3.5 w-3.5 flex-shrink-0"
        style={{ color: included ? 'var(--iris)' : 'var(--ink-subtle)' }}
      />
      {/* The icon is aria-hidden, so the state has to reach a screen reader as
          words — otherwise "Leasing pipeline" reads identically on the plan
          that has it and the plan that does not. */}
      <span className="sr-only">{included ? 'Included: ' : 'Not included: '}</span>
      <span style={{ color: included ? 'var(--ink-muted)' : 'var(--ink-subtle)' }}>{children}</span>
    </li>
  );
}

function Banner({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex gap-2.5 rounded-[var(--r-md)] border p-3"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
    >
      <AlertTriangle
        className="mt-0.5 h-4 w-4 flex-shrink-0"
        style={{ color: 'var(--caution)' }}
      />
      <p className="text-[13px]" style={{ color: 'var(--ink)' }}>
        {children}
      </p>
    </div>
  );
}

export function BillingPanel() {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [invoice, setInvoice] = useState<UpcomingInvoice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // null = still counting. Seats are derived, so the button that charges for
  // them stays disabled until we know the number — quoting a total we have
  // not confirmed is worse than a moment's wait.
  const [memberCount, setMemberCount] = useState<number | null>(null);
  const companyRole = useStore((s) => s.companyRole);
  // roleAtLeast, not an equality check: it is the one place the role
  // ordering lives, and there is no 'owner' in TeamRole — an equality
  // list would have silently locked admins out the day one was added.
  const isAdmin = roleAtLeast(companyRole, 'admin');

  const load = useCallback(async () => {
    try {
      setStatus(await fetchBillingStatus());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load your billing details.');
    }
    // ⚠ AFTER, and in its own statement. It is a second network call that can
    // be slow or fail, and it must never be able to stop the panel rendering
    // — `fetchUpcomingInvoice` swallows its own errors for the same reason.
    // Not `Promise.all`: that would make a Stripe blip look like a failure to
    // load billing at all.
    setInvoice(await fetchUpcomingInvoice());
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // Only an admin can check out, so only an admin needs the count.
    if (!isAdmin) return;
    let cancelled = false;
    listMembers()
      .then((m) => {
        if (!cancelled) setMemberCount(m.length);
      })
      .catch(() => {
        // Leave it null. The plan row says it could not count rather than
        // quoting a total from a number it does not have.
        if (!cancelled) setMemberCount(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  const go = async (fn: () => Promise<string>, key: string) => {
    setBusy(key);
    try {
      window.location.href = await fn();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Something went wrong.');
      setBusy(null);
    }
  };

  /**
   * Pick a plan. CHECKOUT for a first purchase, CHANGE-PLAN for anybody who
   * already pays.
   *
   * ⚠ Both buttons used to call checkout, and that was a double-charge. This
   * panel says "Change plan" to a paying customer and offers a button on every
   * other plan — each of which opened a fresh Stripe Checkout. Stripe will
   * create a SECOND subscription: the customer pays both, and the resulting
   * `subscription.created` webhook overwrites our stored subscription id, so
   * the first one bills forever with nothing in this app able to see it. The
   * server now 409s that path; this is the half that stops asking for it.
   *
   * ⚠ A switch does NOT redirect. It lands on Stripe immediately, so the panel
   * re-reads `/billing/status` — the webhook is the only writer of our row, and
   * trusting the response body would show a plan the customer might not be on.
   */
  const choose = async (key: PaidPlanKey, seats: number) => {
    if (!canSwitchPlan) {
      await go(() => startCheckout(key, seats), key);
      return;
    }
    setBusy(key);
    try {
      await changePlan(key);
      // ⚠ Re-read rather than patch local state. The switch is applied by
      // Stripe and written to our row by the webhook, which may not have landed
      // yet — so this can legitimately still show the old plan for a moment.
      // That is honest; inventing the new one here is what would be wrong.
      await load();
      toast.success('Plan updated. Your next invoice reflects the change.');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Could not change your plan.');
    } finally {
      setBusy(null);
    }
  };

  if (error) {
    return <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>{error}</p>;
  }
  if (!status) {
    return (
      <p className="flex items-center gap-2 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
        <Loader2 className="h-4 w-4 animate-spin" /> Loading your plan…
      </p>
    );
  }

  const trialDays = daysUntil(status.trialEndsAt);
  const onTrial = status.plan === 'trial';
  /**
   * Which action a plan button performs — CHECKOUT or SWITCH.
   *
   * ⚠ This used to be `onTrial`, and the two are different questions that
   * happen to agree for almost everybody. They come apart for the customer
   * whose FIRST PAYMENT EXPIRED: `plan` is still whatever they picked at
   * checkout, so `onTrial` was false, the section below headed itself "Change
   * plan", and every button pushed a re-price at a subscription that never
   * charged and never will — which comes back as "Could not change the plan
   * with Stripe", blaming a vendor that is working fine. Their other route was
   * shut too: checkout's 409 refused them for "already having a subscription"
   * while the chat gate, in the same breath, was telling them that
   * subscription was never completed and to pick a plan again.
   *
   * `onTrial` stays where it means what it says — the trial countdown and the
   * per-person footer are about the PLAN, not about what a button does.
   */
  const canSwitchPlan = status.canChangePlan;
  // The middle rung of the ladder — the plans arrive in ladder order, so this
  // points at the right card without the client naming a plan. A hardcoded
  // 'pro' would have kept badging the $199 tier after 2026-09-04, when every
  // label shifted down a rung and 'pro' stopped being the one it meant.
  const recommendedKey =
    status.plans.length >= 3
      ? status.plans[Math.floor((status.plans.length - 1) / 2)].key
      : null;

  return (
    <div className="space-y-5">
      {status.blockMessage && <Banner>{status.blockMessage}</Banner>}

      {/* ⚠ The SERVER'S sentence, not one composed here. This used to read
          "Occupella keeps working while your bank retries", which was true
          until gate.py grew PAYMENT_GRACE_DAYS and then promised a continuity
          the gate had stopped offering — the same stale claim TrialBanner
          carried, in a second place (gotcha 25's rule: fix it at every layer
          that has it). The notice knows the remaining days; this file cannot. */}
      {status.notice && !status.blockMessage && <Banner>{status.notice.message}</Banner>}

      {/* Fallback for a backend older than `notice`. Deliberately states the
          problem without a deadline it cannot know. */}
      {!status.notice && status.pastDue && !status.blockMessage && (
        <Banner>
          Your last payment didn&apos;t go through — update the card to avoid an
          interruption.
        </Banner>
      )}

      {/* Current state. Deliberately a different shape from the purchasable
          plans below: three identical bordered rectangles is what made this
          panel read as a form rather than a product surface. */}
      <section
        className="rounded-[var(--r-md)] border p-4"
        style={{ borderColor: 'var(--card-edge)', background: 'var(--canvas-raised)' }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[16px] font-medium" style={{ color: 'var(--ink)' }}>
            {status.planLabel}
          </h3>
          {onTrial && trialDays !== null && (
            <Pill tone={trialDays <= 3 ? 'accent' : 'muted'}>
              {trialDays === 0
                ? 'Ended'
                : `${trialDays} day${trialDays === 1 ? '' : 's'} left`}
            </Pill>
          )}
          {!onTrial && status.seats > 1 && <Pill>{status.seats} people</Pill>}
        </div>

        <div className="mt-3.5 flex items-baseline justify-between gap-3">
          {/* "Usage", not "Questions used this period" (founder call,
              2026-09-04). The row already reads `412 of 1,200` beside it, so
              the noun was doing no work the number wasn't; and the unit stays
              out of the LABEL on purpose — the server's block message is the
              one place that names it, so there is nothing here to drift out
              of step with it. */}
          <span className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
            Usage
          </span>
          {/* Built as ONE string rather than interpolated children: React
              splits `{a} of {b}` into three text nodes, which makes the
              number a reader sees unqueryable as a unit and a test that
              asserts on it brittle for a reason unrelated to the product. */}
          <span className="text-[13px] font-medium tabular-nums" style={{ color: 'var(--ink)' }}>
            {`${status.turnsUsed.toLocaleString()} of ${status.turnsAllowed.toLocaleString()}`}
          </span>
        </div>

        <div className="mt-2">
          <Meter used={status.turnsUsed} allowed={status.turnsAllowed} />
        </div>

        <div className="mt-2 flex flex-wrap gap-x-3 text-[12px]" style={{ color: 'var(--ink-subtle)' }}>
          {/* ⚠ "Resets <date>" was the ONLY thing on this page about the
              renewal, and it describes the METER. A three-seat Pro customer
              read "$199 / person / month", a "3 people" pill and a reset date,
              and had to do the multiplication themselves to learn they pay
              $597 — then guess that the reset date is also the charge date.
              Both facts were already on this payload; neither was ever said.

              ⚠ Every number comes from the SERVER (`currentPlan.priceUsd`,
              `status.seats` — the quantity Stripe's own webhook stored), so
              this is arithmetic on server facts and not a price table (the
              shape `PlanOut` exists to abolish). A flat plan multiplies by
              nothing, which is why `perSeat` decides rather than the seat
              count. */}
          {status.periodEnd && (
            <span>
              {nextChargeLabel(status, invoice) ??
                `Resets ${new Date(status.periodEnd).toLocaleDateString()}`}
            </span>
          )}
          {/* ⚠ A card expiring before that charge is the one billing failure
              nobody was warned about: the renewal declines, the row goes
              past_due, and the existing notice finally speaks AFTER the money
              did not move. Stripe knew the expiry the whole time.

              It sits HERE rather than in the standing banner because the
              banner is driven by `notice_for`, which reads only the DB row —
              putting a Stripe call behind it would land on `/billing/status`,
              which every page fetches. This is the screen somebody opened to
              look at their bill, and the sentence is one line from the button
              that fixes it.

              The server sends `cardExpiresFirst` only when it actually read a
              card and the charge falls inside the warning window, so this
              renders nothing on "cannot say". */}
          {invoice?.cardExpiresFirst && (
            <span style={{ color: 'var(--caution)' }}>
              Card expires {invoice.cardExpiry} — update it before then or the renewal
              will decline
            </span>
          )}
          {!status.enforced && (
            // Honest about the state of the world rather than quietly
            // pretending the meter binds. It does not yet, and saying so beats
            // a customer discovering it later. Conditional on the SERVER's
            // flag, so it disappears by itself the day enforcement goes on.
            <span>Tracked, not yet enforced</span>
          )}
        </div>
      </section>

      {isAdmin ? (
        <div className="space-y-2.5">
          <h4
            className="text-[12px] font-medium uppercase tracking-[0.06em]"
            style={{ color: 'var(--ink-subtle)' }}
          >
            {canSwitchPlan ? 'Change plan' : 'Choose a plan'}
          </h4>

          {status.plans.length === 0 && (
            // Nothing to price against. Saying so beats rendering a figure
            // this build happens to remember — a stale price on a button that
            // starts a charge is a billing dispute, not a display bug.
            <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
              Couldn&apos;t load the current plans and prices. Reload the page, or contact
              support if it keeps happening.
            </p>
          )}

          <div className="grid gap-2.5 sm:grid-cols-3">
            {status.plans.map((plan) => {
              const isCurrent = status.plan === plan.key;
              const recommended = plan.key === recommendedKey && !isCurrent;
              // Seats follow the roster. A flat plan bills 1 whatever the team
              // size — the server enforces that too (it ignores the client's
              // number on a flat plan), this just avoids quoting a wrong total.
              const seats = plan.perSeat ? (memberCount ?? 0) : 1;
              const canBuy = !isCurrent && (!plan.perSeat || memberCount !== null);
              return (
                <div
                  key={plan.key}
                  data-testid={`plan-card-${plan.key}`}
                  className="flex flex-col rounded-[var(--r-md)] border p-4"
                  style={{
                    // ⚠ Emphasis is the BORDER and the ground, never a tinted
                    // panel or a coloured left rail — DESIGN.md forbids both,
                    // and a card that shouts is how this panel read as a
                    // marketing page rather than a setting.
                    borderColor: isCurrent || recommended ? 'var(--line-strong)' : 'var(--line)',
                    background: isCurrent ? 'var(--canvas-raised)' : 'var(--canvas)',
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[14px] font-medium" style={{ color: 'var(--ink)' }}>
                      {plan.label}
                    </span>
                    {isCurrent && <Pill tone="accent">Current</Pill>}
                    {recommended && <Pill>Most teams</Pill>}
                  </div>

                  {/* ⚠ The figure and its unit are two nodes so the price can
                      carry the type hierarchy — and that is exactly what this
                      file's meter comment warns against, because a split
                      value is unqueryable as a unit and a reader hears
                      "$243" … "/ person / month" as two facts. So the PAIR
                      gets one accessible name: a screen reader says the whole
                      phrase, and a test can assert on the whole phrase. Do
                      not split a price without one. */}
                  <div
                    className="mt-2 flex items-baseline gap-1.5"
                    aria-label={`$${plan.priceUsd.toLocaleString()} ${priceUnit(plan)}`}
                  >
                    <span
                      aria-hidden
                      className="text-[20px] font-medium tabular-nums"
                      style={{ color: 'var(--ink)' }}
                    >
                      {`$${plan.priceUsd.toLocaleString()}`}
                    </span>
                    <span aria-hidden className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                      {priceUnit(plan)}
                    </span>
                  </div>

                  <div className="mt-1 text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                    {planBlurb(plan)}
                  </div>

                  {plan.tagline && (
                    <p className="mt-2.5 text-[13px]" style={{ color: 'var(--ink)' }}>
                      {plan.tagline}
                    </p>
                  )}

                  <ul className="mt-2.5 flex flex-col gap-1.5">
                    {plan.highlights.map((h) => (
                      <IncludedRow key={h} included>
                        {h}
                      </IncludedRow>
                    ))}
                    {/* Rendered from `features`, so "not included" is the same
                        set the API refuses on. Shown on EVERY card including
                        the ones that have it: a capability listed only where
                        it is missing reads as a footnote, and the reason
                        somebody upgrades is seeing it tick over. */}
                    {FEATURE_ORDER.map((f) => (
                      <IncludedRow key={f} included={plan.features.includes(f)}>
                        {FEATURE_LABEL[f]}
                      </IncludedRow>
                    ))}
                  </ul>

                  <div className="mt-3.5 flex flex-col gap-1.5 pt-0.5">
                    {/* Taking the stepper away means showing the arithmetic. A
                        button that charges per person must say how many people
                        it counted, before it is pressed. */}
                    {plan.perSeat && !isCurrent && (
                      <span
                        className="text-[12px] tabular-nums"
                        style={{ color: 'var(--ink-muted)' }}
                      >
                        {memberCount === null
                          ? 'Counting your team…'
                          : `${seats} ${seats === 1 ? 'person' : 'people'} on your team · $${(
                              plan.priceUsd * seats
                            ).toLocaleString()} per month`}
                      </span>
                    )}
                    {!isCurrent && (
                      <button
                        type="button"
                        // The visible word is "Choose" on every card, so the
                        // accessible name has to carry which plan — otherwise
                        // a screen reader hears three identical buttons that
                        // start three different charges.
                        aria-label={`Choose ${plan.label}`}
                        disabled={busy !== null || !canBuy}
                        onClick={() => void choose(plan.key, seats)}
                        className="h-8 w-full rounded-[var(--r-sm)] px-3.5 text-[13px] font-medium text-white transition-colors duration-150 disabled:opacity-45"
                        style={{ background: 'var(--iris)' }}
                      >
                        {busy === plan.key
                          ? canSwitchPlan
                            ? 'Changing…'
                            : 'Opening…'
                          : 'Choose'}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {plansFooter(onTrial)}

          {/* ⚠ The word "cancel" is load-bearing, not padding. This one link is
              the whole cancellation path — there is no in-app cancel — and a
              customer looking for it scans for that word. Labelled "payment
              method and invoices" it reads as a billing-details link, so the
              cancel route looks absent, which is the shape regulators call a
              dark pattern and customers call a scam. Naming it costs nothing
              and removes the reading.

              ⚠ It is also a CLAIM about the Stripe Customer portal, which only
              offers cancellation when "Customers can cancel subscriptions" is
              switched on in its settings. If that is ever turned off, this
              label promises a control the page it opens does not have — the
              stale-tool-result shape from gotcha 70, pointed at a customer
              instead of at the model. Turn one off and change the other. */}
          {/* ⚠ `hasBillingAccount`, NOT `!onTrial`, and the difference is a
              cancelled customer's receipts. `subscription.deleted` stores
              `plan = 'trial'`, so someone who paid for a year and stopped read
              as a trialling company here and this link — the only route to
              their invoices and payment history — vanished at the exact moment
              they most needed it, and when a chargeback is most likely.

              A never-paid trial still hides it, correctly: there is no Stripe
              customer to open a portal session for. */}
          {status.hasBillingAccount && (
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void go(openBillingPortal, 'portal')}
              className="inline-flex items-center gap-1.5 pt-1 text-[13px] disabled:opacity-45"
              style={{ color: 'var(--iris)' }}
            >
              Manage subscription, payment method and invoices — including cancelling{' '}
              <ExternalLink className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      ) : (
        <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          An admin on your team can change the plan.
        </p>
      )}
    </div>
  );
}

/**
 * "Next charge $597 on 04/10/2026" — the two facts a paying customer came here
 * for, neither of which this panel used to state.
 *
 * ⚠ Exported and pure so the arithmetic is testable without rendering, and so
 * there is ONE place the invoice is described.
 *
 * Returns null — and the caller falls back to the meter's "Resets" line —
 * whenever any ingredient is missing or the company is not actually being
 * charged. A trial, a cancelled subscription and a stale bundle with no
 * `currentPlan` all take that path: naming a charge that is not coming is a
 * false statement about somebody's money, which is the one direction of error
 * that reaches an invoice.
 */
export function nextChargeLabel(
  status: BillingStatus,
  invoice?: UpcomingInvoice | null,
): string | null {
  if (!status.periodEnd) return null;
  // Only a LIVE subscription is charged again. `canceled` still carries a
  // period_end (the paid-through date), and calling that a charge would tell
  // someone who cancelled that money is about to leave their account.
  if (status.status !== 'active' && status.status !== 'past_due') return null;
  const plan = status.currentPlan;
  if (!plan || plan.key === 'trial') return null;

  // ⚠ STRIPE'S OWN FIGURE WINS. The arithmetic below is list price × seats and
  // knows nothing about discounts or proration — it was correct only while
  // nobody could have a coupon, and enabling promotion codes on Checkout is
  // what made it possible to overstate somebody's bill. A confident wrong
  // number about money is the one error that reaches an invoice.
  if (invoice && typeof invoice.totalUsd === 'number') {
    const when = new Date(invoice.at ?? status.periodEnd).toLocaleDateString();
    return `Next charge $${invoice.totalUsd.toLocaleString()} on ${when}`;
  }

  // ⚠ The fallback re-opens that gap for a DISCOUNTED customer during a Stripe
  // outage, and it stays anyway: it is exactly what this panel showed before,
  // it is right for everyone without a coupon, and the alternative — saying
  // nothing whenever Stripe is unreachable — removes the two facts a paying
  // customer opened this page for. Recorded rather than hidden.
  //
  // `seats` is the quantity Stripe's own webhook stored. A flat plan
  // multiplies by nothing, which is why `perSeat` decides.
  const total = plan.perSeat ? plan.priceUsd * Math.max(status.seats, 1) : plan.priceUsd;
  const when = new Date(status.periodEnd).toLocaleDateString();
  return `Next charge $${total.toLocaleString()} on ${when}`;
}

/** Says where seats come from, once, rather than on every per-seat row.
 *  Only shown while choosing — after purchase the Stripe portal owns changes
 *  and telling someone to "invite people" to change their bill would point
 *  them at the wrong control. */
function plansFooter(onTrial: boolean) {
  if (!onTrial) return null;
  return (
    <p className="pt-0.5 text-[12px]" style={{ color: 'var(--ink-subtle)' }}>
      Per-person plans bill for everyone on your team. Add or remove people in Team &amp;
      Permissions.
    </p>
  );
}
