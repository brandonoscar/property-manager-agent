/**
 * Billing API client.
 *
 * ⚠ `block` and `blockMessage` come STRAIGHT from the server and are never
 * re-derived here. Gotcha 73 is this repo's record of the alternative: the SMS
 * status endpoint handed back two booleans, the client guessed which blocker
 * had fired, and the panel's explanation drifted from the refusal it was
 * explaining. Two booleans cannot separate "your trial ended" from "you're out
 * of questions", and those need opposite actions from the user.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type BillingBlock =
  | 'trial_expired'
  | 'subscription_canceled'
  | 'turns_exhausted'
  | 'payment_failed'
  /** The FIRST payment hasn't cleared — a card in 3-D Secure, or an async
   *  payment method. Not a cancellation, and `gate.py` refuses to call it one:
   *  telling somebody mid-bank-redirect that their subscription "has ended"
   *  sends them to start a second one. */
  | 'payment_incomplete'
  /** Stripe exhausted dunning and stopped collecting, leaving an open invoice
   *  on a subscription that still exists. Not a cancellation: the plan picker
   *  would try to mint a second subscription, which checkout 409s on, so the
   *  server's message points at the portal instead. Reachable only since
   *  migration 0097 — the status column could not store `unpaid`, so the row
   *  kept `past_due` and the product was served free. */
  | 'subscription_unpaid';

/**
 * The standing warning BEFORE a block — a trial about to end, a card that
 * stopped working.
 *
 * ⚠ Server-owned, and deliberately NOT derivable here. The panel used to be
 * able to compute "trial ends soon" from `trialEndsAt`; it can never compute
 * the payment grace window, because the remaining days come from
 * `past_due_since` plus a constant the client does not have. A client that
 * derives half of this and reads the other half is a client whose countdown
 * disagrees with the gate that actually enforces it. Same rule as `block`.
 */
export type BillingNoticeReason = 'trial_ending' | 'payment_failed';
export type BillingNoticeSeverity = 'info' | 'warning' | 'critical';

export interface BillingNotice {
  reason: BillingNoticeReason;
  severity: BillingNoticeSeverity;
  /** Names the deadline AND the fix. Render verbatim — do not summarise. */
  message: string;
  /** Label for the action button, or null when there is nothing to do. */
  action: string | null;
}

/**
 * Every plan key the ladder sells, and the trial nobody buys.
 *
 * ⚠ TypeScript cannot import `billing/plans.py`, so this union is a SECOND
 * copy of the key set and drifts silently — a narrower union does not fail,
 * it just makes `status.plan === plan.key` quietly impossible and lets
 * `startCheckout` accept a key the server rejects. That is exactly how
 * `PlanOption.key` sat at `'starter' | 'pro'` while the server had started
 * serving `scale`. `test_billing_plans.py` reads BOTH sides (gotcha 28) and
 * fails the moment they disagree.
 */
export type PaidPlanKey = 'starter' | 'pro' | 'scale';
export type PlanKey = 'trial' | PaidPlanKey;

/** An enforced capability, not a bullet. Mirrors `plans.FEATURE_CRM`. */
export type PlanFeature = 'crm';

/**
 * One purchasable plan, as facts. The panel formats the sentence; every
 * NUMBER in it comes from here.
 *
 * ⚠ Do not reintroduce a local price table. The panel carried one until
 * 2026-09-02 — hand-copied from `billing/plans.py`, with nothing holding the
 * two in agreement — so a price change on the server would have shown the old
 * figure on the card while Stripe charged the new one.
 */
export interface PlanOption {
  key: PaidPlanKey;
  label: string;
  priceUsd: number;
  perSeat: boolean;
  turnsIncluded: number;
  /** The plan's seat ceiling — clamps BOTH the invoice and the allowance.
   *
   *  ⚠ Load-bearing on the invite screen: past this number a teammate is
   *  neither billed for nor given an allowance, and on the trial that number
   *  is 5. Server-owned like every other figure here. */
  maxSeats: number;
  /** Who it's for, in one line. Server-owned, like every other word here. */
  tagline: string;
  /** What you get. ⚠ Carries no price and no allowance — those are rendered
   *  from `priceUsd` / `turnsIncluded`, so there is exactly one copy. */
  highlights: string[];
  /** Enforced capabilities this plan unlocks. The same set the API refuses
   *  on, so a card cannot promise a section the server declines. */
  features: PlanFeature[];
}

