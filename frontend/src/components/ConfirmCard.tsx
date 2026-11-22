/**
 * Confirmation gate — the agent pauses here until the operator
 * approves or cancels a destructive / externally-visible action.
 *
 * Phase 5 of the premium-OS redesign:
 *   • Lucide AlertCircle instead of a ⚠️ emoji.
 *   • Keyboard shortcuts: ⏎ approves, esc cancels (visible hint
 *     chips next to each button).
 *   • Dual-mode: the default chat rendering is a tinted caution
 *     card; mode='rail' collapses it into a compact stack sized for
 *     the 320px activity rail.
 */

import { useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { toast } from 'sonner';
import { confirmAction } from '../services/agent';
import { getAccessToken } from '../services/auth';
import { useStore } from '../stores/store';

interface ConfirmCardProps {
  mode?: 'chat' | 'rail';
}

export function ConfirmCard({ mode = 'chat' }: ConfirmCardProps) {
  // ⚠ Scoped to the gate's OWN thread. The store holds one gate, this card is
  // mounted once, and a gate raised by a run in another conversation must not
  // render here — it would offer the reader a write they never asked for, with
  // no visible link to the question that produced it.
  //
  // The store used to guarantee that by CLEARING the gate on every thread
  // switch, which threw it away: a gate waits up to 300s server-side, so
  // glancing at another thread mid-approval silently removed the only way to
  // say yes, and the run then denied by default. Filtering here means the card
  // is hidden while you are elsewhere and still there when you come back.
  //
  // A gate with no `threadId` is a pre-existing/legacy payload and still
  // renders — declining to show a real gate would be the worse failure.
  const activeThreadId = useStore((s) => s.activeThreadId);
  const raised = useStore((s) => s.pendingConfirmation);
  const pending =
    raised && (raised.threadId == null || raised.threadId === activeThreadId) ? raised : null;
  const setPending = useStore((s) => s.setPendingConfirmation);
  // Optional decline reason (approval-pipeline PR): first Cancel reveals an
  // inline "why?" input; a second Cancel (or ⏎ in the input) sends the
  // decline with whatever was typed — empty is fine, never required.
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState('');
  // Edit-before-approve: the user's working copies of the gate's editable
  // fields, keyed by field name. Seeded lazily from requestBody on first
  // render of an editable gate; only CHANGED fields ride on the approval.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const editableFields = pending?.editableFields ?? [];
  // A different gate = a fresh working copy (keyed by confirm_id).
  useEffect(() => {
    setEdits({});
  }, [pending?.confirmId]);

  const handleApprove = async () => {
    if (!pending) return;
    const token = await getAccessToken();
    if (token) {
      const rb = (pending.requestBody ?? {}) as Record<string, unknown>;
      const changed: Record<string, string> = {};
      for (const f of editableFields) {
        const edited = edits[f];
        if (edited !== undefined && edited !== String(rb[f] ?? '')) {
          changed[f] = edited;
        }
      }
      const resolve = await confirmAction(
        pending.confirmId,
        true,
        token,
        undefined,
        changed,
      );
      // The approval didn't reach a live run — the assistant restarted before
      // it could act. Tell the user instead of silently dropping it.
      if (resolve !== 'resolved') {
        toast.error(
          'That request is no longer active — the assistant may have restarted. Please send it again.',
        );
      }
    }
    setPending(null);
  };

  const submitCancel = async () => {
    if (!pending) return;
    const token = await getAccessToken();
    // On cancel there's nothing to re-send, so an orphaned result is moot —
    // just dismiss the card.
    if (token) await confirmAction(pending.confirmId, false, token, reason);
    setPending(null);
    setDeclining(false);
    setReason('');
  };

  const handleCancel = async () => {
    if (!pending) return;
    if (!declining) {
      // First press: reveal the optional-reason input; nothing is sent yet.
      setDeclining(true);
      return;
    }
    await submitCancel();
  };

  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const typing =
        tag === 'INPUT' || tag === 'TEXTAREA' || target?.isContentEditable;
      if (typing) return;

      // Design-review fix (2026-08-18): the old guard was
      // ``e.metaKey || e.ctrlKey || !typing`` — and after the early return
      // above, ``!typing`` was ALWAYS true, so a bare Enter anywhere on the
      // page approved a pending write. Approval is now an EXPLICIT chord
      // (⌘/Ctrl+Enter) — matching the "⏎" hint on the Approve button, which
      // itself stays a deliberate click.
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        // While the decline-reason input is showing, ⏎ completes the decline
        // (never a surprise approve).
        if (declining) void submitCancel();
        else handleApprove();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        handleCancel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending, declining, reason]);

  if (!pending) return null;

  const compact = mode === 'rail';

  return (
    <div
      data-testid="confirm-gate"
      className="rounded-[var(--r-md)] border overflow-hidden animate-fade-in-up"
      style={{
        // The one card on screen that authorizes an outbound action gets the
        // one emphasized treatment: amber edge + white body + real elevation.
        // Everything else in the thread is white + hairline, so the gate
        // stands out by design, not by being another tinted slab (UI/UX day,
        // 2026-08-19 — matches the Inbox card language).
        borderColor: 'var(--caution)',
        background: 'var(--canvas)',
        boxShadow: 'var(--shadow-md)',
      }}
    >
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3.5 py-2.5 border-b"
        style={{
          borderColor: 'var(--line)',
          background: 'var(--caution-bg)',
        }}
      >
        <AlertCircle
          size={14}
          strokeWidth={1.75}
          style={{ color: 'var(--caution)' }}
        />
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--caution)' }}>
          Confirm
        </span>
        <span
          className="text-[13px] font-medium truncate"
          style={{ color: 'var(--ink)' }}
        >
          {pending.title}
        </span>
      </div>

      {/* Body */}
      <div className={compact ? 'px-3 py-3' : 'px-4 py-3.5'}>
        <p
          className={`leading-snug ${compact ? 'text-[12px]' : 'text-[13px]'}`}
          style={{ color: 'var(--ink-muted)' }}
        >
          {pending.details}
        </p>

        {pending.items.length > 0 && (
          <ul className="mt-2.5 flex flex-col gap-1">
            {pending.items.map((item, i) => (
              <li
                key={i}
                className={`flex items-start gap-2 ${
                  compact ? 'text-[12px]' : 'text-[13px]'
                }`}
                style={{ color: 'var(--ink)' }}
              >
                <span
                  className="shrink-0 mt-1.5 w-1 h-1 rounded-full"
                  style={{ background: 'var(--ink-faint)' }}
                />
                <span className="min-w-0">{item}</span>
              </li>
            ))}
          </ul>
        )}

        {/* Edit-before-approve (UI/UX day item 1): the gate's editable
            fields render as prefilled inputs — the approver can fix wording
            right here and approve THEIR version. Only fields the backend
            declared editable appear; everything else stays read-only. The
            inputs replace the raw JSON dump for these gates (the editors ARE
            the payload view). */}
        {editableFields.length > 0 &&
          (() => {
            const rb = (pending.requestBody ?? {}) as Record<string, unknown>;
            return (
              <div className="mt-3 flex flex-col gap-2.5" data-testid="confirm-gate-editors">
                {editableFields.map((f) => {
                  const value = edits[f] ?? String(rb[f] ?? '');
                  const multiline = f !== 'subject';
                  const label = f === 'subject' ? 'Subject' : 'Message';
                  return (
                    <label key={f} className="flex flex-col gap-1">
                      <span style={{ fontSize: 12, color: 'var(--ink-muted)' }}>
                        {label} (you can edit this)
                      </span>
                      {multiline ? (
                        <textarea
                          data-testid={`confirm-gate-edit-${f}`}
                          value={value}
                          rows={Math.min(10, Math.max(4, value.split('\n').length + 1))}
                          onChange={(e) => setEdits((m) => ({ ...m, [f]: e.target.value }))}
                          className="w-full rounded-[var(--r-sm)] border p-2.5 text-[13px] leading-relaxed resize-y"
                          style={{
                            background: 'var(--canvas)',
                            color: 'var(--ink)',
                            borderColor: 'var(--line-strong)',
                          }}
                        />
                      ) : (
                        <input
                          data-testid={`confirm-gate-edit-${f}`}
                          value={value}
                          onChange={(e) => setEdits((m) => ({ ...m, [f]: e.target.value }))}
                          className="w-full rounded-[var(--r-sm)] border px-2.5 py-1.5 text-[13px]"
                          style={{
                            background: 'var(--canvas)',
                            color: 'var(--ink)',
                            borderColor: 'var(--line-strong)',
                          }}
                        />
                      )}
                    </label>
                  );
                })}
                <span style={{ fontSize: 12, color: 'var(--ink-muted)' }}>
                  Approving sends your edited version.
                </span>
              </div>
            );
          })()}

        {/* SMS approval preview — when the gated action is a text message,
            show the EXACT rendered message as a phone-style bubble so
            approving a text looks like approving the actual text. The body
            comes from the gate's request_body.message (the true rendered
            template output, last-4 recipient only — sms_tools builds it). */}
        {pending.action === 'send_text_message' &&
          typeof (pending.requestBody as Record<string, unknown> | undefined)?.message ===
            'string' &&
          (() => {
            const rb = pending.requestBody as Record<string, unknown>;
            const message = rb.message as string;
            const to = typeof rb.to === 'string' ? rb.to : '';
            // GSM-7 messages split at 160 chars (153 per segment when
            // concatenated) — a rough but honest segment count.
            const segments = message.length <= 160 ? 1 : Math.ceil(message.length / 153);
            return (
              <div className="mt-3" data-testid="confirm-gate-sms-preview">
                {to && (
                  <div className="mb-1.5" style={{ fontSize: 12, color: 'var(--ink-muted)' }}>
                    Text to {to}
                  </div>
                )}
                <div
                  className="rounded-[16px] rounded-bl-[4px]"
                  style={{
                    display: 'inline-block',
                    maxWidth: 340,
                    padding: '9px 13px',
                    background: 'var(--iris)',
                    color: '#FFFFFF',
                    fontSize: compact ? 13 : 14,
                    lineHeight: 1.45,
                    whiteSpace: 'pre-wrap',
                    overflowWrap: 'break-word',
                  }}
                  data-testid="confirm-gate-sms-bubble"
                >
                  {message}
                </div>
                <div
                  className="mt-1"
                  style={{ fontSize: 12, color: 'var(--ink-faint)' }}
                  data-testid="confirm-gate-sms-meta"
                >
                  {message.length} characters · {segments} segment
                  {segments === 1 ? '' : 's'}
                </div>
              </div>
            );
          })()}

        {/* The ACTUAL outbound payload (approval-pipeline PR): what will
            really be sent if approved — not just the summary above. For a
            code-execution run the payload is {language, code}: the operator
            approves the plain-English plan above, and the code rides here as a
            readable code block (real newlines) — NOT an escaped JSON dump.
            Skipped for SMS: the phone-bubble preview above IS the payload
            view, and a raw JSON dump under it would just duplicate it. */}
        {pending.requestBody &&
          pending.action !== 'send_text_message' &&
          editableFields.length === 0 &&
          (() => {
            const rb = pending.requestBody as Record<string, unknown>;
            const isCode = typeof rb.code === 'string';
            const body = isCode ? (rb.code as string) : JSON.stringify(rb, null, 2);
            return (
              <div className="mt-2.5">
                {isCode && (
                  <div className="mb-1" style={{ fontSize: 12, color: 'var(--ink-muted)' }}>
                    {typeof rb.language === 'string' ? rb.language : 'code'}
                  </div>
                )}
                <pre
                  data-testid="confirm-gate-request-body"
                  className={`font-mono whitespace-pre-wrap break-words rounded-[var(--r-sm)] border p-2.5 max-h-[200px] overflow-auto ${
                    compact ? 'text-[12px]' : 'text-[12px]'
                  }`}
                  style={{
                    background: 'var(--canvas)',
                    color: 'var(--ink-subtle)',
                    borderColor: 'var(--line)',
                  }}
                >
                  {body}
                </pre>
              </div>
            );
          })()}

        {declining && (
          <div className="mt-2.5 flex items-center gap-2">
            <input
              autoFocus
              data-testid="confirm-gate-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitCancel();
                }
              }}
              placeholder="Why? (optional — helps tune future proposals)"
              className={`flex-1 h-8 px-2.5 rounded-[var(--r-sm)] border bg-transparent outline-none ${
                compact ? 'text-[12px]' : 'text-[13px]'
              }`}
              style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
            />
          </div>
        )}
      </div>

      {/* Actions */}
      <div
        className="flex items-center justify-end gap-2 px-3.5 py-2.5 border-t"
        style={{
          borderColor: 'var(--line)',
          background: 'var(--canvas-raised)',
        }}
      >
        <ActionButton
          label={declining ? "Don't run" : 'Cancel'}
          hint="esc"
          onClick={handleCancel}
          variant="secondary"
          testId="confirm-gate-cancel"
        />
        {!declining && (
          <ActionButton
            label="Approve"
            hint="⌘⏎"
            onClick={handleApprove}
            variant="primary"
            testId="confirm-gate-confirm"
          />
        )}
      </div>
    </div>
  );
}

