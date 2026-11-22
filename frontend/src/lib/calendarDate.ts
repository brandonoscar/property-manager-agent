/**
 * A lease that ends on the 18th was being shown as ending on the 17th.
 *
 * FOUND 2026-09-10 by a code-reading audit of all 73 coverage-map partials
 * (items 65 and 344). Buildium's date-only fields — `lease_from_date`,
 * `lease_to_date`, `due_date`, `move_out_date`, `paid_date`,
 * `transaction_date` — arrive as `2027-03-18` or `2027-03-18T00:00:00Z`.
 * JavaScript parses both as UTC MIDNIGHT, so `new Date(v).toLocaleDateString()`
 * renders the PREVIOUS DAY for every browser west of UTC — which is every user
 * this product has. Measured: 18 `toLocaleDateString` call sites in the app and
 * exactly ONE place anywhere in `frontend/src` that reads UTC components
 * (`InboxPage.tsx`'s `formatDueDate`). So the correct pattern already existed,
 * in one file, unshared.
 *
 * ⚠ **A CALENDAR DATE IS NOT AN INSTANT, and that distinction is the whole
 * module.** "The lease ends on 18 March" is a fact about a calendar; it does
 * not move when the reader flies to Honolulu. "The invoice was paid at
 * 14:32" is an instant and SHOULD render in the reader's local zone. Do not
 * point this function at an instant: `SettingsPage`'s "last updated",
 * `BillingPanel`'s Stripe period end and `CrmPage`'s tour times are all
 * instants and are all correctly rendered with plain local formatting today.
 *
 * ⚠ **It reads the string, not a `Date`.** Taking the `YYYY-MM-DD` prefix off
 * the wire does no timezone arithmetic at all, so there is no offset to get
 * wrong — where going through `Date` and then asking for UTC components is
 * only correct as long as the value really is midnight. A value carrying a
 * real wall-clock time and a non-UTC offset (`...T20:00:00-04:00`) would make
 * the two disagree by a day, and the prefix is the one that matches what
 * Buildium meant.
 */

/** What every caller here already renders for an absent date. */
export const NO_DATE = '—';

const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})/;

export type CalendarDateStyle = 'short' | 'numeric' | 'long';

const OPTIONS: Record<CalendarDateStyle, Intl.DateTimeFormatOptions> = {
  // "18 Mar 2027" — the dense grid style.
  short: { month: 'short', day: 'numeric', year: 'numeric' },
  // "3/18/2027" — the locale's own numeric form, matching bare
  // `toLocaleDateString()` with no options.
  numeric: {},
  // "18 March 2027".
  long: { month: 'long', day: 'numeric', year: 'numeric' },
};

/**
 * Render a Buildium calendar date on the day it actually names.
 *
 * Returns {@link NO_DATE} for null, undefined, empty, or anything that is not
 * an ISO calendar date — deliberately the same placeholder the call sites
 * already used, so this is a drop-in and an unparseable value degrades to a
 * dash rather than to `Invalid Date`.
 */
export function formatCalendarDate(
  value: string | null | undefined,
  style: CalendarDateStyle = 'numeric',
): string {
  if (!value) return NO_DATE;
  const m = CALENDAR_DATE.exec(value);
  if (!m) return NO_DATE;
  const [, y, mo, d] = m;
  // Date.UTC + timeZone:'UTC' keeps Intl's locale-aware month names while
  // guaranteeing the components come back out exactly as they went in.
  const at = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d)));
  if (Number.isNaN(at.getTime())) return NO_DATE;
  return at.toLocaleDateString(undefined, {
    ...OPTIONS[style],
    timeZone: 'UTC',
  });
}

/**
 * The ISO `YYYY-MM-DD` a calendar value names, or `''`.
 *
 * For `<input type="date">`, sorting, and anywhere a stable key is wanted
 * rather than a rendered string.
 */
export function calendarDateKey(value: string | null | undefined): string {
  if (!value) return '';
  const m = CALENDAR_DATE.exec(value);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : '';
}
