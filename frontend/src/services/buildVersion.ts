/**
 * Is this page the same build of Occupella as the server answering it?
 *
 * Render deploys the API and Vercel deploys this app from the same branch,
 * independently. So after every merge there is a window where the two differ,
 * and for as long as somebody leaves a tab open it can last hours. When the
 * API contract moved in between, their next request fails — and the message
 * they get says the record "may have been deleted" or to "check the fields".
 * Both are false, both send them to retry, and retrying a stale bundle fails
 * identically forever. That is the shape of a failure with no fallback: the
 * one thing that would fix it (reload) is the one thing nothing suggests.
 *
 * Both halves of the fact already existed and were never put together — the
 * bundle bakes in its commit for bug reports, the backend resolved its own SHA
 * for Sentry — so this module is a comparison, not new data.
 *
 * ⚠ **IT CLAIMS A MISMATCH, NEVER A DIRECTION.** Two opaque commit SHAs carry
 * no ordering, so "you are behind" is not something this can know: during a
 * deploy Vercel usually lands first, which puts a FRESH page against an OLD
 * server, and telling that person to reload is advice that cannot work. The
 * honest sentence covers both — the two are out of step, a deploy is probably
 * in flight, reloading in a moment is what ends it either way.
 *
 * ⚠ **UNKNOWN IS NOT A MISMATCH**, in either direction and for three separate
 * reasons, each of which would otherwise fire on every request:
 *   * no header at all — an old backend, or a response that never reached the
 *     middleware;
 *   * the server's own `unknown` sentinel — it could not resolve its SHA;
 *   * a `-dev` bundle — a local build has no commit, and the dev server it
 *     talks to does, so every developer would see the banner permanently.
 * A control that fires when it is not true is one people learn to ignore, and
 * then it is worth nothing on the day it is right.
 */

/** The server's sentinel for "could not work out my own build". Must match
 *  `observability/build_id.UNKNOWN_BUILD` — a cross-language guard pins it. */
export const UNKNOWN_BUILD = 'unknown';

/** The response header the API stamps on every response. Must match
 *  `observability/build_id.BUILD_HEADER`; same guard. */
export const BUILD_HEADER = 'X-Occupella-Build';

/**
 * What this bundle was built from.
 *
 * `__APP_VERSION__` is the first 12 characters of the Vercel commit, or
 * `<pkg version>-dev` for a local build (see `vite.config.ts`). Only the
 * first form is comparable, which is what `clientBuild()` decides.
 */
declare const __APP_VERSION__: string;

function rawClientBuild(): string {
  try {
    return typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '';
  } catch {
    // The define is missing entirely (a bare test environment). Unknown, not
    // a mismatch.
    return '';
  }
}

/** This bundle's build, or `''` when it is not a comparable commit. */
export function clientBuild(): string {
  const value = rawClientBuild().trim();
  if (!value || value === UNKNOWN_BUILD) return '';
  // ⚠ A `-dev` fallback is a version number, not a commit. Comparing it to a
  // real SHA mismatches on every single request, so it reads as unknown.
  if (value.endsWith('-dev')) return '';
  return value;
}

let serverBuild = '';

/**
 * Record the build that answered. Called from `apiFetch` for every response,
 * so the fact rides the SAME response that failed rather than costing a
 * separate probe request.
 */
export function noteServerBuild(value: string | null | undefined): void {
  const next = (value ?? '').trim();
  // ⚠ An absent header must not CLEAR a build we already know. Not every
  // response goes through the API middleware (a proxy error page, an aborted
  // request), and forgetting on those would make the check flicker.
  if (!next) return;
  serverBuild = next === UNKNOWN_BUILD ? '' : next;
}

/** The last build we saw answer, or `''`. Exposed for tests and diagnostics. */
export function lastServerBuild(): string {
  return serverBuild;
}

/**
 * Do the page and the server disagree about which build this is?
 *
 * False whenever either side is unknown — see the module note.
 */
export function buildsDisagree(): boolean {
  const ours = clientBuild();
  if (!ours || !serverBuild) return false;
  return ours !== serverBuild;
}

/**
 * The sentence to add when a request failed in a way a stale page explains.
 *
 * ⚠ APPENDED to the status message, never substituted for it. A 404 really
 * can be a deleted record and a 422 really can be a bad field, and we cannot
 * tell which from two SHAs — so replacing the original reading would trade a
 * misleading sentence for a differently misleading one. Adding names the
 * second possibility and, crucially, the action.
 */
export function skewAdvice(): string {
  return (
    'This page and the server are running different versions of Occupella ' +
    '(a deploy is probably in progress) — reload the page and try again.'
  );
}

/** Test hook. Never called by the product. */
export function resetBuildVersion(): void {
  serverBuild = '';
}
