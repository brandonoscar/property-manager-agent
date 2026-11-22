/**
 * Sentry error tracking for the frontend.
 *
 * When VITE_SENTRY_DSN is blank, initSentry() is a no-op and no error
 * boundary wrapping happens — the app renders normally with no
 * tracking, same graceful-optional pattern as the backend.
 */

import * as Sentry from '@sentry/react';
import { Component } from 'react';
import type { PropsWithChildren, ReactElement } from 'react';

import { AppCrash } from '../components/AppCrash';

const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined;

/**
 * The environment this bundle reports as.
 *
 * ⚠ Vercel's own answer wins for anything that is not production. Every
 * event used to say `production`: a preview built from a `claude/*` or
 * Dependabot branch with the same VITE_SENTRY_ENVIRONMENT value was
 * indistinguishable from the site customers use.
 */
export function sentryEnvironment(vercelEnv: string, configured: string | undefined): string {
  if (vercelEnv && vercelEnv !== 'production') return vercelEnv;
  return configured || 'production';
}

const environment = sentryEnvironment(
  typeof __VERCEL_ENV__ === 'string' ? __VERCEL_ENV__ : '',
  import.meta.env.VITE_SENTRY_ENVIRONMENT as string | undefined,
);

/** The commit, so an event can be matched to the build that threw it. */
const release = (typeof __APP_RELEASE__ === 'string' && __APP_RELEASE__) || undefined;

export const sentryConfigured = Boolean(dsn);

// Replay privacy posture. Tenant/owner PII (names, addresses, email bodies,
// Buildium data) renders in normal DOM, so an error-triggered replay must
// mask all text and block all media before anything is sent to Sentry. Exported
// + asserted in sentry.test.ts so this can never silently regress to `false`.
export const REPLAY_PRIVACY = { maskAllText: true, blockAllMedia: true } as const;

// Errors we never want to see in Sentry — they come from the user's browser,
// not our code, and each one is pure noise that buries real issues (the same
// signal-vs-noise concern as the backend's test-pollution guard, gotcha #16).
//
// The MetaMask entries are the concrete trigger: the MetaMask wallet extension
// injects ``inpage.js`` into EVERY page and throws "Failed to connect to
// MetaMask" when no wallet is present — captured by our global handler as a
// production ``error`` even though Occupella has nothing to do with crypto.
export const IGNORE_ERRORS = [
  // Browser-extension noise (MetaMask + other wallets/extensions).
  'MetaMask',
  'Failed to connect to MetaMask',
  'ethereum',
  // Benign browser churn that is not actionable.
  'ResizeObserver loop limit exceeded',
  'ResizeObserver loop completed with undelivered notifications',
  'Non-Error promise rejection captured',
  // Network blips the user can just retry — not app bugs.
  'Failed to fetch',
  'NetworkError when attempting to fetch resource',
  'Load failed',
];

// Drop any event whose stack originates in a browser extension. This is what
// actually catches the MetaMask case: its frames live at
// ``chrome-extension://.../inpage.js``. denyUrls matches on frame URL, so it's
// robust even when the message text changes between extension versions.
export const DENY_URLS = [
  /^chrome-extension:\/\//i,
  /^moz-extension:\/\//i,
  /^safari-(web-)?extension:\/\//i,
  /extensions\//i,
];

export function initSentry(): void {
  if (!dsn) return;
  Sentry.init({
    dsn,
    environment,
    release,
    // Performance: 20% trace sampling matches the backend default.
    tracesSampleRate: 0.2,
    // Only include session replays on errors to keep free-tier quota lean.
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 1.0,
    // Keep third-party browser-extension + benign-noise events out of Sentry so
    // real Occupella errors aren't buried (see IGNORE_ERRORS / DENY_URLS above).
    ignoreErrors: IGNORE_ERRORS,
    denyUrls: DENY_URLS,
    integrations: [
      Sentry.browserTracingIntegration(),
      // Never ship rendered content — see REPLAY_PRIVACY above.
      Sentry.replayIntegration({ ...REPLAY_PRIVACY }),
    ],
  });
}

/**
 * Fallback boundary for deployments with no Sentry DSN.
 *
 * ⚠ This class exists because the previous implementation returned
 * ``<>{children}</>`` when Sentry was unconfigured — so a render crash
 * unmounted the whole tree and the user got a BLANK WHITE PAGE, in exactly
 * the configuration that also has no error reporting. A crash screen that
 * only appears when someone is already watching is not a crash screen.
 */
class PlainErrorBoundary extends Component<
  PropsWithChildren,
  { crashed: boolean }
> {
  state = { crashed: false };

  static getDerivedStateFromError(): { crashed: boolean } {
    return { crashed: true };
  }

  componentDidCatch(error: unknown): void {
    // Nowhere to report it — say so in the console so a developer running
    // without a DSN still sees the cause instead of a silent screen.
    console.error('Occupella crashed:', error);
  }

  render(): ReactElement {
    if (this.state.crashed) {
      return <AppCrash onRetry={() => this.setState({ crashed: false })} />;
    }
    return <>{this.props.children}</>;
  }
}

/**
 * Error boundary. **Always mounts** — Sentry's when a DSN is configured, our
 * own otherwise — and both paths render the SAME `AppCrash` component, so
 * the screen a customer sees cannot depend on whether our telemetry happens
 * to be switched on.
 */
export function ErrorBoundary({ children }: PropsWithChildren): ReactElement {
  if (!sentryConfigured) {
    return <PlainErrorBoundary>{children}</PlainErrorBoundary>;
  }
  return (
    <Sentry.ErrorBoundary
      fallback={({ resetError, eventId }) => (
        <AppCrash onRetry={resetError} reference={eventId || undefined} />
      )}
    >
      {children}
    </Sentry.ErrorBoundary>
  );
}

export function captureException(error: unknown, extra?: Record<string, unknown>): void {
  if (!sentryConfigured) return;
  Sentry.captureException(error, { extra });
}

/**
 * Attach the current user to Sentry events. Call after login so
 * frontend exceptions are tied to the user who hit them.
 *
 * ⚠ The id only. This sent the email too, so every errored session put a
 * customer's address in a third-party tool (measured 2026-10-23: 3 addresses
 * across 90 events), while the backend runs `send_default_pii=False`. The id
 * finds the person in our own database.
 */
export function setSentryUser(userId: string): void {
  if (!sentryConfigured) return;
  Sentry.setUser({ id: userId });
}

export function clearSentryUser(): void {
  if (!sentryConfigured) return;
  Sentry.setUser(null);
}
