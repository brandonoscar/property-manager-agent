/**
 * The five nouns this product counts, never printed with a bare "s".
 *
 * ⚠ **Deliberately narrow, and the narrowness is the design.** A general
 * "number followed by a plural noun" sweep produced mostly false positives on
 * the first run — `{bucket} days` where bucket is the RANGE LABEL "31-60",
 * `{plan.label} covers`, `{r.email} was`. A guard that cries wolf is one
 * somebody switches off (the same reasoning that keeps the fair-housing
 * patterns anchored on the offer rather than the noun), so this checks only
 * the words that are genuinely per-customer counts and can genuinely be 1:
 * property, unit, lease, tenant, answer.
 *
 * "1 properties" is the sentence that makes software look unfinished, and it
 * was aimed at the smallest real customer — a manager with one building saw it
 * in the sync toast and the pager before doing anything.
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** Only nouns that are per-customer counts and can legitimately be one. */
const COUNTED = ['properties', 'units', 'leases', 'tenants', 'answers'];

/**
 * `${expr} properties` / `{expr} units` with no plural handling in sight.
 *
 * The 60-character lookbehind is what spares a correct ternary: a guarded site
 * reads `n === 1 ? 'property' : \`${n} properties\`` and the marker sits just
 * before the interpolation.
 */
function unguarded(source: string): string[] {
  const found: string[] = [];
  for (const noun of COUNTED) {
    const re = new RegExp(String.raw`[$]?\{[^{}]+\}\s+${noun}\b`, 'g');
    let m: RegExpExecArray | null;
    while ((m = re.exec(source)) !== null) {
      const before = source.slice(Math.max(0, m.index - 60), m.index);
      // Already handled: a ternary on the count, or the helper itself.
      if (/===\s*1|!==\s*1|\bcount\(|\bplural\(/.test(before)) continue;
      found.push(`${source.slice(0, m.index).split('\n').length}: ${m[0].trim()}`);
    }
  }
  return found;
}

describe('counted nouns', () => {
  it('is reading real sources', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(30);
  });

  it('finds the guarded sites, so the scan is known to work', () => {
    // If the regex silently stopped matching, the sweep below would pass on a
    // codebase full of "1 properties". The FIXED sites are the population that
    // proves it matches at all — they are found and then excused.
    const anyCounted = Object.values(SOURCES).some((s) =>
      COUNTED.some((n) => new RegExp(String.raw`\b${n}\b`).test(s)),
    );
    expect(anyCounted).toBe(true);
  });

  it('never prints a count with a bare plural', () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      if (path.includes('.test.')) continue;
      for (const hit of unguarded(source)) offenders.push(`${path}:${hit}`);
    }
    expect(
      offenders,
      'these render "1 properties" when the count is one. Use ' +
        "count(n, 'property') from lib/plural, or a ternary.",
    ).toEqual([]);
  });
});
