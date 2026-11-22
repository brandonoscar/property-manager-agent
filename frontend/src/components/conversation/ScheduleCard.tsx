/**
 * Schedule strip — a day's time slots (``show_schedule``): proposed showing
 * times, vendor visits. Slots are the model's own worked-out times; states
 * are proposed (white + edge), confirmed (iris-soft, the one filled state),
 * and busy (struck, unavailable — shown for honesty about the day).
 */

import { CalendarDays } from 'lucide-react';
import type { ScheduleData } from './types';

export function ScheduleCard({
  data,
  testIdPrefix = 'schedule-card',
}: {
  data: ScheduleData;
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
        className="flex items-center gap-2"
        style={{
          padding: '10px 16px',
          background: 'var(--canvas-raised)',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <CalendarDays size={14} strokeWidth={1.5} style={{ color: 'var(--iris)' }} />
        <span
          className="font-medium"
          style={{ fontSize: 14, color: 'var(--ink)' }}
          data-testid={`${testIdPrefix}-title`}
        >
          {data.title}
        </span>
        {data.day && (
          <span
            className="ml-auto uppercase"
            style={{ fontSize: 12, letterSpacing: '0.1em', color: 'var(--ink-muted)' }}
            data-testid={`${testIdPrefix}-day`}
          >
            {data.day}
          </span>
        )}
      </div>

      <div className="flex flex-col gap-1.5" style={{ padding: '12px 16px' }}>
        {data.slots.map((s, i) => {
          const busy = s.state === 'busy';
          const confirmed = s.state === 'confirmed';
          return (
            <div
              key={i}
              className="flex items-center gap-3 rounded-[var(--r-sm)]"
              style={{
                padding: '7px 12px',
                border: confirmed
                  ? '0.5px solid var(--iris-ring)'
                  : '0.5px solid var(--card-edge)',
                background: confirmed ? 'var(--iris-soft)' : 'var(--canvas)',
                opacity: busy ? 0.55 : 1,
              }}
              data-testid={`${testIdPrefix}-slot-${i}`}
              data-state={s.state}
            >
              <span
                className="font-mono shrink-0"
                style={{
                  fontSize: 12,
                  color: confirmed ? 'var(--iris)' : 'var(--ink-muted)',
                  fontVariantNumeric: 'tabular-nums',
                  minWidth: 88,
                }}
              >
                {s.time}
              </span>
              <span
                className="min-w-0 truncate"
                style={{
                  fontSize: 13,
                  color: 'var(--ink)',
                  textDecoration: busy ? 'line-through' : 'none',
                }}
              >
                {s.label}
              </span>
              {confirmed && (
                <span
                  className="ml-auto uppercase shrink-0"
                  style={{ fontSize: 12, letterSpacing: '0.1em', color: 'var(--iris)' }}
                >
                  Confirmed
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
