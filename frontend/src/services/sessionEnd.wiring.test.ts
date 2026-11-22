/**
 * `sessionEnd.test.ts` proves the primitives are correct. Nothing in it can
 * see whether anything CALLS them — gotcha 62's rule: pair every behavioural
 * test of a new helper with a guard on its call site, because a helper that
 * is right and unwired looks exactly like a feature that works.
 *
 * Two call sites, and one of them has an ORDERING requirement a
 * "does it appear in the file" check cannot express:
 *
 *   · `performSignOut` must mark the intent BEFORE it awaits the network
 *     sign-out. Supabase fires `SIGNED_OUT` from inside `signOut()`, so a
 *     mark placed afterwards is still unset when the handler that reads it
 *     runs — and every deliberate sign-out gets reported to the user as
 *     "your session ended". The call is present, the feature is broken, and
 *     the diff reads fine.
 *   · `App.tsx`'s `SIGNED_OUT` branch must consume the intent, or the
 *     message never fires at all.
 *
 * ⚠ WHY NOT AN AST. Gotcha 40 says assert on the tree, not on substrings, and
 * that is still the right instinct — but there is no parser available here to
 * do it with. `typescript` at this version is the Go-native port: the package
 * exports `version` and nothing else, so `ts.createSourceFile` is `undefined`
 * (measured — the first cut of this file died on `ts.ScriptTarget.Latest`).
 * acorn, @babel/parser and esbuild are all absent, and Vite ships rolldown,
 * which exposes no parse API. Adding a parser dependency days before launch
 * to serve one guard is not a trade worth making silently.
 *
 * So this strips comments and string literals FIRST and scans what is left.
 * That answers gotcha 40's actual complaint — the guard fired on the sentence
 * explaining the guard, and the fix of skipping `#` lines made it blind. Here
 * the stripper is the load-bearing part, so it is TESTED below against both
 * directions: a call it must find, and the same words in a comment and in a
 * string, which it must not.
 */

import { describe, expect, it } from 'vitest';

// ⚠ `?raw` (Vite) rather than `node:fs` — the frontend tsconfig has no node
// types, and a test file tsc rejects still reports GREEN under vitest
// (gotcha 72), so this guard would silently never run. `store.threadRouting.
// test.ts` already established the pattern; the first cut of this file used
// node:fs and turned the typecheck red.
import authSource from './auth.ts?raw';
import appSource from '../App.tsx?raw';

/**
 * Blank out comments and string/template literals, preserving LENGTH so every
 * offset still points at the same place in the original file.
 *
 * Deliberately a small state machine rather than a set of regexes: an
 * apostrophe inside a `// don't` comment ends a naive quote-matching pass in
 * the wrong state and silently blanks the rest of the file — which reads as
 * "the wiring is gone".
 */
export function stripCommentsAndStrings(source: string): string {
  const out = source.split('');
  let i = 0;
  const blank = (from: number, to: number) => {
    for (let k = from; k < to && k < out.length; k++) {
      if (out[k] !== '\n') out[k] = ' ';
    }
  };

  while (i < source.length) {
    const two = source.slice(i, i + 2);
    if (two === '//') {
      const end = source.indexOf('\n', i);
      const stop = end === -1 ? source.length : end;
      blank(i, stop);
      i = stop;
    } else if (two === '/*') {
      const end = source.indexOf('*/', i + 2);
      const stop = end === -1 ? source.length : end + 2;
      blank(i, stop);
      i = stop;
    } else if (source[i] === '"' || source[i] === "'" || source[i] === '`') {
      const quote = source[i];
      let k = i + 1;
      while (k < source.length) {
        if (source[k] === '\\') {
          k += 2;
          continue;
        }
        if (source[k] === quote) break;
        k++;
      }
      blank(i + 1, k);
      i = k + 1;
    } else {
      i++;
    }
  }
  return out.join('');
}


/** Offsets of every `name(` call in real code, in source order. */
function callOffsets(source: string, name: string): number[] {
  const pattern = new RegExp(`\\b${name}\\s*\\(`, 'g');
  const found: number[] = [];
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(source)) !== null) found.push(m.index);
  return found;
}

