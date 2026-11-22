/**
 * PR #113 — shared Button system contracts.
 *
 * The Button component is the stake-in-the-ground that ends every
 * detail-pane button styling itself. These tests pin the contract:
 *
 *   variant → background / color / border roles
 *   size    → text buttons 36/36/40px (sm/md/lg), the site's 36–40 range;
 *             icon squares keep 28/32/36
 *   shape   → every button is the site's 6px (--r-sm); both shapes match
 *   disabled → gray fill, gray text
 *   cursor  → pointer when enabled, not-allowed when disabled
 *   icon    → renders icon-only when variant="icon"
 *
 * The runtime correctness (variant looks right on the dark theme,
 * hover transitions actually fire) is verified visually on the
 * Vercel preview; these tests are the structural floor.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { Button } from './Button';


// ── Size mapping ─────────────────────────────────────────────────


describe('Button — size scale', () => {
  it('sm → 36px tall', () => {
    render(
      <Button size="sm" data-testid="b">
        Refresh
      </Button>,
    );
    expect(screen.getByTestId('b').style.height).toBe('36px');
  });

  it('md → 36px tall (the default action-button size)', () => {
    render(
      <Button size="md" data-testid="b">
        Execute
      </Button>,
    );
    expect(screen.getByTestId('b').style.height).toBe('36px');
  });

  it('lg → 40px tall (composer Send only)', () => {
    render(
      <Button size="lg" data-testid="b">
        Send
      </Button>,
    );
    expect(screen.getByTestId('b').style.height).toBe('40px');
  });

  it('defaults to md when size is omitted', () => {
    render(<Button size="sm" data-testid="small">x</Button>);
    render(<Button data-testid="b">Default</Button>);
    // md and sm share a height; the default is md's 14px text, not sm's 13.
    expect(screen.getByTestId('b').style.height).toBe('36px');
    expect(screen.getByTestId('b').style.fontSize).toBe('14px');
    expect(screen.getByTestId('small').style.fontSize).toBe('13px');
  });
});


// ── Shape mapping ────────────────────────────────────────────────


describe('Button — shape scale', () => {
  it('defaults to the site\'s 6px button radius, not a pill', () => {
    render(<Button data-testid="b">x</Button>);
    expect(screen.getByTestId('b').style.borderRadius).toBe('var(--r-sm)');
  });

  it('rectangle is the same 6px', () => {
    render(
      <Button shape="rectangle" data-testid="b">
        Send
      </Button>,
    );
    expect(screen.getByTestId('b').style.borderRadius).toBe('var(--r-sm)');
  });
});


// ── Cursor rule ──────────────────────────────────────────────────


describe('Button — cursor rule', () => {
  it('enabled → cursor: pointer', () => {
    render(<Button data-testid="b">x</Button>);
    expect(screen.getByTestId('b').style.cursor).toBe('pointer');
  });

  it('disabled → cursor: not-allowed', () => {
    render(
      <Button disabled data-testid="b">
        x
      </Button>,
    );
    expect(screen.getByTestId('b').style.cursor).toBe('not-allowed');
  });
});


// ── Variant rendering ────────────────────────────────────────────


describe('Button — variants render distinct backgrounds', () => {
  it('primary uses iris fill', () => {
    render(
      <Button variant="primary" data-testid="b">
        Send to Clifton
      </Button>,
    );
    // CSS-var-based; pin the source rather than the resolved color.
    expect(screen.getByTestId('b').style.background).toBe('var(--iris)');
  });

  it('secondary is white with a border', () => {
    render(
      <Button variant="secondary" data-testid="b">
        Skip
      </Button>,
    );
    expect(screen.getByTestId('b').style.background).toBe('var(--canvas)');
    expect(screen.getByTestId('b').style.border).toBe('1px solid var(--line-strong)');
  });

  it('ghost is the tertiary text link: no fill, iris text', () => {
    render(
      <Button variant="ghost" data-testid="b">
        Refresh
      </Button>,
    );
    expect(screen.getByTestId('b').style.background).toBe('transparent');
    expect(screen.getByTestId('b').style.color).toBe('var(--iris)');
  });

  it('disabled primary is gray, not a faded blue', () => {
    render(
      <Button variant="primary" disabled data-testid="b">
        Send
      </Button>,
    );
    expect(screen.getByTestId('b').style.background).toBe('var(--disabled-bg)');
    expect(screen.getByTestId('b').style.color).toBe('var(--disabled-ink)');
  });

  it('icon is transparent with no label rendered', () => {
    render(
      <Button
        variant="icon"
        icon={<span data-testid="ico">i</span>}
        data-testid="b"
      >
        text-that-should-not-render
      </Button>,
    );
    const btn = screen.getByTestId('b');
    expect(btn.style.background).toBe('transparent');
    // The label children must NOT render in icon variant.
    expect(btn.textContent).toBe('i');
    expect(screen.getByTestId('ico')).toBeInTheDocument();
  });
});


// ── Icon variant becomes square ──────────────────────────────────


describe('Button — icon variant geometry', () => {
  it('icon variant is square (width === height)', () => {
    render(
      <Button
        variant="icon"
        size="sm"
        icon={<span>i</span>}
        data-testid="b"
      />,
    );
    const btn = screen.getByTestId('b');
    expect(btn.style.height).toBe('28px');
    expect(btn.style.width).toBe('28px');
  });

  it('non-icon variants have no explicit width (let content size them)', () => {
    render(
      <Button variant="primary" size="md" data-testid="b">
        Execute
      </Button>,
    );
    expect(screen.getByTestId('b').style.width).toBe('');
  });
});


// ── Click + disabled wiring ──────────────────────────────────────


describe('Button — interaction', () => {
  it('fires onClick when enabled', () => {
    const onClick = vi.fn();
    render(
      <Button onClick={onClick} data-testid="b">
        x
      </Button>,
    );
    fireEvent.click(screen.getByTestId('b'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('does not fire onClick when disabled', () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick} data-testid="b">
        x
      </Button>,
    );
    fireEvent.click(screen.getByTestId('b'));
    expect(onClick).not.toHaveBeenCalled();
  });
});



// ── Icon buttons stay square under pressure ───────────────────────

/**
 * ⚠ Measured, not assumed. A real-browser pass at 768px found the Inbox's
 * Resummarize and Re-plan buttons rendering **15x28** — the primitive asked
 * for 28 wide, a tight flex row took 13 of it back, and a control the design
 * calls square landed under the 24x24 WCAG 2.2 AA minimum.
 *
 * jsdom has no layout, so it cannot reproduce that squeeze — what it CAN see
 * is the inline style that prevents it. These assertions are the reason the
 * squeeze can no longer happen, not a re-measurement of it; the measurement
 * itself lives in `frontend/scripts/measure-viewports.mjs`, which needs a
 * real browser.
 */
