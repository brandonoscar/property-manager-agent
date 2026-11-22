/**
 * When does Occupella say "we can't reach the server"?
 *
 * Two ways to get this wrong, and they cost opposite things:
 *
 * * **Too eager** — the banner fires on a single blip, people learn it means
 *   nothing, and it is worth nothing on the day it is true.
 * * **Too reluctant** — a Render deploy takes the backend away and the user
 *   is left guessing why five different panels are angry at once.
 *
 * The line between them is what these tests pin: only a transport failure or
 * a gateway status counts, only after it repeats, and any served response
 * clears it immediately.
 *
 * And a third way, which is the one that blames the wrong party: `fetch`
 * rejects with the same `TypeError` whether our server is missing or the
 * user's network is, so a dropped connection used to be reported as
 * Occupella being down. The `offline` half below is that split.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Vite's ?raw gives the file's text without needing node types in this
// tsconfig — the mechanism ServiceBanner.test.tsx already uses for App.tsx.
import apiSource from './api.ts?raw';
import {
  CONSECUTIVE_FAILURES_FOR_OUTAGE,
  getServiceHealth,
  isOutageSignal,
  onServiceHealthChange,
  reachabilityAdvice,
  reportBrowserOffline,
  reportBrowserOnline,
  reportServiceFailure,
  reportServiceReachable,
  resetServiceHealth,
} from './serviceHealth';

beforeEach(() => {
  resetServiceHealth();
});

function failUntilDown(): void {
  for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
    reportServiceFailure();
  }
}

describe('what counts as the service being absent', () => {
  it('treats gateway statuses as an outage', () => {
    expect(isOutageSignal(null, 502)).toBe(true);
    expect(isOutageSignal(null, 503)).toBe(true);
    expect(isOutageSignal(null, 504)).toBe(true);
  });

  it('does NOT treat a 500 as the service being down', () => {
    // ⚠ Deliberate. A 500 is one endpoint throwing while the rest of the app
    // works; telling somebody the product is down because a single report
    // failed is a worse lie than saying nothing.
    expect(isOutageSignal(null, 500)).toBe(false);
  });

  it('does not treat an answered request as an outage, whatever it answered', () => {
    // A 401/403/404/422 all prove a process was there to answer.
    for (const status of [200, 204, 400, 401, 403, 404, 409, 422, 429]) {
      expect(isOutageSignal(null, status)).toBe(false);
    }
  });

  it('treats a failed connection and a timeout as an outage', () => {
    const network = new TypeError('Failed to fetch');
    const timeout = Object.assign(new Error('timed out'), {
      name: 'ApiTimeoutError',
    });
    expect(isOutageSignal(network)).toBe(true);
    expect(isOutageSignal(timeout)).toBe(true);
  });

  it('does not treat an ordinary error as an outage', () => {
    expect(isOutageSignal(new Error('something else'))).toBe(false);
    expect(isOutageSignal(undefined)).toBe(false);
  });
});

describe('when the banner turns on', () => {
  it('stays quiet after a single failure', () => {
    reportServiceFailure();
    expect(getServiceHealth().down).toBe(false);
    // The bar must be more than one, or this test is vacuous.
    expect(CONSECUTIVE_FAILURES_FOR_OUTAGE).toBeGreaterThan(1);
  });

  it('turns on once failures repeat', () => {
    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      reportServiceFailure();
    }
    expect(getServiceHealth().down).toBe(true);
  });

  it('a single served response clears an outage immediately', () => {
    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      reportServiceFailure();
    }
    reportServiceReachable();
    expect(getServiceHealth().down).toBe(false);
    expect(getServiceHealth().failures).toBe(0);
  });

  it('failures must be CONSECUTIVE — a success in between resets the count', () => {
    reportServiceFailure();
    reportServiceReachable();
    reportServiceFailure();
    expect(getServiceHealth().down).toBe(false);
  });
});

describe('subscribers', () => {
  it('are told when the state changes, and not on every failure', () => {
    const seen: boolean[] = [];
    onServiceHealthChange((h) => seen.push(h.down));

    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      reportServiceFailure();
    }
    expect(seen).toEqual([true]);

    // Repeating the failure past the bar must not re-notify — a banner that
    // re-renders on every failed poll is a banner that flickers.
    reportServiceFailure();
    expect(seen).toEqual([true]);

    reportServiceReachable();
    expect(seen).toEqual([true, false]);
  });

  it('a throwing subscriber does not stop the others being told', () => {
    // The health signal is an enhancement; it must never propagate into the
    // request that produced it.
    const good = vi.fn();
    onServiceHealthChange(() => {
      throw new Error('broken listener');
    });
    onServiceHealthChange(good);

    expect(() => {
      for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
        reportServiceFailure();
      }
    }).not.toThrow();
    expect(good).toHaveBeenCalledTimes(1);
  });

  it('unsubscribing stops the notifications', () => {
    const listener = vi.fn();
    const off = onServiceHealthChange(listener);
    off();
    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      reportServiceFailure();
    }
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('whose outage is it', () => {
  it('a lost connection is a FACT, so one report is enough', () => {
    // ⚠ Deliberately unlike the backend arm. That one needs repetition
    // because it INFERS an outage from failures; this is the browser
    // reporting its own state, and asking it to say so twice would only
    // delay a sentence we already know is true.
    reportBrowserOffline();
    expect(getServiceHealth().offline).toBe(true);
    expect(getServiceHealth().down).toBe(false);
  });

  it('a failure that never left the device is not evidence about our server', () => {
    // The whole finding. Without this, a train tunnel accumulates "proof"
    // that Occupella is down and the strip says so.
    reportBrowserOffline();
    failUntilDown();
    failUntilDown();
    expect(getServiceHealth().down).toBe(false);
    expect(getServiceHealth().failures).toBe(0);
  });

  it('clears failures the connection had already caused before the browser noticed', () => {
    // ⚠ The `offline` event can land AFTER the first requests have failed,
    // so those failures belong to the network. Leaving `down` standing would
    // swap one wrong sentence for another the moment the wifi came back.
    failUntilDown();
    expect(getServiceHealth().down).toBe(true);

    reportBrowserOffline();
    reportBrowserOnline();

    expect(getServiceHealth().down).toBe(false);
    expect(getServiceHealth().failures).toBe(0);
  });

  it('re-arms immediately — coming back online is not an amnesty', () => {
    // The clear above is a one-cycle concession, not a mute. If the backend
    // really is down, the next failures say so on the usual bar.
    reportBrowserOffline();
    reportBrowserOnline();
    failUntilDown();
    expect(getServiceHealth().down).toBe(true);
  });

  it('tells subscribers, once per change and not per report', () => {
    const seen: Array<[boolean, boolean]> = [];
    onServiceHealthChange((h) => seen.push([h.offline, h.down]));

    reportBrowserOffline();
    reportBrowserOffline(); // a repeat is not a change
    reportBrowserOnline();
    reportBrowserOnline(); // nor is a repeat of the recovery

    expect(seen).toEqual([
      [true, false],
      [false, false],
    ]);
  });
});

describe('the window events are actually wired', () => {
  // ⚠ A behavioural test of the handlers cannot see that nothing calls them —
  // the recurring failure in this repo is a correct helper reachable by no
  // one. Dispatching a REAL event through the window is what proves the
  // module-scope listeners exist, and it needs no source-grep to do it.
  afterEach(() => {
    window.dispatchEvent(new Event('online'));
    resetServiceHealth();
  });

  it("the browser's offline event reaches the shared state", () => {
    window.dispatchEvent(new Event('offline'));
    expect(getServiceHealth().offline).toBe(true);
  });

  it("the browser's online event clears it", () => {
    window.dispatchEvent(new Event('offline'));
    window.dispatchEvent(new Event('online'));
    expect(getServiceHealth().offline).toBe(false);
  });
});

describe('what a timed-out request tells the user', () => {
  it('blames the backend when we have no reason to blame the network', () => {
    expect(reachabilityAdvice()).toContain('backend');
    expect(reachabilityAdvice()).not.toContain('offline');
  });

  it('names the device once the browser says it has no network', () => {
    // ⚠ Same wrong-party claim as the banner's, in the sentence the user
    // reads FIRST — a timeout message reaches them before any strip does.
    reportBrowserOffline();
    expect(reachabilityAdvice()).toContain('offline');
    expect(reachabilityAdvice()).not.toContain('backend');
  });

  it('both timeout sites in apiFetch use it', () => {
    // ⚠ A behavioural test of this helper cannot see a caller that ignores
    // it, and there are TWO timeout paths — the inner fetch abort and the
    // outer deadline that fires when a deadlocked auth lookup never reaches
    // the fetch at all. One fixed and one forgotten is how the rule that
    // reached `send_sms` and not the CRM reply route got shipped.
    //
    // Comment lines are stripped so prose ABOUT the rule cannot stand in for
    // the rule, and `?raw` avoids needing node types in this tsconfig.
    const code = apiSource
      .split('\n')
      .filter((line: string) => {
        const t = line.trim();
        return !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*');
      })
      .join('\n');

    expect(code.split('reachabilityAdvice()').length - 1).toBe(2);
    expect(code).not.toContain('backend may be sleeping');
  });
});

describe('a page loaded while already offline', () => {
  // Listeners only hear about CHANGES, so a tab opened with no network would
  // otherwise believe it is connected until the wifi comes back and goes
  // again. The seed is read at import, so these re-import the module.
  function withOnLine(value: unknown, run: () => Promise<void>): Promise<void> {
    const original = Object.getOwnPropertyDescriptor(
      window.navigator,
      'onLine',
    );
    Object.defineProperty(window.navigator, 'onLine', {
      configurable: true,
      get: () => value,
    });
    return run().finally(() => {
      if (original) Object.defineProperty(window.navigator, 'onLine', original);
      else delete (window.navigator as { onLine?: unknown }).onLine;
    });
  }

  it('knows it, when the browser positively says so', async () => {
    await withOnLine(false, async () => {
      vi.resetModules();
      const fresh = await import('./serviceHealth');
      expect(fresh.getServiceHealth().offline).toBe(true);
    });
  });

  it('claims NOTHING when the browser does not answer', async () => {
    // ⚠ The distinguishing case, and the reason the seed reads `=== false`
    // rather than `!navigator.onLine`. Where the property is absent — an
    // embedded webview, a non-browser host — the loose spelling would tell
    // the user their connection is down on no evidence at all, which is the
    // exact wrong-party claim this whole split exists to stop.
    await withOnLine(undefined, async () => {
      vi.resetModules();
      const fresh = await import('./serviceHealth');
      expect(fresh.getServiceHealth().offline).toBe(false);
    });
  });
});
