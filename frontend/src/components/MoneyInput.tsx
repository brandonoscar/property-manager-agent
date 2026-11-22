/**
 * The one control in this app for typing an amount of money.
 *
 * See `lib/money.ts` for why this is not `<input type="number">`: that type
 * drops text it cannot parse while leaving it on screen, and steps its own
 * value when the wheel scrolls over it. Both produce a rent figure that
 * differs from the one the person entered, with nothing on screen saying so —
 * and the renewal editor sends that figure to a tenant.
 *
 * ⚠ **The blur format is the feature, not the polish.** Re-writing the field
 * as `$1,250.50` once the person leaves it is how they find out what the
 * program understood. Without it, "did my `$` and comma survive" is a question
 * the screen cannot answer, which is the state the number input left everybody
 * in. When the text does NOT parse it is left exactly as typed and an error is
 * shown — reformatting junk would either destroy their work or invent a
 * number.
 */

import { useId, useState } from 'react';

import { formatMoney, parseMoney } from '../lib/money';
import { FormError } from './FormError';

export function MoneyInput({
  label,
  value,
  onChange,
  min,
  disabled,
  hint,
}: {
  label: string;
  /** The raw text. The parent keeps the string; `parseMoney` reads it. */
  value: string;
  onChange: (next: string) => void;
  /** Refuse amounts below this, e.g. 0 for a rent. */
  min?: number;
  disabled?: boolean;
  hint?: string;
}) {
  const id = useId();
  const [error, setError] = useState('');

  function check(raw: string): number | null {
    const n = parseMoney(raw);
    if (!raw.trim()) {
      setError('');
      return null;
    }
    if (n === null) {
      setError(`Not an amount — try something like ${formatMoney(1250)}.`);
      return null;
    }
    if (min != null && n < min) {
      setError(`Must be ${formatMoney(min)} or more.`);
      return null;
    }
    setError('');
    return n;
  }

  return (
    <div className="text-[12px]" style={{ color: 'var(--ink-secondary)' }}>
      <label htmlFor={id}>{label}</label>
      <div className="relative mt-1">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[13px]"
          style={{ color: 'var(--ink-muted)' }}
        >
          $
        </span>
        <input
          id={id}
          // Never type="number" — see the module docstring.
          type="text"
          inputMode="decimal"
          autoComplete="off"
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={hint ? `${id}-hint` : undefined}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            // Clear a stale error as soon as they start fixing it; do not
            // shout at somebody mid-keystroke for a half-typed number.
            if (error) setError('');
          }}
          onBlur={(e) => {
            const n = check(e.target.value);
            if (n !== null) onChange(formatMoney(n));
          }}
          className="w-full rounded-[var(--r-sm)] border py-1.5 pl-5 pr-2 text-[13px] tabular-nums disabled:opacity-50"
          style={{
            borderColor: error ? 'var(--danger)' : 'var(--line)',
            background: 'var(--canvas)',
            color: 'var(--ink)',
          }}
        />
      </div>
      {hint && !error ? (
        <p id={`${id}-hint`} className="mt-1 text-[12px]" style={{ color: 'var(--ink-muted)' }}>
          {hint}
        </p>
      ) : null}
      <FormError className="mt-1">{error}</FormError>
    </div>
  );
}
