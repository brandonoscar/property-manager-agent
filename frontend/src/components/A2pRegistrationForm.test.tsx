/**
 * The registration form's validation timing.
 *
 * ⚠ `problems` is computed SERVER-side and arrives with the row, so it is
 * present on the very first paint of a brand-new draft. Rendered
 * unconditionally, that greeted a customer who had typed nothing with every
 * required field flagged at once — a screen full of failures for doing
 * nothing wrong, and the reason this file exists.
 *
 * The property is about WHEN, not about wording, so these drive the component
 * rather than reading its source: a static guard cannot see that a list
 * rendered.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import { A2pRegistrationForm } from './A2pRegistrationForm';
import type { A2pRegistration, A2pStatus } from '../services/a2p';

const fetchRegistration = vi.hoisted(() => vi.fn());
const saveRegistration = vi.hoisted(() => vi.fn());
const submitRegistration = vi.hoisted(() => vi.fn());

vi.mock('../services/a2p', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/a2p')>();
  return { ...actual, fetchRegistration, saveRegistration, submitRegistration };
});

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const BLANK_DETAILS = {
  legal_business_name: null,
  ein: null,
  business_type: null,
  business_vertical: null,
  website: null,
  street: null,
  city: null,
  region: null,
  postal_code: null,
  iso_country: null,
  rep_first_name: null,
  rep_last_name: null,
  rep_email: null,
  rep_phone: null,
  rep_job_position: null,
  use_case: null,
  campaign_description: null,
  opt_in_description: null,
};

/** A never-touched draft: the state a customer opens Leasing in on day one. */
function draft(overrides: Partial<A2pRegistration> = {}): A2pRegistration {
  return {
    status: 'draft' as A2pStatus,
    details: { ...BLANK_DETAILS },
    sample_messages: [],
    problems: ['Legal business name is required.', 'City is required.'],
    editable: true,
    failure_reason: null,
    submitted_at: null,
    brand_approved_at: null,
    campaign_approved_at: null,
    ...overrides,
  };
}

async function renderForm(reg: A2pRegistration) {
  fetchRegistration.mockResolvedValue(reg);
  await act(async () => {
    render(<A2pRegistrationForm />);
  });
}

describe('A2pRegistrationForm — when problems are shown', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not flag anything on a draft nobody has submitted yet', async () => {
    await renderForm(draft());
    expect(screen.getByTestId('a2p-registration-form')).toBeInTheDocument();
    expect(screen.queryByTestId('a2p-problems')).not.toBeInTheDocument();
  });

  it('flags what is missing once the customer actually tries to submit', async () => {
    const reg = draft();
    await renderForm(reg);
    saveRegistration.mockResolvedValue(reg);
    submitRegistration.mockRejectedValue(new Error('incomplete'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('a2p-submit'));
    });

    const problems = screen.getByTestId('a2p-problems');
    expect(problems.textContent).toContain('Legal business name is required.');
  });

  it('flags a rejected registration without waiting to be asked', async () => {
    // ⚠ The exception, and the reason this is a condition rather than a
    // one-line "hide until submit". Someone whose registration the carriers
    // sent back has already submitted; the problems ARE the screen.
    await renderForm(draft({ status: 'failed', failure_reason: 'Address not validated' }));
    expect(screen.getByTestId('a2p-problems')).toBeInTheDocument();
    expect(screen.getByTestId('a2p-failure-reason').textContent).toContain(
      'Address not validated',
    );
  });

  it('shows what the customer just typed, not what the row held on load', async () => {
    // ⚠ `submit` saves first and KEEPS that response. Reporting the problems
    // fetched on page load would describe fields the customer has since
    // filled in — telling them to fix something they already fixed.
    const reg = draft();
    await renderForm(reg);
    saveRegistration.mockResolvedValue(
      draft({ problems: ['ZIP is required.'] }),
    );
    submitRegistration.mockRejectedValue(new Error('incomplete'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('a2p-submit'));
    });

    const problems = screen.getByTestId('a2p-problems');
    expect(problems.textContent).toContain('ZIP is required.');
    expect(problems.textContent).not.toContain('Legal business name is required.');
  });
});