/**
 * The body of a named function, blanked outside it so offsets still line up.
 *
 * ⚠ Brace counting must start AFTER the parameter list, not at the function
 * keyword. `performSignOut(options?: { everywhere?: boolean })` opens and
 * closes a brace in its own signature, so counting from the start returns at
 * the parameter's `}` — a body of nothing, in which the guard finds zero
 * calls and reports the wiring as missing. Found by the guard-the-guard
 * assertion, which is the only reason it was not read as a real regression.
 */
function bodyOf(source: string, name: string): string {
  const start = source.search(new RegExp(`function\\s+${name}\\s*\\(`));
  if (start === -1) throw new Error(`function ${name} not found — renamed?`);

  // Walk the parameter list to its matching ')' first.
  let i = source.indexOf('(', start);
  let parens = 0;
  for (; i < source.length; i++) {
    if (source[i] === '(') parens++;
    else if (source[i] === ')') {
      parens--;
      if (parens === 0) break;
    }
  }
  const bodyStart = source.indexOf('{', i);
  if (bodyStart === -1) throw new Error(`no body found for ${name}`);

  let depth = 0;
  for (let k = bodyStart; k < source.length; k++) {
    if (source[k] === '{') depth++;
    else if (source[k] === '}') {
      depth--;
      if (depth === 0) return ' '.repeat(bodyStart) + source.slice(bodyStart, k + 1);
    }
  }
  throw new Error(`unbalanced braces reading ${name}`);
}

describe('the stripper works in BOTH directions', () => {
  // Runs first on purpose. A broken stripper reports zero calls everywhere,
  // which reads as "the wiring is gone" rather than "the guard is broken" —
  // these two separate those on the very first failure.
  it('finds a real call', () => {
    expect(callOffsets(stripCommentsAndStrings('f(); mark();'), 'mark')).toHaveLength(1);
  });

  it('does NOT find the same words in a comment or a string', () => {
    const src = [
      '// mark() in a line comment',
      '/* mark() in a block comment */',
      'const s = "mark()";',
      "const t = 'mark()';",
      'const u = `mark()`;',
    ].join('\n');
    expect(callOffsets(stripCommentsAndStrings(src), 'mark')).toHaveLength(0);
  });

  it("survives an apostrophe in a comment — don't end the file in string state", () => {
    const src = "// don't do this\nmark();";
    expect(callOffsets(stripCommentsAndStrings(src), 'mark')).toHaveLength(1);
  });

  it('preserves offsets, so an ordering comparison is meaningful', () => {
    const src = '// xxxx\nmark();';
    expect(stripCommentsAndStrings(src)).toHaveLength(src.length);
  });
});

describe('performSignOut marks the intent, and marks it FIRST', () => {
  const body = bodyOf(stripCommentsAndStrings(authSource), 'performSignOut');

  it('calls markIntentionalSignOut', () => {
    expect(callOffsets(body, 'markIntentionalSignOut')).toHaveLength(1);
  });

  it('marks BEFORE the sign-out call, because SIGNED_OUT fires from inside it', () => {
    const [mark] = callOffsets(body, 'markIntentionalSignOut');
    const signOuts = [
      ...callOffsets(body, 'signOut'),
      ...callOffsets(body, 'signOutEverywhere'),
    ].sort((a, b) => a - b);

    // Guard the guard: with nothing found the comparison below is vacuous.
    expect(signOuts.length).toBeGreaterThan(0);
    expect(mark).toBeLessThan(signOuts[0]);
  });
});

describe('the SIGNED_OUT handler consumes the intent', () => {
  const app = stripCommentsAndStrings(appSource);

  it('App.tsx calls consumeSignOutIntent', () => {
    expect(callOffsets(app, 'consumeSignOutIntent').length).toBeGreaterThan(0);
  });

  it('and remembers where the user was, so signing back in returns them', () => {
    expect(callOffsets(app, 'rememberReturnView').length).toBeGreaterThan(0);
    expect(callOffsets(app, 'takeReturnView').length).toBeGreaterThan(0);
  });
});
