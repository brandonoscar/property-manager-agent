/**
 * PR #82 — three-state save indicator for any per-cell save flow.
 *
 * Used by the Properties × Roles grid to give Brandon visible
 * feedback ("saving"/"saved"/"failed") next to each dropdown — his
 * post-PR-#81 quote: "the properties dropdown assign boxes work
 * because there's no like saving then some kind of loading that
 * it's actually changing something."
 *
 * Fixed width (~88px) so swapping between states doesn't cause the
 * row to reflow.
 */

import { Check, Loader2, X } from 'lucide-react';

export type SavingPillState = 'saving' | 'saved' | 'failed';

export interface SavingPillProps {
  state: SavingPillState;
}

const STATE_LABEL: Record<SavingPillState, string> = {
  saving: 'Saving…',
  saved: 'Saved',
  failed: 'Failed',
};

export function SavingPill({ state }: SavingPillProps) {
  const isSaving = state === 'saving';
  const isSaved = state === 'saved';
  const isFailed = state === 'failed';

  const color = isSaved
    ? 'var(--iris)'
    : isFailed
    ? 'var(--caution, #c93434)'
    : 'var(--ink-muted)';

  return (
    <span
      role="status"
      data-testid="saving-pill"
      data-state={state}
      aria-live="polite"
      className="inline-flex items-center gap-1 text-[12px] font-medium select-none"
      style={{ color, minWidth: 88 }}
    >
      {isSaving ? (
        <Loader2 size={11} strokeWidth={2} className="animate-spin" aria-hidden />
      ) : isSaved ? (
        <Check size={11} strokeWidth={2.5} aria-hidden />
      ) : (
        <X size={11} strokeWidth={2.5} aria-hidden />
      )}
      <span>{STATE_LABEL[state]}</span>
    </span>
  );
}
