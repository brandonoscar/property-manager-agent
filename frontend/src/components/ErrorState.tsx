/**
 * Shared error-state primitive (PR #117 Phase D).
 *
 * Distinct from EmptyState — the brief's stake-in-the-ground rule
 * says loading / empty / error are three different visual surfaces.
 * This one carries a danger-tinted icon ring, a title that names
 * the failure mode in plain language, optional body copy, and a
 * Retry button that fires ``onRetry``.
 *
 * Used by Inbox / Activity / Properties when a network call rejects
 * AND there's no cached data to fall back on.
 */

import { AlertCircle } from 'lucide-react';
import type { ReactNode } from 'react';

import { Button } from './Button';

interface ErrorStateProps {
  /** Headline — names what failed in plain language ("Couldn't load
   *  the inbox" / "Couldn't reach the agent"). Sentence case. */
  title: string;
  /** Body copy — usually the error message stringified safely. */
  body?: ReactNode;
  /** Retry button label. Defaults to "Try again". */
  retryLabel?: string;
  /** Click handler. Renders the button only when supplied. */
  onRetry?: () => void;
  /** Test hook. */
  'data-testid'?: string;
}

export function ErrorState({
  title,
  body,
  retryLabel = 'Try again',
  onRetry,
  'data-testid': testId = 'error-state',
}: ErrorStateProps) {
  return (
    <div
      data-testid={testId}
      className="flex flex-col items-center justify-center text-center px-6 py-10 gap-3"
    >
      <div
        data-testid={`${testId}-icon`}
        className="flex items-center justify-center w-12 h-12 rounded-full"
        style={{
          background: 'var(--danger-bg)',
          color: 'var(--danger)',
          border: '1px solid var(--danger)',
        }}
      >
        <AlertCircle size={20} strokeWidth={1.75} />
      </div>
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
      {onRetry ? (
        <div className="mt-2">
          <Button
            variant="secondary"
            size="md"
            onClick={onRetry}
            data-testid={`${testId}-retry`}
          >
            {retryLabel}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
