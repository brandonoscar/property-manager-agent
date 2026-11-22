/**
 * Build a local review page for the recorded clips, and open it.
 *
 *     npm run review
 *
 * `render.mjs` calls this automatically, so the page is already there after a
 * render; run it directly to rebuild after editing the copy.
 *
 * ⚠ **LOCAL FILE, DELIBERATELY.** The clips are recorded against production
 * and can contain real residents' names, addresses and balances. This page
 * exists so that footage is reviewed BEFORE anything is uploaded anywhere —
 * so it must never be the thing that does the uploading. It reads the mp4s
 * from beside itself with relative paths and has no network access of any
 * kind; opening it uploads nothing and nothing leaves the machine.
 *
 * Each clip gets a publish checklist rather than a play button alone, because
 * "watch it before publishing" is advice nobody follows and a checkbox next to
 * the video is a step. The state is per-browser (localStorage) and is a memory
 * aid, not a record — it is not consulted by anything.
 */

import { readdir, readFile, writeFile, access } from 'node:fs/promises';
import { join, basename } from 'node:path';
import { execFile } from 'node:child_process';
import { platform } from 'node:os';

const OUT = process.env.DEMO_OUT || './out';
const RAW = join(OUT, 'raw');
const FINAL = join(OUT, 'final');

const exists = (p) =>
  access(p)
    .then(() => true)
    .catch(() => false);

const esc = (s) =>
  String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

function card(c, i) {
  const poster = c.poster ? ` poster="${esc(basename(c.poster))}"` : '';
  return `
  <article class="clip" id="${esc(c.id)}">
    <div class="stage">
      <video src="${esc(basename(c.mp4))}"${poster} controls preload="metadata" playsinline></video>
    </div>
    <div class="meta">
      <div class="eyebrow"><span class="num">${String(i + 1).padStart(2, '0')}</span>${esc(c.id)}${
        c.gateSeen ? '<span class="tag gate">confirm gate shown</span>' : ''
      }${c.optional ? '<span class="tag opt">optional</span>' : ''}</div>
      <h2>${esc(c.title)}</h2>
      <p class="hook">${esc(c.hook)}</p>
      <p class="shows"><b>Proves:</b> ${esc(c.shows)}</p>
      <details>
        <summary>What was typed on camera</summary>
        <p class="prompt">${esc(c.prompt)}</p>
        <p class="stat">${c.rawSeconds ? `${Math.round(c.rawSeconds)}s recorded &rarr; ` : ''}${
          c.seconds ? `${c.seconds.toFixed(1)}s final` : ''
        }${c.mp4 ? ` &middot; <code>${esc(basename(c.mp4))}</code>` : ''}</p>
      </details>
      <ul class="check" data-clip="${esc(c.id)}">
        <li><label><input type="checkbox" data-k="watched"> Watched it end to end</label></li>
        <li><label><input type="checkbox" data-k="pii"> No real resident name, address or balance on screen</label></li>
        <li><label><input type="checkbox" data-k="claim"> Nothing on screen overstates what it does</label></li>
        <li><label><input type="checkbox" data-k="caption"> Will be captioned &ldquo;waiting time compressed&rdquo;</label></li>
      </ul>
      <p class="verdict" data-for="${esc(c.id)}">Not reviewed yet</p>
    </div>
  </article>`;
}

