/**
 * The chat app's Content-Security-Policy (frontend/vercel.json) names the
 * scripts the built app runs, and nothing else (the founder's auth hardening,
 * 2026-11-11).
 *
 * The inventory, measured on the build and on the deployed bundle that day:
 *   - one module script from this origin (/assets/index-*.js; Vite inlines none);
 *   - posthog-js's lazy scripts (web vitals, remote config, surveys, the
 *     toolbar), injected from PostHog's assets host. Web vitals is on in the
 *     project, so production loads it;
 *   - Sentry Replay's compression worker, built from a Blob (a blob: URL). With a
 *     script-src and no worker-src, script-src would govern workers and refuse it;
 *   - the JSON-LD block in index.html, a data block that never runs, so no
 *     policy applies to it.
 * Nothing loads Stripe.js (Checkout is a redirect), and Supabase's realtime
 * worker is off. A missed script is a white screen with no error anyone sees,
 * so each piece of the inventory is pinned here against what produces it.
 */

import { PostHog } from 'posthog-js';
import { describe, expect, it } from 'vitest';

import indexHtml from '../index.html?raw';
import vercelRaw from '../vercel.json?raw';
import { POSTHOG_DEFAULT_HOST, posthogHost } from './services/analytics';

type HeaderRule = { source: string; headers: { key: string; value: string }[] };

const rules = (JSON.parse(vercelRaw) as { headers: HeaderRule[] }).headers;

function cspRule(): { rule: HeaderRule; csp: string } {
  const found = rules.flatMap((rule) =>
    rule.headers
      .filter((h) => h.key.toLowerCase() === 'content-security-policy')
      .map((h) => ({ rule, csp: h.value })),
  );
  expect(found, 'vercel.json should send exactly one CSP').toHaveLength(1);
  return found[0];
}

function directives(csp: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const part of csp.split(';')) {
    const [name, ...values] = part.trim().split(/\s+/);
    if (name) out.set(name, values);
  }
  return out;
}

/** The origin posthog-js itself loads `path` from, for the app's PostHog host. */
function posthogAssetsOrigin(apiHost: string, path: string): string {
  const client = new PostHog();
  client.config = { ...client.config, api_host: apiHost };
  return new URL(client.requestRouter.endpointFor('assets', path)).origin;
}

describe("the chat app's script-src", () => {
  it('allows this origin and PostHog assets host, and nothing else', () => {
    expect(directives(cspRule().csp).get('script-src')).toEqual([
      "'self'",
      'https://us-assets.i.posthog.com',
    ]);
  });

  it('never allows inline script, eval or any host', () => {
    const scriptSrc = directives(cspRule().csp).get('script-src') ?? [];
    for (const loose of ["'unsafe-inline'", "'unsafe-eval'", '*', 'https:', 'data:', 'blob:']) {
      expect(scriptSrc, `script-src allows ${loose}`).not.toContain(loose);
    }
  });

  it('names the host posthog-js itself loads its scripts from', () => {
    // Asked of posthog-js, not restated: its router maps an ingestion host to
    // the assets host its lazy scripts and remote config come from.
    const scriptSrc = directives(cspRule().csp).get('script-src') ?? [];
    for (const path of ['/static/web-vitals.js', '/static/surveys.js', '/array/phc_test/config.js']) {
      expect(scriptSrc).toContain(posthogAssetsOrigin(posthogHost, path));
    }
  });

  it('is pinned to the default PostHog host, the one production uses', () => {
    // The deployed bundle (11/11) carries https://us.i.posthog.com. A different
    // VITE_PUBLIC_POSTHOG_HOST needs the CSP changed with it (analytics.ts).
    expect(posthogHost).toBe(POSTHOG_DEFAULT_HOST);
  });

  it('would refuse PostHog scripts if the host moved to another region', () => {
    // The control for the test above: the check can tell two hosts apart.
    const scriptSrc = directives(cspRule().csp).get('script-src') ?? [];
    expect(scriptSrc).not.toContain(posthogAssetsOrigin('https://eu.i.posthog.com', '/static/x.js'));
  });
});

describe("the chat app's worker-src", () => {
  it("lets Sentry Replay's compression worker start from a blob, and nothing else", () => {
    expect(directives(cspRule().csp).get('worker-src')).toEqual(['blob:']);
  });
});

describe('the rest of the policy', () => {
  it('keeps the directives it had', () => {
    const d = directives(cspRule().csp);
    expect(d.get('frame-ancestors')).toEqual(["'none'"]);
    expect(d.get('base-uri')).toEqual(["'self'"]);
    expect(d.get('object-src')).toEqual(["'none'"]);
    expect(d.get('form-action')).toEqual(["'self'"]);
  });

  it('covers every page of the app', () => {
    const source = new RegExp(`^${cspRule().rule.source}$`);
    expect(source.test('/settings')).toBe(true);
    expect(source.test('/')).toBe(true);
  });
});

describe('index.html under that policy', () => {
  const doc = new DOMParser().parseFromString(indexHtml, 'text/html');
  const scripts = [...doc.querySelectorAll('script')];

  it('runs no inline script: every script without a src is a data block', () => {
    const inline = scripts.filter((s) => !s.getAttribute('src'));
    expect(inline.length).toBeGreaterThan(0); // the JSON-LD block, so the filter is live
    for (const s of inline) {
      expect(s.getAttribute('type'), s.outerHTML.slice(0, 80)).toBe('application/ld+json');
    }
  });

  it('loads its scripts from this origin', () => {
    const external = scripts.filter((s) => s.getAttribute('src'));
    expect(external.length).toBeGreaterThan(0);
    for (const s of external) {
      expect(s.getAttribute('src')).toMatch(/^\/(?!\/)/);
    }
  });

  it('carries no inline event handler', () => {
    const handlers = [...doc.querySelectorAll('*')].flatMap((el) =>
      [...el.attributes].filter((a) => a.name.toLowerCase().startsWith('on')).map((a) => a.name),
    );
    expect(handlers).toEqual([]);
  });
});
