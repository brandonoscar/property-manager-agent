/**
 * Status badge (PR #116 Phase B).
 *
 * Typed status pill — the brief's stake-in-the-ground that ends
 * stringly-typed uppercase status text like "EVICTION PENDING"
 * floating in the middle of markdown bullets. The agent emits a
 * typed status value with a tone enum; this component maps each
 * tone to an existing semantic token (--positive / --caution /
 * --danger / neutral) so the no-color-changes rule holds.
 *
 * Tone vocabulary kept narrow on purpose. Adding a new tone is
 * deliberate; renaming an existing one cascades through every
 * status site at once via the shared token mapping.
 */

import type { ReactNode } from 'react';

export type BadgeTone =
  | 'neutral'
  | 'positive'
  | 'caution'
  | 'danger'
  | 'info';

export type BadgeSize = 'sm' | 'md';

interface BadgeProps {
  tone?: BadgeTone;
  size?: BadgeSize;
  children: ReactNode;
  /** Optional icon rendered before the label. */
  icon?: ReactNode;
  /** Native title attribute — hover tooltip. */
  title?: string;
  'data-testid'?: string;
}

const _TONE: Record<BadgeTone, { bg: string; fg: string; border: string }> = {
  neutral: {
    bg: 'var(--canvas-elevated)',
    fg: 'var(--ink-muted)',
    border: 'var(--line)',
  },
  positive: {
    bg: 'var(--positive-bg)',
    fg: 'var(--positive)',
    border: 'var(--positive-bg)',
  },
  caution: {
    bg: 'var(--caution-bg)',
    fg: 'var(--caution)',
    border: 'var(--caution-bg)',
  },
  danger: {
    bg: 'var(--danger-bg)',
    fg: 'var(--danger)',
    border: 'var(--danger)',
  },
  info: {
    bg: 'var(--info-bg)',
    fg: 'var(--info)',
    border: 'var(--info-bg)',
  },
};

const _HEIGHT_PX: Record<BadgeSize, number> = {
  sm: 20,
  md: 24,
};

const _FONT_PX: Record<BadgeSize, number> = {
  sm: 12,
  md: 12,
};

export function Badge({
  tone = 'neutral',
  size = 'sm',
  children,
  icon,
  title,
  'data-testid': testId,
}: BadgeProps) {
  const t = _TONE[tone];
  return (
    <span
      data-testid={testId}
      data-badge-tone={tone}
      title={title}
      className="inline-flex items-center gap-1 px-2 font-medium rounded-full whitespace-nowrap"
      style={{
        height: _HEIGHT_PX[size],
        fontSize: _FONT_PX[size],
        letterSpacing: '0.02em',
        background: t.bg,
        color: t.fg,
        border: `1px solid ${t.border}`,
      }}
    >
      {icon}
      {children}
    </span>
  );
}
