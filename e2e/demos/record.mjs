/**
 * Record the landing-page demo clips against the DEPLOYED app.
 *
 *     npm install && npm run login   # once — you sign in, session is saved
 *     npm run record                 # capture frames
 *     npm run render                 # encode -> out/final/*.mp4 + review page
 *
 * ⚠ **IT NEVER APPROVES A WRITE.** A confirm gate is the most valuable frame
 * in the set — it is the proof the product acts and still cannot act alone —
 * so the recorder holds on it, then declines. The APPROVE button's test id
 * appears nowhere in this file on purpose, and a unit test asserts exactly
 * that, because "the demo filed a real work order into a customer's Buildium"
 * is not a mistake you get to make twice.
 *
 * ⚠ That invariant is "the literal is absent from the whole file", which means
 * this comment must NOT quote it. Naming the forbidden string in the sentence
 * forbidding it is how a guard fires on its own documentation (CLAUDE.md
 * gotcha 40), and teaching the guard to skip comments is what makes it blind.
 *
 * ⚠ **It runs against production with real data.** Watch every clip before
 * publishing it. The review page exists to force exactly that.
 *
 * ── why this captures frames instead of using the built-in recorder ─
 *
 * Playwright's built-in video option is hardcoded to a low-bitrate realtime
 * VP8 preset. It is right for CI artifacts and wrong for anything a customer
 * watches: text picks up visible mosquito noise and no option changes it.
 *
 * ⚠ Its NAME is deliberately not written anywhere in this file. A guard
 * asserts the literal is absent — the same absent-everywhere form as the
 * approve test id above — so quoting it here to explain the ban would fire
 * the guard on its own documentation. That is gotcha 40, and this file has
 * now hit it twice.
 *
 * `page.screencast.start({ onFrame })` hands us raw JPEGs instead, so the
 * encoder is ours. Combined with deviceScaleFactor 2 — render at 2x, encode
 * at 1x — that supersampling is what actually makes UI text crisp on video.
 *
 * ⚠ **Frames arrive ONLY on visual change**, measured: 3 frames across 33s on
 * a static page. So a fixed frame rate cannot be assumed, and each frame's
 * duration has to be recorded. A long still hold costs ONE frame, which is
 * correct and cheap — render.mjs turns the durations back into real time.
 *
 * ⚠ Frame timestamps come from OUR clock at receipt, not from the frame's own
 * `timestamp` field, whose epoch is not documented and which measured as a
 * 33s span across a 3s capture. A pacing bug from a misread epoch would be
 * invisible until the whole clip ran at the wrong speed.
 */

import { chromium } from 'playwright';
import { mkdir, writeFile, rm, access } from 'node:fs/promises';
import { join } from 'node:path';
import { SCENARIOS, BY_ID } from './scenarios.mjs';

const URL = process.env.HELIXIS_URL || 'https://your-app.vercel.app';
const OUT = process.env.DEMO_OUT || './out';
const STATE = process.env.DEMO_STATE || './.demo-session.json';
const HEADLESS = process.env.HEADLESS === 'true';

//: 16:9 on purpose. Web video players letterbox anything else, which is the
//: black-bar problem this set exists to avoid. 1440 wide because the app's
//: content column is 760px — at 1920 the UI floats in empty margin.
const W = Number(process.env.DEMO_WIDTH || 1440);
const H = Number(process.env.DEMO_HEIGHT || 810);
//: Render at 2x and encode at 1x. This is the single biggest lever on how
//: sharp the text looks.
const DPR = Number(process.env.DEMO_DPR || 2);
const JPEG_QUALITY = Number(process.env.DEMO_QUALITY || 95);

const ANSWER_TIMEOUT_MS = Number(process.env.DEMO_ANSWER_TIMEOUT || 240000);
const READY_TIMEOUT_MS = Number(process.env.DEMO_READY_TIMEOUT || 60000);
const LOGIN_TIMEOUT_MS = Number(process.env.DEMO_LOGIN_TIMEOUT || 300000);
const EXE = process.env.DEMO_CHROMIUM || undefined;

const argv = process.argv.slice(2);
const flag = (n) => argv.includes(`--${n}`);
const opt = (n) => {
  const i = argv.indexOf(`--${n}`);
  return i >= 0 ? argv[i + 1] : undefined;
};

