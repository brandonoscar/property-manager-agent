/**
 * PR #117 Phase D — EmptyState primitive contracts.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { EmptyState } from './EmptyState';


describe('EmptyState', () => {
  it('renders the title and body when supplied', () => {
    render(
      <EmptyState
        title="Nothing in your inbox yet."
        body="Buildium events arrive within seconds."
      />,
    );
    expect(screen.getByTestId('empty-state-title').textContent).toBe(
      'Nothing in your inbox yet.',
    );
    expect(screen.getByTestId('empty-state-body').textContent).toBe(
      'Buildium events arrive within seconds.',
    );
  });

  it('omits the body container when no body is supplied', () => {
    render(<EmptyState title="Nothing yet." />);
    expect(screen.queryByTestId('empty-state-body')).toBeNull();
  });

  it('omits the icon container when no icon is supplied', () => {
    render(<EmptyState title="Nothing yet." />);
    expect(screen.queryByTestId('empty-state-icon')).toBeNull();
  });

  it('renders the icon container when an icon is supplied', () => {
    render(<EmptyState title="x" icon={<span>i</span>} />);
    expect(screen.getByTestId('empty-state-icon')).toBeInTheDocument();
  });

  it('renders the CTA button + fires onCta when both label and handler are supplied', () => {
    const onCta = vi.fn();
    render(
      <EmptyState
        title="No data"
        ctaLabel="Connect Buildium"
        onCta={onCta}
      />,
    );
    const cta = screen.getByTestId('empty-state-cta');
    expect(cta.textContent).toBe('Connect Buildium');
    fireEvent.click(cta);
    expect(onCta).toHaveBeenCalledTimes(1);
  });

  it('omits the CTA when only label OR only handler is supplied', () => {
    const { unmount } = render(<EmptyState title="x" ctaLabel="Click" />);
    expect(screen.queryByTestId('empty-state-cta')).toBeNull();
    unmount();
    render(<EmptyState title="x" onCta={() => undefined} />);
    expect(screen.queryByTestId('empty-state-cta')).toBeNull();
  });
});
