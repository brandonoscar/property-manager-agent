/**
 * Viewport audit — layout and touch targets, measured in a real browser.
 *
 * Five audit rows said the same thing in different words: "CSS is responsive;
 * never tested on a real phone", "not audited", "the sidebar collapses, there
 * is no mobile-specific pattern". This answers them with numbers, and keeps
 * answering them, because a one-off measurement expires the moment somebody
 * adds a control (gotcha 48's shape: a decision taken from a measurement needs
 * something that re-measures).
 *
 * jsdom cannot do this job at all — it has no layout engine, so every
 * `getBoundingClientRect()` returns zeros and every element passes. That is
 * why this is a Playwright harness and not a vitest file.
 *
 * It GATES on two things, both of which are defects a user feels:
 *
 *   1. Horizontal overflow of the document. A page that pans sideways hides
 *      the right edge of every control on it. Wide content (the 22-column
 *      Operations table) is expected to scroll INSIDE its own container —
 *      that is a pass, and the harness distinguishes the two by measuring
 *      the document rather than the table.
 *   2. An on-screen interactive control smaller than 24x24 (WCAG 2.2 AA,
 *      2.5.8 Target Size Minimum). Not the 44px AAA guideline, which this
 *      product does not claim.
 *
 * ⚠ ON-SCREEN is load-bearing, and getting it wrong is how the first version
 * of this script produced a confidently false headline. The mobile nav drawer
 * lives at `left: -260px` when closed: it is in the DOM at full width, its
 * `innerText` reads normally, and its buttons measure fine — while being
 * entirely off the screen. Anything measuring mobile layout has to ask where
 * an element IS, not how big it is.
 *
 * Env:
 *   VIEWPORT_BASE_URL   default http://localhost:5173
 *   VIEWPORT_CHROMIUM   chromium executable (default the pinned pw path)
 */
import { chromium } from 'playwright';

const BASE_URL = process.env.VIEWPORT_BASE_URL || 'http://localhost:5173';
const EXEC =
  process.env.VIEWPORT_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

/** WCAG 2.2 AA 2.5.8. The 44px AAA guideline is not the bar here. */
const MIN_TARGET_PX = 24;

const PAGES = ['Chat', 'Inbox', 'Operations', 'Leasing', 'Settings'];

const VIEWPORTS = [
  { name: 'phone     375x667', width: 375, height: 667, mobile: true },
  { name: 'landscape 667x375', width: 667, height: 375, mobile: true },
  // 200% browser zoom on a 1280-wide screen is 640 CSS px — WCAG 1.4.4's
  // Resize Text, which its own understanding document says zoom satisfies.
  // Named explicitly rather than left implied by the phone entry, because
  // "we test 375 so 640 is covered" is an inference and this is a criterion.
  { name: 'zoom-200%  640x400', width: 640, height: 400, mobile: false },
  { name: 'tablet    768x1024', width: 768, height: 1024, mobile: true },
  { name: 'desktop  1440x900', width: 1440, height: 900, mobile: false },
];

const PROBE = (minPx) => {
  const doc = document.documentElement;
  const vw = doc.clientWidth;

  const overflow = doc.scrollWidth - vw;
  const past = [];
  if (overflow > 0) {
    for (const el of document.querySelectorAll('*')) {
      const b = el.getBoundingClientRect();
      if (b.width > 0 && b.right > vw + 1) {
        past.push({
          tag: el.tagName.toLowerCase(),
          id: el.getAttribute('data-testid') || String(el.className || '').slice(0, 40),
          right: Math.round(b.right),
        });
      }
    }
  }

  const sel =
    'button, a[href], input, select, textarea, [role="button"], [tabindex]:not([tabindex="-1"])';
  const small = [];
  let onscreen = 0;
  for (const el of document.querySelectorAll(sel)) {
    const b = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (!b.width || !b.height || st.visibility === 'hidden' || st.display === 'none') continue;
    // Off-screen (the closed nav drawer) and scrolled-past are not targets.
    if (b.right <= 0 || b.left >= vw || b.bottom <= 0) continue;
    onscreen++;
    if (b.width < minPx || b.height < minPx) {
      small.push({
        id: el.getAttribute('data-testid') || el.tagName.toLowerCase(),
        label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 28),
        size: `${Math.round(b.width)}x${Math.round(b.height)}`,
      });
    }
  }

  const aside = document.querySelector('aside[role="dialog"]');
  return {
    vw,
    drawerLeft: aside ? Math.round(aside.getBoundingClientRect().left) : null,
    overflow,
    past: past.slice(0, 8),
    onscreen,
    small,
  };
};

