/**
 * Getting started — the first-run checklist card (BR list #6).
 *
 * Renders above the chat hero until the PM has done the five setup
 * steps (or dismisses it). Every tick derives from real state and the
 * card re-checks on mount, so it quietly retires itself as the account
 * comes alive. Deliberately a card, not an overlay — DESIGN.md
 * restraint: token palette, 1px var(--line) borders, no spotlight
 * theatrics.
 */

import { useEffect, useState } from 'react';
import { CheckCircle2, ChevronRight, Circle, X } from 'lucide-react';
import {
  fetchChecklist,
  isChecklistDismissed,
  dismissChecklist,
  type ChecklistState,
  type ChecklistTarget,
} from '../services/onboardingChecklist';

export function GettingStartedCard({
  onNavigate,
}: {
  onNavigate: (target: ChecklistTarget) => void;
}) {
  const [state, setState] = useState<ChecklistState | null>(null);
  const [hidden, setHidden] = useState(isChecklistDismissed());

  useEffect(() => {
    if (hidden) return;
    let cancelled = false;
    fetchChecklist()
      .then((s) => {
        if (!cancelled) setState(s);
      })
      .catch(() => {
        // A failed derivation never blocks the page — just stay hidden.
        if (!cancelled) setState(null);
      });
    return () => {
      cancelled = true;
    };
  }, [hidden]);

  if (hidden || state === null || state.allDone) return null;

  // A plain list, not a boxed card (brief, 2026-09-29): a heading with the
  // count, then one row per step. Done steps stay in place with a check, so
  // the list does not reshuffle as it fills in.
  return (
    <section className="shrink-0 px-5 pt-5" data-testid="getting-started-card" aria-labelledby="getting-started-title">
      <div className="mx-auto w-full max-w-[720px]">
        <div className="flex items-center justify-between gap-3 mb-2">
          <h2 id="getting-started-title" className="text-[14px] font-semibold" style={{ color: 'var(--ink)' }}>
            Getting started
            <span className="ml-2 font-normal tabular-nums" style={{ color: 'var(--ink-muted)' }}>
              {state.doneCount} of {state.steps.length} done
            </span>
          </h2>
          {/* 24px hit area around a 14px icon. */}
          <button
            type="button"
            aria-label="Dismiss getting started"
            onClick={() => {
              dismissChecklist();
              setHidden(true);
            }}
            className="flex items-center justify-center w-6 h-6 -mr-1 rounded-[var(--r-sm)] shrink-0"
            style={{ color: 'var(--ink-muted)' }}
          >
            <X size={14} strokeWidth={1.7} />
          </button>
        </div>

        <ol className="flex flex-col border-t" style={{ borderColor: 'var(--line)' }}>
          {state.steps.map((step) => (
            <li key={step.key} className="border-b" style={{ borderColor: 'var(--line)' }}>
              <button
                type="button"
                onClick={() => onNavigate(step.target)}
                disabled={step.done}
                className="w-full flex items-start gap-3 py-2.5 text-left transition-colors enabled:hover:bg-[var(--canvas-hover)]"
              >
                {step.done ? (
                  <CheckCircle2
                    size={16}
                    strokeWidth={1.7}
                    className="shrink-0 mt-0.5"
                    style={{ color: 'var(--positive)' }}
                    aria-label="Done"
                  />
                ) : (
                  <Circle
                    size={16}
                    strokeWidth={1.7}
                    className="shrink-0 mt-0.5"
                    style={{ color: 'var(--ink-faint)' }}
                    aria-hidden
                  />
                )}
                <span className="min-w-0 flex-1">
                  <span
                    className="block text-[14px] font-medium"
                    style={{ color: step.done ? 'var(--ink-muted)' : 'var(--ink)' }}
                  >
                    {step.label}
                  </span>
                  {!step.done && (
                    <span className="block text-[13px]" style={{ color: 'var(--ink-muted)' }}>
                      {step.detail}
                    </span>
                  )}
                </span>
                {!step.done && (
                  <ChevronRight
                    size={14}
                    strokeWidth={1.7}
                    className="shrink-0 mt-0.5"
                    style={{ color: 'var(--ink-faint)' }}
                    aria-hidden
                  />
                )}
              </button>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
