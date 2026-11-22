/**
 * Next-steps card — the work loop's hand-off.
 *
 * The product rule these pin: a step is only useful if clicking it sends the
 * REAL request, and the note offer is an OFFER — clicking it asks, it never
 * writes. A card that silently wrote notes on a manager's records would be a
 * trust failure, not a feature.
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import { NextStepsCard } from './NextStepsCard';

const DATA = {
  steps: [
    { label: 'Draft the late notices', prompt: 'Draft late notices for the ten 61-90 day accounts' },
    { label: 'Check the leak first', prompt: "Show me the open maintenance at Whitfield's unit" },
  ],
  note_hint: "Note that Whitfield's leak blocks collections",
};

describe('NextStepsCard', () => {
  it('sends the full prompt, not the chip label', () => {
    // The label is scannable shorthand; the prompt is the request. Sending
    // the label would ask the agent a different, vaguer question than the
    // one it just proposed.
    const onPick = vi.fn();
    render(<NextStepsCard data={DATA} onPick={onPick} />);

    fireEvent.click(screen.getByText('Draft the late notices'));
    expect(onPick).toHaveBeenCalledWith('Draft late notices for the ten 61-90 day accounts');
  });

  it('offers the note rather than writing it', () => {
    const onPick = vi.fn();
    render(<NextStepsCard data={DATA} onPick={onPick} />);

    fireEvent.click(screen.getByTestId('next-steps-note'));
    expect(onPick).toHaveBeenCalledWith("Note that Whitfield's leak blocks collections");
  });

  it('renders nothing when there is nothing to offer', () => {
    // No chip row under a reply that has no natural follow-up — an empty
    // "Next" heading is wallpaper, and wallpaper teaches users to skip the
    // one place the recommendation lives.
    const { container } = render(<NextStepsCard data={{ steps: [] }} />);
    expect(container.firstChild).toBeNull();
  });

  it('stays inert without a handler instead of looking clickable', () => {
    render(<NextStepsCard data={DATA} />);
    const steps = screen.getAllByTestId('next-steps-step');
    expect(steps.every((s) => (s as HTMLButtonElement).disabled)).toBe(true);
  });

  it('shows the note offer even with no steps', () => {
    render(<NextStepsCard data={{ steps: [], note_hint: 'Write down the vendor decision' }} />);
    expect(screen.getByTestId('next-steps-note')).toBeTruthy();
  });
});
