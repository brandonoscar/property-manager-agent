/**
 * The invite dialog: refusals you can see, and a price that is the server's.
 *
 * ⚠ THIS FILE REPLACES `InviteTeammate.test.tsx` AND CARRIES EVERY ONE OF ITS
 * ASSERTIONS FORWARD. That file existed because of a real report — "for some
 * reason when I'm typing it, it doesn't feel like it's actually sending
 * anything" (founder, 2026-09-04). It wasn't: the button was disabled until the
 * address parsed, and `handleInvite` ALSO opened with a bare
 * `if (!emailValid || loading) return;`, so a half-typed address plus Enter did
 * nothing at all — no error, no movement, no reason. That is gotcha 44's rule
 * ("a refusal the user cannot perceive is indistinguishable from a send")
 * arriving in a form instead of the composer.
 *
 * Replacing the form did NOT make that rule go away, so every one of those
 * eight tests has a counterpart below, adapted to a multi-row form. Deleting
 * them along with the component they described would have quietly dropped the
 * bound; the assertion is about behaviour the product still has.
 *
 * ⚠ These tests assert on what a PERSON can perceive — text on screen, a
 * focused field, a call that did or did not happen. Asserting that some
 * internal `valid` flag is false would pass against the broken version, because
 * the broken version computed it correctly and then said nothing.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/company', async () => {
  const actual = await vi.importActual<typeof import('../services/company')>(
    '../services/company',
  );
  return { ...actual, inviteMembers: vi.fn() };
});

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import { inviteMembers, type InviteResult, type TeamRole } from '../services/company';
import type { CurrentPlan } from '../services/billing';
import { InviteUsersDialog, billableSeats, parseAddresses, seatImpact } from './InviteUsersDialog';

const invite = vi.mocked(inviteMembers);

const ok = (email: string, role: TeamRole = 'member'): InviteResult => ({
  email,
  role,
  status: 'invited',
  emailSent: true,
  error: null,
});

beforeEach(() => {
  vi.clearAllMocks();
  invite.mockImplementation(async (rows) => rows.map((r) => ok(r.email, r.role)));
});

function open(props: Partial<Parameters<typeof InviteUsersDialog>[0]> = {}) {
  return render(
    <InviteUsersDialog onClose={props.onClose ?? (() => {})} memberCount={props.memberCount ?? 1} plan={props.plan ?? null} onInvited={props.onInvited} />,
  );
}

const fields = () => screen.getAllByPlaceholderText('teammate@company.com');
const type = (value: string, i = 0) =>
  fireEvent.change(fields()[i], { target: { value } });
const sendButton = () => screen.getByRole('button', { name: /Send invite|Send \d+ invites|Sending/ });

// ── every refusal is something a person can read ─────────────────────
// (carried over from InviteTeammate.test.tsx — see the file header)

describe('a refusal the user can see', () => {
  it('says something when every field is empty', async () => {
    open();
    fireEvent.click(sendButton());
    expect(await screen.findByText('Type at least one email address.')).toBeInTheDocument();
    expect(invite).not.toHaveBeenCalled();
  });

  it('names the address it could not parse', async () => {
    open();
    type('brandon@');
    fireEvent.click(sendButton());
    expect(await screen.findByText(/brandon@/)).toBeInTheDocument();
    expect(invite).not.toHaveBeenCalled();
  });

  it('leaves the button pressable so the refusal can happen at all', () => {
    // ⚠ The original defect. A disabled button swallows the click before any
    // code runs, so there is no place left to put an explanation — the fix had
    // to move the guard, not add a message beside it.
    open();
    type('nope');
    expect(sendButton()).not.toBeDisabled();
  });

  it('puts the cursor back where the problem is', () => {
    open();
    type('nope');
    fireEvent.click(sendButton());
    expect(document.activeElement).toBe(fields()[0]);
  });

  it('puts the cursor on the BAD row, not the first one', () => {
    // New with multiple rows, and the whole reason the message names an
    // address: "one of these is wrong" in a six-row form is a refusal the
    // person cannot act on.
    open();
    type('good@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('nope', 1);
    fireEvent.click(sendButton());
    expect(document.activeElement).toBe(fields()[1]);
    expect(screen.getByText(/check nope/)).toBeInTheDocument();
  });

  it('clears the complaint as soon as they start fixing it', async () => {
    open();
    type('nope');
    fireEvent.click(sendButton());
    await screen.findByText(/nope/);
    type('nope@example.com');
    await waitFor(() => expect(screen.queryByText(/doesn't look like/)).toBeNull());
  });
});

// ── the send ─────────────────────────────────────────────────────────

describe('a send the user can see', () => {
  it('sends one valid address with its role', async () => {
    open();
    type('teammate@example.com');
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(invite).toHaveBeenCalledWith([{ email: 'teammate@example.com', role: 'member' }]);
  });

  it('sends every filled row in one call, not one call each', async () => {
    // ⚠ The reason the bulk endpoint exists. A client loop spends the invite
    // rate limiter N times AND pushes N seat quantities to Stripe, prorating
    // every intermediate number for one action the admin took once.
    open();
    type('a@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('b@example.com', 1);
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(invite).toHaveBeenCalledTimes(1);
    expect(invite).toHaveBeenCalledWith([
      { email: 'a@example.com', role: 'member' },
      { email: 'b@example.com', role: 'member' },
    ]);
  });

  it("shows the server's own words when the whole request fails", async () => {
    invite.mockRejectedValue(new Error('That address is already on another team.'));
    open();
    type('teammate@example.com');
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(
      await screen.findByText('That address is already on another team.'),
    ).toBeInTheDocument();
  });

  it('does not fire twice while one is in flight', async () => {
    let release: (v: InviteResult[]) => void = () => {};
    invite.mockReturnValue(new Promise((r) => (release = r)));
    open();
    type('teammate@example.com');
    fireEvent.click(sendButton());
    await screen.findByRole('button', { name: /Sending/ });
    fireEvent.click(screen.getByRole('button', { name: /Sending/ }));
    expect(invite).toHaveBeenCalledTimes(1);
    await act(async () => release([ok('teammate@example.com')]));
  });

  it('closes only when every address landed', async () => {
    const onClose = vi.fn();
    open({ onClose });
    type('teammate@example.com');
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('a partial batch', () => {
  beforeEach(() => {
    invite.mockResolvedValue([
      ok('good@example.com'),
      {
        email: 'bad@example.com',
        role: 'member',
        status: 'failed',
        emailSent: false,
        error: 'Could not resolve an account for that email.',
      },
    ]);
  });

  it('stays open and names which address failed, and why', async () => {
    // ⚠ Closing here would replace the only explanation of what went wrong
    // with a toast that says a number.
    const onClose = vi.fn();
    open({ onClose });
    type('good@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('bad@example.com', 1);
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(onClose).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/bad@example.com — Could not resolve an account/),
    ).toBeInTheDocument();
  });

  it('refreshes the roster even though part of it failed', async () => {
    // The invites that landed are real. Not refreshing leaves the admin
    // looking at a roster that is missing people who are already on the team.
    const onInvited = vi.fn();
    open({ onInvited });
    type('good@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('bad@example.com', 1);
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(onInvited).toHaveBeenCalled();
  });

  it('leaves only the failures in the form, so Send retries just those', async () => {
    open();
    type('good@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('bad@example.com', 1);
    await act(async () => {
      fireEvent.click(sendButton());
    });
    // ⚠ A retry that re-sent the successes would land them as
    // `already_member`, which reads to the admin like the first attempt failed.
    const remaining = fields().map((f) => (f as HTMLInputElement).value);
    expect(remaining).toEqual(['bad@example.com']);
  });
});

// ── pasting a list ───────────────────────────────────────────────────

describe('parseAddresses', () => {
  it('splits the separators people actually paste', () => {
    expect(parseAddresses('a@x.com, b@x.com; c@x.com\nd@x.com\te@x.com')).toEqual([
      'a@x.com',
      'b@x.com',
      'c@x.com',
      'd@x.com',
      'e@x.com',
    ]);
  });

  it('unwraps the angle-bracket form an email client copies', () => {
    expect(parseAddresses('Tara Lee <tara@x.com>, Marcus <marcus@x.com>')).toEqual([
      'Tara',
      'Lee',
      'tara@x.com',
      'Marcus',
      'marcus@x.com',
    ]);
    // ⚠ The display-name words survive as their own entries, which is
    // deliberate: they reach a row, fail validation by name, and the person
    // deletes them. Silently dropping anything that "doesn't look like an
    // address" is a refusal nobody can perceive — and it would also swallow a
    // genuine typo, which is the case that matters.
  });

  it('de-duplicates case-insensitively', () => {
    expect(parseAddresses('A@x.com, a@x.com')).toEqual(['A@x.com']);
  });

  it('is empty for empty input rather than yielding one blank row', () => {
    expect(parseAddresses('   \n , ; ')).toEqual([]);
  });
});

describe('pasting into a row', () => {
  it('turns a pasted list into one row each', () => {
    open();
    fireEvent.paste(fields()[0], {
      clipboardData: { getData: () => 'a@x.com, b@x.com, c@x.com' },
    });
    expect(fields().map((f) => (f as HTMLInputElement).value)).toEqual([
      'a@x.com',
      'b@x.com',
      'c@x.com',
    ]);
  });

  it('keeps rows that were already filled in', () => {
    // A paste into the second row must not throw away the first.
    open();
    type('first@x.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    fireEvent.paste(fields()[1], {
      clipboardData: { getData: () => 'a@x.com b@x.com' },
    });
    expect(fields().map((f) => (f as HTMLInputElement).value)).toEqual([
      'first@x.com',
      'a@x.com',
      'b@x.com',
    ]);
  });

  it('leaves a single address to paste normally', () => {
    // preventDefault on a one-address paste would break ordinary typing flow
    // for no benefit.
    open();
    const before = fields().length;
    fireEvent.paste(fields()[0], { clipboardData: { getData: () => 'solo@x.com' } });
    expect(fields().length).toBe(before);
  });
});

// ── what it costs, and who said so ───────────────────────────────────

const plan = (over: Partial<CurrentPlan> = {}): CurrentPlan => ({
  key: 'pro',
  label: 'Pro',
  priceUsd: 199,
  perSeat: true,
  turnsIncluded: 400,
  maxSeats: 50,
  tagline: '',
  highlights: [],
  features: [],
  ...over,
});

describe('billableSeats mirrors billing/seats.py', () => {
  /**
   * ⚠ THE SAME TABLE IS DRIVEN ON THE PYTHON SIDE by
   * `tests/unit/test_billing_seats.py::test_the_clamp_table_the_client_mirrors`.
   * TypeScript cannot import `billing/seats.py`, so this arithmetic is a second
   * copy of a rule about money. Neither file is the authority on its own — they
   * are pinned against one written table, and each names the other. If you
   * change one, the other fails.
   */
  const TABLE: [boolean, number, number, number][] = [
    // perSeat, maxSeats, members, expected
    [false, 50, 0, 1],
    [false, 50, 9, 1], // a flat plan is ONE seat however big the team is
    [true, 50, 0, 1], // never zero — there is always at least one person
    [true, 50, 1, 1],
    [true, 50, 7, 7],
    [true, 5, 9, 5], // clamped at the plan's own ceiling
    [true, 50, 999, 50],
  ];
  it.each(TABLE)('perSeat=%s max=%i members=%i -> %i', (perSeat, maxSeats, members, want) => {
    expect(billableSeats(plan({ perSeat, maxSeats }), members)).toBe(want);
  });
});

