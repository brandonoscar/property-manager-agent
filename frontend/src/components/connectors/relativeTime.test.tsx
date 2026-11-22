/**
 * "3h ago" is the right default and the wrong only answer.
 *
 * Somebody correlating a webhook against a Buildium record, or working out
 * whether a sync ran before or after a change they made, needs the actual
 * time. Relative text alone means opening a database to get it.
 *
 * ⚠ `timeAgo` had exactly one caller and rendered no `title`, so the next
 * caller would have inherited the omission. The pair lives in a component for
 * that reason, and the last test below is the one that keeps it that way.
 */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';

import detail from './BuildiumWebhookDetail.tsx?raw';
import { RelativeTime, timeAgo } from './ui';

describe('RelativeTime', () => {
  it('shows the relative form', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3_600_000).toISOString();
    const { container } = render(<RelativeTime iso={twoHoursAgo} />);
    expect(container.textContent).toBe('2h ago');
  });

  it('carries the exact time on hover', () => {
    const iso = new Date(Date.now() - 2 * 3_600_000).toISOString();
    const { container } = render(<RelativeTime iso={iso} />);
    const title = container.querySelector('span')?.getAttribute('title');
    expect(title).toBe(new Date(iso).toLocaleString());
  });

  it('omits the title rather than showing "Invalid Date"', () => {
    // An unparseable stamp is a real state — a row written by an older
    // schema, a null that reached the client. A tooltip reading "Invalid
    // Date" is worse than no tooltip: it looks like the data is corrupt when
    // only the hover is.
    const { container } = render(<RelativeTime iso="not a date" />);
    expect(container.querySelector('span')?.hasAttribute('title')).toBe(false);
  });

  it('is what the webhook timeline actually renders', () => {
    // A behavioural test of the component cannot see a call site that went on
    // using the bare helper (gotcha 62). Both the import and the usage are
    // checked, because either alone can go stale.
    expect(detail).toContain('RelativeTime');
    expect(detail).not.toMatch(/\{timeAgo\(/);
  });
});

describe('timeAgo', () => {
  it('is still exported for callers that only want the words', () => {
    expect(typeof timeAgo).toBe('function');
  });
});
