/**
 * Set-password screen for the Supabase **invite** and **password-recovery**
 * flows.
 *
 * When someone clicks a Occupella invite email, Supabase signs them in via the
 * invite token but they've never chosen a password — so without this screen
 * they'd land in the app once and be unable to return. This catches that
 * (and the recovery link, same mechanism) and prompts for a password via
 * ``supabase.auth.updateUser({ password })``.
 *
 * The invite/recovery token establishes the session asynchronously
 * (detectSessionInUrl), so we gate submission on the session being ready.
 * Styling mirrors LoginForm (the one decorative iris card).
 */

import { useEffect, useId, useState } from 'react';
import { toast } from 'sonner';

import { supabase } from '../services/auth';
import type { PasswordFlow } from '../services/authFlow';
import {
  firstUnmet,
  meetsRequirements,
  requirements,
  strength,
} from '../lib/passwordStrength';


export function SetPasswordForm({
  flow,
  onDone,
}: {
  flow: PasswordFlow;
  onDone: () => void;
}) {
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [loading, setLoading] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The invite/recovery token sets up the session shortly after load.
  // Wait for it before allowing submit (updateUser needs a live session).
  useEffect(() => {
    let active = true;
    supabase.auth.getSession().then(({ data }) => {
      if (active && data.session) setSessionReady(true);
    });
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session) setSessionReady(true);
    });
    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, []);

  const title = flow === 'invite' ? 'Set your password' : 'Reset your password';
  const subtitle =
    flow === 'invite'
      ? 'Welcome to Occupella — pick a password to finish setting up your account.'
      : 'Choose a new password for your account.';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    const missing = firstUnmet(password);
    if (missing) {
      setError(missing);
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const { error: updateError } = await supabase.auth.updateUser({ password });
      if (updateError) throw updateError;
      toast.success("You're all set — welcome to Occupella.");
      // Clear any leftover token hash so a reload doesn't re-trigger this.
      if (window.location.hash) {
        window.history.replaceState(
          null,
          '',
          window.location.pathname + window.location.search,
        );
      }
      onDone();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Could not set your password.';
      setError(message);
      toast.error(message, { duration: 10000 });
    } finally {
      setLoading(false);
    }
  };

  const canSubmit =
    sessionReady && !loading && meetsRequirements(password) && password === confirm;

  return (
    <div
      className="relative flex items-center justify-center h-screen w-screen overflow-hidden"
      style={{ background: 'var(--canvas)' }}
    >
      <div className="relative z-[1] flex flex-col items-center w-full" style={{ maxWidth: 400 }}>
        <div
          className="w-full rounded-[var(--r-lg)] border animate-fade-in-up"
          style={{
            background: 'var(--canvas-elevated)',
            borderColor: 'var(--line)',
            padding: '40px 32px',
            boxShadow: 'var(--shadow-md)',
          }}
        >
          <h1
            className="text-center font-bold"
            style={{
              fontSize: 28,
              letterSpacing: '-0.02em',
              fontFamily: 'var(--font-display)',
              color: 'var(--ink)',
            }}
          >
            Occupella
          </h1>
          <p className="text-center text-[20px] mt-4 font-medium" style={{ color: 'var(--ink)' }}>
            {title}
          </p>
          <p
            className="text-center text-[14px] mt-1.5 mb-6"
            style={{ color: 'var(--ink-muted)' }}
          >
            {subtitle}
          </p>

          <form onSubmit={handleSubmit} className="flex flex-col gap-2.5">
            <PasswordInput
              placeholder="New password"
              value={password}
              onChange={setPassword}
              disabled={loading}
              autoComplete="new-password"
            />
            <StrengthMeter password={password} />
            <PasswordInput
              placeholder="Confirm password"
              value={confirm}
              onChange={setConfirm}
              disabled={loading}
              autoComplete="new-password"
              /* Only ever a MISMATCH, never "you haven't finished typing".
                 Validating a half-typed confirmation shows red to someone who
                 is doing nothing wrong, which is how people learn to ignore
                 the colour. */
              invalid={confirm.length > 0 && confirm !== password}
              invalidMessage="Passwords do not match."
            />

            <button
              type="submit"
              disabled={!canSubmit}
              className="relative h-11 mt-2 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
              onMouseEnter={(e) => {
                if (!canSubmit) return;
                (e.currentTarget as HTMLElement).style.background = 'var(--iris-hover)';
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = 'var(--iris)';
              }}
            >
              {loading
                ? 'Saving…'
                : sessionReady
                  ? flow === 'invite'
                    ? 'Set password & continue'
                    : 'Update password'
                  : 'Verifying your link…'}
            </button>
          </form>

          {error && (
            <div
              className="mt-3 px-3 py-2 rounded-[var(--r-sm)] border text-[13px] leading-relaxed"
              style={{
                background: 'rgba(255, 90, 90, 0.06)',
                borderColor: 'rgba(255, 90, 90, 0.35)',
                color: 'var(--danger)',
              }}
              role="alert"
              aria-live="polite"
            >
              {error}
            </div>
          )}
        </div>

        <div
          className="mt-6 uppercase"
          style={{ fontSize: 12, letterSpacing: '0.14em', color: 'var(--ink-faint)' }}
        >
          Powered by Supabase
        </div>
      </div>
    </div>
  );
}