export interface BillingStatus {
  plan: PlanKey;
  planLabel: string;
  status: string;
  seats: number;
  turnsUsed: number;
  turnsAllowed: number;
  turnsRemaining: number;
  trialEndsAt: string | null;
  periodEnd: string | null;
  pastDue: boolean;
  block: BillingBlock | null;
  blockMessage: string | null;
  /** False while billing is wired but not yet turning anyone away. */
  enforced: boolean;
  /**
   * Has this company ever been a Stripe customer?
   *
   * ⚠ NOT the same question as `plan !== 'trial'`, and the difference cost a
   * cancelled customer their receipts. `customer.subscription.deleted` stores
   * `plan = 'trial'` deliberately (fall back to the smallest allowance, never
   * `pro`), so a company that paid for a year and stopped is indistinguishable
   * from one that never paid — and the panel hid the Stripe-portal link on
   * exactly that reading, at the moment somebody most needs invoices and a
   * chargeback is most likely.
   *
   * ⚠ Server-owned, like `block` and `notice`. Do NOT reconstruct it from
   * `plan` + `status`: two fields the client combines are two fields that
   * drift from what the server means (gotcha 73).
   */
  hasBillingAccount: boolean;
  /**
   * What the CURRENT plan unlocks — the server's own answer.
   *
   * ⚠ Never re-derive this by finding `plan` in `plans`: that list holds only
   * purchasable plans, so a company on the trial finds no row for itself and
   * would read as having nothing. Same rule as `block` (gotcha 73) — the
   * enforcement path and the UI read one field.
   */
  features: PlanFeature[];
  /** What can be bought, priced by the server. */
  plans: PlanOption[];
  /**
   * THIS company's plan as facts — the trial included, which `plans` omits.
   *
   * ⚠ Null on an older backend. Callers must handle that by saying nothing
   * about cost rather than guessing: the invite screen quotes a per-seat price
   * to an admin, and a figure invented by the client is the hand-copied price
   * table `PlanOption` exists to abolish, wearing a different hat.
   *
   * ⚠ Do NOT reconstruct this by finding `plan` in `plans`. That list is the
   * purchasable ladder with no trial row, so a trialling company — the one
   * whose seat cap is tight enough to matter — would find nothing.
   */
  currentPlan: CurrentPlan | null;
  /** The standing warning before a block, or null. See `BillingNotice`. */
  notice: BillingNotice | null;
  /**
   * Is there a subscription `/billing/change-plan` can actually re-price?
   *
   * ⚠ NOT the same question as `plan !== 'trial'`, and the difference is a
   * dead end for somebody trying to start paying. A first payment that expired
   * leaves `plan` on whatever they picked and `status` on
   * `incomplete_expired`, so the panel read them as a paying customer, headed
   * the section "Change plan", and every button pushed a re-price at a
   * subscription that never charged — surfacing as "Could not change the plan
   * with Stripe" when nothing is wrong with Stripe.
   *
   * ⚠ Server-owned, like `block`, `hasBillingAccount` and `notice`. Do NOT
   * reconstruct it from `plan` + `status`: the set of statuses a subscription
   * never returns from lives beside Stripe's own vocabulary in
   * `billing/stripe_status.py`, where a migration's CHECK constraint is
   * already pinned against it. A second copy here is the drift gotcha 28
   * exists to stop, on the field that decides whether somebody is charged
   * twice.
   *
   * ⚠ Optional on the wire: a backend deployed before this field shipped
   * omits it, and absent must read as FALSE — offer Checkout, whose own 409
   * refuses a second subscription — rather than offering a switch that 502s.
   */
  canChangePlan: boolean;
}

/** The company's own plan. Same facts as `PlanOption`, but the key can be
 *  `trial` — which is never purchasable and so never appears in `plans`. */
export type CurrentPlan = Omit<PlanOption, 'key'> & { key: PlanKey };

/** Does this company's plan include `feature`? ⚠ Defaults to TRUE while
 *  `status` is null (still loading, or the read failed), matching the
 *  server's fail-open posture — a blip must not black out a section somebody
 *  is paying for. Callers that need "still loading" as its own state should
 *  check `status === null` themselves. */