const browser = await chromium.launch({ executablePath: EXEC });
const failures = [];

for (const v of VIEWPORTS) {
  const ctx = await browser.newContext({
    viewport: { width: v.width, height: v.height },
    isMobile: v.mobile,
    hasTouch: v.mobile,
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(800);

  const hamburger = page.getByTestId('titlebar-open-nav');
  const mobileNav = (await hamburger.count()) > 0 && (await hamburger.isVisible());
  console.log(`\n[viewports] ${v.name}  mobile-nav=${mobileNav}`);

  for (const name of PAGES) {
    if (mobileNav) {
      await hamburger.click().catch(() => {});
      await page.waitForTimeout(300);
    }
    // ⚠ NOT exact — a nav item's accessible name can carry a badge count
    // ("Inbox, 4 waiting"), which is deliberate: it is the only way a
    // screen reader hears that anything is waiting.
    const link = page.getByRole('button', { name: new RegExp(`^${name}\\b`) }).first();
    if (await link.count()) {
      await link.click().catch(() => {});
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(600);
    }

    const r = await page.evaluate(PROBE, MIN_TARGET_PX);
    const where = `${v.name} ${name}`;

    if (r.overflow > 0) {
      failures.push(`${where}: document scrolls ${r.overflow}px sideways`);
      for (const p of r.past) console.log(`    past the edge: ${p.tag} ${p.id} right=${p.right}`);
    }
    for (const s of r.small) {
      failures.push(`${where}: ${s.id} "${s.label}" is ${s.size}, under ${MIN_TARGET_PX}x${MIN_TARGET_PX}`);
    }

    // A mobile drawer that stays open after a destination is chosen leaves
    // the menu covering the answer — reported, not gated, because it depends
    // on the click above having actually landed.
    if (mobileNav && r.drawerLeft !== null && r.drawerLeft >= 0) {
      console.log(`    note: nav drawer still open after choosing ${name}`);
    }
    console.log(
      `  ${name.padEnd(11)} overflow=${r.overflow}px  controls=${r.onscreen}  under-${MIN_TARGET_PX}=${r.small.length}`,
    );
  }
  await ctx.close();
}

// ── Rotation ──────────────────────────────────────────────────────────
//
// Two cold loads at two sizes do NOT test rotation — the interesting state is
// the one that survives the transition. Found exactly this way: open the menu
// at 375, turn the handset (many phones are wider than 768 in landscape, so
// the drawer correctly becomes the permanent rail), turn it back, and the
// drawer was still `open` — now an overlay covering the app, which the user
// never asked for and does not remember opening.
{
  const ctx = await browser.newContext({
    viewport: { width: 375, height: 667 },
    isMobile: true,
    hasTouch: true,
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 140)));
  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);

  const drawerLeft = () =>
    page.evaluate(() => {
      const a = document.querySelector('aside[role="dialog"]');
      return a ? Math.round(a.getBoundingClientRect().left) : null;
    });

  await page.getByTestId('titlebar-open-nav').click();
  await page.waitForTimeout(400);
  const opened = await drawerLeft();

  await page.setViewportSize({ width: 844, height: 390 });
  await page.waitForTimeout(600);
  await page.setViewportSize({ width: 375, height: 667 });
  await page.waitForTimeout(600);
  const afterRotation = await drawerLeft();

  console.log(
    `\n[viewports] rotation: drawer left ${opened} when opened, ${afterRotation} after a round trip through landscape`,
  );
  if (opened !== 0) {
    failures.push('rotation: the nav drawer did not open at 375px, so the check proved nothing');
  } else if (afterRotation === null || afterRotation >= 0) {
    failures.push(
      'rotation: the nav drawer is still open after rotating out of mobile and back — ' +
        'the user gets a menu covering the app that they never opened',
    );
  }
  if (errors.length) failures.push(`rotation: page error — ${errors[0]}`);
  await ctx.close();
}

