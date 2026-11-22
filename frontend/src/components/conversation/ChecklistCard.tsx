/**
 * Checklist card — a multi-step plan the model laid out (``show_checklist``),
 * with done-flags it updates by re-emitting as steps complete. Completed
 * steps get a check icon + muted ink — deliberately NOT strikethrough (the
 * design review flagged strikethrough clutter on the hero checklist; the
 * check mark already says "done" without making the text unreadable).
 */

import { Check } from 'lucide-react';
import type { ChecklistData } from './types';

export function ChecklistCard({
  data,
  testIdPrefix = 'checklist-card',
}: {
  data: ChecklistData;
  testIdPrefix?: string;
}) {
  const done = data.items.filter((i) => i.done).length;
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
        <span
          className="font-medium"
          style={{ fontSize: 14, color: 'var(--ink)' }}
          data-testid={`${testIdPrefix}-title`}
        >
          {data.title}
        </span>
        <span
          className="ml-auto font-mono"
          style={{
            fontSize: 12,
            color: 'var(--ink-muted)',
            fontVariantNumeric: 'tabular-nums',
          }}
          data-testid={`${testIdPrefix}-progress`}
        >
          {done} of {data.items.length} done
        </span>
      </div>

      <ul className="flex flex-col" style={{ padding: '8px 16px 10px' }}>
        {data.items.map((item, i) => (
          <li
            key={i}
            className="flex items-start gap-2.5 py-1.5"
            style={i > 0 ? { borderTop: '1px solid var(--line)' } : undefined}
            data-testid={`${testIdPrefix}-item-${i}`}
            data-done={item.done}
          >
            <span
              aria-hidden
              className="shrink-0 flex items-center justify-center rounded-full"
              style={{
                width: 16,
                height: 16,
                marginTop: 1,
                border: item.done ? 'none' : '1.5px solid var(--line-strong)',
                background: item.done ? 'var(--positive)' : 'transparent',
              }}
            >
              {item.done && <Check size={11} strokeWidth={2.5} color="#FFFFFF" />}
            </span>
            <span
              style={{
                fontSize: 13,
                lineHeight: 1.5,
                color: item.done ? 'var(--ink-muted)' : 'var(--ink)',
              }}
            >
              {item.text}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
