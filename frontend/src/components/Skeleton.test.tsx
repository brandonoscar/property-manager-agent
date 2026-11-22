/**
 * PR #117 Phase D — Skeleton primitive contracts.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { Skeleton, SkeletonListRow } from './Skeleton';


describe('Skeleton — variant + dimensions', () => {
  it('defaults to line variant with 10px height + 100% width', () => {
    render(<Skeleton data-testid="s" />);
    const el = screen.getByTestId('s');
    expect(el.getAttribute('data-skeleton-variant')).toBe('line');
    expect(el.style.height).toBe('10px');
    expect(el.style.width).toBe('100%');
  });

  it('rect variant defaults to 56px tall', () => {
    render(<Skeleton variant="rect" data-testid="s" />);
    const el = screen.getByTestId('s');
    expect(el.getAttribute('data-skeleton-variant')).toBe('rect');
    expect(el.style.height).toBe('56px');
  });

  it('circle variant defaults to 24×24 square with full pill radius', () => {
    render(<Skeleton variant="circle" data-testid="s" />);
    const el = screen.getByTestId('s');
    expect(el.getAttribute('data-skeleton-variant')).toBe('circle');
    expect(el.style.width).toBe('24px');
    expect(el.style.height).toBe('24px');
    expect(el.style.borderRadius).toBe('9999px');
  });

  it('overrides default width / height when explicit props are passed', () => {
    render(<Skeleton width={120} height={6} data-testid="s" />);
    const el = screen.getByTestId('s');
    expect(el.style.width).toBe('120px');
    expect(el.style.height).toBe('6px');
  });

  it('always carries role=status + aria-label for assistive tech', () => {
    render(<Skeleton data-testid="s" />);
    const el = screen.getByTestId('s');
    expect(el.getAttribute('role')).toBe('status');
    expect(el.getAttribute('aria-label')).toBe('Loading');
  });

  it('always animates (pulse class)', () => {
    render(<Skeleton data-testid="s" />);
    expect(screen.getByTestId('s').className).toContain('animate-pulse');
  });
});


describe('SkeletonListRow — composition', () => {
  it('renders three line skeletons + a wrapping card', () => {
    render(<SkeletonListRow testIdPrefix="row" />);
    expect(screen.getByTestId('row')).toBeInTheDocument();
    // Three internal Skeleton lines aren't pinned by testid, but the
    // wrapper exists; this is mostly a smoke test that the helper
    // mounts cleanly.
  });
});
