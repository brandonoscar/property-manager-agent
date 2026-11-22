/**
 * A lease ending on the 18th was shown as ending on the 17th.
 *
 * ⚠ **THESE TESTS ONLY MEAN SOMETHING WEST OF UTC.** In UTC — which is what CI
 * ran in until `vite.config.ts` pinned `TZ` — the buggy expression and the
 * correct one agree exactly, so the whole file would pass against the defect it
 * exists to catch. The first assertion below therefore checks the HOST ZONE
 * before anything else: a suite that silently ran in UTC would be reporting a
 * green it did not earn (the "green everywhere except the machine where it
 * matters" shape).
 */

import { describe, expect, it } from 'vitest';
import { NO_DATE, calendarDateKey, formatCalendarDate } from './calendarDate';

describe('the harness itself', () => {
  it('runs west of UTC, or nothing below proves anything', () => {
    // March: Los Angeles is UTC-7. A positive offset here means we are west.
    const offsetMinutes = new Date('2027-03-18T12:00:00Z').getTimezoneOffset();
    expect(offsetMinutes).toBeGreaterThan(0);
  });

  it('demonstrates the defect this module exists to fix', () => {
    // The expression that shipped on six live surfaces.
    const buggy = new Date('2027-03-18').toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
    expect(buggy).toContain('17');
    expect(formatCalendarDate('2027-03-18', 'short')).toContain('18');
  });
});

describe('formatCalendarDate', () => {
  it('renders a date-only value on the day it names', () => {
    expect(formatCalendarDate('2027-03-18', 'short')).toContain('18');
    expect(formatCalendarDate('2027-03-18', 'short')).toContain('2027');
  });

  it('renders a UTC-midnight timestamp on the same day', () => {
    // Buildium sends both shapes for the same field.
    expect(formatCalendarDate('2027-03-18T00:00:00Z', 'short')).toBe(
      formatCalendarDate('2027-03-18', 'short'),
    );
  });

  it('reads the wire string rather than doing timezone arithmetic', () => {
    // ⚠ A value carrying a real wall-clock time and a non-UTC offset would make
    // "parse then read UTC components" disagree with the calendar date Buildium
    // meant. Taking the prefix is what makes that impossible.
    expect(formatCalendarDate('2027-03-18T20:00:00-04:00', 'short')).toContain(
      '18',
    );
  });

  it('crosses a month boundary correctly', () => {
    // The 1st is where an off-by-one day is also off-by-one MONTH, which is
    // what makes "the last day of next month" land in the wrong lease period.
    expect(formatCalendarDate('2027-04-01', 'long')).toContain('April');
    expect(formatCalendarDate('2027-04-01', 'long')).toContain('1');
  });

  it('crosses a year boundary correctly', () => {
    expect(formatCalendarDate('2027-01-01', 'short')).toContain('2027');
  });

  it('survives DST — the day does not shift in either direction', () => {
    // 2027-03-14 is the US spring-forward. A fix that worked at a fixed offset
    // rather than a real zone would slip here.
    expect(formatCalendarDate('2027-03-14', 'short')).toContain('14');
    expect(formatCalendarDate('2027-11-07', 'short')).toContain('7');
  });

  it('returns the shared placeholder for an absent date', () => {
    for (const v of [null, undefined, '']) {
      expect(formatCalendarDate(v)).toBe(NO_DATE);
    }
  });

  it('degrades to the placeholder rather than to "Invalid Date"', () => {
    expect(formatCalendarDate('not a date')).toBe(NO_DATE);
    expect(formatCalendarDate('18/03/2027')).toBe(NO_DATE);
  });
});

describe('calendarDateKey', () => {
  it('returns the ISO day for either wire shape', () => {
    expect(calendarDateKey('2027-03-18')).toBe('2027-03-18');
    expect(calendarDateKey('2027-03-18T00:00:00Z')).toBe('2027-03-18');
  });

  it('returns empty rather than a wrong key', () => {
    expect(calendarDateKey(null)).toBe('');
    expect(calendarDateKey('nope')).toBe('');
  });
});
