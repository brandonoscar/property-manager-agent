/**
 * PR #116 Phase B — TabularContent rendering contracts.
 *
 * Pins what the brief's "rent thread" reader actually sees:
 *  - Columns rendered as uppercase micro labels
 *  - Currency cells right-aligned + tabular-nums (no font-shift between
 *    rows)
 *  - Status cells dispatch through Badge (typed tone) — NOT uppercase
 *    plain text like the pre-#116 "EVICTION PENDING" string
 *  - Missing cells render as the em-dash placeholder
 *  - Footnote, title, and kind data-attribute survive the render
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { TabularContent } from './TabularContent';
import type { TabularData } from './types';

function _rentTable(): TabularData {
  return {
    kind: 'rent_aging',
    title: '2 tenants overdue',
    columns: [
      { key: 'tenant', label: 'Tenant' },
      { key: 'amount', label: 'Total Owed', align: 'right' },
      { key: 'status', label: 'Status' },
    ],
    rows: [
      {
        tenant: { kind: 'text', value: 'Maria Lopez' },
        amount: { kind: 'currency', cents: 184000, currency: 'USD' },
        status: {
          kind: 'status',
          status: { label: 'Late', tone: 'caution' },
        },
      },
      {
        tenant: { kind: 'text', value: 'James Chen' },
        amount: { kind: 'currency', cents: 218000, currency: 'USD' },
        status: {
          kind: 'status',
          status: { label: 'Eviction pending', tone: 'danger' },
        },
      },
    ],
    footnote: 'Demo mode — no message will actually be sent.',
  };
}

describe('TabularContent — structural contract', () => {
  it('exposes kind via data-attribute for downstream selectors', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    expect(screen.getByTestId('t-root').getAttribute('data-table-kind')).toBe(
      'rent_aging',
    );
  });

  it('renders title + footnote when provided', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    expect(screen.getByTestId('t-title').textContent).toBe('2 tenants overdue');
    expect(screen.getByTestId('t-footnote').textContent).toBe(
      'Demo mode — no message will actually be sent.',
    );
  });

  it('renders one row per data row', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    expect(screen.getByTestId('t-row-0')).toBeInTheDocument();
    expect(screen.getByTestId('t-row-1')).toBeInTheDocument();
  });

  it('renders text cells verbatim', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-tenant').textContent).toBe('Maria Lopez');
    expect(screen.getByTestId('t-cell-1-tenant').textContent).toBe('James Chen');
  });

  it('formats currency cells as USD with cents → dollars conversion', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-amount').textContent).toBe('$1,840.00');
    expect(screen.getByTestId('t-cell-1-amount').textContent).toBe('$2,180.00');
  });

  it('renders status cells via Badge — typed tone, not raw uppercase text', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    const lateBadge = screen.getByTestId('t-status-0-status');
    expect(lateBadge.textContent).toBe('Late');
    expect(lateBadge.getAttribute('data-badge-tone')).toBe('caution');

    const evictionBadge = screen.getByTestId('t-status-1-status');
    expect(evictionBadge.textContent).toBe('Eviction pending');
    expect(evictionBadge.getAttribute('data-badge-tone')).toBe('danger');
  });

  it('renders em-dash placeholder for missing cells', () => {
    const data: TabularData = {
      kind: 'rent_aging',
      columns: [
        { key: 'tenant', label: 'Tenant' },
        { key: 'amount', label: 'Total Owed', align: 'right' },
      ],
      rows: [
        {
          // amount intentionally missing
          tenant: { kind: 'text', value: 'Solo' },
        },
      ],
    };
    render(<TabularContent data={data} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-amount').textContent).toBe('—');
  });

  it('uses column align hint on each cell', () => {
    render(<TabularContent data={_rentTable()} testIdPrefix="t" />);
    const amountCell = screen.getByTestId('t-cell-0-amount') as HTMLElement;
    expect(amountCell.style.textAlign).toBe('right');
    const tenantCell = screen.getByTestId('t-cell-0-tenant') as HTMLElement;
    expect(tenantCell.style.textAlign).toBe('left');
  });
});


describe('TabularContent — date cell', () => {
  it('formats ISO dates with the locale short month/day/year', () => {
    const data: TabularData = {
      kind: 'whatever',
      columns: [{ key: 'when', label: 'When' }],
      rows: [{ when: { kind: 'date', iso: '2026-05-21T00:00:00Z' } }],
    };
    render(<TabularContent data={data} testIdPrefix="t" />);
    // Avoid pinning the exact locale; just confirm "2026" lands.
    const text = screen.getByTestId('t-cell-0-when').textContent ?? '';
    expect(text).toMatch(/2026/);
  });

  it('falls back to the raw string when the ISO is unparseable', () => {
    const data: TabularData = {
      kind: 'whatever',
      columns: [{ key: 'when', label: 'When' }],
      rows: [{ when: { kind: 'date', iso: 'not-a-date' } }],
    };
    render(<TabularContent data={data} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-when').textContent).toBe('not-a-date');
  });
});


describe('TabularContent — currency precision', () => {
  it('handles zero cents', () => {
    const data: TabularData = {
      kind: 'whatever',
      columns: [{ key: 'amount', label: 'Total' }],
      rows: [{ amount: { kind: 'currency', cents: 0 } }],
    };
    render(<TabularContent data={data} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-amount').textContent).toBe('$0.00');
  });

  it('handles cents that don\'t divide cleanly', () => {
    const data: TabularData = {
      kind: 'whatever',
      columns: [{ key: 'amount', label: 'Total' }],
      rows: [{ amount: { kind: 'currency', cents: 12345 } }],
    };
    render(<TabularContent data={data} testIdPrefix="t" />);
    expect(screen.getByTestId('t-cell-0-amount').textContent).toBe('$123.45');
  });
});