function PasswordInput({
  placeholder,
  value,
  onChange,
  disabled,
  autoComplete,
  invalid = false,
  invalidMessage,
}: {
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  autoComplete?: string;
  invalid?: boolean;
  invalidMessage?: string;
}) {
  const [focused, setFocused] = useState(false);
  // Revealing is per-field and resets on unmount. A single shared "show
  // passwords" flag would unmask the confirmation too, which defeats the
  // point of asking for it twice.
  const [revealed, setRevealed] = useState(false);
  const errorId = useId();

  return (
    <div className="flex flex-col gap-1">
      <div className="relative">
        <input
          type={revealed ? 'text' : 'password'}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          disabled={disabled}
          autoComplete={autoComplete}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid && invalidMessage ? errorId : undefined}
          className="h-11 w-full pl-3.5 pr-[68px] rounded-[var(--r-sm)] border text-[14px] outline-none transition-colors ph-no-capture"
          style={{
            background: 'var(--canvas)',
            color: 'var(--ink)',
            borderColor: invalid
              ? 'var(--danger)'
              : focused
                ? 'var(--line-focus)'
                : 'var(--line)',
            boxShadow: focused && !invalid ? '0 0 0 3px var(--iris-ring)' : 'none',
          }}
        />
        {/* Typing a password you cannot see, twice, with no way to check it is
            the reason people fall back to something they already know. The
            button is `tabIndex={-1}` so it does not sit between the two fields
            in the tab order — it is a mouse/touch affordance, and a keyboard
            user reaches it with shift-tab if they want it. */}
        <button
          type="button"
          tabIndex={-1}
          onClick={() => setRevealed((r) => !r)}
          disabled={disabled}
          aria-label={revealed ? 'Hide password' : 'Show password'}
          className="absolute right-2 top-1/2 -translate-y-1/2 px-2 py-1 rounded-[var(--r-xs)] text-[12px] font-medium transition-colors disabled:opacity-40"
          style={{ color: 'var(--ink-muted)' }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = 'var(--ink)';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = 'var(--ink-muted)';
          }}
        >
          {revealed ? 'Hide' : 'Show'}
        </button>
      </div>
      {invalid && invalidMessage && (
        <div
          id={errorId}
          role="alert"
          className="text-[12px] pl-0.5"
          style={{ color: 'var(--danger)' }}
        >
          {invalidMessage}
        </div>
      )}
    </div>
  );
}

/**
 * Three segments, a word, and the requirement list. Nothing else.
 *
 * ⚠ **Cut down on 2026-09-09** from four bands plus a rotating advice line.
 * The advice was the part that read as too much: a form that prints three
 * rules, watches you satisfy them, and then offers a fourth opinion is
 * arguing with the person filling it in. The rules gate the button; the
 * meter is a glance.
 *
 * The requirement list is visible from the FIRST keystroke, not after a
 * failed submit — see `requirements()` in lib/passwordStrength.
 */
function StrengthMeter({ password }: { password: string }) {
  const reqs = requirements(password);
  const { score, label } = strength(password);

  if (!password) {
    return (
      <ul className="flex flex-col gap-1 pl-0.5 -mt-0.5 mb-0.5">
        {reqs.map((r) => (
          <RequirementRow key={r.id} met={r.met} label={r.label} />
        ))}
      </ul>
    );
  }

  const colour =
    score >= 3 ? 'var(--positive)' : score === 2 ? 'var(--caution)' : 'var(--danger)';

  return (
    <div className="flex flex-col gap-1.5 -mt-0.5 mb-0.5">
      <div className="flex items-center gap-2">
        <div className="flex gap-1 flex-1" aria-hidden="true">
          {[1, 2, 3].map((band) => (
            <div
              key={band}
              className="h-[3px] flex-1 rounded-full transition-colors"
              style={{ background: score >= band ? colour : 'var(--line)' }}
            />
          ))}
        </div>
        {/* aria-live so a screen-reader user hears the band change instead of
            only sighted users seeing it. `polite` — it must never interrupt
            the character they just typed being announced. */}
        <span
          aria-live="polite"
          className="text-[12px] font-medium tabular-nums"
          style={{ color: colour, minWidth: 52, textAlign: 'right' }}
        >
          {label}
        </span>
      </div>
      <ul className="flex flex-col gap-1 pl-0.5">
        {reqs.map((r) => (
          <RequirementRow key={r.id} met={r.met} label={r.label} />
        ))}
      </ul>
    </div>
  );
}

function RequirementRow({ met, label }: { met: boolean; label: string }) {
  return (
    <li
      className="flex items-center gap-1.5 text-[12px]"
      style={{ color: met ? 'var(--ink-muted)' : 'var(--ink-faint)' }}
    >
      {/* The tick is not the only signal — colour alone must not carry
          meaning (WCAG 1.4.1), and the glyph itself changes with state. */}
      <span aria-hidden="true" style={{ width: 10, display: 'inline-block' }}>
        {met ? '✓' : '·'}
      </span>
      <span>{label}</span>
      <span className="sr-only">{met ? ' — met' : ' — not yet met'}</span>
    </li>
  );
}
