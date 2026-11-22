/**
 * E2E battery — runs the whole scenarios.mjs catalog against the DEPLOYED chat
 * as a fictional property-management company, and writes a big pass/warn/fail
 * report + a screenshot per scenario.
 *
 * SAFE: every scenario that surfaces a confirm gate is CANCELLED — nothing is
 * ever sent/filed. A "write" scenario that produces NO gate is a hard safety
 * FAIL (the F1 regression check). Reads/drafts are side-effect-light.
 *
 * Runs all scenarios in ONE continuous chat thread — a realistic "PM using the
 * assistant all day", and it exercises long-conversation history + cross-turn
 * memory recall. Filter with SCENARIO_ONLY="Maintenance,Web" (category names).
 *
 * Config (env; see run-battery.ps1):
 *   HELIXIS_URL, E2E_EMAIL, E2E_PASSWORD, HEADLESS, SLOWMO, SHOT_DIR,
 *   LOGIN_TIMEOUT_MS, REPLY_TIMEOUT_MS, SCENARIO_ONLY
 */

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { CATEGORY_ORDER, SCENARIOS } from './scenarios.mjs';

const URL = process.env.HELIXIS_URL || 'https://your-app.vercel.app';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const HEADLESS = process.env.HEADLESS === 'true';
const SLOWMO = Number(process.env.SLOWMO || 150);
const SHOT_DIR = process.env.SHOT_DIR || './shots-battery';
const LOGIN_TIMEOUT_MS = Number(process.env.LOGIN_TIMEOUT_MS || 300000);
const REPLY_TIMEOUT_MS = Number(process.env.REPLY_TIMEOUT_MS || 150000);
const ONLY = (process.env.SCENARIO_ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

let shotN = 0;
async function shot(page, name) {
  shotN += 1;
  const file = join(SHOT_DIR, `${String(shotN).padStart(3, '0')}-${name}.png`);
  await page.screenshot({ path: file }).catch(() => {});
  return file;
}

async function login(page) {
  const composer = () => page.getByTestId('chat-composer-input').first();
  if (await composer().isVisible({ timeout: 4000 }).catch(() => false)) return;
  if (EMAIL && PASSWORD) {
    await page.locator('input[type="email"]').first().fill(EMAIL);
    await page.locator('input[type="password"]').first().fill(PASSWORD);
    await page.getByRole('button', { name: /sign in/i }).first().click().catch(() => {});
  } else {
    console.log('\n   👉 LOG IN NOW in the browser window (password or Google, 2FA ok).');
    console.log(`   Waiting up to ${Math.round(LOGIN_TIMEOUT_MS / 1000)}s for the chat to load…\n`);
  }
  await composer().waitFor({ timeout: LOGIN_TIMEOUT_MS });
}

async function runScenario(page, s) {
  const composer = page.getByTestId('chat-composer-input').first();
  await composer.click();
  await composer.fill(s.prompt);
  const before = await page.getByTestId('assistant-bubble').count().catch(() => 0);
  const send = page.getByTestId('chat-composer-send').first();
  if (await send.isEnabled().catch(() => false)) await send.click();
  else await composer.press('Enter');

  await page
    .waitForFunction(
      (n) => document.querySelectorAll('[data-testid="assistant-bubble"]').length > n,
      before,
      { timeout: REPLY_TIMEOUT_MS },
    )
    .catch(() => {});
  await page.waitForTimeout(2500);

  // A gated write must surface a card; screenshot + CANCEL it (never send).
  let gated = false;
  const gate = page.getByTestId('confirm-gate');
  if (await gate.isVisible().catch(() => false)) {
    gated = true;
    await shot(page, `${s.id}-GATE`);
    const cancel = page.getByTestId('confirm-gate-cancel');
    if (await cancel.isVisible().catch(() => false)) await cancel.click().catch(() => {});
    await page.waitForTimeout(1000);
  }

  const bubbles = page.getByTestId('assistant-bubble');
  const n = await bubbles.count().catch(() => 0);
  const reply = n ? (await bubbles.nth(n - 1).innerText().catch(() => '')) : '';
  const file = await shot(page, s.id);

  const notes = [];
  let status = 'PASS';
  if (!reply.trim() && !gated) {
    status = 'FAIL';
    notes.push('empty reply and no gate (hang / error?)');
  }
  if (s.write && !gated) {
    status = 'FAIL';
    notes.push('WRITE ask produced NO confirm gate — safety regression (F1)');
  }
  if (
    status !== 'FAIL' &&
    s.expectAny &&
    reply &&
    !s.expectAny.some((k) => reply.toLowerCase().includes(k.toLowerCase()))
  ) {
    status = 'WARN';
    notes.push(`none of [${s.expectAny.join(', ')}] in reply`);
  }
  return {
    ...s,
    status,
    gated,
    reply: reply.slice(0, 280).replace(/\s+/g, ' ').trim(),
    file,
    notes,
  };
}

async function main() {
  await mkdir(SHOT_DIR, { recursive: true });
  const scenarios = ONLY.length
    ? SCENARIOS.filter((s) => ONLY.includes(s.category))
    : SCENARIOS;
  console.log(
    `\n🏢 Helixis E2E battery → ${URL}\n   ${scenarios.length} scenarios${ONLY.length ? ` (categories: ${ONLY.join(', ')})` : ''}\n   screenshots → ${SHOT_DIR}\n`,
  );

  const browser = await chromium.launch({ headless: HEADLESS, slowMo: SLOWMO });
  const page = await browser.newContext({ viewport: { width: 1440, height: 900 } }).then((c) => c.newPage());

  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await login(page);
  await page.getByTestId('chat-composer-input').first().waitFor({ timeout: 30000 });

  const results = [];
  for (let i = 0; i < scenarios.length; i++) {
    const s = scenarios[i];
    process.stdout.write(`[${i + 1}/${scenarios.length}] ${s.category} · ${s.id} … `);
    try {
      const r = await runScenario(page, s);
      results.push(r);
      console.log(`${r.status}${r.gated ? ' (gated✓)' : ''}`);
    } catch (e) {
      results.push({ ...s, status: 'FAIL', gated: false, reply: '', notes: [`driver error: ${e.message}`] });
      console.log(`FAIL (${e.message})`);
    }
  }

  // Report.
  const count = (st) => results.filter((r) => r.status === st).length;
  const safetyFails = results.filter((r) => r.write && !r.gated);
  const lines = [];
  lines.push(`# Helixis E2E battery — ${scenarios.length} scenarios\n`);
  lines.push(`- Target: ${URL}`);
  lines.push(`- ✅ PASS ${count('PASS')} · ⚠️ WARN ${count('WARN')} · ❌ FAIL ${count('FAIL')}`);
  lines.push(
    `- 🔒 Write-gate safety: ${safetyFails.length === 0 ? 'ALL writes gated ✅' : `${safetyFails.length} UNGATED WRITE(S) ❌ — ${safetyFails.map((r) => r.id).join(', ')}`}`,
  );
  lines.push('');
  for (const cat of CATEGORY_ORDER) {
    const rows = results.filter((r) => r.category === cat);
    if (!rows.length) continue;
    lines.push(`## ${cat}`);
    for (const r of rows) {
      const icon = r.status === 'PASS' ? '✅' : r.status === 'WARN' ? '⚠️' : '❌';
      const gate = r.write ? (r.gated ? ' 🔒gated' : ' 🔓NO-GATE') : '';
      lines.push(`- ${icon} **${r.id}**${gate} — _"${r.prompt}"_`);
      if (r.reply) lines.push(`    - reply: ${r.reply}${r.reply.length >= 280 ? '…' : ''}`);
      if (r.notes?.length) lines.push(`    - ⚠ ${r.notes.join('; ')}`);
    }
    lines.push('');
  }
  const report = join(SHOT_DIR, 'battery-report.md');
  await writeFile(report, lines.join('\n'));

  console.log(`\n${'─'.repeat(52)}`);
  console.log(`✅ ${count('PASS')}  ⚠️ ${count('WARN')}  ❌ ${count('FAIL')}`);
  if (safetyFails.length) console.log(`🔒 SAFETY: ${safetyFails.length} ungated write(s): ${safetyFails.map((r) => r.id).join(', ')}`);
  else console.log('🔒 SAFETY: every write surfaced + cancelled a gate.');
  console.log(`Report → ${report}\n`);

  console.log('Leaving the browser open 20s…');
  await page.waitForTimeout(20000);
  await browser.close();
}

main().catch((e) => {
  console.error('fatal:', e);
  process.exit(1);
});
