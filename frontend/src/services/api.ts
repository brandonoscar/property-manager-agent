/**
 * Authenticated fetch helper.
 *
 * All calls to the Occupella backend go through this. It injects the
 * current Supabase access token as a Bearer credential, sets a JSON
 * content type for bodied requests, and surfaces backend error JSON
 * as an Error message so callers can show useful toasts.
 *
 * Demo mode is short-circuited in the individual services (they branch
 * on ``demoMode`` before ever hitting this helper), so no extra logic
 * is needed here for demo. ``getAccessToken`` already returns the
 * synthetic demo token in demo mode for the rare path where it is
 * called.
 */

import { getAccessToken } from './auth';
import {
  BUILD_HEADER,
  buildsDisagree,
  noteServerBuild,
  skewAdvice,
} from './buildVersion';
import {
  isOutageSignal,
  reachabilityAdvice,
  reportServiceFailure,
  reportServiceReachable,
} from './serviceHealth';
import { reportCredentialAccepted, reportCredentialRejected } from './sessionRejected';

export interface ApiFetchOptions extends RequestInit {
  /** When true, skip the Authorization header (genuinely public routes). */
  anonymous?: boolean;
  /** Override the default request timeout in milliseconds. Pass 0 to
   *  disable the timeout entirely (e.g. for streaming responses). */
  timeoutMs?: number;
}

/**
 * The reason phrases HTTP itself supplies, which are not messages to anybody.
 *
 * ⚠ FastAPI answers an unmatched route with `{"detail": "Not Found"}` and an
 * unhandled exception with `{"detail": "Internal Server Error"}`. Those are
 * status lines, not sentences — and `apiFetch` handed them straight to the
 * screen, so a property manager whose request 404'd read the words
 * **"Not Found"** with no subject, no cause and nothing to do next.
 *
 * A route that has bothered to write a real message ("This action requires the
 * manager role.") keeps it. This list only catches the ones where the backend
 * said nothing and HTTP filled in the blank.
 */
const HTTP_REASON_PHRASES = new Set(
  [
    'bad request',
    'unauthorized',
    'forbidden',
    'not found',
    'method not allowed',
    'conflict',
    'unprocessable entity',
    'unprocessable content',
    'too many requests',
    'internal server error',
    'not implemented',
    'bad gateway',
    'service unavailable',
    'gateway timeout',
  ].map((s) => s),
);

/**
 * What to say when the backend did not say anything worth repeating.
 *
 * ⚠ **403 and 404 are the pair this exists for, and they must not read the
 * same.** "You cannot see this" and "this is not here" send a person to
 * completely different next actions — ask an admin, or stop looking — and a
 * shared "Request failed" leaves them guessing which. That guess is the whole
 * cost of the defect.
 */
/**
 * Statuses a stale page actually explains.
 *
 * ⚠ Narrow on purpose. A 404 means the endpoint is not where this bundle
 * thinks it is; a 422 means the server did not accept the shape it sent.
 * Those are what a moved contract looks like. A 401, 403, 429 or 500 has
 * nothing to do with which build is asking, and saying "reload" there would
 * send somebody to do a useless thing about a real problem — the failure this
 * whole change exists to stop, pointed the other way.
 *
 * 409 is deliberately absent: its message already says to reload.
 */
const SKEW_EXPLAINS = new Set([404, 422]);

/**
 * Add the version-skew sentence when, and only when, it is both relevant and
 * true. APPENDED — see `skewAdvice` for why it never substitutes.
 */
function explainIfStale(message: string, status: number): string {
  if (!SKEW_EXPLAINS.has(status)) return message;
  if (!buildsDisagree()) return message;
  return `${message} ${skewAdvice()}`;
}

