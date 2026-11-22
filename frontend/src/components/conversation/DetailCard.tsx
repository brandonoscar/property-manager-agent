/**
 * Detail card — ONE record's facts (a work order, tenant, lease, vendor)
 * as a scannable card instead of a paragraph. The model picks the labels
 * and values for this specific answer (``show_detail_card``); the card is
 * a frame, never a fixed form. Styling mirrors the Inbox/cockpit language:
 * white card, hairline edge, caps-mono micro-labels, Badge for status.
 */

import { Badge, type BadgeTone } from '../Badge';
import type { DetailCardData } from './types';

const TONE_MAP: Record<DetailCardData['tone'], BadgeTone> = {
  neutral: 'neutral',
  good: 'positive',
  caution: 'caution',
  danger: 'danger',
};

export function DetailCard({
  data,
  testIdPrefix = 'detail-card',
}: {
  data: DetailCardData;
  testIdPrefix?: string;
}) {
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
        style={{
          padding: '12px 16px',
          background: 'var(--canvas-raised)',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="font-medium truncate"
            style={{ fontSize: 14, color: 'var(--ink)' }}
            data-testid={`${testIdPrefix}-title`}
          >
            {data.title}
          </span>
          {data.status && (
            <span className="shrink-0" data-testid={`${testIdPrefix}-status`}>
              <Badge tone={TONE_MAP[data.tone]}>{data.status}</Badge>
            </span>
          )}
        </div>
        {data.subtitle && (
          <div
            style={{ fontSize: 13, color: 'var(--ink-muted)', marginTop: 2 }}
            data-testid={`${testIdPrefix}-subtitle`}
          >
            {data.subtitle}
          </div>
        )}
      </div>

      <dl className="flex flex-col" style={{ padding: '6px 16px 10px' }}>
        {data.fields.map((f, i) => (
          <div
            key={i}
            className="flex items-baseline gap-3 py-1.5"
            style={i > 0 ? { borderTop: '1px solid var(--line)' } : undefined}
            data-testid={`${testIdPrefix}-field-${i}`}
          >
            <dt
              className=" uppercase shrink-0"
              style={{
                fontSize: 12,
                letterSpacing: '0.12em',
                color: 'var(--ink-muted)',
                width: 120,
              }}
            >
              {f.label}
            </dt>
            <dd
              className="min-w-0"
              style={{
                fontSize: 13,
                color: 'var(--ink)',
                overflowWrap: 'break-word',
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {f.value}
            </dd>
          </div>
        ))}
      </dl>

      {data.footnote && (
        <div
          style={{
            padding: '7px 16px',
            borderTop: '1px solid var(--line)',
            fontSize: 12,
            color: 'var(--ink-muted)',
          }}
          data-testid={`${testIdPrefix}-footnote`}
        >
          {data.footnote}
        </div>
      )}
    </div>
  );
}
