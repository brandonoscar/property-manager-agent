/**
 * The URL is app state — the one bit of it a person can send to somebody.
 *
 * Until this existed the app was addressable at exactly one place. Every
 * surface lived behind a Zustand string, so:
 *
 *   - Browser Back left the product entirely. Nine views deep in the Inbox,
 *     one Back and you are on whatever page you were reading before.
 *   - "Look at the Leasing pipeline" could not be a link. It had to be a
 *     sentence: open Occupella, click Leasing.
 *   - A refresh dropped you on Chat, wherever you had been.
 *   - Three open tabs were three copies of the same URL.
 *
 * ⚠ The paths read like the PRODUCT, not like the store. The store's key for
 * Operations is `properties` (it was the Properties page once) and for Leasing
 * is `crm` — leaking either into a URL would give customers a link naming an
 * internal history they have no idea about, and `/crm` is the wrong word for
 * a page the whole UI calls Leasing. That mapping is a place two vocabularies
 * meet, so a test reads BOTH sides of it (gotcha 28).
 *
 * ⚠ Path-based, deliberately, NOT hash-based. `services/authFlow.ts`
 * snapshots `window.location.hash` at import to catch Supabase invite and
 * password-recovery links, and Supabase itself writes its tokens into that
 * hash. A hash router would be sharing a channel with the auth handshake, and
 * losing that race means an invited user never gets to set a password. The
 * hosting side already supports paths: `vercel.json`'s SPA rewrite has always
 * sent everything except `assets/`, `api/`, `browser/` and `legal/` to
 * index.html, so `/inbox` serves the app today with no infra change.
 */

export type View =
  | 'chat'
  | 'inbox'
  | 'properties'
  | 'crm'
  | 'search'
  | 'settings'
  | 'dev-runs';

/** Store key → the first path segment. Chat is the root. */
const VIEW_TO_SEGMENT: Record<View, string> = {
  chat: '',
  inbox: 'inbox',
  properties: 'operations',
  crm: 'leasing',
  search: 'search',
  settings: 'settings',
  'dev-runs': 'dev-runs',
};

const SEGMENT_TO_VIEW: Record<string, View> = Object.fromEntries(
  Object.entries(VIEW_TO_SEGMENT).map(([view, seg]) => [seg, view as View]),
) as Record<string, View>;

/**
 * The Settings sections that may appear in a URL.
 *
 * ⚠ An allowlist, not "whatever segment is there". A section id reaches
 * `SettingsPage`'s own state, and a fabricated one renders an empty rail with
 * no content — so a mistyped or stale link lands on Account rather than on
 * nothing. Kept in sync with `SECTIONS` in SettingsPage by a test that reads
 * both, because the two lists drifting is silent: the link simply stops
 * working and the person who sent it has no way to know.
 */
export const SETTINGS_SECTIONS = [
  'account',
  'security',
  'team',
  'ai',
  'connectors',
  'billing',
  'activity',
] as const;

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export interface Route {
  view: View;
  settingsSection?: SettingsSection;
}

/**
 * What a pathname means. Returns null for anything unrecognised, which the
 * caller reads as "leave the app where it is" — NOT as "go to Chat".
 *
 * ⚠ Those are different, and the difference is a real bug. A signed-in user
 * opening a stale or mistyped link should keep whatever they were on, not be
 * silently relocated; and during the invite / password-recovery flow the URL
 * carries auth state that has nothing to do with navigation.
 */
export function routeFromPath(pathname: string): Route | null {
  const parts = pathname.split('/').filter(Boolean);
  if (parts.length === 0) return { view: 'chat' };

  const view = SEGMENT_TO_VIEW[parts[0]];
  if (!view) return null;

  if (view === 'settings') {
    if (parts.length > 2) return null;
    const section = parts[1];
    if (section && (SETTINGS_SECTIONS as readonly string[]).includes(section)) {
      return { view, settingsSection: section as SettingsSection };
    }
    // A settings URL with a junk section is still a settings URL.
    return { view, settingsSection: 'account' };
  }
  // ⚠ Extra segments are REFUSED, not ignored — my own test caught this
  // resolving `/inbox/extra/deep` to Inbox. Being lenient makes
  // `/inbox/<anything>` a working link today, so the day a real
  // `/inbox/:id` route exists, every one of those already-shared URLs
  // points somewhere plausible and wrong. Declining costs a typo'd link
  // and buys the ability to add depth later (gotcha 55).
  if (parts.length > 1) return null;
  return { view };
}

/** The canonical path for a piece of app state. Always starts with `/`. */
export function pathForRoute(route: Route): string {
  const segment = VIEW_TO_SEGMENT[route.view];
  if (!segment) return '/';
  if (route.view === 'settings' && route.settingsSection) {
    return `/${segment}/${route.settingsSection}`;
  }
  return `/${segment}`;
}
