/**
 * Password recovery has a sender, and the sender is reachable from the UI.
 *
 * The whole receiving half of this flow shipped complete and could never
 * run. `authFlow.parsePasswordFlow` recognises `type=recovery`,
 * `authFlow.test.ts` asserts it, `SetPasswordForm` renders the
 * choose-a-new-password screen, and `App.tsx` routes to it — and
 * `grep -rn resetPasswordForEmail frontend/src src` returned NOTHING, so no
 * recovery link was ever sent and none of that code was reachable. A
 * customer who forgot their password had no way back in; an admin had to
 * reset it by hand in the Supabase dashboard.
 *
 * That is `comms_triage`'s shape (complete, unit-tested, called by nothing),
 * and it is why the first test here is a WIRING guard rather than a
 * behavioural one. A behavioural test of `requestPasswordReset` would have
 * passed against the broken tree the entire time: the function under test
 * did not exist, and the one it replaced was correct in isolation — what was
 * missing was any caller at all.
 *
 * ⚠ `vi.mock` is hoisted above every top-level binding, so a spy shared with
 * a factory must be created inside `vi.hoisted()` or the factory hits a
 * temporal dead zone and the whole suite fails to collect.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  requestPasswordReset: vi.fn(),
  signIn: vi.fn(),
  resendConfirmation: vi.fn(),
  verifySignupCode: vi.fn(),
}));

vi.mock('../services/auth', () => ({
  requestPasswordReset: h.requestPasswordReset,
  signIn: h.signIn,
  resendConfirmation: h.resendConfirmation,
  verifySignupCode: h.verifySignupCode,
  supabaseConfigured: true,
  demoMode: false,
  DEMO_USER: { id: 'd', email: 'd@example.com' },
  DEMO_SESSION: { access_token: 't' },
  DEMO_COMPANY_ID: 'demo-company',
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { LoginForm } from './LoginForm';

const emailBox = () => screen.getByPlaceholderText(/you@company/i);
const forgot = () => screen.getByRole('button', { name: /forgot password/i });

beforeEach(() => {
  vi.clearAllMocks();
  h.requestPasswordReset.mockResolvedValue(undefined);
});

describe('the sender exists and the UI reaches it', () => {
  it('offers a way to ask for a reset at all', () => {
    render(<LoginForm />);
    expect(forgot()).toBeTruthy();
  });

  it('calls requestPasswordReset with the typed address', async () => {
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.click(forgot());
    await waitFor(() =>
      expect(h.requestPasswordReset).toHaveBeenCalledWith('sam@example.com'),
    );
  });

  it('does NOT sign in when the reset control is used', async () => {
    // The control lives inside the <form>. A bare <button> defaults to
    // type="submit", so without type="button" this click would submit the
    // form and attempt a sign-in with whatever is in the password box —
    // the user would see an auth error instead of an email.
    //
    // ⚠ THE PASSWORD MUST BE FILLED IN, and that is the whole test. With it
    // empty, `handleSubmit` returns at its own `if (!email || !password)`
    // guard and never reaches signIn — so the assertion below passes whether
    // or not the button is type="button", and a mutation battery scored it
    // SURVIVED. The fixture was carrying a second signal that masked the
    // defect (gotcha 65's shape); a filled password is what makes a stray
    // submit observable.
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'hunter2' },
    });
    fireEvent.click(forgot());
    await waitFor(() => expect(h.requestPasswordReset).toHaveBeenCalled());
    expect(h.signIn).not.toHaveBeenCalled();
  });
});

describe('what the user is told', () => {
  it('says the same thing whether or not the account exists', async () => {
    // Supabase resolves successfully for an unknown address ON PURPOSE, so
    // the form cannot be used to enumerate customers. Wording the result as
    // "sent to you" would leak precisely what the API refuses to.
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'nobody@example.com' } });
    fireEvent.click(forgot());
    const notice = await screen.findByText(/reset link is on its way/i);
    expect(notice.textContent).toMatch(/if an account exists/i);
  });

  it('asks for an address instead of sending an empty request', async () => {
    render(<LoginForm />);
    fireEvent.click(forgot());
    await screen.findByText(/enter your email address first/i);
    expect(h.requestPasswordReset).not.toHaveBeenCalled();
  });

  it('names the wait when the server rate-limits', async () => {
    h.requestPasswordReset.mockRejectedValueOnce(
      new Error('For security purposes, you can only request this after 60 seconds'),
    );
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.click(forgot());
    // "Couldn't send" would be wrong here — nothing is broken and retrying
    // immediately fails again. The message has to say to wait.
    //
    // ⚠ It asserts the SERVER'S number, not "a minute". This test used to pin
    // the guess, and the guess is right only for the 60-second limiter — wrong
    // for any other, in the direction that sends somebody back too early to be
    // limited again. The message carries the figure; `waitPhrase` reads it.
    await screen.findByText(/wait 60 seconds/i);
  });

  it('surfaces a real failure rather than claiming mail went out', async () => {
    h.requestPasswordReset.mockRejectedValueOnce(new Error('smtp down'));
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.click(forgot());
    await screen.findByText(/couldn't send the reset email/i);
    expect(screen.queryByText(/on its way/i)).toBeNull();
  });
});

describe('the cooldown', () => {
  it('disables the control after a successful request', async () => {
    // GoTrue's ~60s limiter is per RECIPIENT, not per template, so a second
    // click inside the window bounces off the server. A control that still
    // looks armed is the thing the existing countdown exists to prevent.
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.click(forgot());
    await waitFor(() =>
      expect(forgot().hasAttribute('disabled')).toBe(true),
    );
  });

  it('does not fire a second request while cooling down', async () => {
    render(<LoginForm />);
    fireEvent.change(emailBox(), { target: { value: 'sam@example.com' } });
    fireEvent.click(forgot());
    await waitFor(() => expect(h.requestPasswordReset).toHaveBeenCalledTimes(1));
    fireEvent.click(forgot());
    fireEvent.click(forgot());
    expect(h.requestPasswordReset).toHaveBeenCalledTimes(1);
  });
});
