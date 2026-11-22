/**
 * Warn before closing the tab on work that would be lost.
 *
 * ⚠ WHAT THIS IS FOR, AND WHAT IT IS NOT. It covers text a person typed that
 * has no other home — a half-filled carrier registration, an unsent message,
 * an attachment picked but not sent. It is deliberately NOT wired to anything
 * that autosaves (the properties grid) or anything that survives the tab (an
 * agent run continues server-side and the thread reattaches), because a
 * warning that fires when nothing is at stake is one people learn to click
 * through — and then it is not there for the registration form either.
 *
 * ⚠ IT COVERS TWO WAYS OF LEAVING, and until 2026-09-08 only one of them.
 * `beforeunload` fires for a tab close or a reload; it cannot see an IN-APP
 * navigation, because no document is unloading. So a half-filled carrier
 * registration vanished silently the moment somebody clicked Chat in the rail
 * — with this hook attached, looking like it had that covered. The flag is
 * published to the store, and `setView` / `openSettings` read it, so every
 * navigation in the app is guarded from one place.
 *
 * ⚠ THE TWO WAYS DESTROY DIFFERENT THINGS, so they are separate arguments.
 * The chat composer's draft lives in the STORE: a tab close loses it, a view
 * change does not — so warning about it on navigation would fire on every
 * click of the rail with text in the box, which is precisely how a warning
 * becomes one people click through, and then it is not there for the
 * registration either. One boolean answering both questions is gotcha 67's
 * shape; `lostOnNavigation` is the second question, asked out loud.
 *
 * It defaults to TRUE so a new caller is guarded by default — an unguarded
 * form loses somebody's work, while an over-eager prompt is visible and gets
 * reported.
 *
 * ⚠ The browser shows ITS OWN generic wording ("Leave site? Changes you made
 * may not be saved"), not ours. Every modern browser ignores a custom string,
 * so there is nothing to write here and no point pretending otherwise. What
 * we control is exactly one bit: whether it asks at all. That is the whole
 * reason this hook is a boolean.
 */

import { useEffect } from 'react';

import { useStore } from '../stores/store';

export function useUnsavedWork(
  hasUnsavedWork: boolean,
  { lostOnNavigation = true }: { lostOnNavigation?: boolean } = {},
): void {
  // ⚠ Its own effect, and it runs whether or not there is work to lose —
  // publishing `false` is what CLEARS a stale warning after a save. Folding it
  // into the early-returning effect below would leave the flag stuck true and
  // block every navigation until a reload.
  useEffect(() => {
    useStore.getState().setUnsavedWork(hasUnsavedWork && lostOnNavigation);
    // Unmounting is leaving: the form is gone, so nothing is at stake and the
    // next navigation must not be challenged for it.
    return () => useStore.getState().setUnsavedWork(false);
  }, [hasUnsavedWork, lostOnNavigation]);

  useEffect(() => {
    // ⚠ The listener is only ATTACHED while there is something to lose. An
    // always-attached handler that returns early is the same behaviour on
    // paper and worse in practice: Chrome and Safari disable the back/forward
    // cache for any page carrying a `beforeunload` listener, so an idle
    // composer would cost every back-navigation in the app a full reload.
    if (!hasUnsavedWork) return;

    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      // Both are required, and neither alone is enough across browsers:
      // preventDefault is the modern spec, assigning returnValue is what
      // older WebKit and Firefox still key on.
      e.preventDefault();
      e.returnValue = '';
    };

    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [hasUnsavedWork]);
}
