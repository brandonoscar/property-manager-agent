/**
 * The code prompt between signing in and reaching the app.
 *
 * ⚠ WITHOUT THIS, TURNING ON 2FA CHANGES NOTHING. Supabase's
 * `signInWithPassword` succeeds on a password alone even when a TOTP factor
 * is enrolled — it just issues an **aal1** session and waits to be asked for
 * more. So a settings toggle with no gate here is the worst kind of security
 * feature: one that says "On" and stops nothing. Gotcha 86's shape, arriving
 * at auth — a control enforced in one place and invisible in the one that
 * matters.
 *
 * ⚠ **This is a UI gate, and a UI gate cannot bind an API** (gotcha 67). It
 * stops a person at a browser, which is the threat 2FA is actually bought
 * for — somebody with a stolen password. It does NOT stop a crafted request
 * carrying a valid aal1 token, because the backend does not yet require aal2.
 * That is a named, deliberate follow-up rather than something this file
 * pretends to cover: `AuthenticatedUser` would need the `aal` claim and every
 * route a policy about it.
 *
 * ⚠⚠ **AND THE OBVIOUS SPELLING OF THAT FOLLOW-UP LOCKS CUSTOMERS OUT.** A
 * backend rule of the form "a user with a verified factor must present aal2"
 * refuses exactly the people on the RECOVERY path. Supabase ships no recovery
 * codes (measured — see `services/mfa.ts`), so ours live outside its AAL model
 * entirely: `redeemBackupCode` below is answered by OUR backend and calls
 * `onPassed()` while the Supabase session stays **aal1**. Nothing anywhere
 * records that this session satisfied the second factor.
 *
 * So the naive rule's failure mode is: customer loses their phone → redeems a
 * backup code → is now locked out of the API → cannot even reach Settings to
 * turn 2FA off. That is worse than the gap it closes, and it is invisible
 * until a real person loses a real phone.
 *
 * The shape that works needs SESSION-SCOPED server-side state — "this
 * `session_id` satisfied the second factor" — so the policy can read
 * *aal2 OR redeemed*, which means a migration and is why this is not a
 * one-line change. Keyed on the session, never the user: a marker on the user
 * would let one redemption bless every future aal1 token they ever hold.
 */

import { useEffect, useState } from 'react';

import { performSignOut, supabase } from '../services/auth';
import { BadBackupCode, redeemBackupCode } from '../services/mfa';

type Phase = 'checking' | 'clear' | 'needs-code';

/**
 * `null` while it does not yet know. The caller must render NOTHING app-shaped
 * until this resolves — showing the app first and gating afterwards is a leak
 * with extra steps.
 */
export function useTwoFactorGate(userId: string | null): Phase {
  const [phase, setPhase] = useState<Phase>('checking');

  useEffect(() => {
    if (!userId) {
      setPhase('clear');
      return;
    }
    let live = true;
    supabase.auth.mfa
      .getAuthenticatorAssuranceLevel()
      .then(({ data }) => {
        if (!live) return;
        // `nextLevel` is what this account is ENTITLED to reach; `currentLevel`
        // is where the session actually is. They differ exactly when a factor
        // is enrolled and this session has not satisfied it.
        const needs = data?.nextLevel === 'aal2' && data?.currentLevel !== 'aal2';
        setPhase(needs ? 'needs-code' : 'clear');
      })
      .catch(() => {
        // ⚠ FAILS OPEN, and it is a close call stated rather than hidden.
        // Closed would lock every user out of the product whenever this one
        // call blips — including the overwhelming majority who have no second
        // factor at all. Open costs the challenge for a session that should
        // have had it, during an outage, for somebody who already proved the
        // password. The blast radius of the two is not comparable.
        if (live) setPhase('clear');
      });
    return () => {
      live = false;
    };
  }, [userId]);

  return phase;
}

