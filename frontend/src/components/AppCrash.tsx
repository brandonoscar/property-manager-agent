/**
 * The screen a render crash produces.
 *
 * ⚠ Until this existed the crash screen only rendered when Sentry was
 * configured — the boundary returned its children unwrapped otherwise, so a
 * deployment without ``VITE_SENTRY_DSN`` answered a crash with a blank white
 * page. The one configuration with no error reporting was also the one with
 * no error screen, which is exactly backwards.
 *
 * Three decisions worth keeping:
 *
 * * **Reload is the primary action, not "try again".** React's `resetError`
 *   re-renders the same tree from the same state, so a crash caused by bad
 *   data in the store crashes again immediately and the button reads as
 *   broken. A reload discards the state that caused it. "Try again" stays as
 *   the cheap secondary for the transient case.
 * * **No raw error message.** A minified React error ("Cannot read
 *   properties of undefined") tells a property manager nothing, and can
 *   carry internals. What the user needs is something to quote to us, which
 *   is what `reference` is for — it appears only when Sentry actually
 *   assigned an event id, because a reference nobody can look up is worse
 *   than none.
 * * **Current design tokens.** The old fallback was the last file in the app
 *   still on the legacy `bg-surface` / `text-error` aliases, whose own
 *   comment says "remove in the final cleanup pass" — a cleanup that would
 *   have silently unstyled the one screen nobody ever looks at.
 */

import { Button } from './Button';

interface AppCrashProps {
  /** Re-render the same tree. Omitted when the caller has no reset. */
  onRetry?: () => void;
  /** Sentry event id, when one was assigned. Shown so the user can quote it. */
  reference?: string;
}

export function AppCrash({ onRetry, reference }: AppCrashProps) {
  return (
    <div
      data-testid="app-crash"
      className="flex items-center justify-center h-screen px-6"
      style={{ background: 'var(--canvas)' }}
    >
      <div
        className="w-full max-w-[440px] rounded-[var(--r-lg)] border text-center"
        style={{
          background: 'var(--canvas-elevated)',
          borderColor: 'var(--line)',
          padding: '40px 32px',
          boxShadow: 'var(--shadow-md)',
        }}
      >
        <h1
          className="font-semibold tracking-tight"
          style={{ color: 'var(--ink)', fontSize: 'var(--text-page-title)' }}
        >
          Occupella hit an error
        </h1>
        <p
          className="mt-2 leading-relaxed"
          style={{ color: 'var(--ink-muted)', fontSize: 'var(--text-body)' }}
        >
          Nothing you were working on was changed. Reloading usually clears it.
        </p>

        <div className="mt-6 flex items-center justify-center gap-2">
          <Button
            variant="primary"
            size="md"
            onClick={() => window.location.reload()}
            data-testid="app-crash-reload"
          >
            Reload Occupella
          </Button>
          {onRetry ? (
            <Button
              variant="secondary"
              size="md"
              onClick={onRetry}
              data-testid="app-crash-retry"
            >
              Try again
            </Button>
          ) : null}
        </div>

        {reference ? (
          <p
            className="mt-6 font-mono"
            style={{ color: 'var(--ink-faint)', fontSize: 'var(--text-micro)' }}
            data-testid="app-crash-reference"
          >
            Reference {reference}
          </p>
        ) : null}
      </div>
    </div>
  );
}
