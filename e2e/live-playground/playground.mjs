/**
 * Helixis live "property-manager playground".
 *
 * Opens the DEPLOYED web app in a REAL, VISIBLE browser, lets YOU log in
 * (password or Google — the script never touches your credentials), then
 * roleplays a property manager: types real prompts into chat, walks every
 * surface (inbox / activity / properties / CRM / search / settings), and
 * screenshots each step into ./shots.
 *
 * SAFE BY DESIGN — it is read-mostly:
 *   - It NEVER approves a confirm gate. If the agent surfaces one (a Buildium
 *     write / an outbound message), the script screenshots it and clicks
 *     CANCEL. Nothing is ever filed or sent.
 *   - It only types chat prompts and navigates. No settings are changed.
 *
 * Config (all optional) via env vars — see run.ps1 / README.md:
 *   HELIXIS_URL   deployed app base URL   (default https://your-app.vercel.app)
 *   E2E_EMAIL     if set WITH E2E_PASSWORD, tries an automatic password login;
 *   E2E_PASSWORD  otherwise the script pauses for you to log in by hand.
 *   HEADLESS      "true" to hide the browser (default: visible)
 *   SLOWMO        ms of slow-motion between actions (default 250)
 *   SHOT_DIR      screenshot folder (default ./shots)
 *   LOGIN_TIMEOUT_MS  how long to wait for you to finish manual login (default 300000 = 5 min)
 */

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const URL = process.env.HELIXIS_URL || 'https://your-app.vercel.app';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const HEADLESS = process.env.HEADLESS === 'true';
const SLOWMO = Number(process.env.SLOWMO || 250);
const SHOT_DIR = process.env.SHOT_DIR || './shots';
const LOGIN_TIMEOUT_MS = Number(process.env.LOGIN_TIMEOUT_MS || 300000);

// PM prompts to drive. All are read/draft-only from the user's side; if any
// draft triggers a confirm gate the script cancels it (see handleConfirmGate).
const PROMPTS = [
  'What maintenance work orders are open right now?',
  'Summarize what needs my attention today across my properties.',
  'Draft a friendly reply to the most recent tenant maintenance request.',
  'What can you help me with as my property assistant?',
];

const results = [];
let shotN = 0;

async function shot(page, name) {
  shotN += 1;
  const file = join(SHOT_DIR, `${String(shotN).padStart(2, '0')}-${name}.png`);
  try {
    await page.screenshot({ path: file, fullPage: false });
    console.log(`   📸 ${file}`);
  } catch (e) {
    console.log(`   ⚠ screenshot failed (${name}): ${e.message}`);
  }
}

async function step(name, fn) {
  process.stdout.write(`▶ ${name}\n`);
  try {
    await fn();
    results.push({ step: name, ok: true });
    console.log(`   ✅ ${name}`);
  } catch (e) {
    results.push({ step: name, ok: false, error: e.message });
    console.log(`   ⚠ ${name} — ${e.message}`);
  }
}

/** Cancel any confirm gate so the run never files/sends anything. */
async function handleConfirmGate(page) {
  const gate = page.getByTestId('confirm-gate');
  if (await gate.isVisible().catch(() => false)) {
    console.log('   🛑 confirm gate surfaced — screenshotting + CANCELLING (never auto-approve)');
    await shot(page, 'confirm-gate-cancelled');
    const cancel = page.getByTestId('confirm-gate-cancel');
    if (await cancel.isVisible().catch(() => false)) await cancel.click().catch(() => {});
    return true;
  }
  return false;
}

async function isLoggedIn(page) {
  return page
    .getByTestId('chat-composer-input')
    .first()
    .isVisible({ timeout: 4000 })
    .catch(() => false);
}

async function login(page) {
  if (await isLoggedIn(page)) {
    console.log('   already authenticated — skipping login');
    return;
  }
  // Auto password login only when BOTH creds are provided.
  if (EMAIL && PASSWORD) {
    console.log('   attempting automatic password login…');
    await page.locator('input[type="email"]').first().fill(EMAIL);
    await page.locator('input[type="password"]').first().fill(PASSWORD);
    await shot(page, 'login-filled');
    // The submit button reads "Sign in" in signin mode.
    await page
      .getByRole('button', { name: /sign in/i })
      .first()
      .click()
      .catch(() => {});
  } else {
    console.log('\n   👉 LOG IN NOW in the opened browser window (password OR Google — 2FA is fine).');
    console.log(`   Waiting up to ${Math.round(LOGIN_TIMEOUT_MS / 1000)}s for the chat surface to appear…\n`);
  }
  await page.getByTestId('chat-composer-input').first().waitFor({ timeout: LOGIN_TIMEOUT_MS });
}

