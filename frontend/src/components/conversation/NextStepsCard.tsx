/**
 * Next-steps card — what to do now that the question is answered.
 *
 * Answering is not finishing. A manager who just learned who's behind on rent
 * has a next move; a product that stops at the answer makes them think of it
 * themselves every time. These chips are the fast path to the move the
 * assistant just recommended in prose — deliberately BOTH, because a chip row
 * alone reads like a menu and prose alone makes the user retype.
 *
 * The note offer is the other half of a real work loop: the judgement call
 * that would otherwise be scribbled on a pad and lost. It's an offer, never an
 * action — clicking it asks, it doesn't write.
 *
 * Style: the starter-question button (white, 6px, a border, 14px). Per
 * DESIGN.md the accent never becomes a fill here: four filled buttons under
 * every answer is exactly the "vibe-coded" look the spec bans.
 */

import { PenLine } from 'lucide-react';
import type { NextStepsData } from './types';

export function NextStepsCard({
  data,
  onPick,
  testIdPrefix = 'next-steps',
}: {
  data: NextStepsData;
  /** Send this text as the user's next message. */
  onPick?: (prompt: string) => void;
  testIdPrefix?: string;
}) {
  const steps = data.steps ?? [];
  if (steps.length === 0 && !data.note_hint) return null;

  // No "NEXT" eyebrow and no arrows (brief, 2026-09-29: no caps labels, no
  // "→"). The buttons carry their own meaning. They go away as soon as the
  // user moves on: ConversationThread's SPENT_AFTER_USER_TURN.
  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      className="my-3 flex flex-col items-start gap-2"
      style={{ maxWidth: 620 }}
    >
      {steps.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {steps.map((step) => (
            <button
              key={step.prompt}
              type="button"
              data-testid={`${testIdPrefix}-step`}
              title={step.prompt}
              onClick={() => onPick?.(step.prompt)}
              disabled={!onPick}
              // Same button as the empty chat's starter questions.
              className="h-9 px-3 rounded-[var(--r-sm)] border text-[14px] transition-colors disabled:opacity-60 disabled:cursor-not-allowed hover:bg-[var(--canvas-raised)]"
              style={{ borderColor: 'var(--line-strong)', color: 'var(--ink)', background: 'var(--canvas)' }}
            >
              {step.label}
            </button>
          ))}
        </div>
      )}

      {data.note_hint && (
        <button
          type="button"
          data-testid={`${testIdPrefix}-note`}
          onClick={() => onPick?.(data.note_hint as string)}
          disabled={!onPick}
          // Left-aligned: a wrapped hint used to centre its second line under
          // the icon, which read as a caption rather than a button.
          className="inline-flex items-start gap-1.5 text-left text-[13px] leading-snug disabled:opacity-60"
          style={{ color: 'var(--ink-muted)' }}
        >
          <PenLine size={13} aria-hidden className="mt-[2px] shrink-0" />
          <span>{data.note_hint}</span>
        </button>
      )}
    </div>
  );
}