export function httpFallbackMessage(status: number): string {
  if (status === 401) {
    // ⚠ THE OLD SENTENCE NAMED A CAUSE IT COULD NOT KNOW AND AN ACT THAT
    // CANNOT BE DONE: "Your session has expired. Sign in again in another
    // tab". Expiry is only one of the reasons this status arrives — the
    // commonest in practice is a session ENDED from Settings → Sessions on
    // another device, which `api/session_liveness.py` makes effective on the
    // next request. And a second tab finds the stored session, sets `user`,
    // and renders the app: there is no sign-in form to reach there without
    // signing out first.
    //
    // One sentence covers every cause because the remedy is the same for all
    // of them, and `SessionBanner` carries the button that performs it.
    return 'Occupella no longer accepts this device’s sign-in. Sign out and sign back in to continue — anything you have typed here is still on screen.';
  }
  if (status === 403) {
    return "You don't have permission to do that. An admin on your team can change your role.";
  }
  if (status === 404) {
    return 'That is not here — it may have been deleted, or the link may be out of date.';
  }
  if (status === 409) {
    return 'Somebody changed this while you were working on it. Reload and try again.';
  }
  if (status === 413) return 'That file is too large.';
  if (status === 422) return 'Some of that was not accepted. Check the fields and try again.';
  if (status === 429) return 'Too many requests just now. Wait a moment and try again.';
  if (status >= 500) return 'Something went wrong on our side. Try again in a moment.';
  return `The request did not go through (${status}).`;
}

/**
 * Told when a request comes back 403, so anything holding a cached role can
 * go and ask again.
 *
 * ⚠ A plain module-level set rather than a store slice or a `CustomEvent`.
 * `api.ts` is imported by every service and must not depend on the store (a
 * cycle), and a window event would be invisible to the vitest environment
 * without extra setup — this is testable by calling the exported functions.
 */
const permissionDeniedListeners = new Set<() => void>();

/** Subscribe. Returns the unsubscribe, so a React effect can return it. */
export function onPermissionDenied(fn: () => void): () => void {
  permissionDeniedListeners.add(fn);
  return () => {
    permissionDeniedListeners.delete(fn);
  };
}

/**
 * Announce a 403 to everything holding a cached permission.
 *
 * Exported so tests can fire the REAL signal rather than a hand-rolled fake —
 * a test that invents its own notifier still passes when the module stops
 * calling its listeners, which is the one failure it exists to catch. Firing
 * it from outside is harmless by construction: the only effect is a refetch.
 */
export function notifyPermissionDenied(): void {
  for (const fn of [...permissionDeniedListeners]) {
    try {
      fn();
    } catch {
      // ⚠ Swallowed on purpose. This fires while a 403 is being turned into
      // the error the caller will see; a listener that throws here would
      // replace "you don't have permission" with its own stack, which is the
      // one message the person actually needed.
    }
  }
}

/**
 * The field names in a FastAPI 422, in the order the server reported them.
 *
 * FastAPI's request-validation handler answers with a LIST:
 *
 *     {"detail": [{"loc": ["body", "email"], "msg": "...", "type": "..."}]}
 *
 * `apiFetch` kept only string details, so this list was dropped entirely and a
 * failed validation read "Some of that was not accepted. Check the fields and
 * try again" — without saying WHICH. On a form the size of the A2P
 * registration that is a person re-reading twenty fields to find the one.
 *
 * ⚠ **Only the field NAME is taken, never pydantic's `msg`.** Its messages are
 * written for a developer and include the constraint verbatim — "String should
 * match pattern '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'" would put a regex on a
 * property manager's screen. That is the same leak the 401 branch above exists
 * to prevent, and a list of safe-looking messages could not anticipate the
 * next one.
 *
 * ⚠ `loc` starts with the SOURCE (`body` / `query` / `path`), which is not a
 * field a person filled in. Numeric entries are list indices. Both are dropped,
 * so `["body", "invites", 0, "email"]` reads as `invites.email`.
 */
export function validationFields(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const entry of raw) {
    if (typeof entry !== 'object' || entry === null) continue;
    const loc = (entry as { loc?: unknown }).loc;
    if (!Array.isArray(loc)) continue;
    const parts = loc
      .filter((p): p is string => typeof p === 'string')
      .filter((p) => !['body', 'query', 'path', 'header', 'cookie'].includes(p));
    if (!parts.length) continue;
    const name = parts.join('.');
    if (!out.includes(name)) out.push(name);
  }
  return out;
}

