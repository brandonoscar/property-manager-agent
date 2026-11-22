/**
 * Chart card — a single numeric series the model chose to chart
 * (``show_chart``), self-rendered as inline SVG. No charting library:
 * the design system stays in control (iris fill, hairline grid,
 * tabular-nums labels) and the bundle carries zero new dependencies.
 * Bars for comparison, a line for trend; the model picks which.
 */

import type { ChartData } from './types';

const W = 480;
const H = 180;
const PAD = { top: 14, right: 12, bottom: 26, left: 12 };

function fmt(value: number, unit: ChartData['unit']): string {
  const abs = Math.abs(value);
  const compact =
    abs >= 1000 ? `${(value / 1000).toFixed(abs >= 10_000 ? 0 : 1)}k` : `${value}`;
  if (unit === 'currency') return `$${compact}`;
  if (unit === 'percent') return `${value}%`;
  return compact;
}

export function ChartCard({
  data,
  testIdPrefix = 'chart-card',
}: {
  data: ChartData;
  testIdPrefix?: string;
}) {
  const points = data.points;
  if (points.length < 2) return null;

  const values = points.map((p) => p.value);
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const y = (v: number) => PAD.top + innerH - ((v - min) / span) * innerH;

  const step = innerW / points.length;
  const barW = Math.min(48, step * 0.62);

  const linePath = points
    .map((p, i) => {
      const cx = PAD.left + step * i + step / 2;
      return `${i === 0 ? 'M' : 'L'}${cx.toFixed(1)},${y(p.value).toFixed(1)}`;
    })
    .join(' ');

  // Emphasize the endpoint on a line (the "now" value); on bars, the max.
  const emphasisIdx =
    data.kind === 'line' ? points.length - 1 : values.indexOf(Math.max(...values));

  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      className="my-3 rounded-[var(--r-md)] overflow-hidden"
      style={{
        background: 'var(--canvas)',
        border: '0.5px solid var(--card-edge)',
        boxShadow: 'var(--shadow-sm)',
        maxWidth: 520,
      }}
    >
      <div
        className="font-medium"
        style={{
          padding: '10px 16px',
          fontSize: 14,
          color: 'var(--ink)',
          background: 'var(--canvas-raised)',
          borderBottom: '1px solid var(--line)',
        }}
        data-testid={`${testIdPrefix}-title`}
      >
        {data.title}
      </div>

      <div style={{ padding: '10px 12px 6px' }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={data.title}
          style={{ width: '100%', height: 'auto', display: 'block' }}
          data-testid={`${testIdPrefix}-svg`}
        >
          {/* Hairline gridlines at min / mid / max. */}
          {[max, (max + min) / 2, min].map((gv, i) => (
            <g key={i}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={y(gv)}
                y2={y(gv)}
                stroke="var(--line)"
                strokeWidth={1}
              />
              <text
                x={W - PAD.right}
                y={y(gv) - 3}
                textAnchor="end"
                style={{
                  fontSize: 12,
                  fill: 'var(--ink-faint)',
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {fmt(gv, data.unit)}
              </text>
            </g>
          ))}

          {data.kind === 'bar' ? (
            points.map((p, i) => {
              const cx = PAD.left + step * i + step / 2;
              const yTop = Math.min(y(p.value), y(0));
              const h = Math.max(2, Math.abs(y(p.value) - y(0)));
              return (
                <rect
                  key={i}
                  x={cx - barW / 2}
                  y={yTop}
                  width={barW}
                  height={h}
                  rx={3}
                  fill={i === emphasisIdx ? 'var(--iris)' : 'var(--iris-soft)'}
                  stroke={i === emphasisIdx ? 'none' : 'var(--iris-ring)'}
                  strokeWidth={i === emphasisIdx ? 0 : 1}
                  data-testid={`${testIdPrefix}-bar-${i}`}
                />
              );
            })
          ) : (
            <>
              <path d={linePath} fill="none" stroke="var(--iris)" strokeWidth={2} />
              {points.map((p, i) => (
                <circle
                  key={i}
                  cx={PAD.left + step * i + step / 2}
                  cy={y(p.value)}
                  r={i === emphasisIdx ? 4 : 2.5}
                  fill={i === emphasisIdx ? 'var(--iris)' : 'var(--canvas)'}
                  stroke="var(--iris)"
                  strokeWidth={1.5}
                  data-testid={`${testIdPrefix}-dot-${i}`}
                />
              ))}
            </>
          )}

          {/* X labels — thin every other one when crowded. */}
          {points.map((p, i) => {
            const every = points.length > 12 ? 2 : 1;
            if (i % every !== 0) return null;
            return (
              <text
                key={i}
                x={PAD.left + step * i + step / 2}
                y={H - 8}
                textAnchor="middle"
                style={{ fontSize: 12, fill: 'var(--ink-muted)' }}
              >
                {p.label}
              </text>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
