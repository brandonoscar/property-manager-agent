/**
 * Nobody is charged for a seat without saying yes to the number.
 *
 * ⚠ **The price line was already correct and it was not enough.** This dialog
 * has rendered "5 seats × $199 = $995/month — $199 more than today" since it
 * shipped, and a founder still invited four people and found a $995 bill he
 * had never agreed to (2026-09-09). A figure rendered NEXT TO a button is a
 * notice; the button says "Send invite" and reads as free. Disclosure and
 * consent are different things and only one of them was here.
 *
 * ⚠ **The guard is on the ACTION, not only on the disabled button.** A
 * disabled control is the explanation. `LeasingComplianceGate` is this repo's
 * own record of the difference (gotcha 67): a UI gate cannot bind anything,
 * and shipping only the panel would have been theatre.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/company', async () => {
  const actual = await vi.importActual<typeof import('../services/company')>('../services/company');
  return { ...actual, inviteMembers: vi.fn() };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const fetchSeatQuote = vi.fn();
vi.mock('../services/billing', async () => {
  const actual = await vi.importActual<typeof import('../services/billing')>('../services/billing');
  return { ...actual, fetchSeatQuote: (...a: unknown[]) => fetchSeatQuote(...a) };
});

import { inviteMembers, type InviteResult, type TeamRole } from '../services/company';
import type { CurrentPlan, SeatQuote } from '../services/billing';
import { InviteUsersDialog } from './InviteUsersDialog';

const invite = vi.mocked(inviteMembers);

const PRO: CurrentPlan = {
  key: 'pro',
  label: 'Pro',
  priceUsd: 199,
  perSeat: true,
  turnsIncluded: 400,
  maxSeats: 50,
  tagline: '',
  highlights: [],
  features: [],
};

const FLAT: CurrentPlan = { ...PRO, key: 'scale', label: 'Scale', priceUsd: 500, perSeat: false };

const BILLABLE: SeatQuote = {
  adding: 1,
  seatsNow: 4,
  seatsAfter: 5,
  monthlyNowUsd: 796,
  monthlyAfterUsd: 995,
  billable: true,
  proratedNowUsd: 72.97,
  daysLeft: 11,
  periodEnd: null,
  reason: 'per_seat',
};

beforeEach(() => {
  vi.clearAllMocks();
  invite.mockImplementation(async (rows) =>
    rows.map(
      (r): InviteResult => ({
        email: r.email,
        role: r.role as TeamRole,
        status: 'invited',
        emailSent: true,
        error: null,
      }),
    ),
  );
  fetchSeatQuote.mockResolvedValue(BILLABLE);
});

function open(plan: CurrentPlan | null, memberCount = 4) {
  return render(
    <InviteUsersDialog onClose={() => {}} memberCount={memberCount} plan={plan} />,
  );
}

const typeEmail = (value: string, nth = 0) => {
  const field = screen.getAllByPlaceholderText(/teammate@/i)[nth];
  fireEvent.change(field, { target: { value } });
};

const send = () => fireEvent.click(screen.getByText(/^Send/));
const consent = () => screen.queryByTestId('invite-billing-consent');

describe('a paid seat', () => {
  it('asks for agreement and refuses the invite until it is given', async () => {
    open(PRO);
    typeEmail('dana@example.com');
    await waitFor(() => expect(consent()).toBeTruthy());

    send();

    // ⚠ The refusal is what this whole change is. A dialog that merely
    // disabled the button would pass a test asserting `not.toHaveBeenCalled`
    // and still let any other caller through.
    expect(invite).not.toHaveBeenCalled();
    expect(screen.getByText(/tick the box/i)).toBeTruthy();

    fireEvent.click(consent()!);
    send();
    await waitFor(() => expect(invite).toHaveBeenCalledTimes(1));
  });

  it('names the monthly figure being agreed to, not just "your bill"', async () => {
    open(PRO);
    typeEmail('dana@example.com');
    await waitFor(() => expect(consent()).toBeTruthy());
    // Agreeing to an unnamed change is the same non-consent in a checkbox.
    // ⚠ Matched on the CONSENT LABEL's own text, not on a page-wide
    // search: the monthly figure also appears in the disclosure line
    // above, so a loose match would pass with the checkbox saying
    // nothing — the exact gap this change exists to close.
    expect(consent()!.closest('label')!.textContent).toContain('$995/month');
  });

  it('states what is charged TODAY, which the monthly line never did', async () => {
    open(PRO);
    typeEmail('dana@example.com');
    // Stripe prorates a mid-period seat change, so the first thing that
    // actually happens is a part-month charge — and being surprised by a
    // smaller number is still being surprised.
    await waitFor(() => expect(screen.getByText(/\$72\.97 today/)).toBeTruthy());
    expect(screen.getByText(/11 days left/)).toBeTruthy();
  });

  it('withdraws the agreement when the number of people changes', async () => {
    open(PRO);
    typeEmail('dana@example.com');
    await waitFor(() => expect(consent()).toBeTruthy());
    fireEvent.click(consent()!);
    expect((consent() as HTMLInputElement).checked).toBe(true);

    // ⚠ Consent for one person must not carry to a charge three times the
    // size — that is the original complaint re-created inside the fix.
    fireEvent.click(screen.getByText(/Add another/));
    typeEmail('sam@example.com', 1);
    await waitFor(() => expect((consent() as HTMLInputElement).checked).toBe(false));

    send();
    expect(invite).not.toHaveBeenCalled();
  });
});

describe('an invite that costs nothing', () => {
  it('does not ask on a flat plan', async () => {
    fetchSeatQuote.mockResolvedValue({ ...BILLABLE, billable: false, reason: 'flat_plan' });
    open(FLAT);
    typeEmail('dana@example.com');
    // ⚠ Friction on a free action is theatre, and it trains people to tick
    // the box without reading — which is what makes the paid one worthless.
    await waitFor(() => expect(invite).toHaveBeenCalledTimes(0));
    send();
    await waitFor(() => expect(invite).toHaveBeenCalledTimes(1));
    expect(consent()).toBeNull();
  });
});

describe('when the quote cannot be fetched', () => {
  it('still asks, rather than sending a charge nobody saw', async () => {
    // ⚠ Fails toward ASKING. Over-asking costs one click; under-asking is the
    // bug being fixed, and a billing endpoint being briefly unreachable is
    // not a reason to charge somebody silently.
    fetchSeatQuote.mockResolvedValue(null);
    open(PRO);
    typeEmail('dana@example.com');
    await waitFor(() => expect(consent()).toBeTruthy());

    send();
    expect(invite).not.toHaveBeenCalled();
  });

  it('says nothing about a prorated amount it could not compute', async () => {
    fetchSeatQuote.mockResolvedValue({ ...BILLABLE, proratedNowUsd: null, daysLeft: null });
    open(PRO);
    typeEmail('dana@example.com');
    await waitFor(() => expect(consent()).toBeTruthy());
    // A missing figure rendered as $0.00 would say the invite is free.
    expect(screen.queryByText(/today for the/)).toBeNull();
  });
});