export function hasFeature(status: BillingStatus | null, feature: PlanFeature): boolean {
  if (!status) return true;
  return status.features.includes(feature);
}

interface StatusWire {
  plan: BillingStatus['plan'];
  plan_label: string;
  status: string;
  seats: number;
  turns_used: number;
  turns_allowed: number;
  turns_remaining: number;
  trial_ends_at: string | null;
  period_end: string | null;
  past_due: boolean;
  block: BillingBlock | null;
  block_message: string | null;
  enforced: boolean;
  /** Optional on the wire: a backend deployed before this field shipped omits
   *  it, and absent must read as "no billing account" — hiding a link costs a
   *  click, while offering a portal session Stripe cannot open is an error in
   *  the customer's face. */
  has_billing_account?: boolean;
  /** Optional on the wire: a backend deployed before this field shipped omits
   *  it, and absent must read as "nothing to re-price" — Checkout's 409 is a
   *  message the customer can act on, while a switch against a subscription
   *  that cannot be re-priced is a 502 blaming Stripe. */
  can_change_plan?: boolean;
  features?: PlanFeature[];
  /** Optional on the wire: a backend deployed before this field shipped
   *  simply omits it, and a missing warning must read as "nothing to warn
   *  about" rather than crashing the panel that shows the plan. */
  notice?: {
    reason: BillingNoticeReason;
    severity: BillingNoticeSeverity;
    message: string;
    action?: string | null;
  } | null;
  plans?: PlanWire[];
  current_plan?: PlanWire | null;
}

interface PlanWire {
  key: PlanKey;
  label: string;
  price_usd: number;
  per_seat: boolean;
  turns_included: number;
  /** Optional on the wire — see `planFromWire` for why absent is not zero. */
  max_seats?: number;
  tagline?: string;
  highlights?: string[];
  features?: PlanFeature[];
}

/**
 * One wire plan → one client plan.
 *
 * ⚠ `max_seats` absent degrades to `Infinity`, NOT to 0 and not to 50. An
 * older backend that omits it means "this build cannot tell you where the
 * ceiling is", and the invite screen's job at that point is to say nothing
 * about a cap — whereas 0 would refuse every invite and a hardcoded 50 would
 * promise a trialling company ten times the seats it actually has. Infinity is
 * the value that makes every "are we past the cap" comparison false, so the
 * caveat simply does not render.
 */
function planFromWire(p: PlanWire): CurrentPlan {
  return {
    key: p.key,
    label: p.label,
    priceUsd: p.price_usd,
    perSeat: p.per_seat,
    turnsIncluded: p.turns_included,
    maxSeats: p.max_seats ?? Number.POSITIVE_INFINITY,
    // Copy, unlike price, degrades to empty and simply renders nothing.
    tagline: p.tagline ?? '',
    highlights: p.highlights ?? [],
    features: p.features ?? [],
  };
}

export async function fetchBillingStatus(): Promise<BillingStatus> {
  const res = await apiFetch('/api/v1/billing/status');
  if (!res.ok) throw new Error('Could not load your billing details.');
  const w: StatusWire = await res.json();
  return {
    plan: w.plan,
    planLabel: w.plan_label,
    status: w.status,
    seats: w.seats,
    turnsUsed: w.turns_used,
    turnsAllowed: w.turns_allowed,
    turnsRemaining: w.turns_remaining,
    trialEndsAt: w.trial_ends_at,
    periodEnd: w.period_end,
    pastDue: w.past_due,
    block: w.block,
    blockMessage: w.block_message,
    enforced: w.enforced,
    hasBillingAccount: w.has_billing_account ?? false,
    canChangePlan: w.can_change_plan ?? false,
    // ⚠ Absent (an older backend) degrades to EVERY feature, not to none.
    // A missing field must not black out the Leasing section for a Pro
    // customer whose bundle is one deploy behind — same fail-open direction
    // as `hasFeature` and as the server. The plans list below degrades the
    // other way for the opposite reason: a wrong PRICE is a billing dispute.
    features: w.features ?? ['crm'],
    // ⚠ Absent degrades to NO notice, and that is the safe direction here
    // even though it is the opposite of `features` above. Inventing a warning
    // is a false alarm about somebody's money; missing one costs a banner the
    // server will send on the next deploy. Do NOT synthesise one from
    // `trialEndsAt` — see `BillingNotice`.
    notice: w.notice
      ? {
          reason: w.notice.reason,
          severity: w.notice.severity,
          message: w.notice.message,
          action: w.notice.action ?? null,
        }
      : null,
    // ⚠ Absent (an older backend) degrades to NO plans, never to a local
    // price table. The panel then says it could not load them. Showing a
    // figure this build happens to remember is how the drift this field
    // exists to remove comes back — and a wrong price is worse than none.
    plans: (w.plans ?? []).map((p) => planFromWire(p) as PlanOption),
    // ⚠ Absent degrades to NULL, never to a plan this build guessed at. The
    // one caller quotes a per-seat price to an admin about to spend money;
    // "we don't know yet" is the only honest answer a stale bundle can give.
    currentPlan: w.current_plan ? planFromWire(w.current_plan) : null,
  };
}