describe('seatImpact', () => {
  it('says nothing at all when the plan is unknown', () => {
    // ⚠ An older backend omits `current_plan`. Silence is the honest answer;
    // a guessed price on the screen where somebody decides to spend money is
    // the hand-copied price table `PlanOut` exists to abolish.
    expect(seatImpact(null, 3, 2)).toBeNull();
  });

  it('tells a flat plan that adding people is free', () => {
    const s = seatImpact(plan({ key: 'scale', label: 'Scale', perSeat: false, priceUsd: 500 }), 3, 4)!;
    expect(s.billed).toBe(false);
    expect(s.line).toContain("doesn't change your bill");
  });

  it('prices a per-seat plan with the total AND the difference', () => {
    const s = seatImpact(plan(), 3, 2)!;
    expect(s.billed).toBe(true);
    expect(s.line).toContain('5 seats × $199');
    expect(s.line).toContain('$995/month');
    expect(s.line).toContain('$398 more');
  });

  it('discloses that a seat starts at invite, not at acceptance', () => {
    // ⚠ `billing/seats.py` asks for this sentence by name: "charging for a seat
    // nobody has used yet is defensible only if it is disclosed". It is the one
    // assertion here that is about a promise rather than a number.
    expect(seatImpact(plan(), 1, 1)!.caveat).toMatch(/when you invite/i);
  });

  it('names the trial cap and what going past it costs you', () => {
    const trial = plan({ key: 'trial', label: 'Free trial', priceUsd: 0, maxSeats: 5 });
    expect(seatImpact(trial, 2, 1)!.caveat).toBeNull();
    const over = seatImpact(trial, 4, 3)!;
    expect(over.billed).toBe(false);
    expect(over.caveat).toContain('2 past 5');
    expect(over.caveat).toContain("don't add to your monthly allowance");
  });

  it('does not invent a cap when the backend did not send one', () => {
    // `planFromWire` degrades a missing `max_seats` to Infinity precisely so
    // this reads as "no ceiling to mention" rather than "your cap is zero".
    const s = seatImpact(plan({ priceUsd: 0, maxSeats: Number.POSITIVE_INFINITY }), 4, 9)!;
    expect(s.caveat).toBeNull();
  });

  it('stops charging at the ceiling and says why', () => {
    const s = seatImpact(plan({ maxSeats: 5 }), 5, 2)!;
    expect(s.billed).toBe(false);
    expect(s.line).toContain("won't change your bill");
  });

  it('says nothing rather than pricing a roster it has not loaded yet', () => {
    // ⚠ A REAL BUG this caught. With an unloaded roster treated as 0 people, a
    // Pro company adding one person computed billedNow=1 and billedAfter=1 —
    // both hitting the clamp's floor — so delta was 0 and the dialog reported
    // "this won't change your bill" for an invite that costs $199. An unloaded
    // roster is not an empty one, and a false reassurance about money is the
    // one direction of error that reaches an invoice.
    expect(seatImpact(plan(), null, 1)).toBeNull();
  });

  it('never says "your bill is unchanged" unless the cap is the reason', () => {
    // The guard behind the branch above: a future clamp that produced delta 0
    // for some other reason would otherwise inherit the reassuring sentence.
    const s = seatImpact(plan({ maxSeats: 50 }), 3, 0);
    expect(s?.line ?? '').not.toContain("won't change your bill");
  });
});

