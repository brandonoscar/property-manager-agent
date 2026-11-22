/**
 * Login form — Supabase email/password + Google OAuth + demo bypass.
 *
 * DESIGN.md conformance pass (2026-07-06):
 *   • Flat --canvas viewport — the auth glow was flattened per the
 *     "keep them flat" rule; no radial, no gradient anywhere.
 *   • Solid-ink wordmark at weight 600 (headings never 700+, accent
 *     never as a gradient).
 *   • Terms/Privacy are real links now that the legal pages are live
 *     on the marketing site.
 *   • 400px card on --canvas-elevated; inputs on --canvas with iris
 *     focus ring; primary iris CTA; demo bypass as a quiet link.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  requestPasswordReset,
  resendConfirmation,
  sendSignInCode,
  signIn,
  supabaseConfigured,
  verifySignInCode,
  verifySignupCode,
  demoMode,
  DEMO_USER,
  DEMO_SESSION,
  DEMO_COMPANY_ID,
} from '../services/auth';
import { clearSessionExpired, sessionExpired } from '../services/sessionEnd';
import { useStore } from '../stores/store';
import { EmailTypoHint } from './EmailTypoHint';
import { Wordmark } from './Wordmark';

// ⚠ These are PATHS, not URLs, and that is the whole point. This app is an SPA
// that renders index.html for every path vercel.json does not exclude, so an
// absolute link to its own host showed the login screen instead of the document
// (founder-reported 2026-08-20, when this app was on the apex). The 2026-09-15
// swap moved the app to app.occupella.com and the marketing site to the apex —
// the defect moved with the app, it did not go away. A same-origin path always
// resolves.
//
// `frontend/public/legal/*.html` is this app's canonical set — what
// SettingsPage links, what the SMS consent checkbox links, and what the
// compliance work (the SMS program page, the subprocessor list) was written
// against. vercel.json's rewrite excludes `legal/`, so these are real files
// served without auth, which is what a signed-OUT screen needs.
//
// ⚠ The marketing site carries a SECOND copy of three of them — occupella.com
// /terms, /privacy and /sms are React routes in helixis-onboarding's Legal.tsx.
// Two sets of terms can diverge, and the one a customer agreed to then depends
// on which screen they were looking at. The other three here (billing,
// subprocessors, accessibility) exist only on this host. Worth retiring on
// purpose rather than leaving as an accident — it is a legal-review decision,
// not a hostname change.
const PRIVACY_URL = '/legal/privacy.html';

const TERMS_URL = '/legal/terms.html';

// THE one front door for new customers (founder decision, 2026-08-20:
// "it either needs to be sign up or start set up" — it's Start setup).
// The wizard provisions the whole customer — account, company, Buildium,
// webhooks — so the app never runs a parallel signup flow again. ToS
// acceptance and password creation moved to the wizard with it.
// /start lands INSIDE the wizard — the domain root serves the
// marketing landing page (founder-reported, first click). Both are the SAME
// deployment (helixis-onboarding), which took the apex on 2026-09-15.
const SETUP_URL = 'https://occupella.com/start';

/**
 * Did GoTrue refuse this because it is rate-limiting the account?
 *
 * ⚠ The obvious pattern — /rate limit|too many/ — does NOT match the message
 * Supabase actually sends, which is "For security purposes, you can only
 * request this after 60 seconds". Both email paths carried that pattern, so a
 * genuine rate-limit was reported to the user as "Couldn't send the email":
 * the wrong instruction, because nothing is broken and the only cure is to
 * wait. Caught by writing a test against the provider's REAL string rather
 * than a plausible one.
 *
 * ⚠ RENAMED from `isEmailRateLimit`, and the old name is why this took a
 * second pass. SIGN-IN is rate-limited too — the failure the checklist calls
 * "account locked after failed attempts, no unlock path" — and its branch
 * carried `/rate limit|too many requests/`, the very pattern the paragraph
 * above documents as not working. So somebody who mistyped their password a
 * few times got "Sign in didn't go through. Try again, or contact
 * team@occupella.com": the two worst instructions available, because
 * retrying is what extends the lockout and there is nothing for support to
 * do. A name that says "Email" is exactly why the third caller did not find
 * it (gotcha 67 — a rule documented by naming its first caller).
 */