// ── Tab order ─────────────────────────────────────────────────────────
//
// Two properties jsdom structurally cannot check, because it has no layout:
// where focus actually LANDS, and whether the thing it landed on is on the
// screen. Both were real (`src/components/tabOrder.test.tsx` records what was
// measured): stop #1 was the sidebar's Chat button on every page, and on a
// phone the first NINE stops were the closed drawer at `left: -260px` — Tab
// appearing to do nothing nine times, with Enter able to fire a control
// nobody can see.
//
// ⚠ Only the phone viewport is gated for off-screen stops. Above the
// breakpoint that same <aside> is the permanent rail and its controls are
// legitimately on screen; a blanket rule would have nothing to say there.
for (const v of [
  { name: 'desktop  1440x900', width: 1440, height: 900, mobile: false },
  { name: 'phone     375x667', width: 375, height: 667, mobile: true },
]) {
  const ctx = await browser.newContext({
    viewport: { width: v.width, height: v.height },
    isMobile: v.mobile,
    hasTouch: v.mobile,
  });
  const page = await ctx.newPage();
  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(1000);

  // Start from the document, so the first Tab lands on the first stop the
  // browser would give a person who just loaded the page.
  await page.evaluate(() => document.body.focus());

  const READ = () => {
    const el = document.activeElement;
    if (!el || el === document.body) return null;
    const b = el.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    return {
      id: el.getAttribute('data-testid') || el.tagName.toLowerCase(),
      label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24),
      top: Math.round(b.top),
      // ⚠ A skip link is off screen UNTIL focused — that IS the technique —
      // so it is measured while focused, which is exactly when it has to be
      // visible.
      offscreen: b.right <= 0 || b.left >= vw || b.bottom <= 0 || b.top >= vh,
    };
  };

  const stops = [];
  const MAX = 14;
  for (let i = 0; i < MAX; i += 1) {
    await page.keyboard.press('Tab');
    // ⚠ Read the RESTING position, not the first frame after the keypress.
    // The skip link animates in (`transition: top var(--dur-state)`), so an
    // immediate read catches it mid-flight at top ≈ -100 and reports the
    // working control as off screen — which is what the first version of this
    // block did on both viewports. Polling until two reads agree is
    // self-adjusting: raising the duration token cannot turn it into a false
    // alarm, which a fixed sleep would.
    let stop = await page.evaluate(READ);
    for (let settle = 0; settle < 8 && stop; settle += 1) {
      await page.waitForTimeout(60);
      const again = await page.evaluate(READ);
      if (again && again.top === stop.top) { stop = again; break; }
      stop = again;
    }
    if (!stop) break;
    stops.push(stop);
  }

  console.log(`\n[viewports] tab order ${v.name}: ${stops.length} stops`);
  for (const [i, s] of stops.entries()) {
    console.log(`  ${String(i + 1).padStart(2)}. ${s.id.padEnd(22)} ${s.offscreen ? 'OFFSCREEN' : ''} ${s.label}`);
  }

  if (!stops.length) {
    failures.push(`tab order ${v.name}: Tab reached nothing, so this check proved nothing`);
  } else if (stops[0].id !== 'skip-to-content') {
    failures.push(
      `tab order ${v.name}: the first stop is "${stops[0].id}", not the skip link — ` +
        'a keyboard user walks the whole nav before reaching the page, on every page',
    );
  }
  if (v.mobile) {
    for (const s of stops.filter((s) => s.offscreen)) {
      failures.push(
        `tab order ${v.name}: focus lands OFF SCREEN on ${s.id} — ` +
          'Tab appears to do nothing and Enter fires a control nobody can see',
      );
    }
  }
  await ctx.close();
}

await browser.close();

console.log(
  `\n[viewports] RESULT=${failures.length ? 'FAIL' : 'PASS'} ` +
    `viewports=${VIEWPORTS.length} pages=${PAGES.length} failures=${failures.length}`,
);
for (const f of failures) console.log(`[viewports]   ${f}`);
process.exit(failures.length ? 1 : 0);