describe('Button — icon variant is a target, not just a glyph', () => {
  it('pins a minimum width and refuses to shrink', () => {
    render(<Button variant="icon" size="sm" data-testid="b" icon={<span>x</span>} />);
    const el = screen.getByTestId('b');
    expect(el.style.width).toBe('28px');
    expect(el.style.minWidth).toBe('28px');
    expect(el.style.flexShrink).toBe('0');
  });

  it('clears the AA minimum at every size', () => {
    for (const size of ['sm', 'md', 'lg'] as const) {
      render(<Button variant="icon" size={size} data-testid={`b-${size}`} icon={<span>x</span>} />);
      const el = screen.getByTestId(`b-${size}`);
      expect(parseInt(el.style.minWidth, 10), `${size} width`).toBeGreaterThanOrEqual(24);
      expect(parseInt(el.style.height, 10), `${size} height`).toBeGreaterThanOrEqual(24);
    }
  });

  it('leaves TEXT buttons free to shrink — they have a label to give up', () => {
    // The opposite direction matters: pinning min-width on a labelled button
    // would push it past the edge of a narrow row instead of wrapping.
    render(<Button data-testid="t">Refresh</Button>);
    const el = screen.getByTestId('t');
    expect(el.style.minWidth).toBe('');
    expect(el.style.flexShrink).toBe('');
  });
});
