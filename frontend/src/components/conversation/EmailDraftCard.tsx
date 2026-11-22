/**
 * Email-draft card — renders the draft(s) the agent COMPOSED as a real email
 * (To / Cc / Subject header rows + body) instead of a paragraph of chat
 * prose. When the agent composed several alternatives, they render as
 * numbered slide-through options using the SAME stepper pattern as the
 * Inbox's suggested-action cards (chips + "N of M" counter, all variants
 * mounted, inactive hidden via display:none) — so chat and Inbox read as one
 * product (founder direction, 2026-08-19).
 *
 * The content is the model's own reasoned draft for this situation, passed
 * through verbatim by ``show_email_draft`` — this card frames it, it never
 * writes it. Presentation only: nothing is sent from here. Sending still
 * goes through the Gmail send tool and its confirmation gate, and the quiet
 * footer says so — the card must never look like it already went out.
 */

import { useState } from 'react';
import { Mail } from 'lucide-react';
import type { EmailDraftData } from './types';

interface EmailDraftCardProps {
  data: EmailDraftData;
  /** Test hook prefix for the rendered card. */
  testIdPrefix?: string;
}

function HeaderRow({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}) {
  return (
    <div className="flex items-baseline gap-2 min-w-0" data-testid={testId}>
      <span
        className=" uppercase shrink-0"
        style={{
          fontSize: 12,
          letterSpacing: '0.12em',
          color: 'var(--ink-muted)',
          width: 52,
        }}
      >
        {label}
      </span>
      <span
        className="truncate"
        style={{ fontSize: 13, color: 'var(--ink)' }}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

export function EmailDraftCard({
  data,
  testIdPrefix = 'email-draft',
}: EmailDraftCardProps) {
  const [activeIdx, setActiveIdx] = useState(0);
  const variants = data.variants;
  if (variants.length === 0) return null;
  // Defensive clamp — a malformed payload can't strand the pager.
  const active = Math.min(activeIdx, variants.length - 1);
  const to = data.to.join(', ');
  const cc = data.cc.join(', ');

  return (
    <div className="my-3" style={{ maxWidth: 560 }} data-testid={`${testIdPrefix}-root`}>
      {/* Variant tabs — the Inbox ActionCardList stepper, verbatim styling:
          numbered chips, iris-soft fill on the active one only, "N of M"
          counter on the right. Only rendered when the agent actually
          composed alternatives. */}
      {variants.length > 1 && (
        <div
          className="flex items-center gap-1.5 mb-2 flex-wrap"
          role="tablist"
          aria-label="Draft options"
          data-testid={`${testIdPrefix}-tabs`}
        >
          {variants.map((v, pos) => {
            const selected = pos === active;
            return (
              <button
                key={pos}
                type="button"
                role="tab"
                aria-selected={selected}
                data-testid={`${testIdPrefix}-tab-${pos}`}
                onClick={() => setActiveIdx(pos)}
                className="inline-flex items-center gap-1.5 text-[12px] font-medium transition-colors"
                style={{
                  padding: '4px 10px',
                  borderRadius: 'var(--r-sm)',
                  border: selected
                    ? '0.5px solid var(--iris-ring)'
                    : '0.5px solid var(--card-edge)',
                  background: selected ? 'var(--iris-soft)' : 'var(--canvas)',
                  color: selected ? 'var(--iris)' : 'var(--ink-muted)',
                  cursor: 'pointer',
                  maxWidth: 220,
                }}
                title={v.label || `Draft ${pos + 1}`}
              >
                <span
                  className="inline-flex items-center justify-center font-mono"
                  style={{ fontSize: 12, opacity: 0.85 }}
                >
                  {pos + 1}
                </span>
                {v.label ? <span className="truncate">{v.label}</span> : null}
              </button>
            );
          })}
          <span
            className="ml-auto text-[12px]"
            style={{ color: 'var(--ink-subtle)' }}
            data-testid={`${testIdPrefix}-tabs-counter`}
          >
            {active + 1} of {variants.length}
          </span>
        </div>
      )}

      {/* All variants stay mounted (hidden via display:none) so flipping
          between options is instant and scroll position survives — the same
          contract as the Inbox's action cards. */}
      {variants.map((v, pos) => (
        <div key={pos} style={pos === active ? undefined : { display: 'none' }}>
          <div
            data-testid={`${testIdPrefix}-variant-${pos}`}
            className="rounded-[var(--r-md)] overflow-hidden"
            style={{
              background: 'var(--canvas)',
              border: '0.5px solid var(--card-edge)',
              boxShadow: 'var(--shadow-sm)',
            }}
          >
            {/* Envelope header — the To/Cc/Subject rows that make it read
                as an email rather than a message bubble. */}
            <div
              className="flex flex-col gap-1.5"
              style={{
                padding: '12px 16px',
                background: 'var(--canvas-raised)',
                borderBottom: '1px solid var(--line)',
              }}
            >
              <div className="flex items-center gap-2 mb-0.5">
                <Mail size={14} strokeWidth={1.5} style={{ color: 'var(--iris)' }} />
                <span
                  className="font-medium"
                  style={{ fontSize: 13, color: 'var(--ink-muted)' }}
                >
                  Email draft
                  {variants.length > 1 && v.label ? ` — ${v.label}` : ''}
                </span>
              </div>
              {to && (
                <HeaderRow label="To" value={to} testId={`${testIdPrefix}-to-${pos}`} />
              )}
              {cc && (
                <HeaderRow label="Cc" value={cc} testId={`${testIdPrefix}-cc-${pos}`} />
              )}
              {v.subject && (
                <div
                  className="font-medium"
                  style={{ fontSize: 14, color: 'var(--ink)', marginTop: 2 }}
                  data-testid={`${testIdPrefix}-subject-${pos}`}
                >
                  {v.subject}
                </div>
              )}
            </div>

            {/* Body — the model's own prose, whitespace preserved. */}
            <div
              style={{
                padding: '14px 16px',
                fontSize: 14,
                lineHeight: 1.6,
                color: 'var(--ink)',
                whiteSpace: 'pre-wrap',
                overflowWrap: 'break-word',
              }}
              data-testid={`${testIdPrefix}-body-${pos}`}
            >
              {v.body}
            </div>

            {/* Honesty footer — a draft card must never read as "sent". */}
            <div
              style={{
                padding: '7px 16px',
                borderTop: '1px solid var(--line)',
                fontSize: 12,
                color: 'var(--ink-muted)',
              }}
              data-testid={`${testIdPrefix}-footer-${pos}`}
            >
              Draft — nothing has been sent. Sending asks for your approval first.
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
