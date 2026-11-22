/**
 * The path ↔ view mapping, and the two places it meets another vocabulary.
 *
 * ⚠ The corpus half is the load-bearing one. The store's key for Operations is
 * `properties` and for Leasing is `crm`, so this map is a translation between
 * two naming schemes that drifted apart for historical reasons — exactly the
 * boundary gotcha 28 says needs a test that reads BOTH sides. A view added to
 * the store with no segment here silently becomes unaddressable: no error, no
 * failing test, the URL just stops changing when you open it.
 */
import { describe, expect, it } from 'vitest';

import { pathForRoute, routeFromPath, SETTINGS_SECTIONS } from './routing';

const APP = import.meta.glob('../App.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const SETTINGS_PAGE = import.meta.glob('../components/SettingsPage.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const source = (files: Record<string, string>): string => Object.values(files)[0] ?? '';

describe('routeFromPath', () => {
  it('reads the root as Chat', () => {
    expect(routeFromPath('/')).toEqual({ view: 'chat' });
    expect(routeFromPath('')).toEqual({ view: 'chat' });
  });

  it('uses the product\'s words, not the store\'s', () => {
    // `/operations` and `/leasing`, never `/properties` or `/crm` — a customer
    // sent a link should not be reading an internal history they know nothing
    // about, and `/crm` is the wrong word for a page the whole UI calls
    // Leasing.
    expect(routeFromPath('/operations')).toEqual({ view: 'properties' });
    expect(routeFromPath('/leasing')).toEqual({ view: 'crm' });
    expect(routeFromPath('/properties')).toBeNull();
    expect(routeFromPath('/crm')).toBeNull();
  });

  it('round-trips every view', () => {
    for (const path of ['/', '/inbox', '/operations', '/leasing', '/search', '/dev-runs']) {
      const route = routeFromPath(path);
      expect(route, path).not.toBeNull();
      expect(pathForRoute(route!)).toBe(path);
    }
  });

  it('carries a settings section, and round-trips it', () => {
    for (const section of SETTINGS_SECTIONS) {
      const path = `/settings/${section}`;
      expect(routeFromPath(path)).toEqual({ view: 'settings', settingsSection: section });
      expect(pathForRoute(routeFromPath(path)!)).toBe(path);
    }
  });

  it('lands a junk section on Account rather than on nothing', () => {
    // A section id reaches SettingsPage's own state; a fabricated one renders
    // the rail with an empty pane. A stale or mistyped link should show
    // something.
    expect(routeFromPath('/settings/whatever')).toEqual({
      view: 'settings',
      settingsSection: 'account',
    });
    expect(routeFromPath('/settings')).toEqual({ view: 'settings', settingsSection: 'account' });
  });

  it('returns null for an unknown path — which is not the same as Chat', () => {
    // Null means "leave the app where it is". Returning `chat` here would
    // relocate somebody who opened a stale link, and would fight the Supabase
    // invite / recovery flow, where the URL is carrying auth state.
    for (const path of ['/nonsense', '/legal/terms.html', '/inbox/extra/deep']) {
      expect(routeFromPath(path), path).toBeNull();
    }
  });
});

describe('the two vocabularies it sits between', () => {
  it('is reading real sources', () => {
    expect(source(APP).length).toBeGreaterThan(1000);
    expect(source(SETTINGS_PAGE).length).toBeGreaterThan(1000);
  });

  it('has a path for every view App actually renders', () => {
    // App switches on the store's view string. A view added there with no
    // segment here is unaddressable and nothing else would say so.
    const rendered = [...source(APP).matchAll(/view === '([a-z-]+)'/g)].map((m) => m[1]);
    expect(rendered.length).toBeGreaterThan(4);
    const unroutable = rendered.filter(
      (v) => !['chat', 'inbox', 'properties', 'crm', 'search', 'settings', 'dev-runs'].includes(v),
    );
    expect(unroutable, 'App renders a view with no URL — it cannot be linked to').toEqual([]);
  });

  it('has an entry for every section SettingsPage ships', () => {
    // The other direction of the same drift: a new Settings section with no
    // entry here is reachable by clicking and not by link, and the link that
    // does exist silently lands on Account.
    const shipped = [...source(SETTINGS_PAGE).matchAll(/\{ id: "([a-z]+)", label:/g)].map(
      (m) => m[1],
    );
    expect(shipped.length).toBeGreaterThan(3);
    expect([...shipped].sort()).toEqual([...SETTINGS_SECTIONS].sort());
  });
});
