/**
 * EmailDraftCard — the email-frame renderer for drafts the agent composed.
 * Pins: header fields render, body is verbatim with whitespace preserved,
 * the honesty footer is always present (a draft must never read as sent),
 * empty to/cc rows don't render stray labels, and multi-variant drafts get
 * the Inbox-style stepper (numbered tabs, N-of-M counter, all variants
 * mounted with inactive ones hidden).
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { EmailDraftCard } from './EmailDraftCard';
import type { EmailDraftData } from './types';

const SINGLE: EmailDraftData = {
  to: ['Sarah Chen <sarah.chen@gmail.com>'],
  cc: ['owner@sunrisepm.com'],
  variants: [
    {
      label: '',
      subject: 'Your lease renewal — Unit 4B',
      body: 'Hi Sarah,\n\nRent stays at $1,850/mo.\n\nBest,\nDana',
    },
  ],
};

const MULTI: EmailDraftData = {
  to: ['Sarah Chen <sarah.chen@gmail.com>'],
  cc: [],
  variants: [
    { label: 'Standard renewal', subject: 'Renewal A', body: 'Body A' },
    { label: 'Warmer + thank-you', subject: 'Renewal B', body: 'Body B' },
    { label: 'Offer 18-month term', subject: 'Renewal C', body: 'Body C' },
  ],
};

describe('EmailDraftCard', () => {
  it('renders to / cc / subject / body from the composed draft', () => {
    render(<EmailDraftCard data={SINGLE} />);
    expect(screen.getByTestId('email-draft-to-0')).toHaveTextContent(
      'Sarah Chen <sarah.chen@gmail.com>',
    );
    expect(screen.getByTestId('email-draft-cc-0')).toHaveTextContent('owner@sunrisepm.com');
    expect(screen.getByTestId('email-draft-subject-0')).toHaveTextContent(
      'Your lease renewal — Unit 4B',
    );
    // Body is the model's own prose, verbatim.
    expect(screen.getByTestId('email-draft-body-0').textContent).toBe(
      SINGLE.variants[0].body,
    );
  });

  it('preserves the body whitespace (multi-paragraph drafts)', () => {
    render(<EmailDraftCard data={SINGLE} />);
    expect(screen.getByTestId('email-draft-body-0')).toHaveStyle({
      whiteSpace: 'pre-wrap',
    });
  });

  it('always shows the nothing-has-been-sent footer', () => {
    render(<EmailDraftCard data={SINGLE} />);
    expect(screen.getByTestId('email-draft-footer-0')).toHaveTextContent(
      /nothing has been sent/i,
    );
  });

  it('omits the cc row when there are no cc recipients', () => {
    render(<EmailDraftCard data={{ ...SINGLE, cc: [] }} />);
    expect(screen.queryByTestId('email-draft-cc-0')).toBeNull();
  });

  it('renders no stepper for a single draft', () => {
    render(<EmailDraftCard data={SINGLE} />);
    expect(screen.queryByTestId('email-draft-tabs')).toBeNull();
  });

  describe('variant stepper (Inbox ActionCardList pattern)', () => {
    it('renders numbered labeled tabs and an N-of-M counter', () => {
      render(<EmailDraftCard data={MULTI} />);
      expect(screen.getByTestId('email-draft-tab-0')).toHaveTextContent('Standard renewal');
      expect(screen.getByTestId('email-draft-tab-2')).toHaveTextContent(
        'Offer 18-month term',
      );
      expect(screen.getByTestId('email-draft-tabs-counter')).toHaveTextContent('1 of 3');
      expect(screen.getByTestId('email-draft-tab-0')).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });

    it('slides between variants on tab click, keeping all variants mounted', () => {
      render(<EmailDraftCard data={MULTI} />);
      // All three bodies are in the DOM (mounted), only the first visible.
      expect(screen.getByTestId('email-draft-body-0')).toBeInTheDocument();
      expect(screen.getByTestId('email-draft-body-1')).toBeInTheDocument();
      expect(screen.getByTestId('email-draft-variant-1').parentElement).toHaveStyle({
        display: 'none',
      });

      fireEvent.click(screen.getByTestId('email-draft-tab-1'));
      expect(screen.getByTestId('email-draft-tabs-counter')).toHaveTextContent('2 of 3');
      expect(screen.getByTestId('email-draft-variant-0').parentElement).toHaveStyle({
        display: 'none',
      });
      expect(screen.getByTestId('email-draft-tab-1')).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });

    it("shows the active variant's label in the card header", () => {
      render(<EmailDraftCard data={MULTI} />);
      fireEvent.click(screen.getByTestId('email-draft-tab-1'));
      expect(screen.getByTestId('email-draft-variant-1')).toHaveTextContent(
        'Email draft — Warmer + thank-you',
      );
    });
  });
});
