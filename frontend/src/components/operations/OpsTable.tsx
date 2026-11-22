/**
 * OpsTable — the Operations page's sortable table primitive.
 *
 * The design thesis of the page: the table IS the analytics. Every metric
 * column header is clickable — first click sorts descending (you're
 * looking for the biggest number: most spend, most overdue, most open),
 * second click flips ascending, and the active column carries an arrow.
 * No separate dashboard.
 *
 * Rendering follows the Stage-2 system: white card, hairline --card-edge,
 * tinted header band, tabular-nums on every numeric cell.
 */

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';

export interface OpsColumn<Row> {
  key: string;
  label: string;
  /** Extracts the sortable value. Strings sort ascending-first; numbers
   *  and dates sort descending-first (biggest problem on top). */
  value: (row: Row) => string | number | null;
  /** Rendered cell; defaults to String(value). */
  render?: (row: Row) => React.ReactNode;
  /** Right-align + tabular-nums (metric columns). */
  numeric?: boolean;
  width?: number | string;
  /**
   * The column's figure for the WHOLE table, shown in a totals row.
   *
   * ⚠ Opt-in per column, and deliberately a function rather than a `sum: true`
   * flag. Not every numeric column can be added up: occupancy is a RATIO, so
   * summing it across 60 properties produces a number with no meaning that
   * still looks like an answer — and "12.4 occupancy" beside real totals is
   * the kind of figure somebody quotes. A column that cannot be aggregated
   * simply does not supply this and its cell stays blank.
   */
  total?: (rows: Row[]) => React.ReactNode;
}

export function OpsTable<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  defaultSort,
  emptyText,
  testId,
}: {
  columns: Array<OpsColumn<Row>>;
  rows: Row[];
  rowKey: (row: Row) => string | number;
  onRowClick?: (row: Row) => void;
  /** Column key to sort by initially (descending for numeric columns). */
  defaultSort?: string;
  emptyText: string;
  testId: string;
}) {
  const [sortKey, setSortKey] = useState<string | null>(defaultSort ?? null);
  const [asc, setAsc] = useState(false);

  const sortCol = columns.find((c) => c.key === sortKey) ?? null;

  const sorted = useMemo(() => {
    if (!sortCol) return rows;
    const dir = asc ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = sortCol.value(a);
      const vb = sortCol.value(b);
      // Nulls always sink to the bottom regardless of direction — a vendor
      // with no data isn't "best" OR "worst", it's unknown.
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
      return String(va).localeCompare(String(vb)) * dir;
    });
  }, [rows, sortCol, asc]);

  const clickHeader = (col: OpsColumn<Row>) => {
    if (sortKey === col.key) {
      setAsc((v) => !v);
    } else {
      setSortKey(col.key);
      // Numbers: descending first (biggest on top). Text: ascending first.
      setAsc(!col.numeric);
    }
  };

  return (
    <div
      data-testid={testId}
      className="rounded-[var(--r-md)] overflow-x-auto"
      style={{ background: 'var(--canvas)', border: '0.5px solid var(--card-edge)' }}
    >
      <table className="w-full text-[13px]" style={{ color: 'var(--ink)', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: 'var(--canvas-raised)' }}>
            {columns.map((col) => {
              const active = sortKey === col.key;
              return (
                <th
                  key={col.key}
                  onClick={() => clickHeader(col)}
                  data-testid={`${testId}-sort-${col.key}`}
                  aria-sort={active ? (asc ? 'ascending' : 'descending') : 'none'}
                  className="cursor-pointer select-none whitespace-nowrap"
                  // Sentence case, the same header as chat tables
                  // (TabularContent): the site sets no tracked caps.
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: active ? 'var(--ink)' : 'var(--ink-subtle)',
                    padding: '8px 12px',
                    textAlign: col.numeric ? 'right' : 'left',
                    borderBottom: '1px solid var(--line)',
                    width: col.width,
                  }}
                >
                  <span className="inline-flex items-center gap-1">
                    {col.label}
                    {active &&
                      (asc ? (
                        <ChevronUp size={11} strokeWidth={2} />
                      ) : (
                        <ChevronDown size={11} strokeWidth={2} />
                      ))}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr>
              <td
                colSpan={columns.length}
                className="text-center"
                style={{ color: 'var(--ink-subtle)', padding: '28px 12px' }}
              >
                {emptyText}
              </td>
            </tr>
          )}
          {sorted.map((row) => (
            <tr
              key={rowKey(row)}
              data-testid={`${testId}-row-${rowKey(row)}`}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              // ⚠ A clickable row with no keyboard path is not a slow control,
              // it is an ABSENT one — Tab never lands on it and Enter has
              // nothing to act on, so the whole table is mouse-only. The
              // attributes are conditional on `onRowClick` for the same
              // reason: a tab stop on a row that does nothing is a stop the
              // keyboard user has to skip past for no reward.
              tabIndex={onRowClick ? 0 : undefined}
              role={onRowClick ? 'button' : undefined}
              onKeyDown={
                onRowClick
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        // Space scrolls the page otherwise, which moves the
                        // table out from under the row being activated.
                        e.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
              className={onRowClick ? 'cursor-pointer transition-colors' : undefined}
              style={{ borderBottom: '0.5px solid var(--line)' }}
              onMouseEnter={(e) => {
                if (onRowClick) e.currentTarget.style.background = 'var(--canvas-hover)';
              }}
              onMouseLeave={(e) => {
                if (onRowClick) e.currentTarget.style.background = '';
              }}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className="whitespace-nowrap"
                  style={{
                    padding: '9px 12px',
                    textAlign: col.numeric ? 'right' : 'left',
                    fontVariantNumeric: col.numeric ? 'tabular-nums' : undefined,
                  }}
                >
                  {col.render ? col.render(row) : (col.value(row) ?? '—')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {/* ⚠ Rendered only when a column actually declares a total, and only
            when there is more than one row — a "total" under a single row is
            that row again, which reads as a duplicate rather than a sum. The
            counts a manager wants are portfolio-wide (open work orders, money
            owed) and the page's own thesis is that the table IS the
            analytics; without this the only way to get them was to add up
            sixty rows by eye. */}
        {sorted.length > 1 && columns.some((c) => c.total) && (
          <tfoot>
            <tr
              data-testid={`${testId}-totals`}
              style={{ borderTop: '1px solid var(--line-strong)' }}
            >
              {columns.map((col, i) => (
                <td
                  key={col.key}
                  className="whitespace-nowrap font-medium"
                  style={{
                    padding: '9px 12px',
                    textAlign: col.numeric ? 'right' : 'left',
                    fontVariantNumeric: col.numeric ? 'tabular-nums' : undefined,
                    color: 'var(--ink)',
                  }}
                >
                  {/* The first cell labels the row. Without it a bold line of
                      numbers under the table is ambiguous between a total and
                      one more record. */}
                  {col.total
                    ? col.total(sorted)
                    : i === 0
                      ? `All ${sorted.length}`
                      : null}
                </td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}
