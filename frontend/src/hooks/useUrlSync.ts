/**
 * Keeps the address bar and the app's view in step, both directions.
 *
 * ⚠ The push guard is a COMPARISON, not a flag, and that is the whole design.
 * A `popstate` sets the store; the store change re-runs the effect; the effect
 * computes the path for the new view — which is already the current location,
 * so it pushes nothing. A boolean "we're handling a popstate" flag would need
 * to be cleared at exactly the right moment and is the classic way a router
 * ends up either duplicating history entries or swallowing a real navigation.
 *
 * ⚠ An unrecognised path LEAVES THE APP WHERE IT IS rather than sending it to
 * Chat. Those are different: a signed-in person opening a stale or mistyped
 * link should keep what they were doing, and during the Supabase invite /
 * password-recovery flow the URL is carrying auth state that has nothing to do
 * with navigation. Relocating them there would be the app losing their place
 * for a reason they cannot see.
 */

import { useEffect, useRef } from 'react';

import { useStore } from '../stores/store';
import {
  pathForRoute,
  routeFromPath,
  type SettingsSection,
  type View,
} from '../lib/routing';

export function useUrlSync(): void {
  const view = useStore((s) => s.view);
  const settingsSection = useStore((s) => s.settingsSection);
  const mounted = useRef(false);

  // The address bar on arrival wins over the store's default. Runs once:
  // after this the store is the source of truth and the effect below follows
  // it. `replaceState` normalises the path (`/settings` → `/settings/account`)
  // without adding an entry — landing on a link should not put a Back step
  // between the person and wherever they came from.
  useEffect(() => {
    const route = routeFromPath(window.location.pathname);
    if (!route) return;
    const store = useStore.getState();
    if (route.view === 'settings') {
      store.openSettings(route.settingsSection ?? 'account');
    } else if (route.view !== store.view) {
      store.setView(route.view);
    }
    const canonical = pathForRoute(route);
    if (canonical !== window.location.pathname) {
      window.history.replaceState(null, '', canonical + window.location.search);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Back / Forward. The store is set from whatever the browser restored; the
  // push effect then sees a matching path and stays quiet.
  useEffect(() => {
    const onPop = () => {
      const route = routeFromPath(window.location.pathname);
      if (!route) return;
      const store = useStore.getState();
      const moved =
        route.view === 'settings'
          ? store.openSettings(route.settingsSection ?? 'account')
          : store.setView(route.view);
      // ⚠ Put the address bar BACK when the person chose to stay. The browser
      // has already changed the URL by the time popstate fires, and the push
      // effect below only runs when the view changes — which it just did not.
      // Without this the bar says /chat while the app shows the form they
      // asked to keep, and the next Back lands somewhere neither of them
      // expects.
      if (!moved) {
        window.history.pushState(
          null,
          '',
          pathForRoute({
            view: store.view as View,
            settingsSection: store.settingsSection as SettingsSection,
          }) + window.location.search,
        );
      }
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  // Every in-app navigation, from any of the ~20 `setView` call sites, the
  // ⌘K palette and the `g <key>` chords alike — they all end at the store, so
  // this is the one place that has to know about the URL.
  useEffect(() => {
    // ⚠ Skip the MOUNT run, and this is a measured bug rather than a
    // precaution. Effects fire in declaration order, so the read above has
    // already called `setView('settings')` by the time this one runs — but
    // `view` here is closed over the MOUNT render, which still says `chat`.
    // So a cold load of /settings/billing pushed `/` over the address the
    // person had just opened, and every deep link landed on Chat. Measured
    // in a real browser: three deep links, three redirects to `/`.
    //
    // Zustand's `set` is synchronous and React's re-render is not — the same
    // gap that silently ate typed chat messages once. Here the URL is
    // authoritative on arrival and the read effect has already normalised it,
    // so the mount run has nothing to contribute: the store change it caused
    // schedules this effect again, with the real values.
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const want = pathForRoute({
      view: view as View,
      settingsSection: settingsSection as SettingsSection,
    });
    if (want !== window.location.pathname) {
      window.history.pushState(null, '', want + window.location.search);
    }
  }, [view, settingsSection]);
}
