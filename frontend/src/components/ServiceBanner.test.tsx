/**
 * The "we can't reach the server" strip, and the fact that something
 * renders it.
 *
 * ⚠ The mount check is not padding. A perfectly correct banner that no page
 * renders is this repo's recurring failure — complete, unit-tested, and
 * wired to nothing — and a behavioural test of the component structurally
 * cannot see it.
 */

import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), message: vi.fn() },
}));

import { toast } from 'sonner';

// Vite's ?raw gives the file's text without needing node types in this
// tsconfig — the mechanism TrialBanner.test.tsx already uses for App.tsx.
import appSource from '../App.tsx?raw';
import { ServiceBanner } from './ServiceBanner';
import {
  CONSECUTIVE_FAILURES_FOR_OUTAGE,
  reportBrowserOffline,
  reportBrowserOnline,
  reportServiceFailure,
  reportServiceReachable,
  resetServiceHealth,
} from '../services/serviceHealth';

function goDown(): void {
  act(() => {
    for (let i = 0; i < CONSECUTIVE_FAILURES_FOR_OUTAGE; i += 1) {
      reportServiceFailure();
    }
  });
}

/** Straight-quote the RENDERED text — the component writes `&rsquo;`. */
function bannerText(): string {
  return (screen.getByTestId('service-banner').textContent ?? '')
    .toLowerCase()
    .replace(/[‘’]/g, "'");
}

beforeEach(() => {
  vi.clearAllMocks();
  resetServiceHealth();
});

describe('the banner', () => {
  it('renders nothing while the backend is answering', () => {
    render(<ServiceBanner />);
    expect(screen.queryByTestId('service-banner')).toBeNull();
  });

  it('renders nothing after a single failure', () => {
    render(<ServiceBanner />);
    act(() => reportServiceFailure());
    expect(screen.queryByTestId('service-banner')).toBeNull();
  });

  it('appears once the failures repeat', () => {
    render(<ServiceBanner />);
    goDown();
    expect(screen.getByTestId('service-banner')).toBeTruthy();
  });

  it('disappears the moment something answers again', () => {
    render(<ServiceBanner />);
    goDown();
    act(() => reportServiceReachable());
    expect(screen.queryByTestId('service-banner')).toBeNull();
  });

  it('picks up an outage that started before it mounted', () => {
    // Listeners are told about CHANGES only, so a banner mounted mid-outage
    // would otherwise sit silent through the whole thing.
    goDown();
    render(<ServiceBanner />);
    expect(screen.getByTestId('service-banner')).toBeTruthy();
  });

  it('says the visible data is still good', () => {
    // ⚠ A strip, not a takeover. Everything on screen was fetched while the
    // backend was up and is still worth reading; the honest message is that
    // new changes will not save, not that the screen is wrong.
    render(<ServiceBanner />);
    goDown();
    // ⚠ Straight-quote the RENDERED text first. The component writes
    // `&rsquo;`, which renders as a curly apostrophe, so a scan for "won't"
    // misses a sentence that plainly says it — the same trap as checking a
    // required phrase against markup instead of what a reader sees.
    const text = (screen.getByTestId('service-banner').textContent ?? '')
      .toLowerCase()
      .replace(/[\u2018\u2019]/g, "'");
    expect(text).toContain('still accurate');
    expect(text).toContain("won't save");
  });
});

describe('it names the right party', () => {
  it('blames Occupella when Occupella is the one not answering', () => {
    render(<ServiceBanner />);
    goDown();
    expect(bannerText()).toContain('occupella');
    expect(screen.getByTestId('service-banner').dataset.reachability).toBe(
      'down',
    );
  });

  it('names the DEVICE when the browser says the network is gone', () => {
    // ⚠ The finding. `fetch` fails identically either way, so this strip used
    // to tell somebody on a dying hotel connection that our product was
    // broken — a claim about us, made on their network's evidence. They can
    // check this sentence against their wifi icon; they cannot check the
    // other one at all.
    render(<ServiceBanner />);
    act(() => reportBrowserOffline());

    const text = bannerText();
    expect(text).toContain("you're offline");
    expect(text).not.toContain('occupella');
    expect(screen.getByTestId('service-banner').dataset.reachability).toBe(
      'offline',
    );
  });

  it('shows on ONE offline report — no consecutive bar', () => {
    // Unlike the backend arm, which INFERS an outage from repeated failures.
    // Pinned because "make it consistent with the other branch" is the
    // tempting wrong change here.
    render(<ServiceBanner />);
    act(() => reportBrowserOffline());
    expect(screen.getByTestId('service-banner')).toBeTruthy();
  });

  it('offline WINS when both look true', () => {
    // While the device has no network we cannot know whether the backend is
    // up, so saying it is down puts our name on their outage.
    render(<ServiceBanner />);
    goDown();
    act(() => reportBrowserOffline());
    expect(bannerText()).toContain("you're offline");
    expect(bannerText()).not.toContain('occupella');
  });

  it('clears when the connection returns', () => {
    render(<ServiceBanner />);
    act(() => reportBrowserOffline());
    act(() => reportBrowserOnline());
    expect(screen.queryByTestId('service-banner')).toBeNull();
  });
});

