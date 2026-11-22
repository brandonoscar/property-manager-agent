/**
 * LoginForm — sign-in only, with ONE front door for new customers.
 *
 * Founder decision (2026-08-20, mid-provisioning-walk): the app runs NO
 * signup flow. "Start setup" navigates to the onboarding wizard — the
 * single door where accounts are created (ToS, password, company,
 * Buildium, webhooks). The mode switcher is gone. What remains here:
 * password sign-in, and the unconfirmed-account recovery path (6-digit
 * code + resend), which renders as a positive notice — never as the
 * error box (the walk's founder-reported dead-end).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/auth', () => ({
  signIn: vi.fn(),
  resendConfirmation: vi.fn(),
  verifySignupCode: vi.fn(),
  supabaseConfigured: true,
  demoMode: false,
  DEMO_USER: {},
  DEMO_SESSION: {},
  DEMO_COMPANY_ID: 'demo',
}));
vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

import { resendConfirmation, signIn, verifySignupCode } from '../services/auth';
import { LoginForm } from './LoginForm';

const signInMock = vi.mocked(signIn);
const resendMock = vi.mocked(resendConfirmation);
const verifyMock = vi.mocked(verifySignupCode);

beforeEach(() => {
  vi.clearAllMocks();
});

function submitButton(container: HTMLElement): Element {
  const btn = container.querySelector('button[type="submit"]');
  if (!btn) throw new Error('submit button not found');
  return btn;
}

function signInWith(container: HTMLElement) {
  fireEvent.change(screen.getByPlaceholderText(/you@company/i), {
    target: { value: 'new@example.com' },
  });
  const password = container.querySelector('input[type="password"]');
  if (!password) throw new Error('password input not found');
  fireEvent.change(password, { target: { value: 'hunter2hunter2' } });
  fireEvent.click(submitButton(container));
}

async function renderUnconfirmed() {
  signInMock.mockRejectedValue(new Error('Email not confirmed'));
  const { container } = render(<LoginForm />);
  signInWith(container);
  await screen.findByTestId('login-confirm-notice');
}

describe('LoginForm — one front door', () => {
  it('has no signup mode: Start setup is a link to the onboarding wizard', () => {
    render(<LoginForm />);
    const setup = screen.getByTestId('login-start-setup');
    expect(setup.tagName).toBe('A'); // navigation, not a mode switch
    expect(setup).toHaveAttribute('href', 'https://occupella.com/start');
    // No Create-account affordance exists anywhere on the form.
    expect(screen.queryByRole('button', { name: /create account/i })).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull(); // ToS moved to the wizard
  });

  it('an unconfirmed sign-in shows the code + resend notice — never the error box', async () => {
    await renderUnconfirmed();
    const notice = screen.getByTestId('login-confirm-notice');
    expect(notice).toHaveTextContent(/6-digit code/i);
    expect(notice).toHaveAttribute('role', 'status'); // success styling, not alert
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByTestId('login-confirm-code')).toBeInTheDocument();
    expect(screen.getByTestId('login-confirm-verify')).toBeDisabled(); // until 6 digits
  });

  it('typing the 6-digit code verifies it with the service', async () => {
    verifyMock.mockResolvedValue({ session: {} } as never);
    await renderUnconfirmed();

    fireEvent.change(screen.getByTestId('login-confirm-code'), {
      target: { value: '482913' },
    });
    const verify = screen.getByTestId('login-confirm-verify');
    expect(verify).toBeEnabled();
    fireEvent.click(verify);
    await waitFor(() => expect(verifyMock).toHaveBeenCalledWith('new@example.com', '482913'));
  });

  it('a wrong code shows an actionable error and keeps the code form up', async () => {
    verifyMock.mockRejectedValue(new Error('Token has expired or is invalid'));
    await renderUnconfirmed();

    fireEvent.change(screen.getByTestId('login-confirm-code'), {
      target: { value: '000000' },
    });
    fireEvent.click(screen.getByTestId('login-confirm-verify'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/didn't match|expired/i);
    expect(screen.getByTestId('login-confirm-code')).toBeInTheDocument();
  });

  it('the resend button calls the resend service and starts a cooldown', async () => {
    resendMock.mockResolvedValue(undefined as never);
    await renderUnconfirmed();

    fireEvent.click(screen.getByTestId('login-resend-confirmation'));
    await waitFor(() => expect(resendMock).toHaveBeenCalledWith('new@example.com'));
    expect(screen.getByTestId('login-resend-confirmation')).toBeDisabled();
    expect(screen.getByTestId('login-resend-confirmation')).toHaveTextContent(/available in/i);
  });

  it('a genuinely wrong password still renders the error box', async () => {
    signInMock.mockRejectedValue(new Error('Invalid login credentials'));
    const { container } = render(<LoginForm />);
    signInWith(container);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/incorrect/i);
    expect(screen.queryByTestId('login-confirm-notice')).toBeNull();
  });
});
