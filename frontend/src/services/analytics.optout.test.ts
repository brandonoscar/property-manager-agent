/**
 * The switch that turns product analytics off, and the two ways it could be
 * theatre.
 *
 * ⚠ It has to be read BEFORE `posthog.init`, not after. Session recording,
 * autocapture and performance capture all start inside `init()`, so opting out
 * afterwards still ships the load of the page somebody opted out on — a
 * control that misses exactly the event it was clicked to prevent.
 *
 * ⚠ And it has to apply to the CURRENT tab, not just the next one. Writing the
 * key and waiting for a reload means the person watches a toggle move and gets
 * measured anyway, which is worse than not offering the toggle.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const optOut = vi.hoisted(() => vi.fn());
const optIn = vi.hoisted(() => vi.fn());
const init = vi.hoisted(() => vi.fn());

vi.mock('posthog-js', () => ({
  default: {
    init,
    opt_out_capturing: optOut,
    opt_in_capturing: optIn,
    identify: vi.fn(),
    group: vi.fn(),
    capture: vi.fn(),
    reset: vi.fn(),
  },
}));

import { analyticsOptedOut, setAnalyticsOptedOut } from './analytics';

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe('the opt-out preference', () => {
  it('defaults to measuring — the policy documents that, so silence must match it', () => {
    expect(analyticsOptedOut()).toBe(false);
  });

  it('remembers a decision to opt out, and to come back', () => {
    setAnalyticsOptedOut(true);
    expect(analyticsOptedOut()).toBe(true);
    setAnalyticsOptedOut(false);
    expect(analyticsOptedOut()).toBe(false);
  });

  it('stops the CURRENT session, not merely the next one', async () => {
    // ⚠ Needs a CONFIGURED module. `posthogConfigured` is computed once at
    // import from `VITE_PUBLIC_POSTHOG_KEY`, which vitest does not set — so
    // the statically-imported copy above correctly does nothing, and a test
    // written against it would assert the no-analytics path while claiming to
    // cover the live one. Stub the env, reset the registry, re-import.
    vi.stubEnv('VITE_PUBLIC_POSTHOG_KEY', 'phc_test');
    vi.resetModules();
    const live = await import('./analytics');
    live.setAnalyticsOptedOut(true);
    expect(optOut).toHaveBeenCalled();
    live.setAnalyticsOptedOut(false);
    expect(optIn).toHaveBeenCalled();
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('does nothing to PostHog when analytics is not configured at all', () => {
    // The other side of that branch: local dev and any deploy without a key.
    // Calling opt_out on an uninitialised client is not harmless — posthog-js
    // warns and the toggle would appear to act on something that isn't there.
    setAnalyticsOptedOut(true);
    expect(optOut).not.toHaveBeenCalled();
    expect(analyticsOptedOut()).toBe(true);
  });

  it('keeps its own key rather than riding PostHog\'s storage', () => {
    // PostHog stores an opt-out in the same entry it uses for everything else,
    // so clearing site data silently opts a person back IN. The decision is
    // the one thing here that must outlive a cleanup of the tracking state.
    setAnalyticsOptedOut(true);
    expect(localStorage.getItem('occupella_analytics_opt_out')).toBe('1');
  });

  it('reads an unreadable store as NO PREFERENCE, not as an opt-out', () => {
    // A private window or a browser blocking storage cannot record a choice
    // either way. Reporting that as "opted out" would quietly turn analytics
    // off for a population that never asked — and the honest default is the
    // one the privacy policy describes. The control is still in Settings.
    const real = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error('blocked');
    };
    try {
      expect(analyticsOptedOut()).toBe(false);
    } finally {
      Storage.prototype.getItem = real;
    }
  });
});

describe('what the product promises about it', () => {
  const SOURCES = import.meta.glob('./analytics.ts', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>;
  const PRIVACY = import.meta.glob('/public/legal/privacy.html', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>;

  const analytics = Object.values(SOURCES)[0] ?? '';
  const privacy = Object.values(PRIVACY)[0] ?? '';

  it('is reading real sources', () => {
    expect(analytics.length).toBeGreaterThan(500);
    expect(privacy.length).toBeGreaterThan(2000);
  });

  it('passes the preference into init, so capture never starts', () => {
    // A behavioural test cannot see this: `init` is called once at module load
    // in main.tsx, and by the time any assertion runs the decision is made.
    expect(analytics).toMatch(/opt_out_capturing_by_default:\s*analyticsOptedOut\(\)/);
  });

  it('recordings still mask everything on screen', () => {
    // The opt-out is the second line of defence. The first is that a recording
    // carries page STRUCTURE and no text — which is what makes an opt-out
    // (rather than a consent wall) the proportionate control, and what the
    // privacy page now says out loud.
    expect(analytics).toMatch(/mask_all_text:\s*true/);
    expect(analytics).toMatch(/mask_all_element_attributes:\s*true/);
  });

  it('the privacy policy names the storage AND where to turn it off', () => {
    // ⚠ The corpus half. A control nobody is told about is not a choice, and a
    // policy describing a control that moved is worse than one that describes
    // none. Both halves are asserted here so they cannot drift apart quietly.
    expect(privacy).toMatch(/local storage/i);
    expect(privacy.match(/Product analytics/g) ?? []).toHaveLength(2);
    expect(privacy).toMatch(/never the text on the screen/i);
  });
});