/**
 * What Stripe says the next bill actually is.
 *
 * ⚠ A SEPARATE call from `fetchBillingStatus`, and the split is deliberate on
 * the server too: `/billing/status` is fetched by `TrialBanner` on every page,
 * so folding a Stripe round trip into it would put Stripe's latency and
 * Stripe's outages in front of every page load.
 *
 * ⚠ Every field can be null and null means UNKNOWN, never zero. No
 * subscription, a Stripe outage and a 404 all arrive the same way, and
 * rendering any of them as "$0.00 due" is a false statement about money. The
 * panel falls back to its own estimate, which is what it showed before this
 * existed.
 */
export interface UpcomingInvoice {
  totalUsd: number | null;
  at: string | null;
  /** A coupon or promotion code is reducing the total. */
  discounted: boolean;
  /** The card on file expires BEFORE that charge, so the renewal will decline.
   *
   *  ⚠ `false` covers both "the card is fine" and "we could not tell" — the
   *  one place this shape collapses a three-valued answer, and safe only in
   *  this direction. Under-warning costs one declined renewal the dunning path
   *  already handles; over-warning is a false alarm about somebody's money. */
  cardExpiresFirst: boolean;
  /** `09/2026` — the format printed on the card, so the reader can compare it
   *  without translating. Null whenever there is nothing to warn about. */
  cardExpiry: string | null;
}

export async function fetchUpcomingInvoice(): Promise<UpcomingInvoice> {
  const empty: UpcomingInvoice = {
    totalUsd: null,
    at: null,
    discounted: false,
    cardExpiresFirst: false,
    cardExpiry: null,
  };
  try {
    const res = await apiFetch('/api/v1/billing/upcoming-invoice');
    if (!res.ok) return empty;
    const w = (await res.json()) as {
      total_usd?: number | null;
      at?: string | null;
      discounted?: boolean;
      card_expires_first?: boolean;
      card_expiry?: string | null;
    };
    // ⚠ `?? null`, not `?? 0`. An older backend that does not serve this route
    // 404s and lands above; a newer one that cannot read Stripe sends nulls.
    // Both mean "cannot say".
    return {
      totalUsd: typeof w.total_usd === 'number' ? w.total_usd : null,
      at: w.at ?? null,
      discounted: Boolean(w.discounted),
      // An older backend serves neither field, which reads as false/null —
      // no warning, which is the correct behaviour for a build that cannot
      // answer the question.
      cardExpiresFirst: Boolean(w.card_expires_first),
      cardExpiry: w.card_expiry ?? null,
    };
  } catch {
    // ⚠ Never throws. A billing panel that fails to render because a
    // projection was unavailable is worse than one showing yesterday's
    // estimate.
    return empty;
  }
}

/**
 * Move an EXISTING subscription onto a different plan. Admin only.
 *
 * ⚠ NOT `startCheckout`, and the distinction is a double-charge. Checkout
 * CREATES a subscription; Stripe will happily give one customer two, and the
 * `customer.subscription.created` webhook then overwrites our stored
 * subscription id with the new one — leaving the original billing forever with
 * nothing in this app able to see it. The server refuses a second checkout with
 * a 409 for the same reason; this is the path that actually changes a plan.
 *
 * ⚠ Returns the plan we ASKED for, not a URL and not the truth. The webhook is
 * the only writer of our billing row, so the caller must re-read
 * `fetchBillingStatus()` rather than trusting this body.
 */
