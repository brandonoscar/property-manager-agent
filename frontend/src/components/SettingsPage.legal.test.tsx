/**
 * The policy pages are reachable from inside the product, and every link
 * points at a file that exists.
 *
 * ⚠ Two separate defects sat here. The pages had been written and NOTHING in
 * the app linked to them — a terms page nobody can reach from inside the
 * product is a file on a server, not a published policy. And a second copy of
 * Terms and Privacy was rendered by the BACKEND behind four unset env vars,
 * so the product had two sources for one contract and the live one 503'd.
 * The static set won; the backend copy is gone.
 *
 * ⚠ The existence check is the load-bearing half. A link to
 * `/legal/refunds.html` typed from memory looks exactly like a link to a page
 * that ships, and only 404s in front of a customer.
 */

import { describe, expect, it } from 'vitest';

// Vite's ?raw reads the file's text without needing node types here — the
// same mechanism TrialBanner.test.tsx already uses for App.tsx.
import settingsSource from './SettingsPage.tsx?raw';
import terms from '../../public/legal/terms.html?raw';
import privacy from '../../public/legal/privacy.html?raw';
import billing from '../../public/legal/billing.html?raw';
import sms from '../../public/legal/sms.html?raw';
import accessibility from '../../public/legal/accessibility.html?raw';
import subprocessors from '../../public/legal/subprocessors.html?raw';

const PAGES: Record<string, string> = {
  '/legal/terms.html': terms,
  '/legal/privacy.html': privacy,
  '/legal/billing.html': billing,
  '/legal/sms.html': sms,
  '/legal/accessibility.html': accessibility,
  '/legal/subprocessors.html': subprocessors,
};

/** Every `/legal/...` href in a source file. */
function legalHrefs(source: string): string[] {
  return [...source.matchAll(/["'](\/legal\/[^"']+)["']/g)].map((m) => m[1]);
}

describe('Settings links to the policy pages', () => {
  const linked = legalHrefs(settingsSource);

  it('links to every policy page that ships, and to nothing that does not', () => {
    expect(new Set(linked)).toEqual(new Set(Object.keys(PAGES)));
  });

  it('renders them, as a FOOTER rather than a setting', () => {
    // ⚠ RE-POINTED, not relaxed (2026-09-08). They were a `SettingRow` with a
    // paragraph beside them explaining what terms and a privacy policy are —
    // six standalone links carrying the same visual weight as two-factor
    // authentication, which is what made the pane read as legal-heavy. Nobody
    // opens Settings to read a subprocessor list. They must be REACHABLE; they
    // must not be prominent.
    expect(settingsSource).toContain('<LegalFooter />');
    expect(settingsSource).not.toContain('<LegalRow />');
  });
});

describe('every link resolves to a page that ships', () => {
  it.each(Object.keys(PAGES))('%s exists and is a real page', (href) => {
    const html = PAGES[href];
    expect(html).toContain('<title>');
    expect(html.length).toBeGreaterThan(1_000);
  });

  it('the pages cross-link only to each other', () => {
    // A footer link to a page that was renamed or never written 404s in
    // front of somebody reading a contract.
    for (const [href, html] of Object.entries(PAGES)) {
      for (const target of legalHrefs(html)) {
        if (target.endsWith('.css')) continue;
        expect(Object.keys(PAGES), `${href} links to ${target}`).toContain(
          target,
        );
      }
    }
  });
});

describe('the billing page states the terms the code enforces', () => {
  it('names the trial length the signup trigger actually grants', () => {
    // ⚠ 14 days is what migration 0088's trigger writes. A policy page that
    // states a different number is not a weaker page — it misstates the
    // agreement, which is worse than saying nothing.
    expect(billing).toContain('14-day free trial');
  });

  it('names the grace period the billing gate actually applies', () => {
    // PAYMENT_GRACE_DAYS = 21 in billing/gate.py.
    expect(billing).toContain('21 days');
  });

  it('promises self-service cancellation, which the Stripe portal provides', () => {
    // POST /billing/portal returns a Stripe portal URL. Promising this
    // without that endpoint would be an attestation the software does not
    // keep.
    expect(billing.toLowerCase()).toContain('cancel yourself');
  });
});