/**
 * Keys a JSON-object `detail` may carry the human sentence under, in the order
 * they win.
 *
 * ⚠ THIS LIST IS READ BY A BACKEND TEST. `tests/unit/test_error_detail_is_
 * readable.py` parses it out of this file and asserts that every
 * `HTTPException(detail={...})` in `src/helixis` carries one of these keys with
 * a written value — gotcha 28's both-sides rule, with one source of truth so a
 * seventh route cannot invent a fourth spelling and go quiet. Renaming the
 * constant, or moving it, fails that test with instructions.
 */
export const DETAIL_MESSAGE_KEYS = ['message', 'error', 'problems'] as const;

/**
 * The sentence the server wrote, out of whatever shape it wrote it in.
 *
 * ⚠ A `detail` IS SOMETIMES A JSON OBJECT, and six routes in this product use
 * one — the four inbox-execute raises, the A2P submit validation, and the CRM
 * "which application?" conflict. Each carries a written sentence plus a
 * machine-readable part (`tool_id`, the applications list, the problem list).
 * The old extraction kept `detail` only when `typeof raw === 'string'`, so
 * every one of those sentences was dropped and replaced by the generic status
 * fallback. What that cost, in the worst case: the execute route's
 *
 *     "a previous attempt did not complete cleanly; this action needs review
 *      before it can be retried"
 *
 * reached the screen as "Somebody changed this while you were working on it.
 * Reload and try again." — a fabricated cause (nobody changed anything) and an
 * instruction that cannot work (reloading does not clear `blocked_needs_review`;
 * only a person can). The comment above this function's caller already records
 * fixing the LIST shape for 422s and never considered the object shape.
 *
 * ⚠ An ARRAY returns undefined deliberately. FastAPI's own 422 body is a list
 * of `{loc, msg, type}` and `validationFields` owns it — swallowing it here
 * would put pydantic's developer-facing `msg` (regexes included) on a property
 * manager's screen, which is the leak the 401 branch exists to prevent.
 */
export function writtenDetail(raw: unknown): string | undefined {
  if (typeof raw === 'string') return raw;
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const record = raw as Record<string, unknown>;
  for (const key of DETAIL_MESSAGE_KEYS) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return value;
    // A list of written sentences — A2P's `{"problems": [...]}` is the whole
    // list of what a carrier will reject the registration for, and naming none
    // of them is what the generated-defaults work existed to stop.
    if (Array.isArray(value)) {
      const parts = value.filter((v): v is string => typeof v === 'string' && Boolean(v.trim()));
      if (parts.length) return parts.join(' ');
    }
  }
  return undefined;
}

/** The 422 sentence, naming the fields when the server named them. */
export function validationMessage(fields: string[]): string {
  if (!fields.length) return httpFallbackMessage(422);
  const list = fields.join(', ');
  return fields.length === 1
    ? `Check the ${list} field — the server did not accept that value.`
    : `Check these fields — the server did not accept them: ${list}.`;
}

export class ApiError extends Error {
  status: number;
  /**
   * Fields the server rejected, when it said. Empty for anything that is not a
   * FastAPI 422. A form can highlight these rather than making the person hunt.
   */
  fields: string[] = [];
  /**
   * Exactly what the backend sent, including an HTTP reason phrase — for a
   * log line or a support report. `message` is the sentence for the screen;
   * this is the one for the engineer, and they are deliberately not the same
   * string.
   */
  detail?: string;
  constructor(status: number, message: string, detail?: string) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.name = 'ApiError';
  }
}

export class ApiAuthError extends ApiError {
  /**
   * ⚠ Takes `detail` like its parent, and that is load-bearing rather than
   * symmetry. MOST 401 details this backend produces are written for an
   * engineer — "Token has expired", and `f"Invalid token: {exc}"`, which is a
   * JWT library's exception. Keeping it OFF `message` and ON `detail` is what
   * lets the screen say something useful while a bug report still carries the
   * cause.
   *
   * ⚠ "Every" was the word here until 2026-09-18 and it was measurably false:
   * `auth.py::_REVOKED_DETAIL` is "This device was signed out. Sign in
   * again." — a sentence for a person. The rule still holds anyway, and for a
   * better reason than the count: whichever cause fired, the remedy is
   * identical, so ONE written sentence serves all of them and there is no
   * vocabulary to keep in step (see `httpFallbackMessage`).
   */
  constructor(message: string, detail?: string) {
    super(401, message, detail);
    this.name = 'ApiAuthError';
  }
}