export function TwoFactorGate({ onPassed }: { onPassed: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // ⚠ Backup codes are a MODE of this screen, not a separate route. Somebody
  // whose phone is gone is already here; sending them somewhere else to find
  // the recovery path is how they end up emailing support instead.
  const [mode, setMode] = useState<'totp' | 'backup'>('totp');
  const [backup, setBackup] = useState('');

  const switchTo = (next: 'totp' | 'backup') => {
    setMode(next);
    // Clear the error with the mode. An "invalid code" left over from the
    // authenticator attempt reads, on the backup screen, as a rejection of a
    // code they have not typed yet.
    setError(null);
  };

  const submitBackup = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await redeemBackupCode(backup);
      // The code removed the factor, so this session no longer has anything
      // to satisfy. Straight through — re-challenging now would ask for the
      // authenticator we just established they cannot reach.
      onPassed();
    } catch (err) {
      setError(
        err instanceof BadBackupCode
          ? err.message
          : // ⚠ NOT "wrong code". They are holding a finite sheet of paper,
            // and reporting an outage as a bad code makes them burn a second
            // one, then a third.
            'Could not use that code right now. Try again in a moment.',
      );
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const { data: factors, error: listError } = await supabase.auth.mfa.listFactors();
      if (listError) throw listError;
      const factor = (factors?.all ?? []).find((f) => f.status === 'verified');
      if (!factor) {
        // Nothing to satisfy — the factor was removed between sign-in and
        // now. Letting them through is correct: there is no second factor.
        onPassed();
        return;
      }
      const { error: verifyError } = await supabase.auth.mfa.challengeAndVerify({
        factorId: factor.id,
        code: code.replace(/\s/g, ''),
      });
      if (verifyError) {
        // Codes rotate every 30 seconds and people type the one that just
        // expired, so this is the likeliest outcome and gets its own words.
        setError('That code was not accepted. Check your app and enter the current code.');
        return;
      }
      onPassed();
    } catch {
      setError('Could not check that code. Try again in a moment.');
    } finally {
      setBusy(false);
    }
  };

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
            {mode === 'totp' ? 'Enter your code' : 'Use a backup code'}
          </p>
          <p
            className="text-center text-[14px] mt-1.5 mb-6"
            style={{ color: 'var(--ink-muted)' }}
          >
            {mode === 'totp'
              ? 'Open your authenticator app and enter the 6-digit code for Occupella.'
              : // Say what it DOES before they spend one. A code is single-use
                // and switches two-factor off entirely — somebody who thinks
                // it is just another way in will be surprised later, at which
                // point their account is less protected than they believe.
                'Enter one of the codes you saved. It works once, and using it turns two-factor off so you can sign in with your password until you set it up again.'}
          </p>

          {mode === 'totp' ? (
            <form
              className="flex flex-col gap-2.5"
              onSubmit={(e) => {
                e.preventDefault();
                void submit();
              }}
            >
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                autoFocus
                placeholder="123456"
                aria-label="Six-digit authentication code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                disabled={busy}
                className="h-11 px-3.5 rounded-[var(--r-sm)] border text-[16px] tracking-[0.3em] text-center outline-none transition-colors ph-no-capture"
                style={{
                  background: 'var(--canvas)',
                  color: 'var(--ink)',
                  borderColor: error ? 'var(--danger)' : 'var(--line)',
                }}
              />
              <button
                type="submit"
                disabled={busy || code.trim().length < 6}
                className="h-11 mt-2 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
              >
                {busy ? 'Checking…' : 'Continue'}
              </button>
            </form>
          ) : (
            <form
              className="flex flex-col gap-2.5"
              onSubmit={(e) => {
                e.preventDefault();
                void submitBackup();
              }}
            >
              <input
                type="text"
                autoComplete="off"
                autoFocus
                placeholder="A1B2C-D3E4F-G5H6J-K7M8N"
                aria-label="Backup code"
                value={backup}
                onChange={(e) => setBackup(e.target.value)}
                disabled={busy}
                className="h-11 px-3.5 rounded-[var(--r-sm)] border text-[14px] font-mono text-center outline-none transition-colors ph-no-capture"
                style={{
                  background: 'var(--canvas)',
                  color: 'var(--ink)',
                  borderColor: error ? 'var(--danger)' : 'var(--line)',
                }}
              />
              {/* ⚠ Gated on a non-empty field only, NOT on a length or a
                  shape. The server normalises case, spaces and dashes, so a
                  client-side format rule would reject a correct code that
                  somebody typed in a way we did not predict — on the screen
                  where they have already lost their phone. */}
              <button
                type="submit"
                disabled={busy || backup.trim().length === 0}
                className="h-11 mt-2 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
              >
                {busy ? 'Checking…' : 'Use this code'}
              </button>
            </form>
          )}

          {error && (
            <div
              className="mt-3 px-3 py-2 rounded-[var(--r-sm)] border text-[13px] leading-relaxed"
              style={{
                background: 'var(--danger-bg)',
                borderColor: 'var(--danger)',
                color: 'var(--danger)',
              }}
              role="alert"
              aria-live="polite"
            >
              {error}
            </div>
          )}

          {/* ⚠ A way OUT of this screen is not optional. Without it, somebody
              who has lost their phone is stuck on a modal with no exit and no
              way to reach support from inside the product — which is how a
              security feature becomes a support incident. The backup-code
              switch is the FIRST of these, above signing out, because it is
              the one that actually solves the problem they have. */}
          <button
            type="button"
            onClick={() => switchTo(mode === 'totp' ? 'backup' : 'totp')}
            className="mt-4 w-full text-center text-[13px] underline underline-offset-2"
            style={{ color: 'var(--ink-muted)' }}
          >
            {mode === 'totp' ? 'Use a backup code instead' : 'Use my authenticator app instead'}
          </button>
          <button
            type="button"
            onClick={() => void performSignOut()}
            className="mt-2 w-full text-center text-[13px] underline underline-offset-2"
            style={{ color: 'var(--ink-muted)' }}
          >
            Sign in as someone else
          </button>
          <p
            className="mt-3 text-center text-[12px] leading-relaxed"
            style={{ color: 'var(--ink-faint)' }}
          >
            No phone and no backup codes? Email team@occupella.com — the setup key you
            saved will also re-add Occupella in a new authenticator app.
          </p>
        </div>
      </div>
    </div>
  );
}