function selected() {
  const only = opt('only');
  if (!only) return SCENARIOS;
  const ids = only.split(',').map((s) => s.trim());
  const missing = ids.filter((i) => !BY_ID.has(i));
  if (missing.length) {
    console.error(`unknown scenario id(s): ${missing.join(', ')}`);
    process.exit(2);
  }
  return ids.map((i) => BY_ID.get(i));
}

const exists = (p) =>
  access(p)
    .then(() => true)
    .catch(() => false);

/** Sign in once by hand. Never takes a password: a credential in argv is in
 *  the process list and in shell history. */
async function login() {
  const browser = await chromium.launch({ headless: false, executablePath: EXE });
  const ctx = await browser.newContext({ viewport: { width: W, height: H } });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  console.log('\n  👉 Sign in in the browser window (password or Google, 2FA fine).');
  console.log(`     Waiting up to ${Math.round(LOGIN_TIMEOUT_MS / 1000)}s…\n`);
  await page.getByTestId('chat-composer-input').first().waitFor({ timeout: LOGIN_TIMEOUT_MS });
  await ctx.storageState({ path: STATE });
  console.log(`  ✅ session saved → ${STATE} (gitignored; delete it to sign out)`);
  await browser.close();
}

/**
 * Wait for the turn to finish.
 *
 * ⚠ Reports WHAT it was waiting for on timeout. An unexplained "still busy
 * after Ns" is indistinguishable from a slow backend, and that ambiguity cost
 * a 42-minute run once (CLAUDE.md gotcha 43).
 */
async function waitForAnswer(page) {
  const started = Date.now();
  const prior = await page
    .getByTestId('assistant-bubble')
    .count()
    .catch(() => 0);
  try {
    await page.waitForFunction(
      (n) => document.querySelectorAll('[data-testid="assistant-bubble"]').length > n,
      prior,
      { timeout: ANSWER_TIMEOUT_MS },
    );
  } catch {
    const enabled = await page
      .getByTestId('chat-composer-input')
      .first()
      .isEnabled()
      .catch(() => null);
    throw new Error(
      `no reply after ${Math.round((Date.now() - started) / 1000)}s (composer enabled=${enabled})`,
    );
  }
  // Settle: the bubble appears at the FIRST token, and a clip that cuts there
  // shows half a sentence.
  let last = -1;
  let stable = 0;
  while (stable < 2500 && Date.now() - started < ANSWER_TIMEOUT_MS) {
    const len = await page
      .getByTestId('assistant-bubble')
      .last()
      .innerText()
      .then((t) => t.length)
      .catch(() => last);
    stable = len === last ? stable + 400 : 0;
    last = len;
    await page.waitForTimeout(400);
  }
}

/**
 * Lower-third caption, drawn INTO THE PAGE.
 *
 * ⚠ `page.screencast.showChapter` does not appear in captured frames — checked
 * twice, absent from both `page.screenshot` and the `onFrame` JPEGs. Whatever
 * it draws lives outside the captured surface, so a caption built on it looks
 * right in a live window and is invisible in the finished clip. A DOM overlay
 * is part of the page, so it is part of every frame by construction.
 *
 * The pill is near-black with a hairline light border, which reads on a light
 * surface and a dark one — the app is dark and the marketing frame around it
 * may not be, and a caption that only works on one is a caption that silently
 * disappears on the other.
 */
