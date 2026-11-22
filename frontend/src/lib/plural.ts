/**
 * "1 properties" is the sentence that makes software look unfinished.
 *
 * ⚠ And it is aimed squarely at this product's launch audience. A manager with
 * one building sees the count of one on the properties page, in the sync toast
 * and in the pager — three times before they have done anything. The rows that
 * were already correct got there by a hand-written ternary each time, which is
 * why the ones that were missed were missed: there was nothing to reach for.
 *
 * ⚠ **English irregulars are handled by a LOOKUP, not by rules.** "-y → -ies"
 * turns "day" into "daies" and "key" into "keies"; "-s → -ses" turns "bus" into
 * "buss". A plural function that is clever is a function that is wrong in a way
 * nobody notices until it is on screen. Anything the small table below does not
 * cover takes an explicit plural from the caller, and `pluralWord` is exported
 * so a test can assert exactly which words the guessing path is trusted with.
 */

/** Words whose plural cannot be derived, spelled out. */
const IRREGULAR: Record<string, string> = {
  person: 'people',
  is: 'are',
  was: 'were',
  has: 'have',
  this: 'these',
  it: 'they',
};

/** Endings where "+s" is wrong. Order matters — longest match wins. */
const SUFFIX_RULES: Array<[RegExp, string]> = [
  // A consonant before -y: property → properties. A VOWEL before it is not
  // this rule (day → days), which is the half people get wrong.
  [/([^aeiou])y$/, '$1ies'],
  [/(s|ss|sh|ch|x|z)$/, '$1es'],
];

/**
 * The plural of one word.
 *
 * Exported so its behaviour can be pinned directly — the failure here is a
 * wrong string on screen, not an exception, so it has to be asserted rather
 * than caught.
 */
export function pluralWord(word: string): string {
  const known = IRREGULAR[word.toLowerCase()];
  if (known) {
    // Preserve a leading capital, which is all the casing this ever sees.
    return word[0] === word[0].toUpperCase()
      ? known[0].toUpperCase() + known.slice(1)
      : known;
  }
  for (const [pattern, replacement] of SUFFIX_RULES) {
    if (pattern.test(word)) return word.replace(pattern, replacement);
  }
  return `${word}s`;
}

/**
 * The right noun for `n`, without the number.
 *
 * `plural(1, 'property')` → `'property'`; `plural(3, 'property')` → `'properties'`.
 * Pass `many` when the derivation would be wrong or when the phrase changes
 * shape: `plural(n, 'item needs', 'items need')`.
 */
export function plural(n: number, one: string, many?: string): string {
  if (n === 1) return one;
  if (many !== undefined) return many;
  // Multi-word phrases pluralise their FIRST word ("work order" → "work
  // orders" is wrong; "order" is the head). Rather than guess which word is
  // the head, refuse: a caller passing a phrase has to say what the plural is.
  if (one.includes(' ')) {
    throw new Error(
      `plural() cannot derive the plural of the phrase "${one}" — pass it explicitly`,
    );
  }
  return pluralWord(one);
}

/**
 * `n` and its noun together, with the number formatted for reading.
 *
 * `count(1, 'property')` → `'1 property'`; `count(1200, 'property')` →
 * `'1,200 properties'`. The thousands separator is not decoration: these
 * numbers land in toasts and pagers beside other figures that already have it.
 */
export function count(n: number, one: string, many?: string): string {
  return `${n.toLocaleString()} ${plural(n, one, many)}`;
}
