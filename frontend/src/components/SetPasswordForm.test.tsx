/**
 * The invite / password-reset screen.
 *
 * ⚠ THIS FILE EXISTS BECAUSE THE SCREEN HAD NO TESTS AT ALL. It is the
 * higher-stakes of the two password surfaces: `ChangePasswordRow` is somebody
 * already signed in choosing a new password, while this is an invited
 * teammate's FIRST one and the only way back for a customer locked out of
 * their own books. When the floor moved to 8-plus-capital-plus-number
 * (2026-09-08) both surfaces changed and only the Settings one would have
 * failed if the wiring were dropped — a guard on one of two identical call
 * sites reads as coverage and is not (gotcha 62).
 *
 * ⚠ The submit button is `disabled` below the floor, so `fireEvent.click` on
 * it proves nothing about the handler — a refusal test has to reach
 * `handleSubmit` some other way, which is why the requirement checks below
 * assert on the DISABLED STATE and the visible checklist rather than on an
 * error message that a click can never produce.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  updateUser: vi.fn(),
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

// ⚠ A plain stub is safe HERE and would not be in ChangePasswordRow.test.tsx.
// This screen is standalone — it touches `supabase.auth` and nothing else —
// whereas SettingsPage pulls in a transitive tree that needs the real client.
// Copying the Proxy from that file would be cargo-culting a workaround for a
// problem this module does not have.
vi.mock('../services/auth', () => ({
  supabase: {
    auth: {
      updateUser: h.updateUser,
      getSession: h.getSession,
      onAuthStateChange: h.onAuthStateChange,
    },
  },
}));

vi.mock('sonner', () => ({
  toast: { success: h.toastSuccess, error: h.toastError },
}));

import { SetPasswordForm } from './SetPasswordForm';

const GOOD = 'Brand-new-one1';

const field = (placeholder: RegExp) => screen.getByPlaceholderText(placeholder);
const submit = () =>
  screen.getByRole('button', {
    name: /set password & continue|update password|verifying/i,
  });

function type(next: string, confirm = next) {
  fireEvent.change(field(/^new password$/i), { target: { value: next } });
  fireEvent.change(field(/^confirm password$/i), { target: { value: confirm } });
}

/** The screen only enables submit once the invite token has established a
 *  session, so every test has to get past that first. */
const renderReady = async (flow: 'invite' | 'recovery' = 'invite') => {
  const done = vi.fn();
  render(<SetPasswordForm flow={flow} onDone={done} />);
  await waitFor(() => expect(submit().hasAttribute('disabled')).toBe(true));
  return done;
};

beforeEach(() => {
  vi.clearAllMocks();
  h.getSession.mockResolvedValue({ data: { session: { user: { id: 'u1' } } } });
  h.onAuthStateChange.mockReturnValue({
    data: { subscription: { unsubscribe: vi.fn() } },
  });
  h.updateUser.mockResolvedValue({ error: null });
});

describe('it waits for the invite link before letting anyone submit', () => {
  it('says it is still verifying rather than offering a dead button', () => {
    // No session yet — a button labelled "Set password" that cannot work is
    // how somebody concludes the invite is broken and emails support.
    h.getSession.mockReturnValue(new Promise(() => {}));
    render(<SetPasswordForm flow="invite" onDone={vi.fn()} />);
    expect(screen.getByRole('button', { name: /verifying your link/i })).toBeTruthy();
  });

  it('enables submit once the session lands and the password is acceptable', async () => {
    await renderReady();
    type(GOOD);
    await waitFor(() => expect(submit().hasAttribute('disabled')).toBe(false));
  });
});

describe('the floor is the same one Settings enforces', () => {
  it('refuses a password that is too short', async () => {
    await renderReady();
    type('Ab1');
    expect(submit().hasAttribute('disabled')).toBe(true);
  });

  it('refuses a long password with no capital letter', async () => {
    await renderReady();
    type('brand-new-one1');
    expect(submit().hasAttribute('disabled')).toBe(true);
  });

  it('refuses a long password with no number', async () => {
    await renderReady();
    type('Brand-new-one');
    expect(submit().hasAttribute('disabled')).toBe(true);
  });

  it('refuses a mismatched confirmation even when the password is fine', async () => {
    await renderReady();
    type(GOOD, `${GOOD}-typo`);
    expect(submit().hasAttribute('disabled')).toBe(true);
    // And says so, rather than leaving a dead button with no explanation.
    expect(screen.getByText(/do not match/i)).toBeTruthy();
  });

  it('never calls updateUser for a password below the floor', async () => {
    await renderReady();
    type('brand-new-one1');
    fireEvent.click(submit());
    expect(h.updateUser).not.toHaveBeenCalled();
  });
});

describe('the requirements are visible, not discovered by failing', () => {
  it('lists all three before a single key is pressed', async () => {
    // The whole point of the checklist: somebody who can see the bar clears
    // it first time. Revealing it only after a rejection is the version that
    // makes the product feel arbitrary.
    await renderReady();
    expect(screen.getByText(/at least 8 characters/i)).toBeTruthy();
    expect(screen.getByText(/one capital letter/i)).toBeTruthy();
    expect(screen.getByText(/one number/i)).toBeTruthy();
  });

  it('ticks each one independently as it is satisfied', async () => {
    await renderReady();
    fireEvent.change(field(/^new password$/i), { target: { value: 'brandnewone' } });
    // Long enough, no capital, no number.
    expect(screen.getAllByText(/— met/i).length).toBe(1);
    expect(screen.getAllByText(/not yet met/i).length).toBe(2);
  });

  it('does not accuse an untouched field of anything', async () => {
    await renderReady();
    expect(screen.queryAllByText(/— met/i).length).toBe(0);
  });
});

describe('when it succeeds', () => {
  it('sets the password and tells the caller it is done', async () => {
    const done = await renderReady();
    type(GOOD);
    fireEvent.click(submit());
    await waitFor(() => expect(h.updateUser).toHaveBeenCalledWith({ password: GOOD }));
    await waitFor(() => expect(done).toHaveBeenCalled());
  });

  it('reports a failure instead of sending someone into the app', async () => {
    h.updateUser.mockResolvedValue({ error: new Error('token expired') });
    const done = await renderReady('recovery');
    type(GOOD);
    fireEvent.click(submit());
    await screen.findByText(/token expired/i);
    expect(done).not.toHaveBeenCalled();
  });
});