export class ApiTimeoutError extends ApiError {
  constructor(message: string) {
    super(0, message);
    this.name = 'ApiTimeoutError';
  }
}

/**
 * Hard cap on how long a single backend request can hang before we abort
 * and surface a typed error to the UI. Render's free-tier dynos cold-
 * start in well under 30s, so 45s gives a generous window before we give
 * up — but a request still pending at 45s is almost always stuck (auth
 * desync, dropped Render dyno, network blackhole) and the UI must stop
 * spinning so the user can recover instead of staring at "Saving…".
 */
const DEFAULT_TIMEOUT_MS = 45_000;

/**
 * Match the canonical shape of FastAPI routes for our backend. Most routes
 * are verb-tailed (``/agent/run``, ``/agent/confirm``, ``/connectors/<id>/connect``)
 * or have an ID tail (``/agent/cancel/<uuid>``). Only a handful are true
 * collection routes registered on the router root with a trailing slash.
 *
 * The earlier "uuid-or-digit at the tail = item, otherwise add a slash"
 * heuristic was wrong for verb tails like ``run`` and ``confirm`` — those
 * got a slash appended and FastAPI's redirect_slashes then 307'd them
 * back, which (a) doubled latency and (b) without uvicorn's proxy
 * headers in front, redirected back to ``http://`` and tripped mixed
 * content blocking in the browser.
 *
 * Maintaining an explicit allowlist of collection roots is the cleaner
 * fix: we add a trailing slash only for entries on this set and leave
 * every other ``/api/...`` path verbatim. Adding a new collection route
 * is a one-line append here.
 */
const COLLECTION_ROUTES: ReadonlySet<string> = new Set([
  '/api/v1/connectors',
  '/api/v1/webhooks',
]);

export function normalizePath(path: string): string {
  if (!path.startsWith('/api/')) return path;
  const [base, query] = path.split('?', 2);
  const trimmed = base.replace(/\/$/, '');
  const isCollection = COLLECTION_ROUTES.has(trimmed);
  const normalized = isCollection ? `${trimmed}/` : trimmed;
  return query !== undefined ? `${normalized}?${query}` : normalized;
}

export async function apiFetch(
  path: string,
  options: ApiFetchOptions = {},
): Promise<Response> {
  const {
    anonymous,
    headers: extraHeaders,
    body,
    timeoutMs,
    signal: callerSignal,
    ...rest
  } = options;

  const effectiveTimeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;

  // Wrap the entire flow (getAccessToken + fetch) in a single deadline.
  // The previous implementation only wrapped fetch — but Supabase's
  // ``getSession()`` can deadlock in some states (mutex contention,
  // missing storage backend), and that hang happened *before* the
  // AbortController was set up, so a stuck session lookup left the
  // whole call hanging past the timeout.
  return await raceWithDeadline(
    runApiFetch(path, {
      anonymous,
      extraHeaders,
      body,
      callerSignal,
      effectiveTimeout,
      rest,
    }),
    effectiveTimeout,
    path,
  );
}

interface InternalOptions {
  anonymous?: boolean;
  extraHeaders?: HeadersInit;
  body?: BodyInit | null;
  callerSignal?: AbortSignal | null;
  effectiveTimeout: number;
  rest: RequestInit;
}

