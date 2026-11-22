/**
 * S6 disclosure panel — the "How I got this" receipts under a loop-mode
 * answer (fast-follow to PR #680, which started streaming the envelope).
 *
 * The backend derives a three-layer envelope per reasoning-loop turn
 * (``EventType.DISCLOSURE``, emitted just before ``done``):
 *
 *   reasoning.sources           — "buildium: $2,400 past due" lines
 *   reasoning.confidence(+reason)
 *   reasoning.unverified        — what the agent could NOT check
 *   reasoning.specialist_trail  — ordered specialist tools consulted
 *
 * Collapsed by default to one quiet summary row; expanding is a frequent,
 * low-novelty action so the toggle is instant (no animation — DESIGN.md
 * motion rules). Styling: 1px hairline card (``--card-edge``), ink-muted
 * text, radius ``--r-md``; the accent is NOT used here — receipts are
 * chrome, not a call to action. Confidence is data, not decoration, so it
 * renders as plain text (semantic colours are reserved for real domain
 * state like payments/urgency, which this is not).
 */

import { useState } from 'react';
import { ChevronRight } from 'lucide-react';

import { cardPayload } from './types';

export interface DisclosureReasoning {
  sources: string[];
  confidence: string | null;
  confidence_reason: string;
  unverified: string[];
  footer_well_formed: boolean;
  specialist_trail: string[];
}

export interface DisclosureData {
  answer: string;
  reasoning: DisclosureReasoning;
  audit?: { run_id?: string; trace_id?: string };
}

/**
 * Type guard for the wire payload (accepts {data: env} or env directly).
 *
 * ⚠ **THE UNWRAP IS `cardPayload`'S, NOT ITS OWN, AND THAT IS THE FIX.**
 * `disclosure` is in `CARD_EVENT_TYPES`, so it is persisted as a
 * `card:disclosure` tool row and hydrated by `store.ts` with a bare CAST of
 * `tool_result` — a `jsonb` column asyncpg returns as a **string**. The eight
 * other branches of `ConversationThread`'s switch call `cardPayload`, which
 * parses that string; this one hand-rolled `'reasoning' in outer` behind a
 * `typeof raw !== 'object'` guard, so a hydrated envelope returned `null` and
 * the receipts panel silently vanished on every reloaded thread. It fails
 * SAFE rather than throwing (that is the crash `cardPayload` was written for
 * — Sentry PYTHON-FASTAPI-3M), which is exactly why nobody saw it: a missing
 * panel reads identically to a turn that never emitted one.
 *
 * So the fan-out fix reached eight branches of one switch and not the ninth.
 * Delegating means there is ONE answer to "what did the wire hand over".
 */
export function parseDisclosure(raw: unknown): DisclosureData | null {
  const env = cardPayload(raw);
  if (!env) return null;
  const r = env.reasoning as Record<string, unknown> | undefined;
  if (!r || typeof r !== 'object') return null;
  return {
    answer: typeof env.answer === 'string' ? env.answer : '',
    reasoning: {
      sources: Array.isArray(r.sources) ? r.sources.map(String) : [],
      confidence: typeof r.confidence === 'string' ? r.confidence : null,
      confidence_reason:
        typeof r.confidence_reason === 'string' ? r.confidence_reason : '',
      unverified: Array.isArray(r.unverified) ? r.unverified.map(String) : [],
      footer_well_formed: Boolean(r.footer_well_formed),
      specialist_trail: Array.isArray(r.specialist_trail)
        ? r.specialist_trail.map(String)
        : [],
    },
    audit:
      env.audit && typeof env.audit === 'object'
        ? (env.audit as { run_id?: string; trace_id?: string })
        : undefined,
  };
}

/** Human label for a specialist tool name ("buildium_agent" → "Buildium"). */
function specialistLabel(name: string): string {
  if (name === 'check_known_state') return 'Known state';
  const stem = name.replace(/_agent$/, '').replace(/_/g, ' ');
  return stem.charAt(0).toUpperCase() + stem.slice(1);
}

export interface DisclosurePanelProps {
  data: DisclosureData;
  testIdPrefix?: string;
}

export function DisclosurePanel({
  data,
  testIdPrefix = 'disclosure',
}: DisclosurePanelProps) {
  const [open, setOpen] = useState(false);
  const { reasoning } = data;

  const hasAnything =
    reasoning.sources.length > 0 ||
    reasoning.unverified.length > 0 ||
    reasoning.specialist_trail.length > 0 ||
    reasoning.confidence !== null;
  if (!hasAnything) return null;

  const summaryBits: string[] = [];
  if (reasoning.specialist_trail.length > 0) {
    summaryBits.push(
      reasoning.specialist_trail.map(specialistLabel).join(' → '),
    );
  }
  if (reasoning.confidence) summaryBits.push(reasoning.confidence);

  return (
    <div
      data-testid={testIdPrefix}
      className="mb-3 max-w-[720px] rounded-[var(--r-md)] border text-[12px]"
      style={{ borderColor: 'var(--card-edge)', color: 'var(--ink-muted)' }}
    >
      <button
        type="button"
        data-testid={`${testIdPrefix}-toggle`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        style={{ color: 'var(--ink-muted)' }}
      >
        <ChevronRight
          size={13}
          aria-hidden
          style={{
            transform: open ? 'rotate(90deg)' : 'none',
            transition: 'transform 100ms var(--ease, ease-out)',
            flexShrink: 0,
          }}
        />
        <span className="font-medium" style={{ color: 'var(--ink)' }}>
          How I got this
        </span>
        {!open && summaryBits.length > 0 && (
          <span className="truncate">{summaryBits.join(' · ')}</span>
        )}
      </button>

      {open && (
        <div
          className="space-y-2 border-t px-3 pb-3 pt-2"
          style={{ borderColor: 'var(--line)' }}
        >
          {reasoning.specialist_trail.length > 0 && (
            <div data-testid={`${testIdPrefix}-trail`}>
              <div className="mb-0.5 font-medium" style={{ color: 'var(--ink)' }}>
                Checked
              </div>
              <div>
                {reasoning.specialist_trail.map(specialistLabel).join(' → ')}
              </div>
            </div>
          )}

          {reasoning.sources.length > 0 && (
            <div data-testid={`${testIdPrefix}-sources`}>
              <div className="mb-0.5 font-medium" style={{ color: 'var(--ink)' }}>
                Sources
              </div>
              <ul className="list-disc space-y-0.5 pl-4">
                {reasoning.sources.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </div>
          )}

          {reasoning.confidence && (
            <div data-testid={`${testIdPrefix}-confidence`}>
              <div className="mb-0.5 font-medium" style={{ color: 'var(--ink)' }}>
                Confidence
              </div>
              <div>
                {reasoning.confidence}
                {reasoning.confidence_reason
                  ? ` — ${reasoning.confidence_reason}`
                  : ''}
              </div>
            </div>
          )}

          {reasoning.unverified.length > 0 && (
            <div data-testid={`${testIdPrefix}-unverified`}>
              <div className="mb-0.5 font-medium" style={{ color: 'var(--ink)' }}>
                Not verified
              </div>
              <ul className="list-disc space-y-0.5 pl-4">
                {reasoning.unverified.map((u, i) => (
                  <li key={i}>{u}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