describe('something actually renders it', () => {
  // Comment lines are stripped so prose ABOUT the banner cannot stand in for
  // the banner — the trap a substring guard falls into every time.
  const code = appSource
    .split('\n')
    .filter((line: string) => {
      const t = line.trim();
      return !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*');
    })
    .join('\n');

  it('App imports and renders the banner', () => {
    expect(code).toContain("from './components/ServiceBanner'");
    expect(code).toContain('<ServiceBanner');
  });

  it('renders it ABOVE the trial bar', () => {
    // While the backend is unreachable nothing the trial bar asks for can be
    // done — you cannot pay, invite, or save — so it reads first.
    const banner = code.indexOf('<ServiceBanner');
    const trial = code.indexOf('<TrialBanner');
    expect(banner).toBeGreaterThan(-1);
    expect(trial).toBeGreaterThan(-1);
    expect(banner).toBeLessThan(trial);
  });
});

describe('coming back', () => {
  it('says so, instead of just disappearing', () => {
    // ⚠ The strip vanishing is not a signal. It is the same thing that
    // happens on a reload or a scroll, so somebody who stopped working
    // because their changes would not save had no way to know they could
    // start again — they find out by trying, which is what the banner
    // existed to spare them.
    render(<ServiceBanner />);
    goDown();
    expect(screen.getByTestId('service-banner')).toBeTruthy();

    act(() => reportServiceReachable());

    expect(screen.queryByTestId('service-banner')).toBeNull();
    // ⚠ RE-POINTED, not rewritten to taste. This asserted "Back online",
    // which was right about the property and is now the wording of the OTHER
    // recovery — the user's network coming back. A recovery toast that names
    // the wrong party is the same defect as a banner that does.
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
      expect.stringContaining('server is back'),
      expect.anything(),
    );
  });

  it('announces the NETWORK coming back in its own words', () => {
    // Two causes, two recoveries, two sentences. Telling somebody whose wifi
    // just returned that "Occupella's server is back" implies an outage that
    // never happened on our side.
    render(<ServiceBanner />);
    act(() => reportBrowserOffline());
    act(() => reportBrowserOnline());
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
      expect.stringContaining('back online'),
      expect.anything(),
    );
    expect(vi.mocked(toast.success)).not.toHaveBeenCalledWith(
      expect.stringContaining('server is back'),
      expect.anything(),
    );
  });

  it('stays quiet when nothing changed', () => {
    // ⚠ The banner now ALSO tracks the previous state, because with two
    // causes "arriving healthy" no longer says which recovery just ended. So
    // this is no longer the only thing standing between a healthy poll and a
    // toast — but it is still worth pinning: a bus that emits on every report
    // is how a notification becomes noise people dismiss reflexively.
    render(<ServiceBanner />);
    act(() => reportServiceReachable());
    expect(vi.mocked(toast.success)).not.toHaveBeenCalled();
  });

  it('announces nothing when there was no outage to recover from', () => {
    // ⚠ Found by mutation, not review. One failed request does not reach the
    // bar, so the strip never appears — but the bus DOES emit on the success
    // that follows it (the failure count changed), and a banner that toasts
    // on any healthy emission tells the user their service just came back
    // from an outage they never had. Announcing a recovery that did not
    // happen is the same wrong claim as the banner's, wearing a nicer face.
    render(<ServiceBanner />);
    act(() => reportServiceFailure());
    act(() => reportServiceReachable());
    expect(vi.mocked(toast.success)).not.toHaveBeenCalled();
  });

  it('announces once per outage, not once per report', () => {
    render(<ServiceBanner />);
    goDown();
    act(() => reportServiceReachable());
    act(() => reportServiceReachable());
    expect(vi.mocked(toast.success)).toHaveBeenCalledTimes(1);
  });
});