function ActionButton({
  label,
  hint,
  onClick,
  variant,
  testId,
}: {
  label: string;
  hint: string;
  onClick: () => void;
  variant: 'primary' | 'secondary';
  testId?: string;
}) {
  const primary = variant === 'primary';
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      className="flex items-center gap-2 h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
      style={{
        background: primary ? 'var(--iris)' : 'var(--canvas-elevated)',
        color: primary ? 'white' : 'var(--ink)',
        borderColor: primary ? 'var(--iris)' : 'var(--line)',
      }}
      onMouseEnter={(e) => {
        const el = e.currentTarget as HTMLElement;
        if (primary) {
          el.style.background = 'var(--iris-hover)';
          el.style.borderColor = 'var(--iris-hover)';
        } else {
          el.style.background = 'var(--canvas-hover)';
          el.style.borderColor = 'var(--line-strong)';
        }
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLElement;
        if (primary) {
          el.style.background = 'var(--iris)';
          el.style.borderColor = 'var(--iris)';
        } else {
          el.style.background = 'var(--canvas-elevated)';
          el.style.borderColor = 'var(--line)';
        }
      }}
    >
      {label}
      <span
        className="font-mono px-1 py-0.5 rounded-[var(--r-xs)] border"
        style={{
          fontSize: 12,
          letterSpacing: '0.04em',
          borderColor: primary ? 'rgba(255,255,255,0.3)' : 'var(--line)',
          color: primary ? 'rgba(255,255,255,0.85)' : 'var(--ink-subtle)',
          background: primary ? 'rgba(255,255,255,0.1)' : 'transparent',
        }}
      >
        {hint}
      </span>
    </button>
  );
}