describe('the cost line on screen', () => {
  it('shows the price before the button, not after the send', async () => {
    open({ memberCount: 3, plan: plan() });
    type('a@example.com');
    expect(await screen.findByText(/4 seats × \$199/)).toBeInTheDocument();
  });

  it('shows nothing about money when the plan is unknown', () => {
    open({ memberCount: 3, plan: null });
    type('a@example.com');
    expect(screen.queryByText(/\$/)).toBeNull();
  });
});

/**
 * A mistyped address on THIS form fails in total silence, which is what makes
 * the hint worth wiring here rather than only on sign-in.
 *
 * `sam@gmial.com` passes the shape check, the API accepts it, mail goes to a
 * mailbox that does not exist, and the team screen carries an "Invite pending"
 * row for somebody who was never asked. Nothing anywhere reports a failure.
 *
 * ⚠ BEHAVIOURAL on purpose. A source-level guard asserting the component
 * appears in this file SURVIVED a mutation that rendered it with an empty
 * email — the markup matched and the hint never fired (gotcha 62: a scan of
 * the call site cannot see a call site that passes the wrong thing).
 */
describe('a mistyped address, where nothing else would catch it', () => {
  it('offers the correction under the row being typed', () => {
    open();
    type('sam@gmial.com');
    expect(screen.getByTestId('email-typo-hint')).toHaveTextContent('sam@gmail.com');
  });

  it('accepting it rewrites THAT row', () => {
    open();
    type('sam@gmial.com');
    fireEvent.click(screen.getByRole('button', { name: 'sam@gmail.com' }));
    expect((fields()[0] as HTMLInputElement).value).toBe('sam@gmail.com');
  });

  it('leaves a real work domain alone', () => {
    // Every property manager's address is an unfamiliar domain. A hint that
    // second-guesses those is one people stop reading.
    open();
    type('brandon@blueridgeproperties.com');
    expect(screen.queryByTestId('email-typo-hint')).toBeNull();
  });

  it('hints the row it belongs to, not the first one', () => {
    open();
    type('brandon@blueridgeproperties.com', 0);
    fireEvent.click(screen.getByRole('button', { name: /Add another/ }));
    type('sam@gmial.com', 1);
    // One hint, and it is the second row's.
    const hints = screen.getAllByTestId('email-typo-hint');
    expect(hints).toHaveLength(1);
    expect(hints[0]).toHaveTextContent('sam@gmail.com');
  });
});

