/**
 * What a search engine can read, and what the SPA rewrite hides from it.
 *
 * app.occupella.com serves ONE document for almost every path (vercel.json),
 * so
 * the surface a crawler sees is tiny and every part of it matters. Three
 * separate things went wrong here at once and none of them failed anything:
 *
 *  - There was no robots.txt and no sitemap.xml, so a request for either fell
 *    through the rewrite and answered HTML with a 200.
 *  - The only favicon was an SVG, and Google rendered the domain with the
 *    generic globe while every other result on the page showed its mark.
 *  - index.html had no canonical and no structured data, so /inbox and
 *    /properties competed with / as duplicates and nothing anywhere said which
 *    entity "Occupella" refers to.
 *
 * ⚠ THE LOAD-BEARING TEST IN THIS FILE IS THE CORPUS ONE. The rewrite's
 * exclusion list is hand-written, and a hand-written list is how the favicon
 * got missed in the first place: a new file dropped into public/ is served as
 * HTML, silently, and reads exactly like a file that is simply absent. So the
 * list is checked against what public/ ACTUALLY holds rather than against
 * itself (a control whose failure mode is "silently permits more" has to read
 * the corpus).
 *
 * ⚠ NOT VERIFIED AGAINST VERCEL OR GOOGLE. The domain is egress-blocked
 * from the container these tests were written in, so what is pinned here is
 * the half we control: that the pattern WE wrote classifies paths the way we
 * intend. Whether Vercel consults the filesystem before rewrites — in which
 * case the exclusions are belt-and-braces rather than the fix — is unprobed,
 * and the exclusions are correct under either reading.
 */

import { describe, expect, it } from 'vitest';

import html from '../index.html?raw';
import vercelRaw from '../vercel.json?raw';
import robots from '../public/robots.txt?raw';
import sitemap from '../public/sitemap.xml?raw';

/** Every file sitting at the root of public/, by name. Keys only — the
 *  importers are never invoked, so the binaries are not read. */
const PUBLIC_FILES = Object.keys(import.meta.glob('../public/*')).map(
  (p) => p.split('/').pop() as string,
);

/** The catch-all that turns any unmatched path into the app shell. */
function spaRewrite(): RegExp {
  const config = JSON.parse(vercelRaw) as {
    rewrites: { source: string; destination: string }[];
  };
  const spa = config.rewrites.find((r) => r.destination === '/index.html');
  expect(spa, 'vercel.json no longer has an /index.html catch-all').toBeTruthy();
  return new RegExp(`^${spa!.source}$`);
}

describe('static files a crawler asks for reach the filesystem', () => {
  it('found some files to judge', () => {
    // A glob that silently resolves to nothing would make every assertion
    // below vacuously true, and the whole file would report green while
    // testing the empty set.
    expect(PUBLIC_FILES.length).toBeGreaterThan(5);
    expect(PUBLIC_FILES).toContain('favicon.ico');
    expect(PUBLIC_FILES).toContain('robots.txt');
    expect(PUBLIC_FILES).toContain('sitemap.xml');
  });

  it('excludes EVERY file in public/, not just the ones somebody remembered', () => {
    const rewrite = spaRewrite();
    const swallowed = PUBLIC_FILES.filter((name) => rewrite.test(`/${name}`));
    expect(
      swallowed,
      'these files would be served as index.html instead of themselves — ' +
        'add them to the exclusion list in vercel.json',
    ).toEqual([]);
  });

  it('still sends real application routes to the shell', () => {
    // The other direction, and the one a careless widening breaks: a pattern
    // that excludes everything passes the test above perfectly while serving
    // a 404 for every page of the product.
    const rewrite = spaRewrite();
    for (const path of ['/', '/inbox', '/properties', '/settings', '/properties/4610']) {
      expect(rewrite.test(path), `${path} must still render the app`).toBe(true);
    }
  });

  it('leaves the backend and the pre-built assets alone', () => {
    const rewrite = spaRewrite();
    for (const path of ['/api/v1/health', '/assets/index-abc123.js', '/legal/terms.html']) {
      expect(rewrite.test(path), `${path} must not become index.html`).toBe(false);
    }
  });
});

