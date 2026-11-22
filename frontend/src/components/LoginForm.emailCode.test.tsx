/**
 * "Email me a code": sign in with a one-time code instead of the password.
 *
 * The code goes to an EXISTING account only (`sendSignInCode` sends with
 * shouldCreateUser: false; accounts are made at /start). The code box that
 * follows must check the code as a SIGN-IN code, not a signup confirmation,
 * or a correct code is refused.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/auth', () => ({
  signIn: vi.fn(),
  resendConfirmation: vi.fn(),
  verifySignupCode: vi.fn(),
  sendSignInCode: vi.fn(),
  verifySignInCode: vi.fn(),
  requestPasswordReset: vi.fn(),
  supabaseConfigured: true,
  demoMode: false,
  DEMO_USER: {},
  DEMO_SESSION: {},
  DEMO_COMPANY_ID: 'demo',
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import {
  requestPasswordReset,
  sendSignInCode,
  verifySignInCode,
  verifySignupCode,
} from '../services/auth';
import { LoginForm } from './LoginForm';

beforeEach(() => vi.clearAllMocks());

function typeEmail(value = 'pm@example.com') {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value } });
}

describe('Email me a code', () => {
  it('asks for the email first instead of sending to nobody', async () => {
    render(<LoginForm />);
    fireEvent.click(screen.getByTestId('login-email-code'));
    expect(await screen.findByRole('alert')).toHaveTextContent(/enter your email address first/i);
    expect(sendSignInCode).not.toHaveBeenCalled();
  });

  it('sends the code, then checks what is typed as a sign-in code', async () => {
    vi.mocked(sendSignInCode).mockResolvedValue(undefined);
    vi.mocked(verifySignInCode).mockResolvedValue({} as never);
    render(<LoginForm />);
    typeEmail();
    fireEvent.click(screen.getByTestId('login-email-code'));

    expect(await screen.findByTestId('login-confirm-notice')).toHaveTextContent(
      'We sent a sign-in code to pm@example.com.',
    );
    expect(sendSignInCode).toHaveBeenCalledWith('pm@example.com');

    fireEvent.change(screen.getByLabelText('Sign-in code from the email'), { target: { value: '123456' } });
    fireEvent.click(screen.getByTestId('login-confirm-verify'));
    await waitFor(() => expect(verifySignInCode).toHaveBeenCalledWith('pm@example.com', '123456'));
    expect(verifySignupCode).not.toHaveBeenCalled();
  });

  it('points an unknown address at the free trial, without claiming it has no account', async () => {
    vi.mocked(sendSignInCode).mockRejectedValue(new Error('Signups not allowed for otp'));
    render(<LoginForm />);
    typeEmail('someone@example.com');
    fireEvent.click(screen.getByTestId('login-email-code'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/start a free trial at occupella\.com\/start/i);
    expect(alert).not.toHaveTextContent(/no account/i);
  });
});

describe('the reset notice', () => {
  it('shows no code box, because the reset email carries a link', async () => {
    vi.mocked(requestPasswordReset).mockResolvedValue(undefined);
    render(<LoginForm />);
    typeEmail();
    fireEvent.click(screen.getByRole('button', { name: /forgot password/i }));
    expect(await screen.findByTestId('login-confirm-notice')).toHaveTextContent(/on its way/i);
    expect(screen.queryByTestId('login-confirm-code')).toBeNull();
  });
});

describe('the page', () => {
  it('labels its fields and offers the trial link in the header', () => {
    render(<LoginForm />);
    expect(screen.getByLabelText('Email')).toHaveAttribute('type', 'email');
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
    const trial = screen.getByTestId('login-start-setup');
    expect(trial).toHaveTextContent('Start free trial');
    expect(trial).toHaveAttribute('href', 'https://occupella.com/start');
    expect(screen.queryByText(/execution layer/i)).toBeNull();
  });
});
