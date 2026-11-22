/**
 * Where Tab goes first, and where it must not go at all.
 *
 * Two defects, both measured by tabbing the running app in Chromium rather
 * than found by review, because neither is visible in the source:
 *
 *   1. **The skip link was in the wrong render branch.** The only
 *      `<a href="#main" class="skip-link">` sat inside the "Supabase not
 *      configured" error screen — a page with no sidebar and no `#main` to
 *      land on. The app render had none, so tab stop #1 was the sidebar's
 *      Chat button and a keyboard user walked ~20 nav controls before
 *      reaching the page, on every page, every time. The comment on the
 *      dead copy said "it is the first stop for everyone else", which is
 *      exactly the guarantee the product did not have.
 *   2. **A closed mobile drawer stayed in the tab order.** On a phone the
 *      first NINE stops were at `left: -260px` — Tab appeared to do nothing
 *      nine times, and Enter could fire a control nobody could see.
 *
 * ⚠ The dangerous direction on (2) is the opposite one. Above the breakpoint
 * the same `<aside>` is the PERMANENT rail; marking that inert makes the whole
 * navigation unreachable by keyboard, which is far worse than the bug being
 * fixed. So both states are pinned, not just the new one.
 *
 * ⚠ jsdom has no layout engine, so it cannot see WHERE an element is — the
 * off-screen half of finding (2) was only measurable in a real browser
 * (`fuzz/viewports.mjs`, which gates on stop #1 and on off-screen stops).
 * What jsdom CAN see is document order and the attributes, which is what
 * these guards read.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  return { ...actual, demoMode: true };
});
// Healthy on both axes — the banner must stay out of the way of a test about
// tab order. Stated in full rather than relying on `undefined` being falsy.
vi.mock('../services/serviceHealth', () => ({
  getServiceHealth: () => ({ down: false, offline: false, failures: 0 }),
  onServiceHealthChange: () => () => {},
}));

// jsdom does not implement scrolling, and ChatThread scrolls to the bottom on
// mount. Without this the App render dies in an effect far from anything this
// file is about.
if (!Element.prototype.scrollTo) Element.prototype.scrollTo = () => {};

import { App } from '../App';
import { Sidebar } from './Sidebar';
import { BOOT_MIN_MS } from './BootScreen';

const FOCUSABLE =
  'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

/** Media-query stub whose answer to the mobile breakpoint we control. */
function setViewport(mobile: boolean) {
  window.matchMedia = ((query: string) =>
    ({
      matches: query.includes('prefers-reduced-motion')
        ? true
        : query.includes('max-width: 767px') && mobile,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
}

const realMatchMedia = window.matchMedia;
afterEach(() => {
  window.matchMedia = realMatchMedia;
});

/**
 * Render the app and wait for the SIGNED-IN screen.
 *
 * ⚠ Not optional, and the reason is instructive: `App` shows an auth-loading
 * spinner first, so a synchronous assertion races the auth check. The first
 * version of this file did exactly that — one test happened to win the race
 * and two lost it, which reads as "the skip link is missing" rather than "the
 * app has not finished mounting". Waiting on `#main` is independent of the
 * thing under test, so it cannot make these assertions pass by itself.
 */
async function renderApp() {
  const view = render(<App />);
  // ⚠ The wait has to OUTLAST the boot screen, and it has to be derived from
  // `BOOT_MIN_MS` rather than a number typed here. Raising the hold 700 →
  // 1400ms broke this file against `waitFor`'s 1000ms default, and the failure
  // read as "the app rendered no focusable controls at all" — a message about
  // accessibility, on a change about a splash screen. A hardcoded 3000 would
  // fix today and break again the next time somebody moves the constant.
  await waitFor(() => expect(document.getElementById('main')).toBeTruthy(), {
    timeout: BOOT_MIN_MS + 2000,
  });
  return view;
}

describe('the skip link', () => {
  beforeEach(() => setViewport(false));

  it('is the FIRST thing Tab reaches in the signed-in app', async () => {
    const { container } = await renderApp();
    const first = container.querySelectorAll<HTMLElement>(FOCUSABLE)[0];
    expect(first, 'the app rendered no focusable controls at all').toBeTruthy();
    expect(first.getAttribute('data-testid')).toBe('skip-to-content');
  });

  it('points at a target that exists and can actually take focus', async () => {
    // ⚠ Both halves matter. A skip link whose `#main` is absent scrolls
    // nowhere; one whose target is not focusable moves the VIEWPORT and
    // leaves focus behind in the nav, so the next Tab resumes where it was —
    // the link appears to work and changes nothing.
    await renderApp();
    expect(screen.getByTestId('skip-to-content').getAttribute('href')).toBe('#main');

    const main = document.getElementById('main');
    expect(main?.tagName.toLowerCase()).toBe('main');
    expect(main?.getAttribute('tabindex')).toBe('-1');
  });

  it('exists exactly once, so Tab does not meet two of them', async () => {
    const { container } = await renderApp();
    expect(container.querySelectorAll('.skip-link')).toHaveLength(1);
  });
});

describe('tab order inside a form', () => {
  // ⚠ A CORPUS guard, not a behavioural one, because the property is about
  // what the code must never contain. A positive `tabIndex` is the only thing
  // that can make field order differ from reading order: it jumps the element
  // to the FRONT of the whole page's tab sequence, ahead of every natural
  // stop, so one of them re-orders every form on the screen it appears on.
  // Today there are zero, which is why field order is simply DOM order
  // everywhere — a fact worth pinning rather than re-measuring per form.
  const SOURCES = import.meta.glob('../**/*.tsx', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>;

  /**
   * Every number written inside a `tabIndex={…}` expression.
   *
   * ⚠ Not `tabIndex=\{(-?\d+)\}`. That was the first cut and a mutation walked
   * straight past it: `tabIndex={onRowClick ? 3 : undefined}` is a positive
   * tab stop and matches no bare-literal pattern. The braces are walked and
   * every integer in the expression is read, because the harmful value can
   * sit on either arm of a conditional.
   */
  function tabIndexValues(source: string): number[] {
    const out: number[] = [];
    for (let at = source.indexOf('tabIndex={'); at !== -1; at = source.indexOf('tabIndex={', at + 1)) {
      let depth = 0;
      let end = at;
      for (let i = source.indexOf('{', at); i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
          depth -= 1;
          if (depth === 0) { end = i; break; }
        }
      }
      for (const m of source.slice(at, end).matchAll(/-?\d+/g)) out.push(Number(m[0]));
    }
    return out;
  }

  it('is DOM order, because nothing anywhere sets a positive tabIndex', () => {
    const offenders: string[] = [];
    for (const [file, source] of Object.entries(SOURCES)) {
      if (file.includes('.test.')) continue;
      for (const v of tabIndexValues(source)) {
        if (v > 0) offenders.push(`${file}: tabIndex carries ${v}`);
      }
    }
    expect(offenders, 'a positive tabIndex re-orders every form on its screen').toEqual([]);
  });

  it('reads both arms of a conditional, and a real corpus', () => {
    // ⚠ Two self-checks on one guard whose failure mode is "silently permits
    // more". A bad glob scanning nothing looks exactly like zero offenders,
    // and a reader that only sees bare literals looks exactly like a corpus
    // with none — which is the state this guard shipped in for one revision.
    expect(tabIndexValues('<tr tabIndex={onRowClick ? 3 : undefined}>')).toContain(3);
    expect(tabIndexValues('<div tabIndex={-1}>')).toEqual([-1]);

    const scanned = Object.keys(SOURCES).filter((f) => !f.includes('.test.'));
    expect(scanned.length).toBeGreaterThan(50);
    expect(scanned.some((f) => f.endsWith('/App.tsx'))).toBe(true);
  });
});

describe('the mobile nav drawer', () => {
  it('is out of the tab order while it is closed', () => {
    // The whole subtree, not each control: `inert` also drops it from the
    // accessibility tree, so a screen reader does not read a menu that is
    // 260px off the left edge either.
    setViewport(true);
    const { container } = render(<Sidebar open={false} onClose={() => {}} />);
    const aside = container.querySelector('aside[role="dialog"]');
    expect(aside).toHaveAttribute('inert');
  });

  it('is reachable again the moment it opens', () => {
    setViewport(true);
    const { container } = render(<Sidebar open={true} onClose={() => {}} />);
    expect(container.querySelector('aside[role="dialog"]')).not.toHaveAttribute('inert');
  });

  it('NEVER goes inert on desktop, where it is the permanent rail', () => {
    // ⚠ This is the guard that matters most. The mobile fix keyed on `open`
    // alone would make every desktop session keyboard-navigable to nothing —
    // a worse bug than the one it fixes, and one no mobile test can see.
    setViewport(false);
    const { container } = render(<Sidebar open={false} onClose={() => {}} />);
    expect(container.querySelector('aside[role="dialog"]')).not.toHaveAttribute('inert');
  });
});