export async function buildReview() {
  if (!(await exists(FINAL))) return null;
  const finals = (await readdir(FINAL)).filter((f) => f.endsWith('.mp4'));
  if (!finals.length) return null;

  const clips = [];
  for (const f of finals) {
    const id = basename(f, '.mp4');
    let cuts = {};
    if (await exists(join(RAW, `${id}.cuts.json`))) {
      cuts = JSON.parse(await readFile(join(RAW, `${id}.cuts.json`), 'utf8'));
    }
    let meta = {};
    if (await exists(join(FINAL, 'index.json'))) {
      const all = JSON.parse(await readFile(join(FINAL, 'index.json'), 'utf8'));
      meta = all.find((m) => m.id === id) || {};
    }
    clips.push({
      id,
      title: cuts.title || id,
      hook: cuts.hook || '',
      shows: cuts.shows || '',
      prompt: cuts.prompt || '',
      gateSeen: !!cuts.gateSeen,
      mp4: join(FINAL, f),
      poster: (await exists(join(FINAL, `${id}.jpg`))) ? join(FINAL, `${id}.jpg`) : null,
      seconds: meta.seconds ?? null,
      rawSeconds: meta.rawSeconds ?? cuts.segments?.at(-1)?.end ?? null,
    });
  }

  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo clips &mdash; review before publishing</title>
<style>
  :root{
    --ink:#15161a; --muted:#5d6070; --line:#e3e2e6; --bg:#f7f6f4; --card:#fff;
    --iris:#5b4bb8; --warn:#8a5a12; --warn-bg:#fdf5e6; --ok:#1f6b45;
  }
  @media (prefers-color-scheme: dark){ :root:not([data-theme=light]){
    --ink:#e9e8ec; --muted:#9b9daa; --line:#2c2d34; --bg:#131318; --card:#1b1c22;
    --iris:#a99cf0; --warn:#e0b168; --warn-bg:#2a2317; --ok:#6cc79a;
  }}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  header{max-width:1120px;margin:0 auto;padding:44px 24px 8px}
  h1{margin:0 0 6px;font-size:29px;letter-spacing:-.02em}
  .sub{color:var(--muted);margin:0 0 22px;max-width:64ch}
  .note{max-width:1120px;margin:0 auto 26px;padding:14px 16px;border-radius:10px;
    background:var(--warn-bg);border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);
    color:var(--ink);font-size:14px}
  .note b{color:var(--warn)}
  main{max-width:1120px;margin:0 auto;padding:0 24px 72px;display:grid;gap:26px}
  /* Stacked, not side-by-side: a 16:9 clip in a grid column next to a taller
     text block gets letterboxed with dark bars top and bottom. That was the
     "why is it dark at the bottom" bug -- this page's frame, not the video. */
  .clip{background:var(--card);border:1px solid var(--line);border-radius:14px;
    overflow:hidden;display:flex;flex-direction:column}
  .stage{background:var(--card);line-height:0}
  /* aspect-ratio + block display means the element is exactly the video's
     shape, so there is nothing left over to paint. */
  video{width:100%;height:auto;display:block;aspect-ratio:16/9;background:#fff}
  .meta{padding:20px 22px}
  .eyebrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
  .num{font-variant-numeric:tabular-nums;color:var(--iris);font-weight:600}
  .tag{text-transform:none;letter-spacing:0;font-size:11px;padding:2px 7px;border-radius:99px;
    border:1px solid var(--line)}
  .tag.gate{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
  .tag.opt{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
  h2{margin:8px 0 6px;font-size:19px;letter-spacing:-.01em}
  .hook{margin:0 0 10px;color:var(--ink)}
  .shows{margin:0 0 12px;color:var(--muted);font-size:14px}
  .shows b{color:var(--ink)}
  details{margin:0 0 14px;font-size:13.5px;color:var(--muted)}
  summary{cursor:pointer;color:var(--iris)}
  .prompt{margin:8px 0 4px;padding:9px 11px;background:var(--bg);border-radius:8px;
    border:1px solid var(--line);color:var(--ink)}
  .stat{margin:6px 0 0;font-variant-numeric:tabular-nums}
  code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace}
  .check{list-style:none;margin:0 0 10px;padding:12px 0 0;border-top:1px solid var(--line);
    display:grid;gap:7px;font-size:14px}
  .check label{display:flex;gap:9px;align-items:flex-start;cursor:pointer}
  .check input{margin-top:3px;accent-color:var(--iris)}
  .verdict{margin:0;font-size:13px;font-weight:600;color:var(--muted)}
  .verdict.ready{color:var(--ok)}
</style></head><body>
<header>
  <h1>Demo clips</h1>
  <p class="sub">Review every clip here before any of it goes on the site. Nothing on this
  page uploads anything &mdash; the videos are read from the folder next to it.</p>
</header>
<div class="note">
  <b>Two things to check.</b> These were recorded against <b>production with real data</b>,
  so a resident's name or balance can be on screen. And <b>waiting time is compressed</b>:
  the gap between question and answer is shortened, everything else is real time &mdash;
  caption them that way so a viewer doesn't read it as a speed claim.
</div>
<main>${clips.map(card).join('\n')}</main>
<script>
  // Per-browser memory aid only. Nothing reads this but the page itself.
  const KEY = 'occupella-demo-review';
  let state = {};
  try { state = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { state = {}; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
  function refresh(id){
    const list = document.querySelector('.check[data-clip="'+id+'"]');
    const boxes = [...list.querySelectorAll('input')];
    const done = boxes.filter(b => b.checked).length;
    const v = document.querySelector('.verdict[data-for="'+id+'"]');
    v.textContent = done === boxes.length ? 'Cleared to publish' : done + ' of ' + boxes.length + ' checked';
    v.classList.toggle('ready', done === boxes.length);
  }
  document.querySelectorAll('.check').forEach(list => {
    const id = list.dataset.clip;
    state[id] = state[id] || {};
    list.querySelectorAll('input').forEach(box => {
      box.checked = !!state[id][box.dataset.k];
      box.addEventListener('change', () => {
        state[id][box.dataset.k] = box.checked; save(); refresh(id);
      });
    });
    refresh(id);
  });
</script>
</body></html>`;

  const file = join(FINAL, 'review.html');
  await writeFile(file, html, 'utf8');
  return file;
}

function open(file) {
  const p = platform();
  const [cmd, args] =
    p === 'win32'
      ? ['cmd', ['/c', 'start', '', file]]
      : p === 'darwin'
        ? ['open', [file]]
        : ['xdg-open', [file]];
  execFile(cmd, args, () => {}); // best effort — printing the path is the real answer
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const file = await buildReview();
  if (!file) {
    console.error(`nothing to review in ${FINAL} — run \`npm run record && npm run render\` first`);
    process.exit(2);
  }
  console.log(`\n  review page → ${file}\n`);
  if (!process.argv.includes('--no-open')) open(file);
}
