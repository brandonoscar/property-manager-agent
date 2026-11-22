/**
 * The browser tab says what you are looking at.
 *
 * Every tab said "Occupella" — so three open tabs (the usual state for
 * somebody comparing a property against the inbox against a report) were
 * indistinguishable, and browser history was a wall of one repeated word.
 * The tab strip is the only place a person can tell them apart, and it was
 * carrying no information at all.
 *
 * Name first, product second: a tab truncates from the RIGHT, so
 * "Properties · Occupella" survives a narrow tab as "Properties…" while
 * "Occupella · Properties" survives as "Occupella…" — which is the half
 * every tab already shares and therefore the half worth losing.
 */

import { useEffect } from 'react';

export const APP_NAME = 'Occupella';

/**
 * Keyed on the store's `view` union. TypeScript's Record makes adding a view
 * without naming it a compile error rather than a tab that silently reads
 * "Occupella" again — which is the exact failure this hook exists to end, and
 * it would come back invisibly.
 */
export const VIEW_TITLES: Record<
  'chat' | 'inbox' | 'properties' | 'crm' | 'search' | 'settings' | 'dev-runs',
  string
> = {
  chat: 'Chat',
  inbox: 'Inbox',
  properties: 'Properties',
  crm: 'Leasing',
  search: 'Search',
  settings: 'Settings',
  'dev-runs': 'Runs',
};

/** The signed-out tab. Not a view: nobody navigates to it. */
export const SIGN_IN_TITLE = `Sign in · ${APP_NAME}`;

export function titleFor(view: keyof typeof VIEW_TITLES | null | undefined): string {
  if (!view) return APP_NAME;
  const label = VIEW_TITLES[view];
  return label ? `${label} · ${APP_NAME}` : APP_NAME;
}

/**
 * ⚠ `signedIn` is App's to pass, not LoginForm's to set. A child's effect
 * runs BEFORE its parent's, so a title set inside LoginForm was immediately
 * overwritten by App's "Chat · Occupella": the sign-in tab named a screen the
 * visitor could not see.
 */
export function useDocumentTitle(
  view: keyof typeof VIEW_TITLES | null | undefined,
  signedIn = true,
) {
  useEffect(() => {
    document.title = signedIn ? titleFor(view) : SIGN_IN_TITLE;
  }, [view, signedIn]);
}