async function runApiFetch(
  path: string,
  opts: InternalOptions,
): Promise<Response> {
  const { anonymous, extraHeaders, body, callerSignal, effectiveTimeout, rest } =
    opts;

  const headers = new Headers(extraHeaders ?? {});

  if (!anonymous) {
    const token = await getAccessToken();
    if (!token) {
      // A different situation from an expired session below: there is no
      // credential at all, so nothing has been lost and there is nothing to
      // retry — the person needs to sign in. `detail` keeps the machine-facing
      // note for a report.
      throw new ApiAuthError('You are not signed in. Sign in and try again.', 'no access token');
    }
    headers.set('Authorization', `Bearer ${token}`);
  }

  // Default JSON — but never for FormData, where the browser must set the
  // multipart Content-Type (with its boundary) itself.
  if (
    body !== undefined &&
    !(body instanceof FormData) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json');
  }

  const timeoutController = effectiveTimeout > 0 ? new AbortController() : null;
  const timeoutHandle = timeoutController
    ? setTimeout(() => timeoutController.abort(), effectiveTimeout)
    : null;

  const signal = callerSignal
    ? mergeAbortSignals(callerSignal, timeoutController?.signal)
    : timeoutController?.signal;

  let res: Response;
  try {
    res = await fetch(normalizePath(path), { ...rest, body, headers, signal });
  } catch (err) {
    // ⚠ A request the CALLER cancelled says nothing about the backend's
    // health — streams are aborted on every navigation. Counting those
    // would report an outage every time somebody changed page.
    if (callerSignal?.aborted) throw err;
    if (timeoutController?.signal.aborted) {
      reportServiceFailure();
      throw new ApiTimeoutError(
        `Request timed out after ${Math.round(effectiveTimeout / 1000)}s. ` +
          reachabilityAdvice(),
      );
    }
    if (isOutageSignal(err)) reportServiceFailure();
    throw err;
  } finally {
    if (timeoutHandle !== null) clearTimeout(timeoutHandle);
  }

  // ⚠ Reported on the STATUS, before the !res.ok branch below turns it into
  // an Error. A 401/404/422 is a served response and therefore evidence the
  // backend is up — only a gateway status means nothing was there to ask.
  if (isOutageSignal(null, res.status)) reportServiceFailure();
  else reportServiceReachable();

  // ⚠ A SECOND, INDEPENDENT FACT read off the same served response: is the
  // credential we sent still accepted? `serviceHealth` deliberately answers
  // only "was anything there to answer" — its own header says a 401 PROVES
  // the backend is up — so the question of whether our sign-in still works
  // has to be recorded separately or not at all.
  //
  // ⚠ Skipped entirely for an ANONYMOUS request. It carried no credential,
  // so its 401 is not evidence about ours, and counting it would announce a
  // dead session to somebody whose session is fine.
  if (!anonymous) {
    if (res.status === 401) reportCredentialRejected();
    else reportCredentialAccepted();
  }

  // ⚠ Recorded on EVERY response, including the failing one — that is the
  // whole point of carrying it as a header rather than probing for it. The
  // request that breaks under a stale bundle is the request that tells us the
  // bundle is stale, with no extra round trip and no timing gap.
  noteServerBuild(res.headers.get(BUILD_HEADER));

  if (!res.ok) {
    // ⚠ TWO VARIABLES, NOT ONE, and the split is the point. `shown` is what a
    // property manager reads; `detail` is the engineer's copy kept on the
    // error. They were one variable, which is why an object detail could not be
    // handled at all: extracting its sentence into `detail` would have lost the
    // machine-readable half for diagnosis, and stringifying the object into
    // `detail` would have put raw JSON on the screen (the reason-phrase filter
    // below passes anything that is not an HTTP reason phrase).
    let shown: string | undefined;
    let detail: string | undefined;
    let fields: string[] = [];
    try {
      const data = await res.clone().json();
      const raw: unknown = data?.detail ?? data?.message;
      // The 422 list is no longer thrown away — see `validationFields`.
      fields = validationFields(raw);
      // ⚠ `detail` is NOT always a string, and the old `as string` cast said
      // it was. FastAPI's own request-validation handler answers 422 with a
      // LIST of `{loc, msg, type}` objects — which went into `new Error(...)`
      // and reached the screen as "[object Object]".
      shown = writtenDetail(raw);
      detail = typeof raw === 'string' ? raw : raw === undefined ? undefined : JSON.stringify(raw);
    } catch {
      // Non-JSON body — fall through to the status-line fallback.
    }
    if (res.status === 401) {
      // ⚠ The backend's 401 `detail` is NEVER shown, and that is deliberate —
      // this is the status where nearly every string the API produces is
      // written for an engineer: "Token has expired", "Missing Authorization
      // header", and `f"Invalid token: {exc}"`, which puts a JWT library's
      // exception on a property manager's screen. The one human-written
      // exception (`_REVOKED_DETAIL`) is not a reason to start rendering
      // them: its remedy is the same as every other cause's, so the written
      // sentence below already covers it.
      //
      // This branch also used to sit ABOVE the `written` filter below, so it
      // skipped the very rule that exists to stop HTTP reason phrases reaching
      // people — a dead session read as the single word "Unauthorized".
      //
      // The detail is kept on the error (`ApiError.detail` is the engineer's
      // copy, `message` is the screen's), so nothing is lost for diagnosis.
      throw new ApiAuthError(httpFallbackMessage(401), detail);
    }
    // ⚠ A 403 is the ONLY reliable signal that the caller's role changed
    // under them. `require_role` reads `company_users` on every request, so
    // the backend is always right and the CLIENT is the stale one — it holds
    // whatever `GET /company/me` said when the screen mounted, which may be
    // hours old. Announcing it here, at the one place every request passes
    // through, is what lets the affordances correct themselves instead of
    // waiting for a navigation. Never throws: a listener that fails must not
    // replace a permission error with its own.
    if (res.status === 403) notifyPermissionDenied();
    // A route that wrote a real sentence keeps it; an HTTP reason phrase is
    // replaced, because "Not Found" on its own tells a property manager
    // nothing about what happened or what to do.
    // A named-field 422 beats the generic sentence; anything else keeps the
    // existing written-vs-reason-phrase rule.
    if (fields.length) {
      const err = new ApiError(
        res.status,
        explainIfStale(validationMessage(fields), res.status),
        detail,
      );
      err.fields = fields;
      throw err;
    }
    const written = shown && !HTTP_REASON_PHRASES.has(shown.trim().toLowerCase());
    throw new ApiError(
      res.status,
      explainIfStale(written ? (shown as string) : httpFallbackMessage(res.status), res.status),
      detail,
    );
  }

  return res;
}