export function isRateLimited(message: string): boolean {
  return /rate limit|too many|only request this after|over_email_send_rate_limit/i.test(
    message,
  );
}

/**
 * How long GoTrue says to wait, in seconds, or null when it did not say.
 *
 * ⚠ The number is the whole point of the message. Every branch used to say
 * "wait a minute" regardless — a guess that is right for the 60-second
 * limiter and wrong for any other, and wrong in the direction that sends
 * somebody back too early to be limited again. Null means "it did not say",
 * which the caller renders as the honest generic rather than inventing a
 * figure (gotchas 55/57 — unknown is not a value).
 */
export function retryAfterSeconds(message: string): number | null {
  const m = /only request this after (\d+) seconds?/i.exec(message);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** "wait 47 seconds" / "wait a minute" when the server did not say. */
export function waitPhrase(message: string): string {
  const secs = retryAfterSeconds(message);
  if (secs === null) return 'wait a minute';
  if (secs === 1) return 'wait 1 second';
  return `wait ${secs} seconds`;
}

export function LoginForm() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  // Inline error string shown directly under the form. Toasts are easy
  // to miss when stacked behind the iris glow; a permanent inline row
  // makes failures undeniable.
  const [error, setError] = useState<string | null>(null);
  // Confirmation-pending is a SUCCESS state, not an error: signup worked,
  // one step remains. Rendered green with a Resend affordance — the
  // user's only self-service recovery when the email is slow or lost
  // (first real provisioning walk, 2026-08-20: the red "Almost there"
  // box read as a failure and offered no way forward).
  const [notice, setNotice] = useState<string | null>(null);
  const [resendCooldown, setResendCooldown] = useState(0);
  const [resending, setResending] = useState(false);
  // The 6-digit confirmation code (founder's call, 2026-08-20 walk:
  // every door confirms by CODE, matching the wizard — no link-clicking).
  const [code, setCode] = useState('');
  const [verifying, setVerifying] = useState(false);
  // Password reset shares `resendCooldown` with the confirmation resend
  // DELIBERATELY: GoTrue's ~60s limiter is per RECIPIENT, not per template,
  // so a reset request consumes the same budget a resend would. Two
  // independent counters would each read "ready" while the server refuses
  // the second click — a button that looks armed and bounces off the
  // limiter is the thing the existing countdown exists to prevent.
  const [resetting, setResetting] = useState(false);
  // What the notice below the form is about, which decides whether it asks
  // for a code and which call checks it. 'reset' shows no code box: the
  // reset email carries a link. (It used to show one, wired to the SIGNUP
  // verifier, which could never accept a reset.)
  const [codeKind, setCodeKind] = useState<'signup' | 'signin' | 'reset'>('signup');
  const [sendingCode, setSendingCode] = useState(false);
  // Why the form is showing, when the app ended a session whose refresh
  // token was rejected (auth.ts `expireSession`). Read once, then cleared.
  const [expired] = useState(sessionExpired);
  useEffect(() => {
    if (expired) clearSessionExpired();
  }, [expired]);
  const setAuth = useStore((s) => s.setAuth);
  const setCompanies = useStore((s) => s.setCompanies);

  // Supabase rate-limits confirmation resends (~60s); the countdown keeps
  // the button honest instead of letting a click bounce off the limiter.
  useEffect(() => {
    if (resendCooldown <= 0) return;
    const t = setTimeout(() => setResendCooldown((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendCooldown]);

  const handleVerifyCode = async () => {
    if (verifying || code.trim().length !== 6) return;
    setVerifying(true);
    setError(null);
    try {
      if (codeKind === 'signin') {
        await verifySignInCode(email, code.trim());
      } else {
        await verifySignupCode(email, code.trim());
        toast.success('Email confirmed. Welcome to Occupella.');
      }
      // Success returns a live session; the onAuthStateChange listener in
      // App.tsx transitions the app. Nothing else to do here.
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[occupella] code verification failed', err);
      const raw = err instanceof Error ? err.message : '';
      setError(
        /expired|invalid/i.test(raw)
          ? "That code didn't match or has expired. Send a fresh one below."
          : "Couldn't verify the code. Try again, or contact team@occupella.com.",
      );
    } finally {
      setVerifying(false);
    }
  };

  const handleResend = async () => {
    if (resendCooldown > 0 || resending || !email) return;
    setResending(true);
    try {
      if (codeKind === 'signin') {
        await sendSignInCode(email);
        setNotice(`A new code is on its way to ${email}. Check spam if it isn't there in a minute.`);
      } else {
        await resendConfirmation(email);
        setNotice('Confirmation email sent again. Give it a minute, and check spam.');
      }
      setResendCooldown(60);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[occupella] resend confirmation failed', err);
      const raw = err instanceof Error ? err.message : '';
      setError(
        isRateLimited(raw)
          ? `Too many resend attempts: ${waitPhrase(raw)} and try again.`
          : "Couldn't re-send the email. Try again, or contact team@occupella.com.",
      );
    } finally {
      setResending(false);
    }
  };

  const handleForgotPassword = async () => {
    if (resetting || resendCooldown > 0) return;
    if (!email) {
      setError('Enter your email address first, then choose "Forgot password?".');
      return;
    }
    if (!supabaseConfigured) {
      setError(
        "Sign-in isn't configured for this build. Contact team@occupella.com.",
      );
      return;
    }
    setResetting(true);
    setError(null);
    try {
      await requestPasswordReset(email);
      // ⚠ The SAME words whether or not that address has an account.
      // Supabase returns success either way on purpose, so the form cannot
      // be used to discover who is a customer; wording it as "sent to you"
      // would leak exactly what the API refuses to.
      setCodeKind('reset');
      setNotice(
        `If an account exists for ${email}, a reset link is on its way. Check spam. The link opens a page to choose a new password.`,
      );
      setResendCooldown(60);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[occupella] password reset request failed', err);
      const raw = err instanceof Error ? err.message : '';
      setError(
        isRateLimited(raw)
          ? `Too many email requests: ${waitPhrase(raw)} and try again.`
          : "Couldn't send the reset email. Try again, or contact team@occupella.com.",
      );
    } finally {
      setResetting(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || !password) return;

    // Defensive: if the build doesn't have VITE_SUPABASE_URL /
    // VITE_SUPABASE_ANON_KEY set in Vercel, the supabase client points
    // at a placeholder host and every auth call silently no-ops or
    // throws cryptically. Surface this clearly instead.
    if (!supabaseConfigured) {
      // Deploy misconfiguration — the fix goes in the console for whoever
      // operates the deploy; the user just needs to know sign-in is down.
      // eslint-disable-next-line no-console
      console.error(
        '[occupella] auth not configured: VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY missing on this deploy',
      );
      const msg =
        'Sign-in is temporarily unavailable on this deployment. Please contact team@occupella.com.';
      setError(msg);
      toast.error(msg, { duration: 12000 });
      return;
    }

    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      // eslint-disable-next-line no-console
      console.info('[helixis] sign-in attempt', { email });
      const result = await signIn(email, password);
      // eslint-disable-next-line no-console
      console.info('[helixis] sign-in returned', {
        hasUser: !!result?.user,
        hasSession: !!result?.session,
      });
      // If the call resolved with no session, the listener in App.tsx
      // won't fire and the page won't transition. Show that.
      if (!result?.session) {
        // Diagnostic detail (unconfirmed email is the usual cause) goes to
        // the console — the message a user sees must never surface our
        // auth vendor's internals.
        // eslint-disable-next-line no-console
        console.error(
          '[occupella] sign-in returned no session — most common cause: email not confirmed (Supabase dashboard → Authentication → Users)',
        );
        setError(null);
        setCodeKind('signup');
        setNotice(
          "This account's email hasn't been confirmed yet. Enter the " +
            '6-digit code from your email below, or send it again.',
        );
      }
    } catch (err) {
      const verb = 'Sign in';
      // Full vendor error to the console for diagnosis; the user-facing
      // message maps known cases to plain language and never echoes raw
      // auth-vendor internals (release audit bug #4).
      // eslint-disable-next-line no-console
      console.error('[occupella] auth error', err);
      const raw = err instanceof Error ? err.message : '';
      let message: string;
      if (/invalid login credentials/i.test(raw)) {
        message = 'Email or password is incorrect.';
      } else if (/email not confirmed/i.test(raw)) {
        setCodeKind('signup');
        setNotice(
          "This account's email hasn't been confirmed yet. Enter the " +
            '6-digit code from your email below, or send it again.',
        );
        setLoading(false);
        return;
      } else if (isRateLimited(raw)) {
        // ⚠ THE CHECKLIST'S "account locked after failed attempts, no unlock
        // path", and it was this branch. It used to test
        // `/rate limit|too many requests/`, which is the pattern
        // `isRateLimited`'s own docstring documents as NOT matching what
        // Supabase sends — so a limited sign-in fell to the generic `else` and
        // read "Sign in didn't go through. Try again, or contact
        // team@occupella.com". Both instructions are wrong: retrying extends
        // the limit, and there is nothing for support to do.
        //
        // There is no permanent lock to unlock — GoTrue's limiter is
        // time-based — so the unlock path IS the wait, and naming its length
        // is what turns a dead end into an action.
        message = `Too many sign-in attempts: ${waitPhrase(raw)} and try again. ` +
          'Your password still works; nothing has been locked.';
      } else if (/already registered|already been registered/i.test(raw)) {
        message = 'An account with this email already exists. Try signing in instead.';
      } else if (/network|fetch/i.test(raw)) {
        message = 'Couldn\'t reach the sign-in service. Check your connection and try again.';
      } else {
        message = `${verb} didn't go through. Try again, or contact team@occupella.com.`;
      }
      setError(message);
      toast.error(message, { duration: 12000 });
    } finally {
      setLoading(false);
    }
  };

  const handleEmailCode = async () => {
    if (sendingCode || resendCooldown > 0) return;
    if (!email) {
      setError('Enter your email address first, then choose "Email me a code".');
      return;
    }
    if (!supabaseConfigured) {
      setError("Sign-in isn't configured for this build. Contact team@occupella.com.");
      return;
    }
    setSendingCode(true);
    setError(null);
    try {
      await sendSignInCode(email);
      setCodeKind('signin');
      setCode('');
      setNotice(`We sent a sign-in code to ${email}. Enter the 6-digit code below.`);
      setResendCooldown(60);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[occupella] sign-in code request failed', err);
      const raw = err instanceof Error ? err.message : '';
      // ⚠ GoTrue refuses an unknown address when shouldCreateUser is off, so
      // this branch cannot use the same words either way the way the reset
      // does. It says what to do next rather than what we know.
      setError(
        isRateLimited(raw)
          ? `Too many email requests: ${waitPhrase(raw)} and try again.`
          : /signups? not allowed|user not found/i.test(raw)
            ? "We couldn't send a code to that address. Check the spelling, or start a free trial at occupella.com/start."
            : "Couldn't send the code. Try again, or contact team@occupella.com.",
      );
    } finally {
      setSendingCode(false);
    }
  };

  const handleDemo = () => {
    if (!demoMode) return;
    setAuth(DEMO_USER, DEMO_SESSION);
    setCompanies([DEMO_COMPANY_ID]);
  };

  const askForCode = notice !== null && codeKind !== 'reset';
  const signInOff = loading || !email || !password;
  const codeOff = sendingCode || resendCooldown > 0;

  return (
    <div className="min-h-screen flex flex-col" style={{ background: 'var(--canvas)' }}>
      {/* The signup wizard's header: wordmark left, a thin line, the other
          door on the right. */}
      <header
        className="h-16 shrink-0 flex items-center justify-between gap-4 px-4 sm:px-8 border-b"
        style={{ borderColor: 'var(--line)' }}
      >
        <Wordmark size={24} />
        <p className="text-[14px] text-right" style={{ color: 'var(--ink-muted)' }}>
          New to Occupella?{' '}
          {/* Accounts are made only at /start, which also creates the
              company (founder decision, 2026-08-20). */}
          <a
            href={SETUP_URL}
            className="font-medium underline underline-offset-2"
            style={{ color: 'var(--iris)' }}
            data-testid="login-start-setup"
          >
            Start free trial
          </a>
        </p>
      </header>

      <main className="flex-1 flex justify-center px-4 pt-12 sm:pt-20 pb-12">
        <div className="w-full" style={{ maxWidth: 400 }}>
          <h1
            className="mb-6"
            style={{
              fontSize: 28,
              fontWeight: 500,
              letterSpacing: '-0.015em',
              fontFamily: 'var(--font-display)',
              color: 'var(--ink)',
            }}
          >
            Sign in
          </h1>

          {/* NO Google/OAuth sign-in here: deliberately removed by the
              founder (2026-07-06). Do not "restore" a button for
              auth.signInWithGoogle without asking first. */}
          {expired && (
            <div
              className="mb-4 px-3 py-2 rounded-[var(--r-sm)] border text-[14px] leading-relaxed"
              style={{
                background: 'var(--canvas-raised)',
                borderColor: 'var(--card-edge)',
                color: 'var(--ink)',
              }}
              role="status"
              data-testid="login-session-expired"
            >
              Your session expired. Sign in again.
            </div>
          )}

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <LoginInput
                id="login-email"
                label="Email"
                type="email"
                placeholder="you@company.com"
                value={email}
                onChange={(v) => setEmail(v)}
                disabled={loading}
                autoComplete="email"
              />
              <EmailTypoHint email={email} onAccept={setEmail} />
            </div>
            <LoginInput
              id="login-password"
              label="Password"
              type="password"
              value={password}
              onChange={(v) => setPassword(v)}
              disabled={loading}
              autoComplete="current-password"
              sensitive
            />

            <div className="flex flex-col gap-2 mt-1">
              <button
                type="submit"
                disabled={signInOff}
                className="h-10 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:cursor-not-allowed"
                style={
                  signInOff
                    ? { background: 'var(--disabled-bg)', color: 'var(--disabled-ink)' }
                    : { background: 'var(--iris)', color: '#fff' }
                }
                onMouseEnter={(e) => {
                  if (!signInOff) (e.currentTarget as HTMLElement).style.background = 'var(--iris-hover)';
                }}
                onMouseLeave={(e) => {
                  if (!signInOff) (e.currentTarget as HTMLElement).style.background = 'var(--iris)';
                }}
              >
                {loading ? <LoadingDots /> : 'Sign in'}
              </button>
              {/* Secondary: white with a border. `type="button"` so it never
                  submits the password form. */}
              <button
                type="button"
                onClick={handleEmailCode}
                disabled={codeOff}
                className="h-10 rounded-[var(--r-sm)] border text-[14px] font-medium transition-colors disabled:cursor-not-allowed"
                style={{
                  background: 'var(--canvas)',
                  borderColor: 'var(--line-strong)',
                  color: codeOff ? 'var(--disabled-ink)' : 'var(--ink)',
                }}
                data-testid="login-email-code"
              >
                {sendingCode ? 'Sending…' : 'Email me a code'}
              </button>
            </div>

            {/* Recovery's landing half (SetPasswordForm + the `type=recovery`
                hash parser) needs this to be reachable at all. `type="button"`
                matters: a bare <button> inside a <form> submits it. */}
            <button
              type="button"
              onClick={handleForgotPassword}
              disabled={resetting || resendCooldown > 0}
              className="self-start text-[14px] underline underline-offset-2 disabled:cursor-not-allowed"
              style={{ color: resetting || resendCooldown > 0 ? 'var(--disabled-ink)' : 'var(--ink-muted)' }}
            >
              {resetting
                ? 'Sending…'
                : resendCooldown > 0
                  ? `Forgot password? (${resendCooldown}s)`
                  : 'Forgot password?'}
            </button>
          </form>

          {notice && (
            <div
              className="mt-4 px-3 py-3 rounded-[var(--r-sm)] border text-[14px] leading-relaxed"
              style={{
                background: 'var(--positive-bg)',
                borderColor: 'var(--positive)',
                color: 'var(--positive)',
              }}
              role="status"
              aria-live="polite"
              data-testid="login-confirm-notice"
            >
              {notice}
              {askForCode ? (
                <>
                  <div className="mt-2 flex gap-2">
                    <input
                      type="text"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      maxLength={6}
                      placeholder="6-digit code"
                      value={code}
                      onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                      className="flex-1 h-10 px-3 rounded-[var(--r-sm)] border text-[14px] tracking-[0.25em]"
                      style={{
                        background: 'var(--canvas)',
                        borderColor: 'var(--card-edge)',
                        color: 'var(--ink)',
                      }}
                      aria-label={
                        codeKind === 'signin'
                          ? 'Sign-in code from the email'
                          : 'Confirmation code from the email'
                      }
                      data-testid="login-confirm-code"
                    />
                    <button
                      type="button"
                      onClick={handleVerifyCode}
                      disabled={code.trim().length !== 6 || verifying}
                      className="h-10 px-4 rounded-[var(--r-sm)] font-medium text-[14px] disabled:cursor-not-allowed"
                      style={
                        code.trim().length !== 6 || verifying
                          ? { background: 'var(--disabled-bg)', color: 'var(--disabled-ink)' }
                          : { background: 'var(--iris)', color: '#fff' }
                      }
                      data-testid="login-confirm-verify"
                    >
                      {verifying ? 'Checking…' : codeKind === 'signin' ? 'Sign in' : 'Verify'}
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={handleResend}
                    disabled={resendCooldown > 0 || resending}
                    className="block mt-2 underline underline-offset-2 disabled:no-underline"
                    style={{ color: 'var(--positive)' }}
                    data-testid="login-resend-confirmation"
                  >
                    {resending
                      ? 'Sending…'
                      : resendCooldown > 0
                        ? `Send again available in ${resendCooldown}s`
                        : codeKind === 'signin'
                          ? 'Send a new code'
                          : 'Send the confirmation email again'}
                  </button>
                </>
              ) : null}
            </div>
          )}

          {error && (
            <div
              className="mt-4 px-3 py-2 rounded-[var(--r-sm)] border text-[14px] leading-relaxed"
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

          {demoMode && (
            <button
              type="button"
              onClick={handleDemo}
              disabled={loading}
              className="mt-6 text-[14px] underline underline-offset-2"
              style={{ color: 'var(--ink-muted)' }}
            >
              Continue as demo
            </button>
          )}
        </div>
      </main>

      {/* Legal row in normal case. The entity name stays on the entry
          screen: A2P/Twilio verification crawls match it to the site. */}
      <footer
        className="shrink-0 border-t px-4 sm:px-8 py-5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]"
        style={{ borderColor: 'var(--line)', color: 'var(--ink-muted)' }}
      >
        <FooterLink href={PRIVACY_URL} label="Privacy" />
        <span aria-hidden>·</span>
        <FooterLink href={TERMS_URL} label="Terms" />
        <span aria-hidden>·</span>
        <span>Operated by Oscar Ventures LLC</span>
      </footer>
    </div>
  );
}

function FooterLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline underline-offset-2"
      style={{ color: 'var(--ink-muted)' }}
    >
      {label}
    </a>
  );
}

function LoginInput({
  id,
  label,
  type,
  placeholder,
  value,
  onChange,
  disabled,
  autoComplete,
  sensitive = false,
}: {
  id: string;
  /** Shown above the field. A placeholder is not a label: it disappears on
   *  the first keystroke. */
  label: string;
  type: string;
  placeholder?: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  autoComplete?: string;
  /**
   * Mark the input as holding sensitive data. Adds the `ph-no-capture`
   * class so PostHog's autocapture skips it and session-recording
   * masking redacts its text. Use for passwords, API keys, secrets.
   */
  sensitive?: boolean;
}) {
  const [focused, setFocused] = useState(false);
  const baseClass =
    'h-10 w-full px-3 rounded-[var(--r-sm)] border text-[14px] outline-none transition-colors';
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-[14px] font-medium" style={{ color: 'var(--ink)' }}>
        {label}
      </label>
      <input
        id={id}
        type={type}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        disabled={disabled}
        autoComplete={autoComplete}
        className={sensitive ? `${baseClass} ph-no-capture` : baseClass}
        style={{
          background: 'var(--canvas)',
          color: 'var(--ink)',
          borderColor: focused ? 'var(--line-focus)' : 'var(--line-strong)',
          boxShadow: focused ? '0 0 0 3px var(--iris-ring)' : 'none',
        }}
      />
    </div>
  );
}

function LoadingDots() {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{
          background: 'white',
          animation: 'pulse-dot 1.4s var(--ease) infinite',
          animationDelay: '0ms',
        }}
      />
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{
          background: 'white',
          animation: 'pulse-dot 1.4s var(--ease) infinite',
          animationDelay: '160ms',
        }}
      />
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{
          background: 'white',
          animation: 'pulse-dot 1.4s var(--ease) infinite',
          animationDelay: '320ms',
        }}
      />
    </span>
  );
}

