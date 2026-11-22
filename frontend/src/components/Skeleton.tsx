/**
 * Shared skeleton primitive (PR #117 Phase D).
 *
 * One component, three variants — replaces ad-hoc shimmer divs
 * scattered across pages.
 *
 * Variants
 *   line    → text-row shimmer (default; for paragraphs / labels)
 *   rect    → block shimmer (cards / images / table cells)
 *   circle  → avatar / icon-button shimmer
 *
 * The animation token (``animate-pulse``) is the existing Tailwind
 * baseline; no new motion was introduced. Color tokens use the
 * --canvas-elevated / --canvas-hover pair from PR #110's foundation.
 */

import type { CSSProperties } from 'react';

export type SkeletonVariant = 'line' | 'rect' | 'circle';

interface SkeletonProps {
  variant?: SkeletonVariant;
  /** Width in CSS units (e.g. ``"100%"``, ``"160px"``). Falls
   *  through to variant defaults when unset. */
  width?: string | number;
  /** Height in CSS units. Falls through to variant defaults. */
  height?: string | number;
  /** Extra Tailwind classes (e.g. layout / spacing). */
  className?: string;
  /** Stable test hook. */
  'data-testid'?: string;
}

const _DEFAULTS: Record<SkeletonVariant, { width: string | number; height: string | number; radius: string }> = {
  line: { width: '100%', height: 10, radius: '4px' },
  rect: { width: '100%', height: 56, radius: '8px' },
  circle: { width: 24, height: 24, radius: '9999px' },
};

export function Skeleton({
  variant = 'line',
  width,
  height,
  className = '',
  'data-testid': testId,
}: SkeletonProps) {
  const d = _DEFAULTS[variant];
  const style: CSSProperties = {
    width: width ?? d.width,
    height: height ?? d.height,
    borderRadius: d.radius,
    background: 'var(--canvas-hover)',
  };
  return (
    <div
      data-testid={testId}
      data-skeleton-variant={variant}
      role="status"
      aria-label="Loading"
      aria-live="polite"
      className={`animate-pulse ${className}`}
      style={style}
    />
  );
}


// ── Composition helpers ────────────────────────────────────────────


/**
 * A common row pattern: title line + two body lines + a footer line.
 * Used in inbox / activity / properties lists during initial load.
 * Sized to roughly match a real row so the layout doesn't pop on
 * transition. Wrap in a div with the same padding as the real row.
 */
export function SkeletonListRow({
  testIdPrefix = 'skeleton-row',
}: {
  testIdPrefix?: string;
}) {
  return (
    <div
      data-testid={testIdPrefix}
      className="rounded-[var(--r-sm)] p-3 border space-y-2"
      style={{
        borderColor: 'var(--line)',
        background: 'var(--canvas-elevated)',
      }}
    >
      <Skeleton variant="line" width="40%" height={9} />
      <Skeleton variant="line" width="75%" height={11} />
      <Skeleton variant="line" width="55%" height={11} />
    </div>
  );
}