/**
 * Race a promise against a hard deadline. The deadline applies to the
 * *entire* flow — including pre-fetch async work like resolving an
 * auth token — so a deadlocked dependency can't make the whole call
 * hang past the timeout window. ``effectiveTimeout <= 0`` disables the
 * race entirely (used for streaming endpoints).
 */
async function raceWithDeadline<T>(
  work: Promise<T>,
  effectiveTimeout: number,
  path: string,
): Promise<T> {
  if (effectiveTimeout <= 0) return work;
  let deadlineHandle: ReturnType<typeof setTimeout> | null = null;
  const deadline = new Promise<never>((_, reject) => {
    deadlineHandle = setTimeout(() => {
      // The outer deadline can fire while `runApiFetch` is still pending —
      // a deadlocked auth lookup never reaches the fetch, so its own catch
      // never runs. Reporting here is what makes that case visible.
      reportServiceFailure();
      reject(
        new ApiTimeoutError(
          `Request to ${path} timed out after ${Math.round(
            effectiveTimeout / 1000,
          )}s. ${reachabilityAdvice()}`,
        ),
      );
    }, effectiveTimeout);
  });
  try {
    return await Promise.race([work, deadline]);
  } finally {
    if (deadlineHandle !== null) clearTimeout(deadlineHandle);
  }
}

function mergeAbortSignals(
  caller: AbortSignal,
  timeout: AbortSignal | undefined,
): AbortSignal {
  if (!timeout) return caller;
  const merged = new AbortController();
  if (caller.aborted) merged.abort(caller.reason);
  if (timeout.aborted) merged.abort(timeout.reason);
  caller.addEventListener('abort', () => merged.abort(caller.reason), {
    once: true,
  });
  timeout.addEventListener('abort', () => merged.abort(timeout.reason), {
    once: true,
  });
  return merged.signal;
}