async function showCaption(page, { text, sub, s = 3 } = {}) {
  if (!text) return;
  await page.evaluate(
    ({ text, sub }) => {
      document.getElementById('__demo_caption__')?.remove();
      const el = document.createElement('div');
      el.id = '__demo_caption__';
      el.style.cssText = [
        'position:fixed',
        'left:48px',
        'bottom:56px',
        'z-index:2147483647',
        'max-width:60%',
        'padding:14px 22px',
        'border-radius:12px',
        'background:rgba(12,12,14,0.86)',
        'border:1px solid rgba(255,255,255,0.14)',
        'box-shadow:0 8px 32px rgba(0,0,0,0.34)',
        'color:#fff',
        'font-family:Geist,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
        'pointer-events:none',
        'opacity:0',
        'transform:translateY(8px)',
        'transition:opacity 260ms ease,transform 260ms ease',
      ].join(';');
      const main = document.createElement('div');
      main.textContent = text;
      main.style.cssText = 'font-size:22px;font-weight:600;line-height:1.3;letter-spacing:-0.01em';
      el.appendChild(main);
      if (sub) {
        const s2 = document.createElement('div');
        s2.textContent = sub;
        s2.style.cssText = 'margin-top:5px;font-size:15px;opacity:0.72;line-height:1.35';
        el.appendChild(s2);
      }
      document.body.appendChild(el);
      requestAnimationFrame(() => {
        el.style.opacity = '1';
        el.style.transform = 'translateY(0)';
      });
    },
    { text, sub },
  );
  await page.waitForTimeout(s * 1000);
  await page.evaluate(() => {
    const el = document.getElementById('__demo_caption__');
    if (!el) return;
    el.style.opacity = '0';
    el.style.transform = 'translateY(8px)';
    setTimeout(() => el.remove(), 300);
  });
  await page.waitForTimeout(340);
}

/**
 * Wait until the agent has something ON SCREEN, for `startWhen: 'working'`.
 *
 * ⚠ This gates the START OF THE CAMERA, so it must never be able to hang the
 * run (gotcha 43: a wait for a condition that cannot arrive burned 42 minutes
 * and produced nothing). It has its own timeout and, on expiry, says what it
 * was watching and rolls anyway — a clip that opens half a second early is a
 * pacing flaw; a clip that never records is a lost run.
 */
async function waitForWork(page, timeoutMs = 45000) {
  const signals = ['thinking-indicator', 'tool-pill', 'narration-line', 'assistant-bubble'];
  try {
    await page.waitForFunction(
      (ids) => ids.some((id) => document.querySelector(`[data-testid="${id}"]`)),
      signals,
      { timeout: timeoutMs },
    );
  } catch {
    console.log(`   (no visible work after ${Math.round(timeoutMs / 1000)}s — rolling anyway; watched ${signals.join(', ')})`);
  }
}

/** Hold on the confirm card, then DECLINE. Approving is not implemented. */
async function handleGate(page, { hold = 6 } = {}) {
  const gate = page.getByTestId('confirm-gate');
  const seen = await gate.isVisible({ timeout: 15000 }).catch(() => false);
  if (!seen) return false;
  await page.waitForTimeout(hold * 1000);
  const cancel = page.getByTestId('confirm-gate-cancel');
  if (await cancel.isVisible().catch(() => false)) await cancel.click().catch(() => {});
  await page.waitForTimeout(1200);
  return true;
}

/** One scripted step. Unknown verbs throw rather than being skipped: a typo
 *  that silently does nothing produces a clip missing its point. */
async function runStep(page, step, state) {
  switch (step.act) {
    case 'ask': {
      const composer = page.getByTestId('chat-composer-input').first();
      await composer.click();
      await composer.fill(step.text);
      const send = page.getByTestId('chat-composer-send').first();
      if (await send.isEnabled().catch(() => false)) await send.click();
      else await composer.press('Enter');
      return;
    }
    case 'await':
      await waitForAnswer(page);
      return;
    case 'nav':
      await page
        .getByRole('button', { name: new RegExp(`^${step.label}$`, 'i') })
        .first()
        .click({ timeout: 15000 });
      await page.waitForTimeout(1200);
      return;
    case 'click': {
      const el = page.getByTestId(step.testId).first();
      if (await el.isVisible().catch(() => false)) await el.click();
      else console.log(`   (nothing matched ${step.testId} — step skipped)`);
      await page.waitForTimeout(1000);
      return;
    }
    case 'hold':
      await page.waitForTimeout((step.s ?? 2) * 1000);
      return;
    case 'caption':
      await showCaption(page, step);
      return;
    case 'gate':
      state.gateSeen = await handleGate(page, step);
      return;
    default:
      throw new Error(`unknown step verb: ${step.act}`);
  }
}

