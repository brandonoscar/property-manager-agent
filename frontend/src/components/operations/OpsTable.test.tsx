/**
 * OpsTable — the sort contract the Operations page depends on:
 * numeric columns sort DESCENDING on first click (biggest problem on
 * top), text ascending, second click flips, nulls always sink.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { OpsTable, type OpsColumn } from './OpsTable';

interface Row {
  id: number;
  name: string;
  spend: number | null;
}

const COLS: Array<OpsColumn<Row>> = [
  { key: 'name', label: 'Name', value: (r) => r.name },
  { key: 'spend', label: 'Spend', numeric: true, value: (r) => r.spend },
];

// A third column that is numeric and CANNOT be added up — the case the
// opt-in `total` function exists for.
const WITH_TOTAL: Array<OpsColumn<Row>> = [
  COLS[0],
  {
    ...COLS[1],
    total: (rows) => rows.reduce((n, r) => n + (r.spend ?? 0), 0),
  },
  { key: 'ratio', label: 'Ratio', numeric: true, value: () => 0.5 },
];

const ROWS: Row[] = [
  { id: 1, name: 'Beta', spend: 50 },
  { id: 2, name: 'Alpha', spend: null },
  { id: 3, name: 'Gamma', spend: 900 },
];

function names(): string[] {
  return screen
    .getAllByTestId(/t-row-/)
    .map((tr) => tr.querySelector('td')?.textContent ?? '');
}

describe('OpsTable', () => {
  it('numeric defaultSort puts the biggest number first and sinks nulls', () => {
    render(
      <OpsTable testId="t" columns={COLS} rows={ROWS} rowKey={(r) => r.id} defaultSort="spend" emptyText="none" />,
    );
    expect(names()).toEqual(['Gamma', 'Beta', 'Alpha']);
  });

  it('second click on the active column flips to ascending — nulls still last', () => {
    render(
      <OpsTable testId="t" columns={COLS} rows={ROWS} rowKey={(r) => r.id} defaultSort="spend" emptyText="none" />,
    );
    fireEvent.click(screen.getByTestId('t-sort-spend'));
    expect(names()).toEqual(['Beta', 'Gamma', 'Alpha']);
  });

  it('text columns sort ascending on first click', () => {
    render(<OpsTable testId="t" columns={COLS} rows={ROWS} rowKey={(r) => r.id} emptyText="none" />);
    fireEvent.click(screen.getByTestId('t-sort-name'));
    expect(names()).toEqual(['Alpha', 'Beta', 'Gamma']);
  });

  it('has no totals row until a column asks for one', () => {
    // The default. Every table on the page predates totals and must be
    // unchanged by their arrival.
    render(<OpsTable testId="t" columns={COLS} rows={ROWS} rowKey={(r) => r.id} emptyText="none" />);
    expect(screen.queryByTestId('t-totals')).toBeNull();
  });

  it('adds up the whole table, not the visible sort order', () => {
    render(
      <OpsTable
        testId="t"
        columns={WITH_TOTAL}
        rows={ROWS}
        rowKey={(r) => r.id}
        defaultSort="spend"
        emptyText="none"
      />,
    );
    const totals = screen.getByTestId('t-totals');
    expect(totals).toHaveTextContent('950'); // 50 + 900, null contributing nothing
    // Sorting is a view; it must not change what a total means.
    fireEvent.click(screen.getByTestId('t-sort-spend'));
    expect(screen.getByTestId('t-totals')).toHaveTextContent('950');
  });

  it('labels the row so it cannot be read as one more record', () => {
    // A bold line of numbers under the last row is ambiguous between a total
    // and another entry — the first cell is what disambiguates it.
    render(
      <OpsTable testId="t" columns={WITH_TOTAL} rows={ROWS} rowKey={(r) => r.id} emptyText="none" />,
    );
    expect(screen.getByTestId('t-totals')).toHaveTextContent('All 3');
  });

  it('leaves a column that declares no total BLANK rather than guessing', () => {
    // The load-bearing half: `total` is opt-in per column because some
    // numeric columns cannot be added up (a ratio, an average with no
    // weight). Filling those in would print a plausible wrong number.
    render(
      <OpsTable testId="t" columns={WITH_TOTAL} rows={ROWS} rowKey={(r) => r.id} emptyText="none" />,
    );
    const cells = screen.getByTestId('t-totals').querySelectorAll('td');
    expect(cells).toHaveLength(3);
    expect(cells[2].textContent).toBe(''); // 'ratio' declares no total
  });

  it('says nothing about one row, or none', () => {
    // A "total" under a single row is that row again, printed twice in
    // different weights — it reads as a duplicate, not as a sum.
    const { rerender } = render(
      <OpsTable testId="t" columns={WITH_TOTAL} rows={[ROWS[0]]} rowKey={(r) => r.id} emptyText="none" />,
    );
    expect(screen.queryByTestId('t-totals')).toBeNull();
    rerender(
      <OpsTable testId="t" columns={WITH_TOTAL} rows={[]} rowKey={(r) => r.id} emptyText="none" />,
    );
    expect(screen.queryByTestId('t-totals')).toBeNull();
  });

  it('fires onRowClick and shows the empty state', () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <OpsTable testId="t" columns={COLS} rows={ROWS} rowKey={(r) => r.id} onRowClick={onClick} emptyText="none" />,
    );
    fireEvent.click(screen.getByTestId('t-row-3'));
    expect(onClick).toHaveBeenCalledWith(ROWS[2]);
    rerender(
      <OpsTable testId="t" columns={COLS} rows={[]} rowKey={(r) => r.id} emptyText="nothing here" />,
    );
    expect(screen.getByText('nothing here')).toBeInTheDocument();
  });
});
