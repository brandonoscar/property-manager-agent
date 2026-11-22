/**
 * "Did you mean gmail.com?"
 *
 * A mistyped domain at signup is the most expensive typo in the product: the
 * confirmation email goes to nobody, the person waits, decides it is broken,
 * and leaves. They cannot fix it themselves either — the address is already
 * the account's identity by the time anyone notices. One line under the field
 * at the moment of typing is the only cheap place to catch it.
 *
 * Edit distance against a short list of domains people actually use, and
 * NOTHING else. Deliberately not a validity check: `@mycompany.io` is a real
 * address and a property-management customer is far more likely to be on
 * their own domain than on Gmail. So the rule is asymmetric — suggest only
 * when the typed domain is a NEAR MISS of a known one, never when it is
 * simply unfamiliar. A false suggestion on somebody's real work address is
 * worse than no suggestion, because it tells them their own email is wrong.
 */

/** Domains common enough that a near-miss is far more likely a typo than a real host. */
const KNOWN_DOMAINS = [
  'gmail.com',
  'yahoo.com',
  'hotmail.com',
  'outlook.com',
  'live.com',
  'aol.com',
  'icloud.com',
  'me.com',
  'msn.com',
  'comcast.net',
  'verizon.net',
  'att.net',
  'sbcglobal.net',
  'protonmail.com',
  'proton.me',
  'yahoo.co.uk',
  'googlemail.com',
];

/** Top-level parts worth correcting on their own — `.con` for `.com` is the classic. */
const KNOWN_TLDS = ['com', 'net', 'org', 'edu', 'gov', 'io', 'co', 'us', 'co.uk'];

/**
 * Damerau-Levenshtein — Levenshtein PLUS adjacent transposition as a single
 * edit.
 *
 * ⚠ The transposition rule is not a refinement, it decides the answer. Under
 * plain Levenshtein `cmo` is 2 edits from `com` (two substitutions) and 1 edit
 * from `co` (one deletion) — so `blueridge.cmo` gets "corrected" to
 * `blueridge.co`, which is a REAL top-level domain, and the hint confidently
 * points at an address that will also bounce. Swapping two adjacent keys is
 * the single most common way a domain gets mistyped, so scoring it as one
 * edit is what makes `cmo → com` win. Caught by the test, not by review.
 *
 * Needs the full matrix rather than two rolling rows, because a transposition
 * looks back two rows.
 */
function distance(a: string, b: string): number {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;

  const rows: number[][] = [];
  for (let i = 0; i <= a.length; i++) {
    rows.push(new Array<number>(b.length + 1).fill(0));
    rows[i][0] = i;
  }
  for (let j = 0; j <= b.length; j++) rows[0][j] = j;

  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let best = Math.min(
        rows[i - 1][j] + 1, // deletion
        rows[i][j - 1] + 1, // insertion
        rows[i - 1][j - 1] + cost, // substitution
      );
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        best = Math.min(best, rows[i - 2][j - 2] + 1); // transposition
      }
      rows[i][j] = best;
    }
  }
  return rows[a.length][b.length];
}

/**
 * The closest candidate within its own threshold, or null.
 *
 * ⚠ CLOSEST, not first-within-threshold. Returning the first acceptable match
 * makes the answer depend on list order, so adding a domain to the list can
 * silently change what an unrelated typo corrects to. Ties break toward the
 * earlier entry, and the lists are written in rough frequency order for
 * exactly that reason.
 */
function closest(
  input: string,
  candidates: readonly string[],
  limit: (candidate: string) => number,
): string | null {
  let best: string | null = null;
  let bestDistance = Infinity;
  for (const candidate of candidates) {
    const d = distance(input, candidate);
    if (d === 0 || d > limit(candidate)) continue;
    if (d < bestDistance) {
      bestDistance = d;
      best = candidate;
    }
  }
  return best;
}

/**
 * How far off a domain may be and still read as a typo rather than a
 * different address. Scaled by length so a 1-char slip in `me.com` is not
 * treated the same as a 1-char slip in `sbcglobal.net` — but capped at 2,
 * because past that "near miss" stops being true and the suggestion starts
 * inventing an address the person never meant.
 */
function threshold(domain: string): number {
  return domain.length <= 7 ? 1 : 2;
}

/**
 * The corrected address, or null when there is nothing worth saying.
 *
 * Returns the WHOLE address rather than just the domain so the caller can
 * offer it as one click — asking somebody to retype after being told they
 * mistyped is the wrong end of the interaction.
 */
export function suggestEmail(raw: string): string | null {
  const email = raw.trim();
  const at = email.lastIndexOf('@');
  // No domain yet, or an empty local part — nothing to compare and nothing
  // to suggest. Firing while somebody is mid-word is how a hint becomes noise.
  if (at < 1 || at === email.length - 1) return null;

  const local = email.slice(0, at);
  const domain = email.slice(at + 1).toLowerCase();
  if (!domain.includes('.')) return null;

  // Already a domain we know: by construction not a typo. This is the guard
  // that stops `gmail.com` being "corrected" to `googlemail.com`.
  if (KNOWN_DOMAINS.includes(domain)) return null;

  const domainMatch = closest(domain, KNOWN_DOMAINS, threshold);
  if (domainMatch) return `${local}@${domainMatch}`;

  // The domain is unfamiliar — almost always somebody's real work address, so
  // the NAME is left alone. Only the ending is checked, and only against a
  // near miss: `occupella.con` → `.com`, while `occupella.dev` is untouched
  // because `dev` is not one edit from anything in the list.
  const firstDot = domain.indexOf('.');
  const name = domain.slice(0, firstDot);
  const tld = domain.slice(firstDot + 1);
  if (KNOWN_TLDS.includes(tld)) return null;
  const tldMatch = closest(tld, KNOWN_TLDS, () => 1);
  if (tldMatch) return `${local}@${name}.${tldMatch}`;

  return null;
}
