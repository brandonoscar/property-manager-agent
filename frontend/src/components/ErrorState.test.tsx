/**
 * PR #117 Phase D — ErrorState primitive contracts.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { ErrorState } from './ErrorState';


describe('ErrorState', () => {
  it('renders the title + body', () => {
    render(
      <ErrorState
        title="Couldn't load the inbox"
        body="Network timed out after 30s."
      />,
    );
    expect(screen.getByTestId('error-state-title').textContent).toBe(
      "Couldn't load the inbox",
    );
    expect(screen.getByTestId('error-state-body').textContent).toBe(
      'Network timed out after 30s.',
    );
  });

  it('always renders a danger-tinted icon ring', () => {
    render(<ErrorState title="x" />);
    expect(screen.getByTestId('error-state-icon')).toBeInTheDocument();
  });

  it('omits body when not supplied', () => {
    render(<ErrorState title="x" />);
    expect(screen.queryByTestId('error-state-body')).toBeNull();
  });

  it('renders retry button + fires onRetry when supplied; default label is "Try again"', () => {
    const onRetry = vi.fn();
    render(<ErrorState title="x" onRetry={onRetry} />);
    const retry = screen.getByTestId('error-state-retry');
    expect(retry.textContent).toBe('Try again');
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('uses custom retryLabel when supplied', () => {
    render(<ErrorState title="x" retryLabel="Reload" onRetry={() => undefined} />);
    expect(screen.getByTestId('error-state-retry').textContent).toBe('Reload');
  });

  it('omits the retry button when no onRetry handler is supplied', () => {
    render(<ErrorState title="x" />);
    expect(screen.queryByTestId('error-state-retry')).toBeNull();
  });
});
