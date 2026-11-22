/**
 * Shared empty-state primitive (PR #117 Phase D).
 *
 * The brief's distinct-states rule says: loading shimmer ≠ empty
 * illustration ≠ error retry. This component is the shared empty
 * branch. Renders an icon, a headline, optional body copy, and an
 * optional primary CTA.
 *
 * Replaces ad-hoc empty divs across pages (Inbox / Team Activity).
 * Existing in-file ``EmptyState`` helpers in InboxPage.tsx and
 * ActivityPage.tsx remain for now; Phase E will fold them into this
 * shared primitive once the surrounding layouts settle.
 */

import type { ReactNode } from 'react';

import { Button } from './Button';

interface EmptyStateProps {
  /** Lucide icon or any ReactNode. Rendered above the headline. */
  icon?: ReactNode;
  /** Headline copy — one short sentence. Sentence case. */
  title: string;
  /** Body copy — one or two short sentences. Optional. */
  body?: ReactNode;
  /** Primary CTA label. Renders nothing if omitted. */
  ctaLabel?: string;
  /** Primary CTA click handler. */
  onCta?: () => void;
  /** Test hook. */
  'data-testid'?: string;
}

export function EmptyState({
  icon,
  title,
  body,
  ctaLabel,
  onCta,
  'data-testid': testId = 'empty-state',
}: EmptyStateProps) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col items-center justify-center text-center px-6 py-10 gap-3"
    >
      {icon ? (
        <div
          data-testid={`${testId}-icon`}
          className="flex items-center justify-center w-12 h-12 rounded-full"
          style={{
            background: 'var(--canvas-elevated)',
            color: 'var(--ink-muted)',
            border: '1px solid var(--line)',
          }}
        >
          {icon}
        </div>
      ) : null}
      <div
        data-testid={`${testId}-title`}
        className="font-medium"
        style={{
          color: 'var(--ink)',
          fontSize: 'var(--text-section)',
        }}
      >
        {title}
      </div>
      {body ? (
        <div
          data-testid={`${testId}-body`}
          className="max-w-[420px] leading-relaxed"
          style={{
            color: 'var(--ink-muted)',
            fontSize: 'var(--text-body)',
          }}
        >
          {body}
        </div>
      ) : null}
      {ctaLabel && onCta ? (
        <div className="mt-2">
          <Button
            variant="primary"
            size="md"
            onClick={onCta}
            data-testid={`${testId}-cta`}
          >
            {ctaLabel}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
