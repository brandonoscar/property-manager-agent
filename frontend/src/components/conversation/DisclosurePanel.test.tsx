/**
 * S6 disclosure UI — the "How I got this" receipts panel (fast-follow to
 * PR #680, which started streaming the envelope).
 *
 * These pin the contract: ``parseDisclosure`` accepts both wire shapes
 * ({data: env} and env directly) and rejects junk; the panel collapses by
 * default with the trail·confidence summary; expanding reveals the
 * Checked / Sources / Confidence / Not verified sections; an empty
 * envelope renders nothing at all.
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { DisclosurePanel, parseDisclosure } from './DisclosurePanel';
import type { DisclosureData } from './DisclosurePanel';

const ENVELOPE = {
  answer: 'Unit 4B owes $2,400 and is 14 days late.',
  reasoning: {
    sources: ['buildium: $2,400 past due on lease 88'],
    confidence: 'high',
    confidence_reason: 'mirror data as of last sync',
    unverified: ['current-day payments (mirror may lag)'],
    footer_well_formed: true,
    specialist_trail: ['check_known_state', 'buildium_agent'],
  },
  audit: { run_id: 'run-1', trace_id: 'trace-1' },
};

describe('parseDisclosure', () => {
  it('accepts the bare envelope', () => {
    const parsed = parseDisclosure(ENVELOPE);
    expect(parsed).not.toBeNull();
    expect(parsed!.reasoning.sources).toHaveLength(1);
    expect(parsed!.reasoning.confidence).toBe('high');
    expect(parsed!.reasoning.specialist_trail).toEqual([
      'check_known_state',
      'buildium_agent',
    ]);
  });

  it('accepts the {data: envelope} wrapper', () => {
    const parsed = parseDisclosure({ data: ENVELOPE });
    expect(parsed).not.toBeNull();
    expect(parsed!.answer).toBe(ENVELOPE.answer);
  });

  it('rejects junk and shapes without reasoning', () => {
    expect(parseDisclosure(null)).toBeNull();
    expect(parseDisclosure('nope')).toBeNull();
    expect(parseDisclosure({})).toBeNull();
    expect(parseDisclosure({ data: { answer: 'x' } })).toBeNull();
  });

  it('coerces missing reasoning fields to safe empties', () => {
    const parsed = parseDisclosure({ reasoning: {} });
    expect(parsed).not.toBeNull();
    expect(parsed!.reasoning.sources).toEqual([]);
    expect(parsed!.reasoning.confidence).toBeNull();
    expect(parsed!.reasoning.unverified).toEqual([]);
  });
});

describe('DisclosurePanel', () => {
  const data = parseDisclosure(ENVELOPE) as DisclosureData;

  it('collapses by default with the trail·confidence summary', () => {
    render(<DisclosurePanel data={data} testIdPrefix="d1" />);
    const toggle = screen.getByTestId('d1-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('How I got this');
    // Specialist names are humanized in the summary row.
    expect(toggle.textContent).toContain('Known state → Buildium');
    expect(toggle.textContent).toContain('high');
    expect(screen.queryByTestId('d1-sources')).toBeNull();
  });

  it('expands to the receipts sections on click', () => {
    render(<DisclosurePanel data={data} testIdPrefix="d2" />);
    fireEvent.click(screen.getByTestId('d2-toggle'));
    expect(screen.getByTestId('d2-trail').textContent).toContain(
      'Known state → Buildium',
    );
    expect(screen.getByTestId('d2-sources').textContent).toContain(
      'buildium: $2,400 past due on lease 88',
    );
    expect(screen.getByTestId('d2-confidence').textContent).toContain(
      'high — mirror data as of last sync',
    );
    expect(screen.getByTestId('d2-unverified').textContent).toContain(
      'current-day payments (mirror may lag)',
    );
  });

  it('renders nothing for an empty envelope', () => {
    const empty = parseDisclosure({
      reasoning: {},
    }) as DisclosureData;
    const { container } = render(
      <DisclosurePanel data={empty} testIdPrefix="d3" />,
    );
    expect(container.firstChild).toBeNull();
  });
});
