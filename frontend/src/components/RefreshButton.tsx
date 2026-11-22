/**
 * Reload what this page is showing, without leaving it.
 *
 * ⚠ **Four of this product's list surfaces fetched once on mount and never
 * again** — Activity, Operations, Leads and Dev runs. Every one of them shows
 * data that a webhook, a worker sweep or a colleague changes seconds later, so
 * the only way to see the current state was to navigate away and back. People
 * find that on their own and then do it constantly, which reads as the product
 * being stale rather than as a missing button.
 *
 * ⚠ **The spin is the answer to "did that do anything".** A reload that
 * returns the same rows — the common case, and the reassuring one — is
 * indistinguishable from a click that missed unless something moves while it
 * is in flight. So `busy` is not decoration and callers have to pass it.
 *
 * ⚠ `aria-label` names the SURFACE, not the verb. A screen reader listing the
 * controls on a page reads "Refresh, Refresh, Refresh" when three of these end
 * up in one view; "Refresh activity" is the one that identifies which.
 */

import { RefreshCw } from 'lucide-react';

import { Button } from './Button';

export function RefreshButton({
  onClick,
  busy,
  label,
  testId,
}: {
  onClick: () => void;
  /** True while the reload is in flight — drives the spin and the disable. */
  busy: boolean;
  /** What is being reloaded, e.g. "activity". Becomes "Refresh activity". */
  label: string;
  testId?: string;
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onClick}
      disabled={busy}
      aria-label={`Refresh ${label}`}
      data-testid={testId}
      icon={
        <RefreshCw
          size={13}
          className={busy ? 'animate-spin' : ''}
          style={{ color: 'var(--ink-muted)' }}
        />
      }
    >
      Refresh
    </Button>
  );
}
