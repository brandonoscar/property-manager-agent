/**
 * Encode captured frames into web-ready clips.
 *
 *     npm run render                # every capture in ./out/raw
 *     npm run render -- --wait 2.2  # hold the "thinking" beat a bit longer
 *
 * Frames arrive from the browser only on visual change, each stamped with the
 * moment it was received, so this rebuilds real time from those stamps using
 * ffmpeg's concat demuxer: every frame gets an explicit duration. A ten-second
 * still hold is ONE frame with a ten-second duration — correct, and far
 * cheaper than 300 identical ones.
 *
 * WHAT IS AND IS NOT ALTERED. The typing, the answer, the confirm card and the
 * reading beats are real time. The ONLY thing compressed is the window between
 * "question submitted" and "answer finished", because the app's median turn is
 * ~40s and without that a landing-page clip is ninety percent spinner.
 *
 * ⚠ Say so publicly. "Waiting time compressed" under the video costs nothing
 * and is the difference between an edit and a claim about speed.
 *
 * ffmpeg comes from `ffmpeg-static`, so `npm install` is the whole setup — no
 * system ffmpeg, no PATH surgery on Windows. A system ffmpeg wins if present.
 */

import { readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import { join, basename, resolve } from 'node:path';
import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';
import ffmpegStatic from 'ffmpeg-static';
import { buildReview } from './review.mjs';

const run = promisify(execFile);
const OUT = process.env.DEMO_OUT || './out';
const RAW = join(OUT, 'raw');
const FINAL = join(OUT, 'final');

const argv = process.argv.slice(2);
const opt = (n, d) => {
  const i = argv.indexOf(`--${n}`);
  return i >= 0 ? Number(argv[i + 1]) : d;
};
/** How long a compressed wait LASTS on screen. A fixed target, not a speed
 *  factor: a 40s turn and a 170s turn then read identically, so the set is
 *  visually consistent instead of one clip crawling. */
const WAIT_TARGET_S = opt('wait', 1.8);
/** Any gap longer than this is dead air rather than something happening. */
const DEAD_AIR_S = opt('deadair', 2.5);
const CRF = opt('crf', 18);
const FPS = opt('fps', 30);

function resolveFfmpeg() {
  try {
    execFileSync('ffmpeg', ['-version'], { stdio: 'ignore' });
    return { bin: 'ffmpeg', source: 'system' };
  } catch {
    /* fall through */
  }
  if (ffmpegStatic) return { bin: ffmpegStatic, source: 'ffmpeg-static' };
  throw new Error('no ffmpeg — run `npm install`, or put ffmpeg on PATH');
}

/**
 * Frame list -> concat entries with durations, dead air squeezed.
 *
 * Exported because it is the one piece with real logic and it is worth
 * testing without a browser or an encoder in the loop.
 *
 * ⚠ Only compresses a gap that is BOTH long and quiet. A long gap between
 * frames means nothing changed on screen — that is the spinner. A burst of
 * frames is the answer streaming in and must stay at 1x.
 */
export function planTimeline(frames, { waitTarget = WAIT_TARGET_S, deadAir = DEAD_AIR_S } = {}) {
  const real = frames.filter((f) => f.name);
  if (!real.length) return { entries: [], rawSeconds: 0, finalSeconds: 0 };
  const endsAt = frames[frames.length - 1].at;
  const entries = [];
  let rawSeconds = 0;
  let finalSeconds = 0;
  for (let i = 0; i < real.length; i++) {
    const next = i + 1 < real.length ? real[i + 1].at : endsAt;
    const held = Math.max(next - real[i].at, 1 / 120);
    const shown = held > deadAir ? Math.min(waitTarget, held) : held;
    entries.push({ name: real[i].name, duration: shown, compressed: shown < held });
    rawSeconds += held;
    finalSeconds += shown;
  }
  return { entries, rawSeconds, finalSeconds };
}

async function renderOne(ff, manifest) {
  const dir = join(RAW, manifest.id);
  const { entries, rawSeconds, finalSeconds } = planTimeline(manifest.frames || []);
  if (!entries.length) return { id: manifest.id, ok: false, why: 'no frames captured' };

  // ⚠ ffmpeg's concat demuxer ignores the final entry's `duration`, so the
  // last file is repeated once without it. Omitting this makes every clip end
  // one frame early — invisible until the last thing on screen is the payoff.
  const list =
    entries.map((e) => `file '${e.name}'\nduration ${e.duration.toFixed(4)}`).join('\n') +
    `\nfile '${entries[entries.length - 1].name}'\n`;
  const listPath = join(dir, 'frames.txt');
  await writeFile(listPath, list, 'utf8');

  // ⚠ ABSOLUTE. ffmpeg runs with cwd set to the frames directory so the
  // concat list's relative filenames resolve; a relative OUTPUT path would
  // then be written relative to that directory too, and ffmpeg fails with a
  // bare "No such file or directory" that names the right path and the wrong
  // base.
  const mp4 = resolve(FINAL, `${manifest.id}.mp4`);
  const poster = resolve(FINAL, `${manifest.id}.jpg`);
  await run(
    ff.bin,
    [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-f', 'concat', '-safe', '0', '-i', 'frames.txt',
      // lanczos downscale is where the 2x capture turns into crisp text.
      '-vf', `fps=${FPS},scale=${manifest.width}:${manifest.height}:flags=lanczos`,
      '-c:v', 'libx264',
      '-pix_fmt', 'yuv420p',  // Safari refuses yuv444
      '-crf', String(CRF),
      '-preset', 'slow',
      '-movflags', '+faststart',  // plays before the whole file arrives
      '-an',
      mp4,
    ],
    { cwd: dir, maxBuffer: 1 << 26 },
  );

  await run(
    ff.bin,
    ['-hide_banner', '-loglevel', 'error', '-y', '-i', mp4, '-ss', '1', '-frames:v', '1', '-q:v', '2', poster],
    { maxBuffer: 1 << 26 },
  ).catch(() => {});

  return {
    id: manifest.id,
    ok: true,
    mp4,
    poster,
    seconds: finalSeconds,
    rawSeconds,
    frames: entries.length,
    compressed: entries.filter((e) => e.compressed).length,
  };
}

async function main() {
  const ff = resolveFfmpeg();
  await mkdir(FINAL, { recursive: true });
  const files = (await readdir(RAW).catch(() => [])).filter((f) => f.endsWith('.frames.json'));
  if (!files.length) {
    console.error(`nothing to render in ${RAW} — run \`npm run record\` first`);
    process.exit(2);
  }
  console.log(`\n🎞  rendering ${files.length} clip(s)  [ffmpeg: ${ff.source}]\n`);

  const done = [];
  for (const f of files) {
    const manifest = JSON.parse(await readFile(join(RAW, f), 'utf8'));
    process.stdout.write(`▶ ${manifest.id} `);
    try {
      const r = await renderOne(ff, manifest);
      done.push({ ...r, title: manifest.title, hook: manifest.hook, target: manifest.target });
      console.log(
        r.ok
          ? `→ ${basename(r.mp4)}  ${r.rawSeconds.toFixed(0)}s raw → ${r.seconds.toFixed(1)}s ` +
            `(${r.frames} frames, ${r.compressed} gaps squeezed)  target ${manifest.target}`
          : `⚠ ${r.why}`,
      );
    } catch (e) {
      done.push({ id: manifest.id, ok: false, why: e.message });
      console.log(`⚠ ${e.message}`);
    }
  }

  await writeFile(join(FINAL, 'index.json'), JSON.stringify(done, null, 2));
  console.log(`\n${'─'.repeat(52)}\n${done.filter((d) => d.ok).length}/${done.length} → ${FINAL}`);
  const review = await buildReview().catch(() => null);
  if (review) console.log(`\nreview them before publishing (real tenant data):\n  ${review}\n`);
  if (done.some((d) => !d.ok)) process.exitCode = 1;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error('fatal:', e);
    process.exit(1);
  });
}
