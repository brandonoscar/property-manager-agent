/**
 * `fetchBillingStatus` — the wire transform, and specifically what it does
 * when the server sends NO plan catalog.
 *
 * This file exists because a mutation survived. `BillingPanel.test.tsx` mocks
 * this whole module, so it proves what the panel does with a `plans` array it
 * is handed and says nothing about where that array comes from. Reinstating a
 * hardcoded fallback here —
 *
 *     plans: (w.plans ?? [{ key: 'starter', price_usd: 199, … }]).map(…)
 *
 * — left all 17 tests green while putting a client-invented "$199 / user /
 * month" back on the button that opens a Stripe Checkout. That is the exact
 * drift the server-side catalog was added to remove, reintroduced one layer
 * below every guard.
 *
 * ⚠ The absent case is the one that matters. A present `plans` array is
 * exercised by every panel test; `plans` MISSING is what an older backend
 * sends, and a client that fills the gap from memory is asserting a price
 * nobody charges (gotcha 55 — decline to assert rather than guess).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import { fetchBillingStatus, fetchUpcomingInvoice } from './billing';

const apiFetchMock = vi.mocked(apiFetch);

/** The wire shape, minus `plans` — callers add it when they mean to. */
function wire(over: Record<string, unknown> = {}) {
  return {
    plan: 'trial',
    plan_label: 'Free trial',
    status: 'trialing',
    seats: 1,
    turns_used: 3,
    turns_allowed: 150,
    turns_remaining: 147,
    trial_ends_at: null,
    period_end: null,
    past_due: false,
    block: null,
    block_message: null,
    enforced: true,
    ...over,
  };
}

function respond(body: unknown, ok = true) {
  apiFetchMock.mockResolvedValue({
    ok,
    json: async () => body,
  } as unknown as Response);
}

beforeEach(() => {
  apiFetchMock.mockReset();
});

describe('the plan catalog is the server’s, or it is empty', () => {
  it('carries the served plans through, renaming the wire fields', async () => {
    // Deliberately not the shipped figures: a transform that ignored the
    // payload and returned remembered values would still look right.
    respond(
      wire({
        plans: [
          {
            key: 'starter',
            label: 'Starter',
            price_usd: 243,
            per_seat: true,
            turns_included: 675,
          },
        ],
      }),
    );
    const status = await fetchBillingStatus();
    // ⚠ The wire here omits `tagline` / `highlights` / `features` — an older
    // backend. COPY degrades to empty (the card renders nothing extra) while
    // the PRICE fields are carried through, which is the asymmetry the next
    // test pins: a missing sentence is a gap, a missing price is a dispute.
    expect(status.plans).toEqual([
      {
        key: 'starter',
        label: 'Starter',
        priceUsd: 243,
        perSeat: true,
        turnsIncluded: 675,
        // ⚠ INFINITY, not 0 and not 50. The wire above omits `max_seats`, and
        // an omission means "this build cannot tell you where the ceiling is".
        // Infinity makes every "are we past the cap" comparison false, so the
        // invite dialog simply says nothing about a limit — whereas 0 would
        // refuse every invite and a remembered 50 would promise a trialling
        // company ten times the seats it actually has.
        maxSeats: Number.POSITIVE_INFINITY,
        tagline: '',
        highlights: [],
        features: [],
      },
    ]);
  });

  it('carries a plan\'s copy and capabilities when the server sends them', async () => {
    respond(
      wire({
        plans: [
          {
            key: 'pro',
            label: 'Pro',
            price_usd: 811,
            per_seat: false,
            turns_included: 2450,
            tagline: 'For a team.',
            highlights: ['Everything in Starter'],
            features: ['crm'],
          },
        ],
      }),
    );
    const status = await fetchBillingStatus();
    expect(status.plans[0].tagline).toBe('For a team.');
    expect(status.plans[0].highlights).toEqual(['Everything in Starter']);
    expect(status.plans[0].features).toEqual(['crm']);
  });

  it('assumes every capability when the server sends no features at all', async () => {
    // ⚠ The OPPOSITE default to `plans`, deliberately. A bundle one deploy
    // behind a backend that has not shipped this field must not black out the
    // Leasing section for a customer paying for it — `gate.py`'s rule 1, and
    // the same posture the server and `hasFeature` both take. The plans list
    // degrades to empty for the opposite reason: a wrong price is a billing
    // dispute, and there is no safe guess.
    respond(wire());
    const status = await fetchBillingStatus();
    expect(status.features).toEqual(['crm']);
  });

  it('returns NO plans when the server sends none, inventing no price', async () => {
    // The mutation this file was written for. An older backend omits `plans`;
    // the honest result is an empty catalog and a panel that says it could
    // not load them — never a figure this build happens to remember.
    respond(wire());
    const status = await fetchBillingStatus();
    expect(status.plans).toEqual([]);
  });

  it('invents no price when the server sends an explicitly empty catalog', async () => {
    respond(wire({ plans: [] }));
    expect((await fetchBillingStatus()).plans).toEqual([]);
  });
});

