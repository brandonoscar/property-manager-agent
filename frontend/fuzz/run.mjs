/**
 * UI-fuzz Playwright sweep (keyless).
 *
 * Loads the demo app with `?fuzz=<seed>` for a spread of seeds, walks the Inbox
 * (list + every row's detail card), and fails on real render breakage:
 *   - an uncaught page error (React error boundary / thrown render), OR
 *   - a browser console error, OR
 *   - an expected element missing (empty row list, or a selected row that
 *     renders no detail card).
 * Horizontal overflow is measured and reported as a soft signal (clipping is a
 * real bug class, but internal scroll containers make it noisy — so it warns,
 * it doesn't fail the run).
 *
 * Deterministic: seeds are BASE_SEED + 0..N-1, printed on every failure so a
 * break replays exactly by opening /?fuzz=<seed> by hand. Exits 1 on any hard
 * failure, 0 otherwise. Prints a machine-readable final line the routine +
 * Makefile parse:  [ui-fuzz] RESULT=PASS ...  /  [ui-fuzz] RESULT=FAIL ...
 *
 * Env:
 *   FUZZ_BASE_URL   default http://localhost:5173
 *   FUZZ_SEEDS      how many seeds to sweep (default 8)
 *   FUZZ_BASE_SEED  starting seed (default 1)
 *   FUZZ_SHOT_DIR   screenshot output dir (default ./fuzz/shots)
 *   FUZZ_CHROMIUM   chromium executable (default the pinned pw-browsers path)
 */
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const BASE_URL = process.env.FUZZ_BASE_URL || 'http://localhost:5173';
const SEED_COUNT = Number.parseInt(process.env.FUZZ_SEEDS || '8', 10);
const BASE_SEED = Number.parseInt(process.env.FUZZ_BASE_SEED || '1', 10);
const SHOT_DIR = process.env.FUZZ_SHOT_DIR || 'fuzz/shots';
const EXEC =
  process.env.FUZZ_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

mkdirSync(SHOT_DIR, { recursive: true });

// Console noise we never want to gate on. These are network/asset/dev chatter,
// orthogonal to render correctness — a missing favicon or an egress-blocked
// font is not a UI break. Real render breakage surfaces as a `pageerror`
// (thrown exception / React error boundary) or a genuine JS console.error,
// which we DO gate on.
const IGNORE_CONSOLE = [
  'Download the React DevTools',
  'React Router Future Flag',
  'Multiple GoTrueClient instances',
  'ERR_TUNNEL_CONNECTION_FAILED', // egress-blocked font/asset in this sandbox
  'Failed to load resource', // missing favicon / asset 404 — not a render bug
  'net::ERR_',
  '[vite]',
];
const ignorable = (t) => IGNORE_CONSOLE.some((s) => t.includes(s));

async function launch() {
  try {
    return await chromium.launch({ executablePath: EXEC, headless: true, args: ['--no-sandbox'] });
  } catch {
    // Fall back to whatever chromium Playwright can find (e.g. `channel`).
    return await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  }
}

const failures = [];
const warnings = [];

const browser = await launch();

for (let i = 0; i < SEED_COUNT; i++) {
  const seed = BASE_SEED + i;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(`pageerror: ${String(e).slice(0, 200)}`));
  page.on('console', (m) => {
    if (m.type() === 'error' && !ignorable(m.text())) {
      errors.push(`console: ${m.text().slice(0, 200)}`);
    }
  });

  const fail = async (reason) => {
    const shot = `${SHOT_DIR}/fuzz-seed-${seed}.png`;
    try {
      await page.screenshot({ path: shot });
    } catch {
      /* best-effort */
    }
    failures.push({ seed, reason, shot });
    console.log(`[ui-fuzz] seed=${seed} FAIL — ${reason} (shot: ${shot})`);
  };

  try {
    await page.goto(`${BASE_URL}/?fuzz=${seed}`, { waitUntil: 'networkidle', timeout: 30_000 });
    await page.waitForTimeout(800);

    // ⚠ NOT exact: the Inbox nav item's accessible name carries its unread
    // count ("Inbox, 4 waiting") so a screen reader announces it. Anchoring
    // on the start survives that; `exact` breaks the moment anything is
    // waiting, which is most of the time.
    await page.getByRole('button', { name: /^Inbox\b/ }).first().click();
    await page.waitForTimeout(700);

    const rows = page.locator('[data-testid="inbox-row-item"]');
    const rowCount = await rows.count();
    if (rowCount === 0) {
      await fail('inbox row list is empty with fuzz rows injected');
    } else {
      for (let r = 0; r < rowCount; r++) {
        await rows.nth(r).click();
        await page.waitForTimeout(400);
        // A selected row must render its detail card (EventHeader summary/grid).
        const header = page.locator('[data-testid="inbox-event-summary"], [data-testid="inbox-agent-noticed"], [data-testid="inbox-workflow-card"]');
        if ((await header.count()) === 0) {
          await fail(`row ${r} selected but no detail card rendered`);
          break;
        }
        if (errors.length) {
          await fail(`runtime error on row ${r}: ${errors[0]}`);
          break;
        }
      }
    }

    // Soft signal: body-level horizontal overflow = a clip/blowout somewhere.
    const overflow = await page.evaluate(() => {
      const el = document.scrollingElement || document.body;
      return el.scrollWidth - window.innerWidth;
    });
    if (overflow > 16) {
      warnings.push({ seed, overflow });
      console.log(`[ui-fuzz] seed=${seed} WARN — horizontal overflow ${overflow}px`);
    }

    if (errors.length && !failures.some((f) => f.seed === seed)) {
      await fail(`runtime error: ${errors[0]}`);
    }
    if (!failures.some((f) => f.seed === seed)) {
      console.log(`[ui-fuzz] seed=${seed} ok (${rowCount} rows)`);
    }
  } catch (e) {
    await fail(`harness/navigation error: ${String(e).slice(0, 200)}`);
  } finally {
    await page.close();
  }
}

await browser.close();

const summary =
  `[ui-fuzz] RESULT=${failures.length ? 'FAIL' : 'PASS'} ` +
  `seeds=${BASE_SEED}..${BASE_SEED + SEED_COUNT - 1} ` +
  `failures=${failures.length} warnings=${warnings.length}`;
console.log(summary);
if (failures.length) {
  console.log('[ui-fuzz] failing seeds:', failures.map((f) => f.seed).join(', '));
  process.exit(1);
}
process.exit(0);
