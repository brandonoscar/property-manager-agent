/**
 * Every legal link in the app points at a page that EXISTS.
 *
 * Three spellings were in the codebase at once and only one of them worked:
 *
 *   - `/legal/terms.html`                  — a real file in `frontend/public`
 *   - `https://<this app's host>/terms`    — the LOGIN SCREEN
 *   - `https://<the marketing host>/terms` — a second copy on another deployment
 *
 * The middle one is the defect. This app is an SPA that renders `index.html`
 * for every path `vercel.json` does not exclude, and the exclusion list is
 * `assets/ api/ browser/ legal/` — so `/terms` is not on it. Somebody clicking
 * "Terms" got a login form. It was founder-reported on the signed-out screen
 * on 2026-08-20 and fixed THERE; the SMS consent checkbox kept the broken
 * spelling for three weeks, which is gotcha 25's shape — a rule fixed on one
 * surface never reaching the other — landing on the two links the A2P unified
 * content policy specifically requires (rejection code 30564).
 *
 * ⚠ THE HOST THAT IS DANGEROUS CHANGED ON 2026-09-15 AND THE GUARD HAD TO
 * INVERT WITH IT. Before the domain swap this app was on the apex, so
 * `occupella.com/terms` was the login screen. After it, the apex is the
 * MARKETING SITE, where `/terms` is a real React route that resolves — and
 * `app.occupella.com/terms` is the login screen. A guard left pointing at the
 * apex would forbid the one spelling that works and permit the one that does
 * not: wrong in both directions at once, from a rename nobody has to touch
 * this file for. The regex names `app.` for that reason, not by oversight.
 *
 * ⚠ A CORPUS guard, not a component test. The failure mode is a new link
 * somewhere nobody is looking, and a per-component test only covers the
 * components somebody thought to write one for.
 */
import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** The static pages that actually ship in `frontend/public/legal`. */
const PAGES = import.meta.glob('../../public/legal/*.html', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/**
 * Every string literal that is a URL or path to a legal document.
 *
 * ⚠ Deliberately NOT `href="…"`. That was the first cut and it found exactly
 * ONE of the three files: `LoginForm` assigns `const TERMS_URL = '…'` and
 * `SettingsPage` builds `{ href: "…", label: … }` in an array, so neither has
 * the literal next to the attribute. Gotcha 40's lesson — a scan anchored on
 * the syntax around a value goes blind the moment the value moves into a
 * variable. Anchoring on the VALUE cannot.
 *
 * Only literals that start with `/` or `http` count, which keeps ordinary
 * prose containing the word "accessibility" out of the results.
 *
 * ⚠ And only literals in a VALUE POSITION — right after `=`, `:` or `(`. The
 * comments above this line quote both broken URLs in order to explain them,
 * and a scan of every quoted string flags the sentence describing the guard
 * (gotchas 40 and 59, arriving in TypeScript). Requiring an assignment is a
 * structural distinction rather than a convention: a comment says "pointed at
 * X"; code says `href=X` or `URL = X`.
 */
const LEGAL_LITERAL =
  /[=:(]\s*\{?\s*["'`](\/[^"'`\s]*|https?:\/\/[^"'`\s]*)["'`]/g;

function legalHrefs(source: string): string[] {
  return [...source.matchAll(LEGAL_LITERAL)]
    .map((m) => m[1])
    .filter((v) => /terms|privacy|legal|subprocessor|accessibility/i.test(v));
}

describe('legal links', () => {
  it('never point at an app path the SPA will answer with the login screen', () => {
    const offenders: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes('.test.')) continue;
      for (const href of legalHrefs(source)) {
        // The app's own origin, but NOT under /legal/ — the one shape that
        // silently resolves to index.html. `www.` is deliberately absent: it
        // aliases the APEX, which is the marketing site and resolves fine.
        if (/^https?:\/\/app\.occupella\.com\/(?!legal\/)/i.test(href)) {
          offenders.push(`${file}: ${href}`);
        }
      }
    }
    expect(offenders, 'this href renders the login screen, not the document').toEqual([]);
  });

  it('name a file that is actually shipped', () => {
    const shipped = new Set(
      Object.keys(PAGES).map((p) => `/legal/${p.split('/').pop()}`),
    );
    expect(shipped.size, 'no legal pages were found — bad glob, not a clean repo').toBeGreaterThan(3);

    const missing: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes('.test.')) continue;
      for (const href of legalHrefs(source)) {
        if (!href.startsWith('/legal/')) continue; // external links are not ours to check
        if (!shipped.has(href.split('#')[0].split('?')[0])) missing.push(`${file}: ${href}`);
      }
    }
    expect(missing, 'a legal link points at a page that does not exist').toEqual([]);
  });

  it('scans a real corpus, so a bad glob cannot report green on nothing', () => {
    // ⚠ Both assertions above pass trivially against zero files, which is
    // exactly the failure mode a link guard has: it silently permits more.
    const withLegal = Object.entries(SOURCES).filter(
      ([f, s]) => !f.includes('.test.') && legalHrefs(s).length > 0,
    );
    expect(withLegal.length).toBeGreaterThanOrEqual(3);
  });
});
