/**
 * Reading and writing an amount of money that a person typed.
 *
 * ⚠ **`<input type="number">` is the wrong control for money, and the way it
 * fails is silent.** Two documented behaviours of that input type, both of
 * which land on a rent figure that goes out to a tenant in an offer email:
 *
 *  - **Text the input cannot parse makes `value` read as the EMPTY STRING**
 *    while the characters stay on screen (the "bad input" state of the number
 *    type's value-sanitization algorithm). So somebody pasting `$1,250` sees
 *    their amount and the program sees nothing — and the caller's
 *    `rent ? Number(rent) : null` then sends `null`. The user is looking at
 *    the number they meant while the request carries an absence.
 *  - **The scroll wheel over a focused number input steps the value.** The
 *    renewal editor is a scrollable panel, so scrolling the panel with the
 *    rent field focused changes the rent. Nothing is typed, nothing is
 *    flagged, and the amount that leaves is not the amount that was entered.
 *
 * So the input is `type="text"` with `inputMode="decimal"` (the numeric
 * keypad on a phone, none of the number type's behaviour) and the parsing is
 * done here, where it can be tested against real strings.
 *
 * ⚠ **A comma group that is not three digits is REFUSED, not guessed.**
 * `"1,25"` is 1.25 to a European reader and 125 to a naive comma-stripper —
 * and 125 is a hundredfold error on somebody's rent, entered by the software
 * rather than by them. This product is US-only throughout (ZIPs, NPAs, TCPA
 * windows), so a comma here is a thousands separator or it is a mistake;
 * either way, declining beats inventing an amount (gotcha 55).
 */

/** `1250.5` → `"$1,250.50"`. Null/absent renders as an em dash. */
export function formatMoney(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * What the user typed, as a number — or `null` when it is not an amount.
 *
 * `null` covers three different things on purpose: empty, and therefore
 * nothing to send; junk; and an ambiguous comma group. A caller that needs to
 * tell "blank" from "wrong" checks the raw string for emptiness first — the
 * distinction matters for whether to show an error, and not for what to send.
 */
export function parseMoney(raw: string): number | null {
  const s = raw.trim().replace(/^\$/, '').replace(/\s/g, '');
  if (!s) return null;

  const sign = s.startsWith('-') ? -1 : 1;
  const body = s.replace(/^[+-]/, '');

  // Grouped: every comma must separate exactly three digits.
  if (body.includes(',')) {
    if (!/^\d{1,3}(,\d{3})+(\.\d+)?$/.test(body)) return null;
    return sign * Number(body.replace(/,/g, ''));
  }

  if (!/^\d+(\.\d+)?$|^\.\d+$/.test(body)) return null;
  return sign * Number(body);
}
