/**
 * A seventh surface must not quietly reopen the off-by-one-day bug.
 *
 * Six live surfaces rendered Buildium calendar dates through
 * `new Date(v).toLocaleDateString()`, which shows the PREVIOUS DAY for every
 * browser west of UTC (coverage items 65/344). The per-function tests in
 * `calendarDate.test.ts` prove the shared formatter is right; they are
 * structurally blind to the next component that does not use it.
 *
 * ⚠ **Anchored on the FIELD NAME, not on the formatter.** Banning
 * `toLocaleDateString` outright would be wrong and would get this guard
 * switched off: an INSTANT — Stripe's period end, a memory's "last updated", a
 * CRM tour time — is correctly rendered in the reader's local zone, and there
 * are legitimate call sites doing exactly that. What is never correct is
 * pairing a local-time formatter with a Buildium date-only field. So the scan
 * looks for the two together on one line.
 *
 * ⚠ `import.meta.glob(..., '?raw')` rather than `node:fs` — the frontend
 * tsconfig has no node types, so `node:fs` compiles under vitest (which does
 * NOT typecheck) and turns `tsc --noEmit` red. `sessionEnd.wiring.test.ts`
 * records the same trap; I walked into it anyway and its comment is what got
 * me out.
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('/src/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/**
 * Buildium's date-only fields, as they appear on the wire.
 *
 * These carry no time and no zone: `2027-03-18`. Every other date-bearing
 * field in the product is an instant and is out of scope by design.
 */
const CALENDAR_FIELDS = [
  'lease_from_date',
  'lease_to_date',
  'due_date',
  'move_out_date',
  'paid_date',
  'transaction_date',
];

/** Formatters that render in the READER's zone. Correct for an instant. */
const LOCAL_TIME_RENDER = /toLocaleDateString|toLocaleString|toDateString/;

const IS_TEST = /\.(test|spec)\.(ts|tsx)$/;

describe('no surface renders a Buildium calendar date in the reader’s zone', () => {
  const files = Object.entries(SOURCES).filter(([p]) => !IS_TEST.test(p));

  it('finds source files at all', () => {
    // A bad glob collecting zero files would report a green that means nothing
    // — the scan has to prove it looked.
    expect(files.length).toBeGreaterThan(50);
  });

  it('has no line pairing a calendar field with a local-time formatter', () => {
    const offenders: string[] = [];
    for (const [path, src] of files) {
      src.split('\n').forEach((line, i) => {
        if (!LOCAL_TIME_RENDER.test(line)) return;
        // An explicit UTC zone on the same line is the correct, deliberate
        // form. ⚠ Quote-agnostic: this repo writes single quotes but nothing
        // enforces it (there is no prettier config), so a double-quoted file
        // would silently lose its exemption and the guard would cry wolf.
        if (/timeZone:\s*['"]UTC['"]/.test(line)) return;
        const field = CALENDAR_FIELDS.find((c) => line.includes(c));
        if (field)
          offenders.push(
            `${path}:${i + 1} — ${field} — ${line.trim().slice(0, 100)}`,
          );
      });
    }
    expect(
      offenders,
      'a Buildium calendar date is being rendered in the reader’s local zone, so ' +
        'it shows the previous day for every user west of UTC. Use ' +
        'formatCalendarDate from lib/calendarDate:\n' +
        offenders.join('\n'),
    ).toEqual([]);
  });

  it('still catches the shape it is written for', () => {
    // Guard the guard: the detector must fire on the exact line that shipped.
    const shipped =
      "{c.lease_to_date ? new Date(c.lease_to_date).toLocaleDateString() : '—'}";
    expect(LOCAL_TIME_RENDER.test(shipped)).toBe(true);
    expect(CALENDAR_FIELDS.some((c) => shipped.includes(c))).toBe(true);
  });

  it('spares a legitimate instant render', () => {
    // Both directions (gotcha 38). A guard that flagged this would be switched
    // off, and then it protects nothing.
    const instant =
      'const formatUpdated = (iso: string) => new Date(iso).toLocaleString();';
    expect(CALENDAR_FIELDS.some((c) => instant.includes(c))).toBe(false);
  });
});