async function sendPrompt(page, text) {
  const composer = page.getByTestId('chat-composer-input').first();
  await composer.click();
  await composer.fill(text);
  const before = await page.getByTestId('assistant-bubble').count().catch(() => 0);
  const send = page.getByTestId('chat-composer-send').first();
  if (await send.isEnabled().catch(() => false)) {
    await send.click();
  } else {
    await composer.press('Enter');
  }
  // Wait for a new assistant bubble (streamed reply), tolerant of slow turns.
  await page
    .waitForFunction(
      (n) => document.querySelectorAll('[data-testid="assistant-bubble"]').length > n,
      before,
      { timeout: 90000 },
    )
    .catch(() => console.log('   (no new assistant bubble within 90s — screenshotting anyway)'));
  // Let the stream settle a moment, then deal with any gate.
  await page.waitForTimeout(2500);
  await handleConfirmGate(page);
}

/** Click a left-nav item by its accessible name and screenshot the surface. */
async function goToSurface(page, name, shotName) {
  const nav = page.getByRole('button', { name: new RegExp(`^${name}$`, 'i') }).first();
  await nav.click({ timeout: 15000 });
  await page.waitForTimeout(2000);
  await shot(page, shotName);
}

async function main() {
  await mkdir(SHOT_DIR, { recursive: true });
  console.log(`\n🏠 Helixis PM playground → ${URL}\n   screenshots → ${SHOT_DIR}\n`);

  const browser = await chromium.launch({ headless: HEADLESS, slowMo: SLOWMO });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  await step('open the app', async () => {
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await shot(page, 'landing');
  });

  await step('log in', () => login(page));
  await step('chat surface loaded', async () => {
    await page.getByTestId('chat-composer-input').first().waitFor({ timeout: 30000 });
    await shot(page, 'chat-home');
  });

  // Roleplay: type real PM prompts.
  for (let i = 0; i < PROMPTS.length; i++) {
    await step(`chat prompt ${i + 1}: "${PROMPTS[i].slice(0, 42)}…"`, async () => {
      await sendPrompt(page, PROMPTS[i]);
      await shot(page, `chat-reply-${i + 1}`);
    });
  }

  // Walk every surface.
  await step('open Inbox', async () => {
    await goToSurface(page, 'Inbox', 'inbox');
    const firstRow = page.getByTestId('inbox-row-item').first();
    if (await firstRow.isVisible().catch(() => false)) {
      await firstRow.click().catch(() => {});
      await page.waitForTimeout(1500);
      await shot(page, 'inbox-detail');
    } else {
      console.log('   (inbox empty — seed via scripts/synthetic_traffic.py to populate)');
    }
  });
  await step('open Team Activity', () => goToSurface(page, 'Team Activity', 'activity'));
  await step('open Properties', () => goToSurface(page, 'Properties', 'properties'));
  await step('open CRM', async () => {
    await goToSurface(page, 'CRM', 'crm');
    // Try opening the first lead card if the board has any (guarded — selector varies).
    const lead = page.locator('[data-testid^="crm-lead"]').first();
    if (await lead.isVisible().catch(() => false)) {
      await lead.click().catch(() => {});
      await page.waitForTimeout(1500);
      await shot(page, 'crm-lead-detail');
    }
  });
  await step('open Search', () => goToSurface(page, 'Search', 'search'));
  await step('open Settings', () => goToSurface(page, 'Settings', 'settings'));

  // Power-user surface: ⌘K / Ctrl+K command palette.
  await step('open command palette', async () => {
    await page.keyboard.press('Control+K');
    await page.waitForTimeout(800);
    await shot(page, 'command-palette');
    await page.keyboard.press('Escape');
  });

  // Summary.
  const passed = results.filter((r) => r.ok).length;
  const md =
    `# Helixis PM playground run\n\n- Target: ${URL}\n- Steps: ${passed}/${results.length} ok\n\n` +
    results.map((r) => `- ${r.ok ? '✅' : '⚠️'} ${r.step}${r.error ? ` — ${r.error}` : ''}`).join('\n') +
    `\n\nScreenshots in \`${SHOT_DIR}\`.\n`;
  await writeFile(join(SHOT_DIR, 'summary.md'), md);
  console.log(`\n${'─'.repeat(48)}\n${passed}/${results.length} steps ok — summary → ${join(SHOT_DIR, 'summary.md')}\n`);

  console.log('Leaving the browser open for 20s so you can poke around…');
  await page.waitForTimeout(20000);
  await browser.close();
}

main().catch((e) => {
  console.error('fatal:', e);
  process.exit(1);
});