async function recordOne(browser, scenario) {
  const dir = join(OUT, 'raw', scenario.id);
  await rm(dir, { recursive: true, force: true });
  await mkdir(dir, { recursive: true });

  const ctx = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: DPR,
    storageState: STATE,
  });
  const page = await ctx.newPage();
  const state = { gateSeen: false };
  const frames = [];
  let error = null;
  let n = 0;
  const writes = [];

  try {
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.getByTestId('chat-composer-input').first().waitFor({ timeout: READY_TIMEOUT_MS });
    await page.waitForTimeout(1200);

    // ── off camera ──────────────────────────────────────────────
    for (const step of scenario.preroll ?? []) await runStep(page, step, state);

    // ⚠ `startWhen: 'working'` is the fix for "the video is somebody typing".
    // The question goes in `preroll`, and the camera waits for the agent to put
    // something on screen — so frame one is mid-task rather than an empty
    // composer. Anything else starts immediately, which is right for the
    // navigation clips where there is no turn to wait for.
    if (scenario.startWhen === 'working') await waitForWork(page);

    // ── camera on ───────────────────────────────────────────────
    // A visible cursor that animates between click points is what turns a
    // navigation clip from "screens changing" into "somebody using it".
    await page.screencast.showActions({ cursor: 'pointer', duration: 700 });
    const t0 = Date.now();
    await page.screencast.start({
      quality: JPEG_QUALITY,
      size: { width: W, height: H },
      onFrame: ({ data }) => {
        const at = (Date.now() - t0) / 1000;
        const name = `${String(n++).padStart(5, '0')}.jpg`;
        frames.push({ name, at });
        // Queue rather than await: blocking the callback back-pressures the
        // browser's capture and drops frames during the busiest moments.
        writes.push(writeFile(join(dir, name), data));
      },
    });

    for (const step of scenario.steps) await runStep(page, step, state);

    const duration = (Date.now() - t0) / 1000;
    await page.screencast.stop();
    await Promise.all(writes);
    frames.push({ name: null, at: duration }); // sentinel: gives the last frame its length
    console.log(
      `   🎞  ${scenario.id}: ${n} frames over ${duration.toFixed(1)}s` +
        (state.gateSeen ? ', gate shown + declined' : ''),
    );
  } catch (e) {
    error = e.message;
    console.log(`   ⚠ ${scenario.id}: ${e.message}`);
    await page.screencast.stop().catch(() => {});
    await Promise.all(writes).catch(() => {});
  }

  await ctx.close();

  const manifest = {
    id: scenario.id,
    title: scenario.title,
    hook: scenario.hook,
    shows: scenario.shows,
    target: scenario.target,
    width: W,
    height: H,
    dpr: DPR,
    gateSeen: state.gateSeen,
    error,
    frames,
  };
  await writeFile(join(OUT, 'raw', `${scenario.id}.frames.json`), JSON.stringify(manifest, null, 2));
  return manifest;
}

async function main() {
  if (flag('login')) return login();
  if (!(await exists(STATE))) {
    console.error(`\nNo saved session at ${STATE}.\nRun:  npm run login\n`);
    process.exit(2);
  }
  const list = selected();
  await mkdir(join(OUT, 'raw'), { recursive: true });
  console.log(`\n🎬 recording ${list.length} demo(s) at ${W}x${H} @${DPR}x → ${URL}\n`);

  const browser = await chromium.launch({ headless: HEADLESS, executablePath: EXE });
  const done = [];
  for (const s of list) {
    console.log(`▶ ${s.id} — ${s.title}  (target ${s.target})`);
    done.push(await recordOne(browser, s));
  }
  await browser.close();

  const ok = done.filter((d) => !d.error).length;
  console.log(`\n${'─'.repeat(52)}\n${ok}/${done.length} recorded → ${join(OUT, 'raw')}`);
  const missedGate = done.filter((d) => BY_ID.get(d.id)?.steps.some((s) => s.act === 'gate') && !d.gateSeen);
  if (missedGate.length) {
    console.log(`⚠ expected a confirm gate and saw none: ${missedGate.map((d) => d.id).join(', ')}`);
  }
  console.log(`next:  npm run render\n`);
  if (done.some((d) => d.error)) process.exitCode = 1;
}

main().catch((e) => {
  console.error('fatal:', e);
  process.exit(1);
});
