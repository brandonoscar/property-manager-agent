# Landing-page demo videos

Records the deployed app solving real property-management problems, then
turns each recording into a web-ready `.mp4` with a poster frame.

```powershell
cd e2e/demos
npm install          # also installs ffmpeg — nothing else to set up
npm run login        # opens a browser, you sign in ONCE
npm run record       # one video per scenario
npm run render       # compress dead air -> out/final/*.mp4 + *.jpg
```

`render` finishes by printing the path to **`out/final/review.html`** and
opening it. That is where you watch them: every clip side by side with its
caption copy, what was typed on camera, and a four-point publish checklist per
clip. Rebuild it any time with `npm run review`.

The page is a plain local file that reads the videos from the folder beside
it. It has no network access of any kind — opening it uploads nothing.

Output also lands in `out/final/` as one `<id>.mp4` and one `<id>.jpg` per
clip, plus a `README.md` listing them with the marketing copy, for whoever
builds the page.

## How it captures (and why it looks better than it used to)

Playwright's built-in video option is hardcoded to a low-bitrate realtime VP8
preset — fine for CI artifacts, and it puts visible noise around text. So this
uses `page.screencast` to take **raw JPEG frames** and encodes them itself:

| choice | why |
|---|---|
| `page.screencast` frames -> our ffmpeg | escapes the hardcoded low-bitrate encoder |
| render at **2x**, encode at **1x** | supersampling — the single biggest lever on text sharpness |
| **1440x810 (16:9)** | web players letterbox anything else; 1440 because the content column is 760px and at 1920 the UI floats in empty margin |
| h264 CRF 18, `yuv420p`, faststart | web-ready and Safari-safe |

⚠ Frames arrive **only on visual change** — measured at 3 frames across 33s on
a static page. So a still hold costs one frame with a long duration, and
`render.mjs` rebuilds real time from the recorded per-frame durations. That is
correct and much cheaper than encoding 300 identical frames.

## The one thing to know before publishing

**Waiting time is compressed.** A real turn takes roughly 40 seconds and
sometimes much longer, so raw footage is mostly a spinner. `render.mjs`
shortens *only* the gap between "question submitted" and "answer finished" —
typically to about 1.8s. The typing, the answer, the confirm gate and the
reading pause are all untouched, real time.

That is an ordinary edit, and it is also a claim someone could read as a
speed promise. Put **"waiting time compressed"** under the video wherever it
is published. It costs nothing and it is the difference between an edit and
an overstatement.

## Safety

- **It never approves a write.** The `leak-to-work-order` clip deliberately
  drives an ask that raises a confirm gate — that frame is the most valuable
  one in the set, because it is the proof the product stops and asks. The
  recorder holds on the gate, then clicks **Cancel**. Nothing is ever filed.
  `tests/unit/test_demo_recorder.py` asserts the approve control is not named
  anywhere in the recorder, and that the prohibition still points at a real
  button.
- **It runs against production with real data.** Answers can name real
  residents and real addresses. **Watch every clip before it goes on the
  site.** Where a specific record is needed the scenarios use the fixture
  property (200 E Colfax Ave) rather than a live resident.
- **No password is ever typed, stored or passed.** `npm run login` opens a
  browser, you sign in yourself, and Playwright's session state is saved to
  `.demo-session.json` (gitignored). Delete that file to sign out.

## The scenarios

Edit `scenarios.mjs`. Every entry carries a `hook` (the problem, in a PM's
words — this is your caption) and a `shows` (the capability the clip is
evidence for). If you cannot write the `shows` line, the clip is footage
rather than a demo; a guard fails on thin copy for that reason.

Three clips, in the order I would put them on the page:

| id | what it proves | target |
|---|---|---|
| `leak-to-work-order` | it **acts** — and asks first. The hero clip. | 35-45s |
| `inbox-workflow` | every Buildium event arrives already triaged | 40-50s |
| `product-tour` | what Occupella *is*, without a sales call | 60-75s |

⚠ **A scenario is a SCRIPT, not a prompt.** The first version was one sentence
per clip and the footage was somebody typing. Two rules are now structural:
the question is asked in `preroll` (**before the camera**, so frame one is the
agent already working), and every clip **ends on an artifact** — a confirm
card, a populated inbox, a rendered table. A clip that ends on prose looks
like a chatbot; one that ends on a card looks like software.

Length targets come from the completion data: under 2 minutes holds 88% of
viewers, past 3 drops to ~50%, and a landing-page clip should carry its point
in well under 90 seconds.

## Options

```powershell
npm run record -- --only leak-to-work-order,inbox-workflow   # a subset
npm run render -- --wait 2.5                            # longer "thinking" beat
npm run render -- --crf 18                              # higher quality, bigger file
```

| env | default | |
|---|---|---|
| `HELIXIS_URL` | the deployed app | point at `http://localhost:5173` for a local build |
| `HEADLESS` | `false` | `true` to record without a visible window |
| `DEMO_WIDTH` / `DEMO_HEIGHT` | `1440` / `810` | 16:9 — do not make it 16:10, players letterbox it |
| `DEMO_DPR` | `2` | render scale; 2 is what makes text crisp |
| `DEMO_QUALITY` | `95` | JPEG quality of the captured frames |
| `DEMO_ANSWER_TIMEOUT` | `240000` | give up on a turn after this |
| `DEMO_READY_TIMEOUT` | `60000` | how long to wait for the app to be usable |

## What has and has not been verified

Verified end to end in the dev container against a local build: real frame
capture (74 frames over 8.5s), the timeline planner squeezing dead air
(8.5s → 4.3s), the h264 encode, and the output inspected frame-by-frame —
1440x810, full-bleed white, small monospace legible, no letterbox. Plus the
catalogue's hygiene and every testid checked against the frontend that
defines them. Five mutations, each gated on `node --check`, all caught.

**Not verified from here: the live run.** The dev container cannot reach the
deployed app, so login, the answer-settle loop and the step script against the
real UI have only met a local stub. Expect the first real run to need one
round of adjustment — most likely `hold` values, which are pure taste.

⚠ `inbox-workflow` needs the Inbox to actually have cards. If it is empty the
clip records an empty list, which is worse than not shipping it.
