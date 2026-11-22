/**
 * Gen-UI wave-2 cards — DetailCard / ChartCard / ScheduleCard / ChecklistCard.
 * Pins each card's contract: model-composed content renders verbatim, states
 * map to the right visual (badge tone, confirmed slot, done tick), and the
 * chart renders real SVG marks from the numeric series.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DetailCard } from './DetailCard';
import { ChartCard } from './ChartCard';
import { ScheduleCard } from './ScheduleCard';
import { ChecklistCard } from './ChecklistCard';

describe('DetailCard', () => {
  it('renders title, status badge, subtitle, and labeled fields', () => {
    render(
      <DetailCard
        data={{
          title: 'AC not cooling — unit 4B',
          subtitle: '128 Lexington Ave',
          status: 'In progress',
          tone: 'caution',
          fields: [
            { label: 'Vendor', value: 'Northside Plumbing' },
            { label: 'Priority', value: 'High' },
          ],
          footnote: '3rd similar issue in 90 days.',
        }}
      />,
    );
    expect(screen.getByTestId('detail-card-title')).toHaveTextContent(
      'AC not cooling — unit 4B',
    );
    expect(screen.getByTestId('detail-card-status')).toHaveTextContent('In progress');
    expect(screen.getByTestId('detail-card-field-0')).toHaveTextContent('Vendor');
    expect(screen.getByTestId('detail-card-field-0')).toHaveTextContent(
      'Northside Plumbing',
    );
    expect(screen.getByTestId('detail-card-footnote')).toHaveTextContent(
      '3rd similar issue',
    );
  });
});

describe('ChartCard', () => {
  const points = [
    { label: 'Mar', value: 11400 },
    { label: 'Apr', value: 11850 },
    { label: 'May', value: 12800 },
  ];

  it('renders one bar per point with the max emphasized', () => {
    render(<ChartCard data={{ title: 'Rent', kind: 'bar', unit: 'currency', points }} />);
    expect(screen.getByTestId('chart-card-bar-0')).toBeInTheDocument();
    expect(screen.getByTestId('chart-card-bar-2')).toHaveAttribute(
      'fill',
      'var(--iris)',
    );
    expect(screen.getByTestId('chart-card-bar-0')).toHaveAttribute(
      'fill',
      'var(--iris-soft)',
    );
  });

  it('renders dots on a line chart with the endpoint emphasized', () => {
    render(<ChartCard data={{ title: 'NOI', kind: 'line', unit: 'currency', points }} />);
    expect(screen.getByTestId('chart-card-dot-2')).toHaveAttribute('fill', 'var(--iris)');
  });

  it('renders nothing with fewer than two points', () => {
    const { container } = render(
      <ChartCard
        data={{ title: 'x', kind: 'bar', unit: 'count', points: [points[0]] }}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('ScheduleCard', () => {
  it('renders slots with confirmed fill and busy struck', () => {
    render(
      <ScheduleCard
        data={{
          title: 'Showings — Unit 2B',
          day: 'Thu Aug 21',
          slots: [
            { time: '9:00–9:30', label: 'Alicia Reyes', state: 'proposed' },
            { time: '10:00–10:30', label: 'Jordan Park', state: 'confirmed' },
            { time: '11:00–12:00', label: 'Vendor visit', state: 'busy' },
          ],
        }}
      />,
    );
    expect(screen.getByTestId('schedule-card-day')).toHaveTextContent('Thu Aug 21');
    expect(screen.getByTestId('schedule-card-slot-1')).toHaveAttribute(
      'data-state',
      'confirmed',
    );
    expect(screen.getByTestId('schedule-card-slot-1')).toHaveTextContent('Confirmed');
    expect(screen.getByTestId('schedule-card-slot-2')).toHaveAttribute(
      'data-state',
      'busy',
    );
  });
});

describe('ChecklistCard', () => {
  it('renders progress and per-item done state without strikethrough', () => {
    render(
      <ChecklistCard
        data={{
          title: 'Turnover plan — Unit 1A',
          items: [
            { text: 'Move-out inspection', done: true },
            { text: 'Make-ready work orders', done: false },
            { text: 'List the unit', done: false },
          ],
        }}
      />,
    );
    expect(screen.getByTestId('checklist-card-progress')).toHaveTextContent('1 of 3 done');
    expect(screen.getByTestId('checklist-card-item-0')).toHaveAttribute(
      'data-done',
      'true',
    );
    // Done items are muted, never struck — the review flagged strikethrough
    // as clutter; the check mark carries the state.
    expect(screen.getByTestId('checklist-card-item-0')).not.toHaveStyle({
      textDecoration: 'line-through',
    });
  });
});
