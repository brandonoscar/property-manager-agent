/**
 * A render crash must always produce a screen.
 *
 * ⚠ It did not. `ErrorBoundary` returned `<>{children}</>` when
 * `VITE_SENTRY_DSN` was unset, so on any deployment without Sentry a crash
 * unmounted the whole tree and the user got a BLANK WHITE PAGE — in exactly
 * the configuration that also has no error reporting, so nobody would ever
 * hear about it. That is the defect this file exists to hold closed.
 *
 * The other property here is that BOTH boundary paths render the same
 * component. Two crash screens drift, and the one that drifts is the one
 * nobody looks at.
 */

import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppCrash } from './AppCrash';

function Boom(): never {
  throw new Error('render exploded');
}

/** React logs a caught render error; silence it so the run stays readable. */
let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  consoleError.mockRestore();
  vi.resetModules();
  vi.unstubAllEnvs();
});

describe('the crash screen itself', () => {
  it('leads with reload, not retry', async () => {
    // ⚠ `resetError` re-renders the SAME tree from the SAME state, so a
    // crash caused by bad data in the store crashes again the instant it is
    // pressed. Reload is what actually gets somebody out.
    render(<AppCrash onRetry={() => {}} />);
    expect(await screen.findByTestId('app-crash-reload')).toBeTruthy();
  });

  it('offers no retry button when the caller has no reset', () => {
    render(<AppCrash />);
    expect(screen.queryByTestId('app-crash-retry')).toBeNull();
    expect(screen.getByTestId('app-crash-reload')).toBeTruthy();
  });

  it('shows a reference only when one actually exists', () => {
    // A reference nobody can look up is worse than none — it invites the
    // user to quote a string that resolves to nothing.
    const { rerender } = render(<AppCrash />);
    expect(screen.queryByTestId('app-crash-reference')).toBeNull();

    rerender(<AppCrash reference="abc123" />);
    expect(screen.getByTestId('app-crash-reference').textContent).toContain(
      'abc123',
    );
  });

  it('never puts the raw error text on screen', () => {
    // The old fallback rendered `error.message`. A minified React error
    // tells a property manager nothing and can carry internals; there is no
    // prop to pass one, and that is the point.
    render(<AppCrash onRetry={() => {}} />);
    const text = screen.getByTestId('app-crash').textContent ?? '';
    expect(text).not.toContain('undefined');
    expect(text).not.toContain('Error:');
  });
});

describe('the boundary mounts either way', () => {
  it('catches a crash with NO Sentry DSN configured', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', '');
    vi.resetModules();
    const { ErrorBoundary } = await import('../services/sentry');

    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    // Before the fix this rendered nothing at all.
    expect(screen.getByTestId('app-crash')).toBeTruthy();
  });

  it('catches a crash WITH a Sentry DSN configured', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', 'https://key@example.ingest.sentry.io/1');
    vi.resetModules();
    const { ErrorBoundary } = await import('../services/sentry');

    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByTestId('app-crash')).toBeTruthy();
  });

  it('renders children normally when nothing throws', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', '');
    vi.resetModules();
    const { ErrorBoundary } = await import('../services/sentry');

    render(
      <ErrorBoundary>
        <span>the app</span>
      </ErrorBoundary>,
    );

    expect(screen.getByText('the app')).toBeTruthy();
    expect(screen.queryByTestId('app-crash')).toBeNull();
  });
});
