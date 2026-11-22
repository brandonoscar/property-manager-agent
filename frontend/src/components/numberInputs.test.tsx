/**
 * Every `<input type="number">` in the product, counted rather than banned.
 *
 * That input type has two behaviours nobody wants on a field that decides an
 * amount or a term (see `lib/money.ts`): it holds the EMPTY STRING when its
 * text will not parse, while the characters stay on screen; and the scroll
 * wheel steps its value when it has focus. Both change what gets submitted
 * with nothing on screen saying so.
 *
 * ⚠ **Pinning the COUNT, not forbidding the name** — the technique gotcha 68
 * used for `skip_signature_check`. A blanket ban is wrong: a plain integer
 * count with the wheel neutralised is a reasonable control, and a rule that
 * forbids something reasonable gets an exemption bolted on and then ignored.
 * A rule that says "there were N of these, each deliberate, and now there are
 * N+1" makes the next one a decision somebody takes on purpose.
 *
 * If this fails: either use `MoneyInput` (any amount of money), or add
 * `onWheel={(e) => e.currentTarget.blur()}` and raise the number below.
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** Opening tags carrying `type="number"`, with their file and line. */
function numberInputs(): Array<{ where: string; tag: string }> {
  const found: Array<{ where: string; tag: string }> = [];
  for (const [path, source] of Object.entries(SOURCES)) {
    if (path.includes('.test.')) continue;
    // Comments quote the forbidden form in order to explain it, so strip them
    // first — a scan that fires on the sentence documenting the rule is the
    // trap gotchas 40/49/59 all record.
    const code = source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/\/\/[^\n]*/g, ' ');
    const re = /<input\b[^>]*?type="number"[^>]*?>/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(code)) !== null) {
      found.push({
        where: `${path}:${code.slice(0, m.index).split('\n').length}`,
        tag: m[0],
      });
    }
  }
  return found;
}

describe('number inputs', () => {
  it('is reading real sources', () => {
    // A bad glob collecting nothing would make every assertion below vacuous.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(30);
  });

  it('has exactly the number inputs that were reviewed', () => {
    const found = numberInputs();
    expect(
      found.map((f) => f.where),
      'a new <input type="number"> arrived. Money goes through MoneyInput; ' +
        'anything else needs the onWheel blur guard and a bump here.',
    ).toHaveLength(1);
  });

  it('neutralises the scroll wheel on every one of them', () => {
    for (const { where, tag } of numberInputs()) {
      expect(tag, `${where} steps its value when the page scrolls under it`).toMatch(/onWheel/);
    }
  });

  it('routes money through MoneyInput', () => {
    // The renewal offer is the one place a person types an amount, and the
    // amount goes to a tenant in an email.
    // Found by suffix rather than by an exact glob key: the key's shape
    // depends on where this file sits, and a lookup that silently misses
    // would make the assertions below vacuous.
    const key = Object.keys(SOURCES).find((k) => k.endsWith('properties/RenewalsSection.tsx'));
    expect(key, 'RenewalsSection.tsx not found in the glob').toBeTruthy();
    const renewals = SOURCES[key as string];
    expect(renewals).toMatch(/<MoneyInput\b/);
    // Number() on user text is what turned "$1,250" into NaN -> null.
    expect(renewals).not.toMatch(/Number\(editor\.proposedRent\)/);
  });
});
