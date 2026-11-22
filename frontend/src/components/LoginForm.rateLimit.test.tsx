/**
 * Rate-limited sign-in: the checklist's "account locked after failed
 * attempts, no unlock path".
 *
 * There is no permanent lock to unlock — GoTrue's limiter is time-based — so
 * the unlock path IS the wait, and the defect was never a missing feature. It
 * was that the sign-in branch tested `/rate limit|too many requests/`, the
 * exact pattern `isRateLimited`'s own docstring documents as not matching what
 * Supabase sends. A limited sign-in therefore fell to the generic `else` and
 * read "Sign in didn't go through. Try again, or contact team@occupella.com":
 * retrying is what extends the limit, and there is nothing for support to do.
 *
 * ⚠ Two email paths already used the shared predicate. The third did not, and
 * the reason is the name it used to carry — `isEmailRateLimit`. That is gotcha
 * 67 stated exactly: a rule documented by naming its first caller is a rule
 * the second caller will not find.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

const h = vi.hoisted(() => ({
  signIn: vi.fn(),
  signUp: vi.fn(),
  resendConfirmation: vi.fn(),
  requestPasswordReset: vi.fn(),
  verifyEmailCode: vi.fn(),
}));

vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  // ⚠ `supabaseConfigured: true` is not decoration. `handleSubmit` opens with
  // a deploy-misconfiguration guard that short-circuits BEFORE `signIn` when
  // the env vars are missing — which they are under vitest — so without this
  // every assertion below measures "sign-in is temporarily unavailable on this
  // deployment" and reads as a failure of the branch under test. A fixture has
  // to say which state it means (gotchas 67/72/73).
  return { ...actual, ...h, demoMode: false, supabaseConfigured: true };
});

import { LoginForm, isRateLimited, retryAfterSeconds, waitPhrase } from './LoginForm';

// The string GoTrue ACTUALLY sends. Writing the plausible one instead is what
// produced the bug in the first place.
const REAL = 'For security purposes, you can only request this after 47 seconds';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the predicate matches what the provider really sends', () => {
  it("recognises Supabase's own wording", () => {
    expect(isRateLimited(REAL)).toBe(true);
  });

  it('still recognises the plainer forms', () => {
    for (const m of [
      'Request rate limit reached',
      'Too many requests',
      'over_email_send_rate_limit',
    ]) {
      expect(isRateLimited(m), m).toBe(true);
    }
  });

  it('does not claim a rate limit on an ordinary failure', () => {
    // ⚠ The other direction (gotcha 38). A predicate that answered true for
    // everything would pass every test above and tell somebody with a genuinely
    // wrong password to go and wait.
    for (const m of ['Invalid login credentials', 'Email not confirmed', 'smtp down']) {
      expect(isRateLimited(m), m).toBe(false);
    }
  });
});

describe('the wait comes from the message, not from a guess', () => {
  it('reads the number the server named', () => {
    expect(retryAfterSeconds(REAL)).toBe(47);
    expect(waitPhrase(REAL)).toBe('wait 47 seconds');
  });

  it('falls back to the generic when the server did not say', () => {
    // ⚠ Null is "it did not say", never a fabricated figure. "Wait a minute"
    // is right for the 60-second limiter and wrong for any other — and wrong
    // in the direction that sends somebody back too early to be limited again.
    expect(retryAfterSeconds('Request rate limit reached')).toBeNull();
    expect(waitPhrase('Request rate limit reached')).toBe('wait a minute');
  });

  it('does not say "1 seconds"', () => {
    expect(waitPhrase('you can only request this after 1 second')).toBe('wait 1 second');
  });

  it('refuses a figure that is not a usable wait', () => {
    // ⚠ THIS TEST EXISTS BECAUSE A MUTATION STOPPED BEING CAUGHT. Dropping
    // `Number.isFinite(n) && n > 0` was RED in the first battery and ALIVE in
    // the second — and nothing about the code changed between them. What
    // changed is that the password-reset test used to assert "wait a minute",
    // the old guess, so ANY change to the number failed it by accident. Fixing
    // that stale assertion removed a coincidental catch and revealed this
    // property was never covered.
    //
    // A mutation caught by a stale assertion is not covered; it is caught by a
    // test that would have failed anyway. The distinguishing inputs are the
    // ones the guard is actually for, and both render as nonsense without it:
    // "wait 0 seconds" tells somebody to wait for no time, and a digit run long
    // enough to overflow gives "wait Infinity seconds".
    expect(retryAfterSeconds('you can only request this after 0 seconds')).toBeNull();
    expect(waitPhrase('you can only request this after 0 seconds')).toBe('wait a minute');
    const absurd = `you can only request this after ${'9'.repeat(400)} seconds`;
    expect(retryAfterSeconds(absurd)).toBeNull();
    expect(waitPhrase(absurd)).toBe('wait a minute');
  });
});

describe('a rate-limited sign-in says what to do', () => {
  it('names the wait and does not send the user to support', async () => {
    h.signIn.mockRejectedValueOnce(new Error(REAL));
    render(<LoginForm />);
    fireEvent.change(screen.getByPlaceholderText(/you@company/i), {
      target: { value: 'sam@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByText(/47 seconds/)).toBeInTheDocument();
    // The two instructions the old message gave, both wrong here.
    expect(screen.queryByText(/team@occupella\.com/)).toBeNull();
  });

  it('says the account is not locked, because it is not', async () => {
    // The item is "account locked after failed attempts, no unlock path". The
    // honest answer is that nothing is locked — saying so is what stops
    // somebody resetting a password that was already correct.
    h.signIn.mockRejectedValueOnce(new Error(REAL));
    render(<LoginForm />);
    fireEvent.change(screen.getByPlaceholderText(/you@company/i), {
      target: { value: 'sam@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(await screen.findByText(/nothing has been locked/i)).toBeInTheDocument();
  });

  it('still says the password is wrong when it is', async () => {
    // ⚠ The direction that matters most. A branch that read every failure as a
    // rate limit would tell somebody with a genuinely wrong password to wait —
    // and they would wait, and it would still be wrong.
    h.signIn.mockRejectedValueOnce(new Error('Invalid login credentials'));
    render(<LoginForm />);
    fireEvent.change(screen.getByPlaceholderText(/you@company/i), {
      target: { value: 'sam@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'wrong-password-here' },
    });
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(await screen.findByText(/password is incorrect/i)).toBeInTheDocument();
  });
});