export async function changePlan(plan: PaidPlanKey): Promise<{ plan: PlanKey; seats: number }> {
  const res = await apiFetch('/api/v1/billing/change-plan', {
    method: 'POST',
    body: JSON.stringify({ plan }),
  });
  const body = (await res.json()) as { plan: PlanKey; seats: number };
  return { plan: body.plan, seats: body.seats };
}

/** Returns a Stripe Checkout URL to redirect to. Admin only. */
export async function startCheckout(plan: PaidPlanKey, seats: number): Promise<string> {
  const res = await apiFetch('/api/v1/billing/checkout', {
    method: 'POST',
    body: JSON.stringify({ plan, seats }),
  });
  if (!res.ok) {
    // Surfaces the server's own words — it distinguishes "not an admin" from
    // "that plan isn't available yet", and those need different responses.
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || 'Could not start checkout.');
  }
  const { url } = await res.json();
  return url as string;
}

/** Returns a Stripe billing-portal URL to redirect to. Admin only. */
export async function openBillingPortal(): Promise<string> {
  const res = await apiFetch('/api/v1/billing/portal', { method: 'POST' });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || 'Could not open the billing portal.');
  }
  const { url } = await res.json();
  return url as string;
}

/**
 * What inviting `adding` more people does to this company's bill.
 *
 * ⚠ **The PRORATION is the part only the server can answer.** The monthly
 * figures are derivable on the client from the plan (and `seatImpact` renders
 * them), but "what you are charged today for the rest of this period" needs
 * the billing period, which lives in `helixis_billing` and is not on
 * `/billing/status`. Computing it client-side would mean shipping a second
 * copy of the period arithmetic — the drift this repo has now been bitten by
 * five times.
 *
 * ⚠ **`billable` is the SERVER'S answer, not a re-derivation.** Gotcha 73 is
 * this repo shipping a status endpoint whose two booleans the client
 * re-combined into a different verdict than the send path used. The dialog
 * gates consent on this field.
 *
 * Never throws — a quote that cannot be fetched must not stop somebody
 * inviting a colleague; the caller falls back to asking for consent anyway.
 */
export interface SeatQuote {
  adding: number;
  seatsNow: number;
  seatsAfter: number;
  monthlyNowUsd: number;
  monthlyAfterUsd: number;
  billable: boolean;
  proratedNowUsd: number | null;
  daysLeft: number | null;
  periodEnd: string | null;
  reason: string;
}

export async function fetchSeatQuote(adding: number): Promise<SeatQuote | null> {
  if (demoMode) {
    return {
      adding,
      seatsNow: 4,
      seatsAfter: 4 + adding,
      monthlyNowUsd: 796,
      monthlyAfterUsd: 796 + adding * 199,
      billable: true,
      proratedNowUsd: Math.round(adding * 199 * (11 / 30) * 100) / 100,
      daysLeft: 11,
      periodEnd: null,
      reason: 'per_seat',
    };
  }
  try {
    const res = await apiFetch(`/api/v1/billing/seat-quote?adding=${encodeURIComponent(adding)}`);
    if (!res.ok) return null;
    const w = (await res.json()) as Record<string, unknown>;
    return {
      adding: typeof w.adding === 'number' ? w.adding : adding,
      seatsNow: typeof w.seats_now === 'number' ? w.seats_now : 0,
      seatsAfter: typeof w.seats_after === 'number' ? w.seats_after : 0,
      monthlyNowUsd: typeof w.monthly_now_usd === 'number' ? w.monthly_now_usd : 0,
      monthlyAfterUsd: typeof w.monthly_after_usd === 'number' ? w.monthly_after_usd : 0,
      billable: w.billable === true,
      // ⚠ A missing figure stays null. Rendering it as $0.00 would tell
      // somebody an invite is free when we simply could not price it.
      proratedNowUsd: typeof w.prorated_now_usd === 'number' ? w.prorated_now_usd : null,
      daysLeft: typeof w.days_left === 'number' ? w.days_left : null,
      periodEnd: typeof w.period_end === 'string' ? w.period_end : null,
      reason: typeof w.reason === 'string' ? w.reason : 'unknown',
    };
  } catch {
    return null;
  }
}