describe('the block reaches the client as the server wrote it', () => {
  it('passes block and blockMessage through without re-deriving either', async () => {
    // Gotcha 73: two booleans cannot separate "your trial ended" from "you're
    // out of questions", and those need opposite actions from the user.
    const message = 'Your 14-day trial has ended. Settings → Billing to pick a plan.';
    respond(wire({ block: 'trial_expired', block_message: message }));
    const status = await fetchBillingStatus();
    expect(status.block).toBe('trial_expired');
    expect(status.blockMessage).toBe(message);
  });

  it('reports a failed read as an error rather than a plan-less success', async () => {
    // A 500 rendered as "no plans, no block" would read to the customer as a
    // healthy account with nothing to buy.
    respond({}, false);
    await expect(fetchBillingStatus()).rejects.toThrow(/billing details/);
  });
});


// ── fetchUpcomingInvoice ────────────────────────────────────────────
//
// ⚠ Same reason as the file above it: `BillingPanel.test.tsx` mocks this whole
// module, so it proves what the panel does with an invoice it is HANDED and
// says nothing about how one is read off the wire. Two mutations survived the
// panel battery for exactly that — `?? 0` in place of a null check, and a
// throw where the catch is — and both are the "unknown must not read as zero"
// rule pointed at somebody's bill.

describe('the next invoice is Stripe’s answer, or it is nothing', () => {
  it('reads the served total and date', async () => {
    respond({ total_usd: 448.5, at: '2026-10-04T00:00:00Z', discounted: true });
    expect(await fetchUpcomingInvoice()).toEqual({
      totalUsd: 448.5,
      at: '2026-10-04T00:00:00Z',
      discounted: true,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
  });

  it('keeps a REAL zero, because a fully-discounted period is an answer', async () => {
    // ⚠ The pair this test exists for. `$0.00 due` and `we cannot say` must
    // stay distinguishable in both directions, and a truthiness check
    // collapses them — which is why the transform reads the TYPE.
    respond({ total_usd: 0, at: null, discounted: true });
    expect((await fetchUpcomingInvoice()).totalUsd).toBe(0);
  });

  it('reads an absent total as UNKNOWN, never as zero', async () => {
    // A `?? 0` here would put "$0.00" on the panel for a customer who is
    // about to be charged — a false statement about money, made by the layer
    // furthest from anyone who would notice.
    for (const body of [{}, { total_usd: null }, { total_usd: 'lots' }]) {
      respond(body);
      expect((await fetchUpcomingInvoice()).totalUsd, JSON.stringify(body)).toBeNull();
    }
  });

  it('says nothing when the server refuses', async () => {
    // 404 is the ORDINARY case — an older backend with no such route, and a
    // customer with no subscription, both land here.
    respond({ detail: 'not found' }, false);
    expect(await fetchUpcomingInvoice()).toEqual({
      totalUsd: null,
      at: null,
      discounted: false,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
  });

  it('never throws, because a projection must not break the billing page', async () => {
    // ⚠ A caller that has to try/catch a read-only figure is one that will
    // eventually forget to, and the cost lands on the page a paying customer
    // opened to see what they owe.
    apiFetchMock.mockRejectedValue(new Error('offline'));
    await expect(fetchUpcomingInvoice()).resolves.toEqual({
      totalUsd: null,
      at: null,
      discounted: false,
      cardExpiresFirst: false,
      cardExpiry: null,
    });

    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => {
        throw new Error('not json');
      },
    } as unknown as Response);
    await expect(fetchUpcomingInvoice()).resolves.toEqual({
      totalUsd: null,
      at: null,
      discounted: false,
      cardExpiresFirst: false,
      cardExpiry: null,
    });
  });
});

describe('the card that will not survive the next charge', () => {
  it('carries the warning and the expiry through', async () => {
    respond({
      total_usd: 199,
      at: '2026-10-04T00:00:00Z',
      card_expires_first: true,
      card_expiry: '09/2026',
    });
    const got = await fetchUpcomingInvoice();
    expect(got.cardExpiresFirst).toBe(true);
    expect(got.cardExpiry).toBe('09/2026');
  });

  it('warns nobody when an older backend serves neither field', async () => {
    // ⚠ The direction that matters on a deploy skew. Absent must read as "no
    // warning", never as a warning with a missing date — the panel would then
    // render "Card expires — update it" with a blank, which reads as a defect
    // rather than as information.
    respond({ total_usd: 199, at: '2026-10-04T00:00:00Z' });
    const got = await fetchUpcomingInvoice();
    expect(got.cardExpiresFirst).toBe(false);
    expect(got.cardExpiry).toBeNull();
  });
});
