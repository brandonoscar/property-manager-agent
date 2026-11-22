/**
 * Form card — the assistant asking for the blanks instead of interrogating.
 *
 * "Make a task for the leaky faucet in 4B" tells you the job and leaves the
 * rest empty. The old reply was a paragraph of questions — *"what should the
 * task say (title/description), and is there a specific unit, priority, or
 * due date?"* — which makes the user write a second paragraph the model then
 * has to take apart again. This puts the blanks on screen, prefilled with
 * whatever they already said, and sends the answers back structured.
 *
 * ⚠ **It asks; it does not act.** Submit composes an ordinary chat message
 * through the same path as typing one. Every write it leads to still passes
 * its own confirm gate and role floor — a card that wrote directly would be a
 * second write surface with none of those.
 *
 * ⚠ **A submitted form must be visibly spent.** Leaving the button live after
 * the message is sent invites a second identical send, which reads to the
 * model as the user asking twice and is how a duplicate task gets created.
 * It locks and says so.
 *
 * Style: one bordered card on `--canvas`, `--r-sm`, inputs ghost-bordered with
 * iris only on focus. Per DESIGN.md the accent is a border, never a fill.
 */

import { useState } from 'react';
import { Check } from 'lucide-react';
import type { FormData, FormFieldData } from './types';

/** Compose the message a filled-in form sends.
 *
 * ⚠ Blank optional fields are OMITTED, never sent as an empty line. "Due
 * date:" with nothing after it reads to the model as an instruction to clear
 * the due date, which is a different request from not mentioning it.
 */
export function composeFormMessage(data: FormData, values: Record<string, string>): string {
  const filled = (data.fields ?? [])
    .map((f) => [f.label, (values[f.name] ?? '').trim()] as const)
    .filter(([, v]) => v.length > 0);
  if (filled.length === 0) return '';
  const lead = data.submit_label?.trim() || 'Send';
  return [`${lead}:`, ...filled.map(([label, v]) => `- ${label}: ${v}`)].join('\n');
}

const INPUT_STYLE: React.CSSProperties = {
  background: 'var(--surface)',
  border: '0.5px solid var(--card-edge)',
  borderRadius: 'var(--r-xs)',
  color: 'var(--ink)',
  padding: '6px 9px',
  fontSize: 13,
  width: '100%',
  outline: 'none',
};

function focusRing(e: { currentTarget: HTMLElement }, on: boolean): void {
  e.currentTarget.style.borderColor = on ? 'var(--iris)' : 'var(--card-edge)';
}

export function FormCard({
  data,
  onSubmit,
  testIdPrefix = 'form-card',
}: {
  data: FormData;
  /** Send this text as the user's next message. */
  onSubmit?: (message: string) => void;
  testIdPrefix?: string;
}) {
  const fields = data.fields ?? [];
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, f.value ?? ''])),
  );
  const [sent, setSent] = useState(false);

  if (fields.length === 0) return null;

  const set = (name: string, v: string) => setValues((prev) => ({ ...prev, [name]: v }));
  const message = composeFormMessage(data, values);
  const canSend = !sent && !!onSubmit && message.length > 0;

  const send = () => {
    if (!canSend) return;
    setSent(true);
    onSubmit?.(message);
  };

  const renderField = (f: FormFieldData) => {
    const id = `${testIdPrefix}-${f.name}`;
    const common = {
      id,
      'data-testid': `${testIdPrefix}-input-${f.name}`,
      value: values[f.name] ?? '',
      disabled: sent,
      style: INPUT_STYLE,
      onFocus: (e: { currentTarget: HTMLElement }) => focusRing(e, true),
      onBlur: (e: { currentTarget: HTMLElement }) => focusRing(e, false),
    };
    if (f.kind === 'textarea') {
      return (
        <textarea
          {...common}
          rows={3}
          style={{ ...INPUT_STYLE, resize: 'vertical' }}
          onChange={(e) => set(f.name, e.target.value)}
        />
      );
    }
    if (f.kind === 'choice') {
      return (
        <select {...common} onChange={(e) => set(f.name, e.target.value)}>
          {/* The empty option is deliberate: a select that opens on the first
              alternative silently answers a question nobody read. */}
          <option value="">—</option>
          {(f.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
    }
    return (
      <input
        {...common}
        type={f.kind === 'date' ? 'date' : 'text'}
        onChange={(e) => set(f.name, e.target.value)}
      />
    );
  };

  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      className="my-3"
      style={{
        maxWidth: 520,
        background: 'var(--canvas)',
        border: '0.5px solid var(--card-edge)',
        borderRadius: 'var(--r-sm)',
        padding: '14px 16px',
      }}
    >
      <div
        className="text-[14px] font-medium mb-1"
        style={{ color: 'var(--ink)' }}
        data-testid={`${testIdPrefix}-title`}
      >
        {data.title}
      </div>
      {data.intro && (
        <div className="text-[13px] mb-3" style={{ color: 'var(--ink-muted)' }}>
          {data.intro}
        </div>
      )}

      <div className="flex flex-col gap-3 mt-3">
        {fields.map((f) => (
          <div key={f.name} className="flex flex-col gap-1">
            <label
              htmlFor={`${testIdPrefix}-${f.name}`}
              className="text-[12px]"
              style={{ color: 'var(--ink-muted)' }}
            >
              {f.label}
            </label>
            {renderField(f)}
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2 mt-4">
        <button
          type="button"
          data-testid={`${testIdPrefix}-submit`}
          onClick={send}
          disabled={!canSend}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] rounded-[var(--r-xs)] transition-colors disabled:opacity-50"
          style={{
            background: 'var(--surface)',
            border: '0.5px solid var(--card-edge)',
            color: 'var(--ink)',
          }}
          onMouseEnter={(e) => {
            if (!canSend) return;
            e.currentTarget.style.borderColor = 'var(--iris)';
            e.currentTarget.style.background = 'var(--iris-soft)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = 'var(--card-edge)';
            e.currentTarget.style.background = 'var(--surface)';
          }}
        >
          {sent && <Check size={12} aria-hidden />}
          {sent ? 'Sent' : data.submit_label}
        </button>
        {!sent && (
          <span className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
            Nothing is saved until you send this.
          </span>
        )}
      </div>
    </div>
  );
}
