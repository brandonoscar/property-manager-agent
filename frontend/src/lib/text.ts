/**
 * Display-text helpers for user-facing titles.
 *
 * Buildium webhook payloads (and manager-typed task titles) arrive in
 * whatever casing someone typed — "water heater pilot out", "noise
 * complaint" — and the rail renders them as headlines. titleCase gives
 * every title headline casing WITHOUT destroying intentional casing:
 * it only ever UPcases a first letter, never lowercases anything, so
 * "AC not cooling", "HVAC", "McAllister" and unit codes survive intact.
 */

// Small words stay lowercase mid-title (standard headline style) — but
// only if they were already lowercase; we never downcase user input.
const SMALL_WORDS = new Set([
  'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'is',
  'nor', 'of', 'on', 'or', 'per', 'the', 'to', 'via', 'vs', 'with',
]);

export function titleCase(input: string): string {
  const words = input.split(/(\s+)/); // keep whitespace tokens
  let firstWordSeen = false;
  return words
    .map((token) => {
      if (/^\s+$/.test(token) || token.length === 0) return token;
      const isFirst = !firstWordSeen;
      firstWordSeen = true;
      // Mid-title small words keep their lowercase (headline style).
      if (!isFirst && SMALL_WORDS.has(token)) return token;
      // Upcase the first ALPHABETICAL character; leave the rest alone
      // ("iPhone" is not our problem to solve — but "unit 4b" → "Unit 4b"
      // and "AC" stays "AC").
      const i = token.search(/\p{L}/u);
      if (i === -1) return token;
      return token.slice(0, i) + token[i].toUpperCase() + token.slice(i + 1);
    })
    .join('');
}
