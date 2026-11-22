/**
 * A sticky header inside `overflow-hidden` is a sticky header that never
 * sticks — and both halves look correct in the diff.
 *
 * `overflow: hidden` makes an element a SCROLL CONTAINER. So the wrapper around
 * the properties table became the nearest scrollport for its own
 * `<thead className="sticky top-0">`, and that wrapper does not scroll — the
 * page does. The header could not move, the class said it should, and nothing
 * anywhere failed. It shipped that way.
 *
 * ⚠ This is a SOURCE guard, and it can only see the class names. It cannot see
 * a header actually pinned to the top of a scrolled table — that needs a real
 * browser, which the frontend suite does not have. What it CAN do is stop the
 * one-word regression that caused this, which is the failure that actually
 * happened. Saying which of those it covers matters more than the coverage.
 */

import { describe, expect, it } from 'vitest';

import properties from './PropertiesPage.tsx?raw';

/** Every element that opens within `window` lines above a `sticky` thead. */
function ancestorsAbove(source: string, needle: string, window = 12): string {
  const lines = source.split('\n');
  const at = lines.findIndex((l) => l.includes(needle));
  expect(at, `"${needle}" is not in the file — this guard is measuring nothing`).toBeGreaterThan(
    -1,
  );
  return lines.slice(Math.max(0, at - window), at).join('\n');
}

describe('the properties table header', () => {
  it('is still declared sticky', () => {
    // If somebody removes the sticky header the test below passes vacuously,
    // which would make this file report green on a feature that is gone.
    expect(properties).toContain('sticky top-0');
  });

  it('is not wrapped in a scroll container that cannot scroll', () => {
    const wrapper = ancestorsAbove(properties, 'sticky top-0');
    expect(
      wrapper,
      'an ancestor of the sticky <thead> uses overflow-hidden, which makes it ' +
        'the sticky scrollport and pins the header to a box that never moves. ' +
        'Use overflow-clip: it clips without becoming a scroll container.',
    ).not.toContain('overflow-hidden');
  });

  it('still clips the table to the rounded border', () => {
    // The reason `overflow-hidden` was there in the first place. Dropping the
    // clip entirely would fix the header and leave square corners poking
    // through a rounded frame.
    const wrapper = ancestorsAbove(properties, 'sticky top-0');
    expect(wrapper).toMatch(/overflow-clip/);
  });
});
