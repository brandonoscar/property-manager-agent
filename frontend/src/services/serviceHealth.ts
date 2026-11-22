/**
 * Is the backend reachable right now — and if not, WHOSE fault is that?
 *
 * Every call goes through ``apiFetch``, so that is the one place that sees
 * every failure — and until now each caller turned its own failure into its
 * own error state. During a Render deploy that meant the Inbox said one
 * thing, the Operations grid said another, and the composer said a third,
 * about a single fact: Occupella is briefly not there. One shared answer
 * gives one honest sentence instead.
 *
 * ⚠ **A served response means the backend is UP, whatever its status.** A
 * 401, a 404 and a 422 all prove a process answered; only a transport
 * failure or a gateway status says otherwise. That distinction is the whole
 * design — see ``isOutageSignal``.
 *
 * ⚠ **One failure is not an outage.** A banner that fires on a single blip
 * is one people learn to scroll past, and then it is worth nothing on the
 * day it is true (the same reasoning the fair-housing regex notes record).
 * ``CONSECUTIVE_FAILURES_FOR_OUTAGE`` is the bar; any served response
 * resets the count to zero.
 *
 * ⚠ **A DROPPED CONNECTION IS NOT EVIDENCE ABOUT THE BACKEND.** `fetch`
 * rejects with the same `TypeError` whether our server is missing or the
 * user's wifi is, so this module used to answer "Occupella can't reach its
 * server" to somebody whose hotel network had just died — telling a customer
 * the product is broken when it is not, which is the one direction that
 * blames us for something we can see is not ours. `offline` is that second
 * fact, and it is the browser's own report rather than an inference:
 *
 * * `offline === true`  — the browser says it has no network. A FACT, so it
 *   needs no consecutive-failure bar, and it OUTRANKS `down`: while the
 *   network is gone we cannot know whether the backend is up, and guessing
 *   would put our name on their outage.
 * * `offline === false` — inconclusive, never proof. A captive portal, a
 *   wedged VPN and a working connection all report `true`, so this side
 *   claims nothing and the failure counting below decides as it always did.
 *
 * The asymmetry is deliberate (the decline-to-assert posture the consent
 * ledger and the mirror rollups take): under-reporting offline costs the
 * user a less precise sentence, over-reporting it costs them a wrong one.
 */

/** Consecutive transport/gateway failures before we say the service is down. */
export const CONSECUTIVE_FAILURES_FOR_OUTAGE = 2;

export interface ServiceHealth {
  /**
   * The BACKEND is not answering: the bar above was met by failures we could
   * not attribute to the user's own network.
   */
  down: boolean;
  /** The browser says THIS DEVICE has no network. Outranks `down`. */
  offline: boolean;
  /** Consecutive failures seen. Exposed for tests and diagnostics. */
  failures: number;
}

type Listener = (health: ServiceHealth) => void;

let failures = 0;
let down = false;
let offline = false;
const listeners = new Set<Listener>();

function snapshot(): ServiceHealth {
  return { down, offline, failures };
}

function emit(): void {
  const current = snapshot();
  for (const listener of listeners) {
    // A throwing subscriber must not stop the others from being told, and
    // must never propagate into apiFetch's caller — the health signal is an
    // enhancement and is not allowed to break the request that produced it.
    try {
      listener(current);
    } catch {
      /* a broken listener is not the request's problem */
    }
  }
}

/**
 * Does this failure mean "the service is not there", as opposed to "the
 * service answered and said no"?
 *
 * ⚠ **500 is deliberately NOT an outage.** A 500 is one endpoint throwing —
 * the rest of the app works, and telling somebody the product is down
 * because a single report failed is a worse lie than saying nothing. 502 /
 * 503 / 504 come from the proxy in front of a process that is missing,
 * restarting or wedged, which is the state this exists to name.
 *
 * ⚠ This answers "was anything there to answer", NOT "is it our end or
 * theirs" — a `TypeError` is the same object either way. Separating those is
 * `offline`'s job, and it happens in `reportServiceFailure` below rather
 * than here, so that every caller gets the split without having to know
 * about it.
 */
export function isOutageSignal(error: unknown, status?: number): boolean {
  if (status !== undefined) {
    return status === 502 || status === 503 || status === 504;
  }
  if (error instanceof Error) {
    // A timeout is the backend being unreachable for 45 seconds. `fetch`
    // rejects with a TypeError when the connection cannot be made at all.
    if (error.name === 'ApiTimeoutError') return true;
    if (error.name === 'TypeError') return true;
  }
  return false;
}

/** Record that the backend answered — any status. Clears an outage. */
export function reportServiceReachable(): void {
  const wasDown = down;
  const hadFailures = failures > 0;
  failures = 0;
  down = false;
  if (wasDown || hadFailures) emit();
}

/**
 * Record a failure that looked like the service being absent.
 *
 * ⚠ **Ignored entirely while the browser is offline.** A request that never
 * left the device says nothing about our server, so counting it would let a
 * train tunnel accumulate "evidence" of an outage that is not happening.
 */
export function reportServiceFailure(): void {
  if (offline) return;
  failures += 1;
  const nowDown = failures >= CONSECUTIVE_FAILURES_FOR_OUTAGE;
  const changed = nowDown !== down;
  down = nowDown;
  if (changed) emit();
}

/** The browser lost its network. A fact, so it needs no repetition. */
export function reportBrowserOffline(): void {
  if (offline) return;
  offline = true;
  emit();
}

/**
 * The browser has a network again.
 *
 * ⚠ **This clears the failure count, and that is not tidiness.** The
 * `offline` event can land AFTER the first requests have already failed, so
 * those failures are the network's and not the backend's — and leaving
 * `down` standing would swap one wrong sentence for another the moment the
 * connection came back. Under-reporting for one request cycle is the safe
 * direction: the next real failure re-arms the count immediately.
 */
export function reportBrowserOnline(): void {
  if (!offline && failures === 0 && !down) return;
  offline = false;
  failures = 0;
  down = false;
  emit();
}

export function getServiceHealth(): ServiceHealth {
  return snapshot();
}

/**
 * What to tell somebody whose request never came back.
 *
 * ⚠ The banner is not the only place that names a party. A timed-out request
 * throws a message the user reads directly, and both of `api.ts`'s timeout
 * sites said "The backend may be sleeping" — our server, blamed, on evidence
 * that is just as consistent with their wifi. The rule lives here rather
 * than at those two call sites so a third one cannot quietly disagree.
 */
export function reachabilityAdvice(): string {
  return offline
    ? 'Your device is offline — reconnect and try again.'
    : 'The backend may be sleeping — try again.';
}

/** Subscribe; returns the unsubscribe. The current value is NOT replayed —
 *  callers read `getServiceHealth()` for the initial render. */
export function onServiceHealthChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Test hook. Never called by the product. */
export function resetServiceHealth(): void {
  failures = 0;
  down = false;
  offline = false;
  listeners.clear();
}

// ⚠ Installed at module scope, on purpose. The health module owns the fact
// and the banner only renders it, so a page that never mounts the banner
// still has the right answer the moment it does — and every consumer gets
// the split without wiring anything. There is nothing to tear down: this is
// a singleton for the tab's whole life.
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('offline', reportBrowserOffline);
  window.addEventListener('online', reportBrowserOnline);

  // ⚠ `=== false`, never `!navigator.onLine`. The whole asymmetry above rests
  // on only believing a POSITIVE report: anywhere the property is absent or
  // undefined, `!x` would claim the device is offline on no evidence at all.
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    offline = true;
  }
}