// ── an address that already has an account (migration 0112) ──────────
// They are NOT on the team until they accept, and no email goes out. Saying only
// "Invited" would leave the admin looking for them on the roster.

describe('an existing account waits for its answer', () => {
  it("says they'll see the invite when they sign in, not that they were added", async () => {
    const { toast } = await import('sonner');
    invite.mockResolvedValue([
      { email: 'has@example.com', role: 'member', status: 'pending', emailSent: false, error: null },
    ]);
    open();
    type('has@example.com');
    await act(async () => {
      fireEvent.click(sendButton());
    });
    const said = vi.mocked(toast.success).mock.calls.map((c) => String(c[0]));
    expect(said).toEqual([
      "has@example.com already has an account. They'll see your invite next time they sign in.",
    ]);
  });

  it('names the pending one beside a failure in a partial batch', async () => {
    invite.mockResolvedValue([
      { email: 'has@example.com', role: 'member', status: 'pending', emailSent: false, error: null },
      { email: 'bad@example.com', role: 'member', status: 'failed', emailSent: false, error: 'No.' },
    ]);
    open();
    type('has@example.com', 0);
    fireEvent.click(screen.getByRole('button', { name: 'Add another' }));
    type('bad@example.com', 1);
    await act(async () => {
      fireEvent.click(sendButton());
    });
    expect(
      await screen.findByText(/has@example.com already has an account\. No email sent/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/added, no email sent/)).toBeNull();
  });
});
