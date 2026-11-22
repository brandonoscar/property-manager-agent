/**
 * Structured tabular renderer (PR #116 Phase B).
 *
 * The brief's highest-leverage UX win: when the agent has an answer
 * that's actually a table (rent aging report, maintenance list),
 * render it as a SCANNABLE TABLE with right-aligned monospaced
 * currency, typed status badges, and proper column headers — not as
 * a markdown bullet list with inline strings like
 * ``[61-90d $350.00, 0-30d $170.00] — EVICTION PENDING``.
 *
 * Inputs come from the agent's ``tabular_data`` event, typed in
 * ``./types.ts``. Each cell is a tagged-union (text / currency /
 * status / date) so the renderer dispatches correctly without
 * format-sniffing.
 */

import type { TabularCellValue, TabularData } from './types';
import { Badge, type BadgeTone } from '../Badge';
import { formatCalendarDate, NO_DATE } from '../../lib/calendarDate';

interface TabularContentProps {
  data: TabularData;
  /** Test hook prefix for the rendered table. */
  testIdPrefix?: string;
}

export function TabularContent({
  data,
  testIdPrefix = 'tabular',
}: TabularContentProps) {
  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      data-table-kind={data.kind}
      className="my-3 rounded-[var(--r-md)] overflow-hidden"
      style={{
        background: 'var(--canvas)',
        border: '0.5px solid var(--card-edge)',
      }}
    >
      {data.title ? (
        <div
          className="font-medium"
          style={{
            color: 'var(--ink)',
            fontSize: 'var(--text-section)',
            background: 'var(--canvas-raised)',
            borderBottom: '1px solid var(--line)',
            padding: '8px 16px',
          }}
          data-testid={`${testIdPrefix}-title`}
        >
          {data.title}
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <table
          className="w-full"
          style={{ borderCollapse: 'collapse' }}
          data-testid={`${testIdPrefix}-table`}
        >
          <thead>
            <tr>
              {data.columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  // The app's table style, shared with Operations (OpsTable):
                  // sentence-case 12px headers on a raised row, not tracked caps.
                  style={{
                    color: 'var(--ink-subtle)',
                    fontSize: 12,
                    fontWeight: 600,
                    background: 'var(--canvas-raised)',
                    textAlign: col.align ?? 'left',
                    borderBottom: '1px solid var(--line)',
                    padding: '8px 12px',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {col.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, rowIdx) => (
              <tr
                key={rowIdx}
                data-testid={`${testIdPrefix}-row-${rowIdx}`}
                style={{
                  borderBottom:
                    rowIdx === data.rows.length - 1
                      ? 'none'
                      : '1px solid var(--line)',
                }}
              >
                {data.columns.map((col) => {
                  const cell = row[col.key];
                  return (
                    <td
                      key={col.key}
                      style={{
                        color: 'var(--ink)',
                        fontSize: 13,
                        textAlign: col.align ?? 'left',
                        verticalAlign: 'middle',
                        padding: '9px 12px',
                        whiteSpace: 'nowrap',
                        fontVariantNumeric: col.align === 'right' ? 'tabular-nums' : undefined,
                      }}
                      data-testid={`${testIdPrefix}-cell-${rowIdx}-${col.key}`}
                    >
                      {_renderCell(cell, testIdPrefix, rowIdx, col.key)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data.footnote ? (
        <div
          className="italic"
          style={{
            color: 'var(--ink-muted)',
            fontSize: 'var(--text-micro)',
            borderTop: '1px solid var(--line)',
            padding: '8px 16px',
          }}
          data-testid={`${testIdPrefix}-footnote`}
        >
          {data.footnote}
        </div>
      ) : null}
    </div>
  );
}


// ── Cell dispatch ────────────────────────────────────────────────


function _renderCell(
  cell: TabularCellValue | undefined,
  testIdPrefix: string,
  rowIdx: number,
  key: string,
) {
  if (cell === undefined) {
    return (
      <span style={{ color: 'var(--ink-faint)' }}>—</span>
    );
  }
  switch (cell.kind) {
    case 'text':
      return <span>{cell.value}</span>;
    case 'currency':
      return (
        <span
          style={{
            fontVariantNumeric: 'tabular-nums',
            fontFeatureSettings: '"tnum"',
          }}
        >
          {_formatCurrency(cell.cents, cell.currency)}
        </span>
      );
    case 'date':
      return <span>{_formatDate(cell.iso)}</span>;
    case 'status':
      return (
        <Badge
          tone={cell.status.tone as BadgeTone}
          data-testid={`${testIdPrefix}-status-${rowIdx}-${key}`}
        >
          {cell.status.label}
        </Badge>
      );
  }
}

function _formatCurrency(cents: number, currency: 'USD' | undefined): string {
  const dollars = cents / 100;
  const fmt = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: currency ?? 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return fmt.format(dollars);
}

function _formatDate(iso: string): string {
  // A date cell in an agent table is a Buildium calendar date, not an instant —
  // see lib/calendarDate. Keeps the previous behaviour of echoing an
  // unparseable value rather than replacing it with a dash.
  const out = formatCalendarDate(iso, 'short');
  return out === NO_DATE ? iso : out;
}