describe('robots.txt', () => {
  it('lets the site be crawled', () => {
    expect(robots).toMatch(/^User-agent:\s*\*/m);
    expect(robots).toMatch(/^Allow:\s*\//m);
    // The failure that matters is the inverse — a blanket disallow ships a
    // site nobody can find and nothing reports it.
    expect(robots).not.toMatch(/^Disallow:\s*\/\s*$/m);
  });

  it('points at a sitemap that exists', () => {
    const declared = robots.match(/^Sitemap:\s*(\S+)$/m)?.[1];
    expect(declared, 'robots.txt names no sitemap').toBeTruthy();
    // Both sides (the file it names, and the file we ship): a sitemap
    // directive pointing at a 404 is worse than none, because a crawler
    // retries it instead of concluding there is nothing to fetch.
    const named = declared!.split('/').pop();
    expect(PUBLIC_FILES).toContain(named);
  });
});

describe('sitemap.xml', () => {
  const locs = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);

  it('lists the root and the legal pages', () => {
    expect(locs).toContain('https://app.occupella.com/');
    expect(locs.length).toBeGreaterThan(1);
  });

  it('every legal url it names is a file this repo actually ships', () => {
    // A sitemap is a promise about what exists. Listing a page that 404s is
    // the one thing that makes a crawler trust the file less, so the names
    // are checked against the directory rather than against intent.
    const legalFiles = Object.keys(import.meta.glob('../public/legal/*.html')).map(
      (p) => p.split('/').pop() as string,
    );
    expect(legalFiles.length).toBeGreaterThan(0);

    const legalLocs = locs.filter((l) => l.includes('/legal/'));
    expect(legalLocs.length).toBeGreaterThan(0);
    for (const loc of legalLocs) {
      expect(legalFiles, `${loc} is in the sitemap and not in public/legal/`).toContain(
        loc.split('/').pop(),
      );
    }
  });

  it('claims no freshness it cannot measure', () => {
    // Nothing in the build observes when a policy document changed, so a
    // lastmod here would be a date that is wrong the day after it is written —
    // the same fabricated-timestamp failure as a sync stamp nobody computed.
    // Absent is honest; add it when something generates it.
    //
    // ⚠ COMMENTS ARE STRIPPED FIRST, and the first cut of this test did not
    // do that: the sitemap's own header explains the rule by naming the tag,
    // so a whole-file scan flagged the CORRECT file. A guard that cannot tell
    // a defect from its own explanation is the wrong guard — the same trap
    // this repo has hit in Python, in SQL and in TSX.
    const markup = sitemap.replace(/<!--[\s\S]*?-->/g, '');
    expect(markup).toContain('<loc>');
    expect(markup).not.toContain('<lastmod>');
  });
});

describe('index.html tells a crawler what this is', () => {
  it('has a title that is not just the contested brand token', () => {
    const title = html.match(/<title>([^<]+)<\/title>/)?.[1] ?? '';
    expect(title).toContain('Occupella');
    // "Occupella" alone is a word that already belongs to somebody else in
    // search. The title has to carry the category as well, or the only query
    // it can win is the one we lose on brand history.
    expect(title.trim()).not.toBe('Occupella');
    expect(title.toLowerCase()).toContain('buildium');
  });

  it('declares one canonical url for a shell served at every path', () => {
    expect(html).toMatch(/<link rel="canonical" href="https:\/\/app\.occupella\.com\/" \/>/);
  });

  it('offers a raster icon as well as the svg', () => {
    // Google's favicon crawler asks for /favicon.ico by convention, and the
    // SVG-only setup rendered as a generic globe in results.
    expect(html).toMatch(/<link rel="icon" href="\/favicon\.ico"/);
    // The SVG stays and must stay FIRST-preferred for browsers: `sizes="any"`
    // is what makes a modern browser take the vector over the raster.
    expect(html).toMatch(/href="\/favicon\.svg"[^>]*sizes="any"/);
  });
});

describe('the structured data block', () => {
  const block = html.match(
    /<script type="application\/ld\+json">([\s\S]*?)<\/script>/,
  )?.[1];

  it('exists and is valid JSON', () => {
    // Malformed JSON-LD is not an error anywhere — Google skips the block and
    // the page carries on looking perfectly healthy. Present in the source,
    // absent in effect.
    expect(block, 'index.html ships no JSON-LD').toBeTruthy();
    expect(() => JSON.parse(block!)).not.toThrow();
  });

  it('names the same legal entity the visible page does', () => {
    // A structured claim that disagrees with the rendered page is the one
    // thing this format is actually policed for, and the footer below #root
    // is where a crawler reads the entity without running JS.
    const graph = JSON.parse(block!)['@graph'] as Record<string, string>[];
    const org = graph.find((n) => n['@type'] === 'Organization');
    expect(org).toBeTruthy();
    expect(html).toContain(org!.legalName);
  });

  it('points at images this repo actually ships', () => {
    const graph = JSON.parse(block!)['@graph'] as Record<string, string>[];
    const org = graph.find((n) => n['@type'] === 'Organization')!;
    for (const url of [org.logo, org.image]) {
      expect(PUBLIC_FILES, `${url} is declared and not in public/`).toContain(
        url.split('/').pop(),
      );
    }
  });

  it('gives the site a name, which is what search shows above the result', () => {
    const graph = JSON.parse(block!)['@graph'] as Record<string, string>[];
    const site = graph.find((n) => n['@type'] === 'WebSite');
    expect(site?.name).toBe('Occupella');
  });
});
