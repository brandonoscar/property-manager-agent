/**
 * PR #116 Phase B — Badge tone contract.
 *
 * Each tone in the BadgeTone vocabulary must map to a distinct
 * background token AND survive a `data-badge-tone` attribute hop
 * (so analytics / E2E can pin the rendered status without parsing
 * CSS colors).
 *
 * These tests use the existing semantic tokens (--positive /
 * --caution / --danger / --info) — no new color values introduced.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { Badge } from './Badge';

describe('Badge — tone routing', () => {
  it('renders the label children', () => {
    render(<Badge data-testid="b">Late</Badge>);
    expect(screen.getByTestId('b').textContent).toBe('Late');
  });

  it('defaults to neutral tone when no tone supplied', () => {
    render(<Badge data-testid="b">x</Badge>);
    expect(screen.getByTestId('b').getAttribute('data-badge-tone')).toBe('neutral');
  });

  it('maps each tone to a data-attribute for downstream pinning', () => {
    for (const tone of ['neutral', 'positive', 'caution', 'danger', 'info'] as const) {
      const { unmount } = render(
        <Badge tone={tone} data-testid={`b-${tone}`}>
          x
        </Badge>,
      );
      expect(screen.getByTestId(`b-${tone}`).getAttribute('data-badge-tone')).toBe(tone);
      unmount();
    }
  });

  it('renders the tooltip attribute when title is supplied', () => {
    render(
      <Badge title="The vendor hasn't responded" data-testid="b">
        Late
      </Badge>,
    );
    expect(screen.getByTestId('b').getAttribute('title')).toBe(
      "The vendor hasn't responded",
    );
  });

  it('size scale: sm = 20px, md = 24px', () => {
    const { unmount } = render(
      <Badge size="sm" data-testid="b-sm">
        x
      </Badge>,
    );
    expect((screen.getByTestId('b-sm') as HTMLElement).style.height).toBe('20px');
    unmount();
    render(
      <Badge size="md" data-testid="b-md">
        x
      </Badge>,
    );
    expect((screen.getByTestId('b-md') as HTMLElement).style.height).toBe('24px');
  });
});
