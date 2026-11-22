#!/usr/bin/env python3
"""Record a long session of the live app, then cut videos out of it afterwards.

⚠ **RECORD LONG, CUT LATER.** The first version timed each clip live: mark the
phases as they happen, warp, done. That is brittle for the obvious reason — a
mark landing half a second early is a take you cannot fix without re-running a
two-minute turn — and it caps you at one video per recording. So capture is now
one continuous pass over a whole SCRIPT of questions and navigation, writing
every frame with its timestamp plus an EVENT LOG of what happened when. The cut
is a separate program that reads that log. One 30-minute session yields every
video you want, re-cut as many times as you like, with no browser and no agent
turn.

    pip install playwright && playwright install chromium   # once
    python scripts/record_demo.py --login                   # sign in once
    python scripts/record_demo.py --record chat-tour        # the long take
    python scripts/record_demo.py --cut chat-tour           # as many times as you like

Editing is a JSON file. ``--cut`` writes ``edl.json`` on the first run — one
segment per event span, with a speed — and reads it on every run after. Change
a number, drop a segment, re-cut in seconds.

⚠ **PYTHONUTF8=1 on Windows** (gotcha 49) — this prints ⚠ and →, and without
UTF-8 mode a redirected run dies on the line announcing that it worked.

Per session, under ``--out``:
    frames/       the captured JPEGs, the raw take — never rewritten by a cut
    events.json   what happened and when
    edl.json      the edit: segments, speeds, labels. Yours to edit.
    <name>.mp4    the cut (WebM if no libx264 — it says so)

⚠ **NO WRITE IS EVER APPROVED.** A step marked ``stop_at_gate`` stops AT the
confirm card. An automated approval is exactly the thing the gate exists to
prevent, and the card appearing IS the story — "it stops and asks" is the
claim, not "it went ahead".

Selectors, the session file and the sign-in flow are imported from
``chat_probe`` rather than copied: three copies of ``COMPOSER`` drift, and that
file's constants carry the live-DOM verification comments (gotcha 25).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _demo_zoom as zoom  # noqa: E402  (path shim above must run first)
from chat_probe import (  # noqa: E402  (path shim above must run first)
    COMPOSER,
    REPLY,
    SESSION_FILE,
    THINKING,
    _start_fresh_thread,
    _wait_idle,
    interactive_login,
)

try:
    from _console import force_utf8_console
except ImportError:  # pragma: no cover - the helper is optional here

    def force_utf8_console() -> None:  # type: ignore[misc]
        return


DEFAULT_URL = "https://your-app.vercel.app"

#: Capture at 2x device pixels and deliver at 1x. Rendering at 2x and
#: downscaling is the single biggest legibility win for UI text — a native-res
#: capture of a web app is the "blurry demo" everyone recognises.
VIEWPORT = {"width": 1440, "height": 900}
DEVICE_SCALE = 2
OUT_W, OUT_H = 1440, 900
FPS = 30

#: Default playback speed per segment kind. 1.0 is real time; 0.0625 is 16x.
#:
#: ⚠ ``think`` is the dead air, and cutting it is the whole point: a turn takes
#: 40-160 seconds and a real-time recording is mostly spinner, which makes the
#: product look slow when the answer is the fast part. ``type`` stays at 1.0
#: because with no narration the question IS the caption — a viewer reads it as
#: it appears. These are only DEFAULTS; the edl is the authority.
SPEEDS: dict[str, float] = {
    "intro": 1.0,
    "type": 1.0,
    "think": 0.0625,
    "answer": 0.5,
    "hold": 1.0,
    "nav": 1.0,
    "read": 1.0,
}

#: Per-character typing delay, ms. Fast enough not to bore, slow enough to read.
TYPE_DELAY_MS = 42

#: The confirm card, verified against ``ConfirmCard.tsx``.
CONFIRM_CARD = "[data-testid='confirm-gate']"

#: Session scripts. Each is a continuous take; the cut carves videos out of it.
#:
#: ⚠ ``chat-tour`` deliberately mixes a lookup, a cross-source question, a
#: FAIR-HOUSING refusal and a write that stops at the gate. The refusal is the
#: differentiator — every competitor demos retrieval — but see gotcha 52: the
#: failure mode is declining and then OFFERING a proxy (school ratings, crime
#: stats). Watch that answer before shipping the clip; a bad roll is a
#: compliance problem on your landing page, not just a weak take.
SCRIPTS: dict[str, list[dict[str, Any]]] = {
    "chat-tour": [
        {"ask": "What needs my attention today?"},
        {"ask": "Which leases expire in the next 60 days?", "same_thread": True},
        {
            "ask": ("What's the family situation like around 200 E Colfax Ave — lots of kids?"),
            "same_thread": True,
        },
        {
            "ask": (
                "Add a note to the open work order at 200 E Colfax Ave "
                "saying the vendor confirmed Friday."
            ),
            "same_thread": True,
            "stop_at_gate": True,
        },
    ],
    # ⚠ Runs against a DEMO BUILD (VITE_DEMO_MODE=true), which has no backend —
    # so it navigates and reads and never asks a question. A chat turn there
    # would hang on a server that does not exist, and the honest reason to keep
    # the two apart is larger than that: page fixtures are fine to film, but a
    # FABRICATED agent answer is the one thing this product must never show.
    # Chat footage comes from `chat-tour`, against the real backend.
    "product-tour": [
        {"pause": 1.5},
        {
            "nav": "Inbox",
            "read": 3.5,
            "clicks": [["AC Not Cooling", 4.0], ["Rent Charged Below the Lease", 4.0]],
        },
        {
            "nav": "Operations",
            "read": 3.0,
            "clicks": [["Tenants", 3.0], ["Vendors", 3.0], ["Team", 2.5]],
        },
        {
            "nav": "Leasing",
            "read": 3.5,
            "clicks": [["Jordan Reyes", 4.0], ["Marcus Webb", 4.0]],
        },
    ],
    # The Leasing CRM on its own, because the product tour only lands on it.
    #
    # ⚠ EVERY label here OPENS something. The lead drawer also carries "Call",
    # "Mark as lost" and the five stage buttons — all WRITES, all deliberately
    # absent: a demo build has no backend to take them, and filming a stage
    # change that did not happen is a fabricated capability, not a shortcut.
    # The drawer is closed between leads (✕) because it covers the pipeline,
    # which is what cost the second lead click on the first take.
    "crm-tour": [
        {"pause": 1.0},
        {
            "nav": "Leasing",
            "read": 4.0,
            "clicks": [
                ["Jordan Reyes", 5.0],
                ["✕", 1.2],
                ["Marcus Webb", 5.0],
                ["✕", 1.2],
                ["View all 4", 4.0],
            ],
        },
    ],
    # ⚠ THE LONG TAKE — a property manager's morning, ~25 questions, 30-40
    # minutes of real footage. Written to be EDITED DOWN, not played whole.
    #
    # The four-question `chat-tour` guesses in advance which moments are worth
    # filming. This one does not guess: it runs a plausible day and leaves the
    # choosing until after, when the answers exist and can be watched. That is
    # the founder's call (2026-09-05) and it is the right one — a scripted
    # highlight reel is a bet on which answer will come out well, and this
    # product's answers vary run to run.
    #
    # ⚠ **Some of these WILL come back blank or thin, by design of the take
    # rather than by defect.** A portfolio-wide question can hit the refusal
    # ceiling or the wall-clock deadline and return nothing (gotchas 32/50);
    # a rollup missing one field sends the model into an N+1 loop. Filming 25
    # questions and keeping 8 is the point — a blank costs one clip, where a
    # 4-question take that loses one loses a quarter of the video.
    #
    # ⚠ **Fresh thread per question unless a follow-up is the POINT.** Gotcha
    # 42: a shared thread lets a miss on one question harden into an asserted
    # absence on the next, and then generalise to a source that turn never
    # touched. The `same_thread` pairs here are deliberate — a follow-up that
    # builds on the previous answer is the single best thing this product
    # does on camera, and it only works inside one thread.
    #
    # ⚠ **Exactly ONE step writes, and it stops at the gate.** Everything else
    # reads. `stop_at_gate` films the confirm card and never answers it; no
    # take approves a write, ever.
    #
    # Cost, from measured per-turn spend (p50 $0.07, p95 $0.60): roughly
    # $2-15 for the whole take. A projection, not a measurement of this run.
    "pm-day": [
        {"pause": 2.0},
        # — the morning triage —
        {"ask": "What needs my attention today?"},
        {"ask": "Which tenants are behind on rent?"},
        {
            "ask": "Which of those already have open maintenance?",
            "same_thread": True,  # the join is the demo
        },
        {"ask": "Show me open work orders by property as a chart."},
        {"nav": "Operations", "read": 3.0, "clicks": [["Tenants", 3.0], ["Vendors", 3.0]]},
        # — the lease book —
        {"ask": "Which leases expire in the next 60 days?"},
        {"ask": "Who are the tenants on those, and what do they pay?", "same_thread": True},
        {"ask": "Are any of my units vacant right now?"},
        {"ask": "Which tenants are completely paid up?"},
        # — the money —
        {"ask": "What do I owe vendors right now?"},
        {"ask": "Which property is costing me the most in repairs?"},
        {"ask": "Summarize this month's spend by category."},
        {"nav": "Inbox", "read": 3.0, "clicks": [["AC Not Cooling", 3.5]]},
        # — maintenance —
        {"ask": "Are there any work orders stalled more than a week?"},
        {"ask": "Which vendor do we use most, and how fast do they close?"},
        {"ask": "What's happened at 200 E Colfax Ave in the last 30 days?"},
        # — a one-fact question, for pacing: the fast answer next to the slow ones —
        {"ask": "What's the open balance for unit 4B?"},
        {"nav": "Leasing", "read": 3.0, "clicks": [["Jordan Reyes", 4.0], ["✕", 1.2]]},
        # — leasing —
        {"ask": "Which leads have gone quiet?"},
        {"ask": "Draft a follow-up to the one that's been quiet longest.", "same_thread": True},
        # — the refusal. ⚠ WATCH THIS CLIP BEFORE PUBLISHING (gotcha 52): the
        #   failure is declining correctly and then OFFERING school ratings or
        #   crime stats, which is the same violation. Re-run if it does that.
        {"ask": "What's the family situation like around 200 E Colfax Ave — lots of kids?"},
        # — the flagship. Slow, expensive, and the thing nothing else does —
        {"ask": "Give me a full portfolio review."},
        # — memory, which only reads as real on a thread with history —
        #
        # ⚠ THIS STEP CONTRADICTED ITS OWN COMMENT UNTIL 2026-09-06. Without
        # `same_thread`, `_do_ask` calls `_start_fresh_thread` first, so the
        # question about what came earlier was asked in an EMPTY thread — the
        # one state the comment says makes the beat not read as real.
        # `load_session_history` is per-session, so there was nothing to
        # recall; Mem0 stores company FACTS, not the order of today's
        # questions. The beat could not have worked, and the reason was one
        # missing key rather than anything about memory.
        #
        # ⚠ It is still THIN and that is a content decision, not a bug: the
        # thread it now joins holds only the portfolio review, so the honest
        # answer is "the portfolio review". Making it land properly means
        # chaining several consecutive asks into one thread, which changes
        # the shape of the take — the founder's call, not a silent edit.
        {"ask": "What did I ask you about first today?", "same_thread": True},
        # — the write, and the ONLY one. Stops at the card. —
        {
            "ask": (
                "Add a note to the open work order at 200 E Colfax Ave "
                "saying the vendor confirmed Friday."
            ),
            "stop_at_gate": True,
        },
        # — the close —
        {"ask": "What should I deal with first thing tomorrow?"},
    ],
    # ⚠ THE TAKE TO SHOOT WHILE BUILDIUM IS DEAD (2026-09-07).
    #
    # Blue Ridge's stored Buildium credential is REJECTED. Measured, not
    # inferred — the webhook worker's backfill at 16:48 that day logged
    # ``Buildium 401 … "Authentication failed. Please check your client id and
    # secret"`` on every endpoint it tried (/rentals, /leases, /tenants,
    # /tasks, /vendors, /bills, /workorders, /glaccounts, /bankaccounts,
    # /applicants, /leases/outstandingbalances), for BOTH credentialed
    # companies. There is no key to reconnect with, so `pm-day` cannot be
    # re-shot as written.
    #
    # ⚠ Do NOT read `helixis_mirror_sync_state` as a liveness signal here.
    # The `transactions` row is stamped on every pass while the other 13
    # stages sit at their last success, so "one fresh stamp" looked like a
    # working credential and was not — gotcha 57's rule ("a stamp is evidence
    # a stage did not error") does not hold for that stage. The Render log is
    # what settled it.
    #
    # **The mirror still holds the whole portfolio** — 50 properties, 50
    # active leases, 75 tenants, 61 work orders, 67 bills, 93 tasks, 20
    # balances, 3 vendors, 7 applicants, 1 bank account. Every tool in
    # `mirror_tools` reads THAT, with no Buildium call, so this take asks
    # only questions that route there. That is not a workaround for the
    # camera: it is the same path a customer hits between syncs.
    #
    # Which is why 6 of the 9 usable clips in the 2026-09-06 `pm-day` take
    # are here unchanged. The three that said "reconnect Buildium" on screen
    # (the work-order chart, stalled work orders, unit 4B) reached for LIVE
    # tools; these do not.
    #
    # ⚠ FOUR BEATS ARE DELIBERATELY ABSENT, and each is a fact about the
    # account rather than a taste call:
    #  * **The write / confirm card.** Every write tool confirms the record
    #    exists first (`buildium_agent`'s own rule), that lookup 401s, and
    #    the run stops before the gate — which is exactly what Step 24 did on
    #    2026-09-06. There is no confirm card to film without a credential.
    #    Filming one would need a live Buildium account, not a different
    #    question.
    #  * **Anything leasing.** `crm_leads` holds ZERO rows for this company,
    #    so "which leads have gone quiet" failed with no output and the
    #    `crm-tour` script's "Jordan Reyes" / "Marcus Webb" clicks have no
    #    lead to open either.
    #  * **"Which tenants are completely paid up?"** — `delinquency_report`
    #    returns only leases that HAVE a balance, so the complement is
    #    unreachable from it (gotcha 57). It failed with 0 characters.
    #  * **The memory beat.** Thin by construction (see `pm-day`'s note) and
    #    it answered with the wrong date.
    #
    # ⚠ The freshness footer says how old the mirror is, and that number
    # GROWS — 120h on 2026-09-06, and nothing will reduce it. Most answers
    # never surface it; when one does, that is the product being honest, and
    # backdating `helixis_mirror_sync_state` to hide it would be falsifying
    # the one layer whose whole job is telling the truth about staleness.
    # If it starts leaking into clips, that is a founder decision, not a
    # silent edit.
    "mirror-day": [
        {"pause": 2.0},
        # — the morning triage —
        {"ask": "What needs my attention today?"},
        {"ask": "Which tenants are behind on rent?"},
        # The join, and the single best thing on camera. Same thread or it
        # cannot work (gotcha 42). ⚠ All THREE `same_thread` follow-ups in
        # `pm-day` produced no orchestrator_runs row at all on 2026-09-06 —
        # the timestamps leave no room for them — so this beat is UNPROVEN,
        # not merely unrecorded. Watch it land before counting on it.
        {"ask": "Which of those already have open maintenance?", "same_thread": True},
        {"nav": "Operations", "read": 3.0, "clicks": [["Tenants", 3.0], ["Vendors", 3.0]]},
        # — the lease book. `expiring_leases` gave the best answer of the
        #   whole take: 16s, real dates, and it caught a stale stored note. —
        {"ask": "Which leases expire in the next 60 days?"},
        {"ask": "Who are the tenants on those, and what do they pay?", "same_thread": True},
        # ⚠ A surname that appears EXACTLY ONCE in the mirror, so this hits
        # `contact_lookup`'s single-hit path rather than its ambiguity list —
        # two different code paths that answer the same name oppositely
        # (gotcha 51). Re-pick from the mirror if the fixture changes.
        {"ask": "Who is Jasmine Holloway, and what do they pay?"},
        # — the money —
        {"ask": "What do I owe vendors right now?"},
        {"ask": "How much do we have across our bank accounts?"},
        {"ask": "Summarize this month's spend by category."},
        {"nav": "Inbox", "read": 3.0, "clicks": [["AC Not Cooling", 3.5]]},
        # ⚠ Mirror-backed, and SLOW: 139s across 10 tool calls on
        # 2026-09-06. Kept because vendor performance is the answer nothing
        # else gives; cut it first if the take is running long.
        {"ask": "Which vendor do we use most, and how fast do they close?"},
        # — the refusal. ⚠ WATCH THIS CLIP BEFORE PUBLISHING (gotcha 52):
        #   the failure is declining correctly and then OFFERING school
        #   ratings or crime stats, which is the same violation. It answered
        #   in 5s with zero tool calls on 2026-09-06 and did not do that.
        {"ask": "What's the family situation like around 200 E Colfax Ave — lots of kids?"},
        # — the flagship —
        {"ask": "Give me a full portfolio review."},
        # — the close —
        {"ask": "What should I deal with first thing tomorrow?"},
    ],
    "overview": [
        {"pause": 1.5},
        {"nav": "Inbox", "read": 4.0},
        {"nav": "Operations", "read": 5.0},
        {"nav": "Leasing", "read": 4.0},
        {"ask": "What needs my attention today?"},
    ],
}


@dataclass
class Event:
    t: float
    kind: str
    step: int
    detail: str = ""


@dataclass
class Segment:
    """One cut of the take. ``speed`` overrides the default for ``name``."""

    name: str
    start: float
    end: float
    speed: float | None = None
    label: str = ""
    #: Where this segment pushes in to: a name from ``_demo_zoom.REGIONS``,
    #: optionally ``name@zoom`` ("composer@2.0"). ``None`` takes the per-kind
    #: default; ``"full"`` forces the whole frame. See ``_demo_zoom``.
    focus: str | None = None

    @property
    def rate(self) -> float:
        return self.speed if self.speed is not None else SPEEDS.get(self.name, 1.0)


@dataclass
class Take:
    session: str
    frames: list[tuple[str, float]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def log(self, kind: str, t: float, step: int = -1, detail: str = "") -> None:
        self.events.append(Event(t=t, kind=kind, step=step, detail=detail))


async def _capture(page: Any, take: Take, frames_dir: Path, t0: float) -> Any:
    """Start a CDP screencast, writing every frame with its arrival time.

    ⚠ CDP pushes a frame when the page CHANGES, not on a clock, so the gaps are
    uneven by design — which is exactly what the re-timer needs. Sampling on a
    timer instead (``page.screenshot`` in a loop) costs ~80ms per shot at 2x
    and cannot keep up with typing.

    ⚠ Every frame MUST be acknowledged. Chrome stops sending after a couple of
    unacked frames, so a missing ``screencastFrameAck`` gives you a 2-frame
    video and no error — it reads as "the page never changed".
    """
    cdp = await page.context.new_cdp_session(page)
    seen = {"n": 0}

    def on_frame(evt: dict[str, Any]) -> None:
        n = seen["n"]
        seen["n"] = n + 1
        path = frames_dir / f"f{n:06d}.jpg"
        path.write_bytes(base64.b64decode(evt["data"]))
        take.frames.append((path.name, time.monotonic() - t0))
        asyncio.ensure_future(  # noqa: RUF006 - fire and forget; see the ack note
            cdp.send("Page.screencastFrameAck", {"sessionId": evt["sessionId"]})
        )

    cdp.on("Page.screencastFrame", on_frame)
    await cdp.send(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "quality": 92,
            "maxWidth": VIEWPORT["width"] * DEVICE_SCALE,
            "maxHeight": VIEWPORT["height"] * DEVICE_SCALE,
            "everyNthFrame": 1,
        },
    )
    return cdp


async def _wait_for_first_token(page: Any, replies_before: int, timeout_s: float) -> None:
    """Return when the answer STARTS, which is where the dead air ends.

    ⚠ Not ``_wait_idle`` — that is the turn BOUNDARY, and waiting for it here
    would put the answer inside the compressed span. The distinction is the
    whole point: the wait is worthless to a viewer and the answer is the only
    thing worth watching.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await page.locator(REPLY).count() > replies_before:
            if (await page.locator(REPLY).last.inner_text()).strip():
                return
        if await page.locator(CONFIRM_CARD).count():
            return
        await asyncio.sleep(0.15)
    raise RuntimeError(
        f"no assistant text and no confirm card after {timeout_s:.0f}s — "
        "the turn never started, so there is nothing to record"
    )


#: How far down a section to scroll during its dwell, and how often to step.
#: A quarter-second cadence is slow enough to read and fast enough that the
#: screencast has something to encode between the two ends.
_SCROLL_TICK_S = 0.25
_SCROLL_SETTLE_S = 0.7


_FIND_SCROLLER = """
() => {
  let best = document.scrollingElement || document.body, gap = 0;
  const room = (el) => el.scrollHeight - el.clientHeight;
  gap = room(best);
  for (const el of document.querySelectorAll('*')) {
    const ov = getComputedStyle(el).overflowY;
    if (ov !== 'auto' && ov !== 'scroll') continue;
    const g = room(el);
    if (g > gap) { gap = g; best = el; }
  }
  best.setAttribute('data-demo-scroller', '1');
  return gap;
}
"""


async def _dwell(page: Any, seconds: float) -> None:
    """Hold on a section — scrolling it, if it has anywhere to go.

    ⚠ **A still page emits NO frames.** Chrome's ``Page.startScreencast``
    fires on paint, so a five-second look at a static screen produces exactly
    one frame and the cut has nothing to show for the time it spent. The first
    product-tour take was 66 frames over 21 seconds for that reason: three
    transitions and three freezes.

    So the dwell MOVES the page. Scrolling is also the honest thing to film —
    it is what a person does when they land on a list, and it reveals rows a
    frozen viewport hides.

    ⚠ **Falls back to a plain wait when there is nothing to scroll.** A short
    page has no scroll room, and forcing a wheel event on it would produce the
    same single frame plus a misleading log line. A dwell that cannot move
    says so by simply waiting.
    """
    settle = min(_SCROLL_SETTLE_S, seconds)
    await asyncio.sleep(settle)
    remaining = seconds - settle
    if remaining <= 0:
        return
    try:
        room = float(await page.evaluate(_FIND_SCROLLER) or 0)
    except Exception:  # noqa: BLE001 - motion is a nicety, never the take
        room = 0.0
    if room < 40:
        await asyncio.sleep(remaining)
        return

    ticks = max(1, int(remaining / _SCROLL_TICK_S))
    # Stop short of the bottom: the last rows of a list are usually padding,
    # and ending mid-content reads as "there is more" rather than "that's all".
    step_px = (room * 0.85) / ticks
    for n in range(1, ticks + 1):
        with contextlib.suppress(Exception):
            await page.evaluate(
                "(y) => { const el = document.querySelector('[data-demo-scroller]');"
                " if (el) el.scrollTop = y; }",
                step_px * n,
            )
        await asyncio.sleep(_SCROLL_TICK_S)


#: Sign-in affordances, for telling "signed out" apart from "not painted yet".
#: Deliberately several: the wording of a login button is the kind of thing a
#: design pass changes, and a check that silently stops recognising the
#: signed-out state degrades to the race it replaced.
_SIGNED_OUT = (
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "input[type='password']",
    "[data-testid='sign-in']",
)

#: How long to let the app hydrate before deciding. A React bundle on a cold
#: Vercel edge is comfortably under this; a slow one that exceeds it reports
#: UNDECIDED rather than a wrong answer.
_HYDRATION_TIMEOUT_S = 20.0


async def _sign_in_form_present(page: Any) -> bool:
    """Positive evidence that we have been signed OUT. One look, no waiting.

    Used mid-take, where the page is already loaded and the question is only
    "has a login form appeared". Distinct from `signed_in_verdict`, which
    waits for hydration because nothing has painted yet.
    """
    for sel in _SIGNED_OUT:
        with contextlib.suppress(Exception):
            if await page.locator(sel).count():
                return True
    return False


async def signed_in_verdict(page: Any) -> str:
    """`signed-in` / `signed-out` / `undecided (…)` — after hydration.

    ⚠ **The old check ran at `domcontentloaded` and was a RACE.** A React app
    has painted nothing at that moment, so `locator(COMPOSER).count()` is 0
    for a signed-IN app too. It could therefore refuse a perfectly good
    session, or — with any incidental delay before it — pass on a sign-in
    screen and hand the recorder 40 minutes of a login form. Both directions
    are wrong for the same reason: it read a MECHANISM (is this node here
    yet) as a PROPERTY (are we signed in), which is gotcha 84's shape.

    So it now waits for EITHER side to appear and reports which. Polling both
    is the whole point — waiting only for the composer cannot distinguish a
    signed-out app from a slow one, and it is the difference between "log in"
    and "your backend is down" as the instruction the operator gets.

    ⚠ **UNDECIDED is never a pass.** Neither surface appearing inside the
    window means the page did not load, and recording that is the same wasted
    take as recording a login form (LED-013's skip-floor: a harness that could
    not tell is not one that validated anything).
    """
    deadline = time.monotonic() + _HYDRATION_TIMEOUT_S
    while time.monotonic() < deadline:
        if await page.locator(COMPOSER).count():
            return "signed-in"
        for sel in _SIGNED_OUT:
            with contextlib.suppress(Exception):
                if await page.locator(sel).count():
                    return "signed-out"
        await asyncio.sleep(0.25)
    return f"undecided (neither composer nor a sign-in form after {_HYDRATION_TIMEOUT_S:.0f}s)"


async def _do_clicks(page: Any, clicks: list) -> None:
    """Open things inside a section — the tab, the card, the lead.

    ⚠ **This is the fix for a still page, not scrolling.** Measured on the
    demo build: Inbox has 179px of scroll room, Operations 0, Leasing 57 — the
    fixtures fit the viewport, so there is nowhere to scroll TO and a wheel
    event would produce the same single frame. Opening a tab or a card both
    moves the screen and shows a layer of the product a tour of three landing
    screens never reaches.

    ⚠ **Read-only labels only.** A demo build has no backend, so a click that
    would WRITE has nothing to talk to — and, more to the point, filming a
    fabricated write is the one thing this product must never show. Every
    entry here opens or selects; nothing here submits.

    A label that cannot be found costs its click and nothing else: the section
    is already on film, and losing the whole take over one renamed tab is the
    trade `run_take`'s per-step guard exists to avoid.
    """
    for entry in clicks:
        label, hold = entry if isinstance(entry, (list, tuple)) else (entry, 3.0)
        try:
            await page.get_by_text(str(label), exact=False).first.click(timeout=4000)
        except Exception as exc:  # noqa: BLE001 - one label, not the section
            print(f"    ⚠ click {label!r} skipped: {type(exc).__name__}")
            continue
        await _dwell(page, float(hold))


async def _return_to_chat(page: Any) -> None:
    """Put the app back on the chat surface before asking a question.

    ⚠ **A nav step leaves the app where it navigated TO.** `pm-day` tours
    Operations, Inbox and Leasing between questions, and none of those pages
    has a composer — so `_wait_idle` timed out at 30s on the very next ask,
    and would have done so on every ask until something went back. Seven
    questions lost to one missing click.

    It lives HERE rather than as a `{"nav": "Chat"}` step in each script for
    the reason gotcha 81 gives about per-caller fixes: the next script that
    interleaves a tour would silently re-open it. Asking a question implies
    being on the chat page, so the ask is where that gets guaranteed.

    A no-op when the composer is already there, which is the common case —
    so this costs one `count()` on a normal question.
    """
    if await page.locator(COMPOSER).count():
        return
    with contextlib.suppress(Exception):
        await (
            page.get_by_role("link", name="Chat")
            .or_(page.get_by_role("button", name="Chat"))
            .or_(page.get_by_text("Chat", exact=True))
            .first.click(timeout=5000)
        )
        # Give the route change time to paint. Deliberately a WAIT FOR THE
        # COMPOSER, not a sleep: the point is to hand `_wait_idle` a page it
        # can actually judge, and a fixed sleep either wastes time or lies.
        await page.locator(COMPOSER).first.wait_for(timeout=10_000)


async def _wait_for_gate(page: Any, *, timeout_s: float) -> None:
    """Block until the confirm card is visible, and say what happened if it never is.

    ⚠ **The take of 2026-09-06 lost its ONE write step to a bare
    ``wait_for(state="visible")``**, which raises a Playwright ``TimeoutError``
    naming the selector and nothing else. Four hundred seconds of a real,
    paid run produced one fact — *no element matched* — and every candidate
    explanation was equally consistent with it: the model refused, the run hit
    its deadline, the gate rendered in a thread the card was not showing, the
    tool was never reached, the stream died. Three of those were ruled out
    afterwards by reading code, which is exactly the work a diagnostic would
    have skipped.

    That is gotcha 43's lesson, and `_wait_idle` in ``chat_probe`` already
    carries the fix — *"a wait that cannot say what it was waiting for is not
    diagnosable"*. This is the same rule reaching the other wait in the same
    take. The state below is what separates the candidates:

    * **composer enabled + no thinking pill** — the run FINISHED without a
      gate. The reply text is the model's own account of why, and it is
      quoted, because a refusal ("I could not find an open work order there")
      and a deadline are one sentence apart and indistinguishable otherwise.
    * **still thinking** — the run was alive at the cut-off, so the ceiling is
      the wall clock or the cost gate, not the write surface.
    * **a reply that never started** — the stream never delivered, which is a
      transport question and not an agent one.

    It NEVER clicks and never approves; it only reads. The module docstring's
    rule is absolute and a diagnostic must not be the thing that breaks it.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await page.locator(CONFIRM_CARD).count():
            return
        await asyncio.sleep(0.25)

    thinking = await page.locator(THINKING).count()
    try:
        enabled = await page.locator(COMPOSER).first.is_enabled()
    except Exception:  # pragma: no cover - the composer can be absent entirely
        enabled = False
    reply = ""
    with contextlib.suppress(Exception):
        if await page.locator(REPLY).count():
            reply = (await page.locator(REPLY).last.inner_text()).strip()
    # ⚠ Truncated, and the tail is what matters: a refusal names its reason at
    # the END of the sentence, and the deadline salvage labels itself there too.
    tail = reply[-400:] if len(reply) > 400 else reply
    raise RuntimeError(
        f"no confirm card after {timeout_s:.0f}s "
        f"(composer enabled={enabled}, thinking pills={thinking}, "
        f"reply chars={len(reply)}). The turn's last words were: {tail!r}"
    )


async def _do_ask(page: Any, take: Take, t0: float, i: int, step: dict, settle_s: float) -> None:
    question = step["ask"]
    await _return_to_chat(page)
    if not step.get("same_thread"):
        await _wait_idle(page, timeout_s=settle_s)
        await _start_fresh_thread(page)
        await asyncio.sleep(0.8)

    take.log("step_start", time.monotonic() - t0, i, question)
    composer = page.locator(COMPOSER).first
    await composer.click()
    take.log("type_start", time.monotonic() - t0, i)
    try:
        await composer.press_sequentially(question, delay=TYPE_DELAY_MS)
    except AttributeError:  # older Playwright
        await composer.type(question, delay=TYPE_DELAY_MS)
    await asyncio.sleep(0.5)

    replies_before = await page.locator(REPLY).count()
    await composer.press("Enter")
    take.log("sent", time.monotonic() - t0, i)

    await _wait_for_first_token(page, replies_before, timeout_s=settle_s)
    take.log("first_token", time.monotonic() - t0, i)

    if step.get("stop_at_gate"):
        # ⚠ Stop AT the card. Never click it — see the module docstring.
        await _wait_for_gate(page, timeout_s=settle_s)
        await asyncio.sleep(1.0)
        take.log("gate_shown", time.monotonic() - t0, i)
    else:
        with contextlib.suppress(RuntimeError):
            await _wait_idle(page, timeout_s=settle_s)
        take.log("answer_done", time.monotonic() - t0, i)

    await asyncio.sleep(2.5)
    take.log("step_end", time.monotonic() - t0, i)


async def record_session(name: str, url: str, out: Path, headed: bool, settle_s: float) -> Take:
    from playwright.async_api import async_playwright

    steps = SCRIPTS[name]
    frames_dir = out / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    take = Take(session=name)

    async with async_playwright() as pw:
        # ⚠ Escape hatch for a pip/browser version mismatch, which reports as
        # "Executable doesn't exist at …chromium_headless_shell-<N>" and reads
        # like a broken install rather than two pinned versions disagreeing.
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
        browser = await pw.chromium.launch(headless=not headed, executable_path=exe)
        ctx = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=DEVICE_SCALE,
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        verdict = await signed_in_verdict(page)
        if verdict != "signed-in":
            await browser.close()
            raise RuntimeError(
                f"{verdict} — refusing to record. ({SESSION_FILE}: "
                f"{'present' if SESSION_FILE.exists() else 'MISSING'})\n"
                "Run:  python scripts/record_demo.py --login --url " + url
            )
        await _wait_idle(page, timeout_s=60)

        t0 = time.monotonic()
        cdp = await _capture(page, take, frames_dir, t0)
        take.log("session_start", 0.0)
        await asyncio.sleep(1.2)

        for i, step in enumerate(steps):
            # ⚠ **Re-check that we are still signed in, every step.** The check
            # above runs ONCE, before the first frame, and cannot see a session
            # that dies at question 12 — the app drops to the sign-in form and
            # the screencast happily films it for the remaining twenty. On a
            # 40-minute take that is the whole take, discovered at the cut.
            #
            # This is the harness reporting on the MECHANISM (frames captured)
            # rather than the PROPERTY (a signed-in app was on screen), which
            # is the shape gotcha 84 names — a check pointed at the wrong
            # question reads green while the thing it exists for is broken.
            #
            # It ABORTS rather than skipping: every later step would fail the
            # same way, and the frames already captured are still cuttable.
            #
            # ⚠ **It asks whether a SIGN-IN FORM is present — never whether the
            # composer is absent.** The first cut checked `not
            # locator(COMPOSER).count()` and killed a perfectly healthy take at
            # step 6, because step 5 navigates to Operations and that page has
            # no chat composer. Absence of the composer is not absence of a
            # session; on a multi-page app it is the normal state of four of
            # the five screens. That is the SAME mistake as the start check it
            # was written to complete — asserting a MECHANISM (is this node
            # here) instead of the PROPERTY (are we signed out) — and it cost a
            # live take four questions in.
            #
            # The asymmetry is deliberate: only POSITIVE evidence of a sign-in
            # form aborts. A page we cannot classify keeps recording, because a
            # false abort throws away a real take while a missed one costs the
            # frames the final step-count check catches anyway.
            if await _sign_in_form_present(page):
                take.log("session_lost", time.monotonic() - t0, i)
                print(
                    f"  ⚠ signed out at step {i} — aborting the take.\n"
                    f"    {i} step(s) are on film and still cuttable; the rest "
                    "would have recorded a sign-in screen."
                )
                break
            try:
                if "ask" in step:
                    await _do_ask(page, take, t0, i, step, settle_s)
                elif "nav" in step:
                    take.log("nav_start", time.monotonic() - t0, i, step["nav"])
                    # ⚠ Three ways to find it. The sidebar renders as buttons
                    # in one build and links in another, and a nav item the
                    # locator cannot see fails the whole STEP, not just a
                    # click — losing that section from the take.
                    await (
                        page.get_by_role("link", name=step["nav"])
                        .or_(page.get_by_role("button", name=step["nav"]))
                        .or_(page.get_by_text(step["nav"], exact=True))
                        .first.click()
                    )
                    await _dwell(page, float(step.get("read", 3.0)))
                    await _do_clicks(page, step.get("clicks") or [])
                    take.log("nav_done", time.monotonic() - t0, i, step["nav"])
                elif "pause" in step:
                    await asyncio.sleep(float(step["pause"]))
                take.log("step_ok", time.monotonic() - t0, i)
            except Exception as exc:  # noqa: BLE001
                # ⚠ One bad step must not cost the whole take. A 30-minute
                # session is expensive in agent turns AND wall clock; the
                # frames already captured are still cuttable, and the log says
                # exactly which step died so the edl can skip it.
                take.log("step_failed", time.monotonic() - t0, i, f"{type(exc).__name__}: {exc}")
                print(f"  ⚠ step {i} failed and was skipped: {exc}")

        take.log("session_end", time.monotonic() - t0)
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.4)  # let in-flight frames land
        await browser.close()

    return take


def default_edl(take: Take) -> list[Segment]:
    """Turn the event log into a first draft of the edit.

    Every span between two events becomes a segment with a sensible speed. This
    is a STARTING POINT, written to disk once and then owned by whoever edits
    it — which is the whole reason capture and cut are separate programs.
    """
    segs: list[Segment] = []
    by_step: dict[int, dict[str, float]] = {}
    for ev in take.events:
        by_step.setdefault(ev.step, {})[ev.kind] = ev.t

    start = take.events[0].t if take.events else 0.0
    first_step = min((s for s in by_step if s >= 0), default=None)
    if first_step is not None:
        segs.append(
            Segment("intro", start, by_step[first_step].get("step_start", start), label="open")
        )

    for step in sorted(s for s in by_step if s >= 0):
        m = by_step[step]
        if "nav_start" in m:
            segs.append(
                Segment("nav", m["nav_start"], m.get("nav_done", m["nav_start"]), label="nav")
            )
            continue
        if "type_start" not in m:
            continue
        end = m.get("gate_shown") or m.get("answer_done")
        if end is None:
            continue  # a failed step: nothing coherent to cut
        segs.append(Segment("type", m["type_start"], m["sent"], label=f"q{step} typing"))
        segs.append(Segment("think", m["sent"], m["first_token"], label=f"q{step} waiting"))
        segs.append(Segment("answer", m["first_token"], end, label=f"q{step} answer"))
        segs.append(Segment("hold", end, m.get("step_end", end), label=f"q{step} hold"))
    return segs


def _frame_size(frames_dir: Path) -> tuple[int, int] | None:
    """The real (width, height) of a captured frame, or None if unreadable.

    ⚠ Reads the JPEG's own SOF marker rather than trusting a constant, and
    deliberately does NOT need Pillow — `build_sequence` runs before the
    cropper's dependency check, and a zoom pointed at the wrong region is
    exactly the failure that must not depend on an optional import.

    Returns None rather than guessing: the caller falls back to the computed
    size and the crop is no worse than it was, whereas a fabricated dimension
    would silently re-create the defect this exists to close.
    """
    frames = sorted(frames_dir.glob("*.jpg")) or sorted(frames_dir.glob("*.jpeg"))
    if not frames:
        return None
    try:
        data = frames[0].read_bytes()
    except OSError:
        return None
    i = 2  # skip SOI
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF15 carry the dimensions; C4/C8/CC are DHT/JPG/DAC, not SOF.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height) if width and height else None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    return None


def build_sequence(
    take: Take, segments: list[Segment], out: Path
) -> tuple[list[Path], list[tuple[int, int, int, int] | None], float]:
    """Warp each segment's real time into playback time, then resample to FPS.

    ⚠ Segments need NOT cover the whole take, and that is what makes this an
    editor rather than a re-timer: anything outside them is dropped.

    ⚠ Resampling onto a FIXED grid rather than emitting each captured frame
    with its own duration. CDP frame gaps are uneven, so per-frame durations
    stutter in exactly the places the page was busiest — the typing and the
    streaming answer, i.e. the only parts anyone watches.
    """
    if len(take.frames) < 2:
        raise RuntimeError(
            f"only {len(take.frames)} frame(s) captured — the screencast never "
            "ran. Check that every frame was acknowledged (see _capture)."
        )

    # ⚠ Each entry carries its SEGMENT and the playback clock that segment
    # started at, because the zoom ramp is a function of position WITHIN a
    # segment. Deriving it later from the frame's absolute time would need the
    # warp inverted, and the warp is not injective — a still screen emits one
    # frame for a span, so several playback times map back to one capture time.
    warped: list[tuple[str, float, Segment, float]] = []
    clock = 0.0
    for seg in segments:
        seg_start_clock = clock
        inside = [(n, t) for n, t in take.frames if seg.start <= t < seg.end]
        if not inside:
            # ⚠ A segment with no frames is normal, not an error: a still
            # screen emits none. Carry its duration on the frame that was last
            # shown, or the video cuts on the final streamed character with no
            # beat — worst of all on a landing-page loop, which then snaps
            # straight back to a blank composer.
            if warped:
                clock += (seg.end - seg.start) * seg.rate
                warped.append((warped[-1][0], clock, seg, seg_start_clock))
            continue
        prev_t = inside[0][1]
        for name, t in inside:
            clock += (t - prev_t) * seg.rate
            prev_t = t
            warped.append((name, clock, seg, seg_start_clock))
        if seg.end > prev_t:  # trailing stillness inside this segment
            clock += (seg.end - prev_t) * seg.rate
            warped.append((warped[-1][0], clock, seg, seg_start_clock))

    if not warped:
        raise RuntimeError("no frames fell inside any segment — check edl.json against events.json")

    # Playback duration per segment, for the ramp. Taken from the warped clock
    # rather than (end-start)*rate so a segment whose frames were clipped by
    # the take's end ramps over the time it ACTUALLY occupies.
    seg_span: dict[int, tuple[float, float]] = {}
    for _n, c, seg, s0 in warped:
        lo, hi = seg_span.get(id(seg), (s0, c))
        seg_span[id(seg)] = (min(lo, s0), max(hi, c))

    # ⚠ MEASURED, never computed. This read `VIEWPORT["width"] * DEVICE_SCALE`
    # and the frames on disk are 1440x900 — Chrome's screencast `maxWidth` is a
    # CEILING, not a target, and it delivers at the surface size regardless of
    # `device_scale_factor`. So the crop rect was built against 2880x1800 and
    # applied to a 1440x900 image: the composer centre at 0.60 landed at x=1728
    # in a 1440-wide frame, got clamped to the right edge, and the zoom pushed
    # into the WRONG REGION and upscaled on the way out. Both halves were
    # invisible — a plausible video, softer and pointed somewhere else.
    frame_w, frame_h = _frame_size(seq_dir := out / "frames") or (
        VIEWPORT["width"] * DEVICE_SCALE,
        VIEWPORT["height"] * DEVICE_SCALE,
    )
    headroom = zoom.max_lossless_zoom(frame_w, OUT_W)
    if headroom <= 1.0001:
        # Not a warning about taste — at 1:1 there is no headroom, so ANY crop
        # is an upscale. Say the measurement, so the next reader can act on it
        # rather than re-deriving it from a soft video.
        print(
            f"  ⚠ frames are {frame_w}x{frame_h} and output is {OUT_W}x{OUT_H} — "
            "no zoom headroom, so the push-in is DISABLED (it would upscale). "
            "See scripts/_demo_zoom.py."
        )

    total = warped[-1][1]
    step = 1.0 / FPS
    seq: list[Path] = []
    rects: list[tuple[int, int, int, int] | None] = []
    i = k = 0
    while k * step <= total:
        target = k * step
        while i + 1 < len(warped) and warped[i + 1][1] <= target:
            i += 1
        name, _clock, seg, seg_start_clock = warped[i]
        seq.append(seq_dir / name)
        lo, hi = seg_span[id(seg)]
        focus = zoom.clamp_to_headroom(zoom.focus_for(seg.name, seg.rate, seg.focus), headroom)
        rects.append(
            zoom.crop_rect(
                focus,
                zoom.ramp(target - lo, max(0.0, hi - lo)),
                frame_w,
                frame_h,
                OUT_W,
                OUT_H,
            )
        )
        k += 1
    return seq, rects, total


def find_ffmpeg() -> str | None:
    """A real ffmpeg on PATH, else Playwright's bundled one.

    ⚠ The bundled binary is built ``--disable-everything``: VP8 only, no
    libx264, and no concat demuxer. So it is a fallback that changes the OUTPUT
    FORMAT, not a drop-in — ``render`` asks which encoders exist rather than
    assuming, because an h264 command against it fails with "Unknown encoder"
    at the very end of a run that already spent the recording.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for base in (
        Path("/opt/pw-browsers"),
        Path.home() / "AppData/Local/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ):
        for path in sorted(base.glob("ffmpeg-*/ffmpeg*")):
            if path.is_file():
                return str(path)
    return None


def has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return any(line.split()[1:2] == [name] for line in out.stdout.splitlines() if line.strip())


def _cropper(rects: list[tuple[int, int, int, int] | None]) -> Any:
    """A per-frame crop, or None when there is nothing to do.

    ⚠ Degrades LOUDLY rather than silently rendering a flat video. A missing
    Pillow is a one-line install, and a demo that quietly lost its polish pass
    is discovered by watching all 25 clips — the same class as `render()`'s own
    "no libx264, writing VP8" warning beside it.
    """
    if not any(rects):
        return None
    try:
        from PIL import Image
    except ImportError:
        print(
            "  ⚠ no Pillow — rendering WITHOUT the zoom pass.\n"
            "    pip install pillow    (then re-cut; the frames are untouched)"
        )
        return None

    import io

    def crop(data: bytes, rect: tuple[int, int, int, int] | None) -> bytes:
        if rect is None:
            return data
        left, top, w, h = rect
        img = Image.open(io.BytesIO(data))
        img = img.crop((left, top, left + w, top + h))
        # Resize HERE rather than leaving it to ffmpeg's single scale filter:
        # frames of differing sizes on one image2pipe stream make the decoder
        # re-init mid-stream, which the bundled build does not survive.
        img = img.resize((OUT_W, OUT_H), Image.LANCZOS)
        buf = io.BytesIO()
        # Quality 95: this frame is re-encoded a second time, and the h264 pass
        # after it is what sets the real ceiling. Anything lower stacks two
        # lossy passes on the text the zoom exists to make readable.
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    return crop


def render(
    seq: list[Path],
    out: Path,
    name: str,
    ffmpeg: str,
    rects: list[tuple[int, int, int, int] | None] | None = None,
) -> Path:
    """Pipe the cut frames into ffmpeg on stdin.

    ⚠ NOT the concat demuxer. Playwright's bundled ffmpeg enables exactly four
    things — ``image2pipe``, the mjpeg decoder, the webm muxer and libvpx — so
    ``-f concat`` dies with "Unrecognized option 'safe'", which reads like a bad
    argument rather than a binary that was never compiled with the demuxer.
    ``image2pipe`` is the one input path that works on BOTH that build and a
    full ffmpeg, and repeating a frame is just writing its bytes twice.
    """
    common = [
        ffmpeg,
        "-y",
        "-f",
        "image2pipe",
        # ⚠ Both load-bearing on the bundled build, and each fails by naming
        # the wrong layer. ``-i -`` gives "Protocol not found" (only ``pipe``
        # and ``file`` are compiled in), and without ``-vcodec mjpeg`` the probe
        # reports "Video: none" and then "Output file does not contain any
        # stream" — an output error caused by a missing input decoder. A full
        # ffmpeg accepts both forms, so this stays portable.
        "-vcodec",
        "mjpeg",
        "-framerate",
        str(FPS),
        "-i",
        "pipe:0",
        "-vf",
        f"scale={OUT_W}:{OUT_H}:flags=lanczos",
        "-r",
        str(FPS),
        "-an",
    ]
    # ⚠ **ABSOLUTE, and create the directory first.** A relative output path
    # makes the write depend on ffmpeg's working directory matching Python's,
    # and when it does not the failure reads as
    # `Error opening output demos\\pm-day\\pm-day.webm: No such file or
    # directory` — which names the FILE and sounds like a missing input, while
    # the real subject is the DIRECTORY the child process could not see. It
    # then surfaces as `BrokenPipeError` from the frame writer, three layers
    # from the cause. Resolving here costs nothing and removes the ambiguity.
    out.mkdir(parents=True, exist_ok=True)
    out_abs = out.resolve()
    if has_encoder(ffmpeg, "libx264"):
        target = out_abs / f"{name}.mp4"
        cmd = [
            *common,
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    else:
        # ⚠ Say so, loudly. A landing page wants h264/MP4 for the widest
        # coverage; silently shipping VP8 is the kind of substitution only
        # discovered by a viewer whose browser shows a black box.
        target = out_abs / f"{name}.webm"
        print(
            f"  ⚠ no libx264 in {ffmpeg} — writing VP8 WebM instead of MP4.\n"
            "    Install a full ffmpeg for MP4:  winget install ffmpeg"
        )
        cmd = [*common, "-c:v", "libvpx", "-b:v", "2M", "-crf", "10", str(target)]

    crop = _cropper(rects) if rects else None
    if crop is not None:
        zoomed = sum(1 for r in (rects or []) if r is not None)
        print(f"  zoom pass: {zoomed} of {len(seq)} frames cropped")

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=out)
    assert proc.stdin is not None
    try:
        for idx, path in enumerate(seq):
            data = path.read_bytes()
            if crop is not None:
                data = crop(data, (rects or [])[idx])
            proc.stdin.write(data)
    except BrokenPipeError as exc:  # ffmpeg died early — its stderr says why
        raise RuntimeError(f"ffmpeg exited while frames were being written: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode} — see its output above")
    return target


def save_take(take: Take, out: Path) -> None:
    (out / "events.json").write_text(
        json.dumps(
            {
                "session": take.session,
                "frames": [[n, round(t, 4)] for n, t in take.frames],
                "events": [
                    {"t": round(e.t, 3), "kind": e.kind, "step": e.step, "detail": e.detail}
                    for e in take.events
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_take(out: Path) -> Take:
    data = json.loads((out / "events.json").read_text(encoding="utf-8"))
    take = Take(session=data["session"])
    take.frames = [(n, float(t)) for n, t in data["frames"]]
    take.events = [
        Event(t=e["t"], kind=e["kind"], step=e["step"], detail=e.get("detail", ""))
        for e in data["events"]
    ]
    return take


def _zoom_default_name(seg: Segment) -> str:
    """What the edl should SHOW for a segment with no explicit focus.

    ⚠ Writes the resolved default rather than an empty string, so somebody
    opening edl.json sees ``"focus": "composer@1.8"`` on the typing segments
    and can change or clear it. A field that only appears once you already
    know it exists is a feature nobody finds.
    """
    f = zoom.focus_for(seg.name, seg.rate, None)
    return f"{f.region}@{f.zoom}" if f else "full"


def save_edl(segments: list[Segment], out: Path) -> None:
    (out / "edl.json").write_text(
        json.dumps(
            [
                {
                    "name": s.name,
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
                    "speed": s.rate,
                    "label": s.label,
                    # ⚠ Always written, so the field is DISCOVERABLE. An
                    # optional key nobody sees in the file they are editing is
                    # a feature that does not exist.
                    "focus": s.focus or _zoom_default_name(s),
                }
                for s in segments
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


def load_edl(out: Path) -> list[Segment]:
    return [
        Segment(
            name=s["name"],
            start=float(s["start"]),
            end=float(s["end"]),
            speed=float(s["speed"]),
            label=s.get("label", ""),
            focus=s.get("focus"),
        )
        for s in json.loads((out / "edl.json").read_text(encoding="utf-8"))
    ]


def main() -> int:
    force_utf8_console()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--record", choices=sorted(SCRIPTS), help="capture a long session")
    ap.add_argument("--cut", help="render from a captured session (its name)")
    ap.add_argument("--login", action="store_true", help="sign in once and save the session")
    ap.add_argument("--fresh-edl", action="store_true", help="regenerate edl.json, losing edits")
    ap.add_argument(
        "--clips",
        default="",
        help=(
            "comma-separated labels to cut as SEPARATE files, e.g. 'q9,q11,q19,q25'. "
            "Each becomes <name>-<label>.webm from the same frames; the full cut is skipped."
        ),
    )
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="demos")
    ap.add_argument("--headed", action="store_true", help="show the browser while recording")
    ap.add_argument("--settle", type=float, default=240.0, help="seconds to allow per turn")
    args = ap.parse_args()

    if args.login:
        return asyncio.run(interactive_login(args.url))

    name = args.record or args.cut
    if not name:
        print("Record a take, then cut it:")
        print("    --record " + " | --record ".join(sorted(SCRIPTS)))
        print("    --cut <the same name>")
        return 2
    out = Path(args.out) / name
    out.mkdir(parents=True, exist_ok=True)

    if args.record:
        print(f"[{name}] recording {len(SCRIPTS[name])} step(s) — this is the slow part")
        take = asyncio.run(record_session(name, args.url, out, args.headed, args.settle))
        save_take(take, out)
        span = take.events[-1].t if take.events else 0.0
        print(f"[{name}] {len(take.frames)} frames over {span:.0f}s -> {out}/events.json")
        # ⚠ **A take that recorded nothing must EXIT NON-ZERO.** Frames captured
        # is a MECHANISM; steps completed is the property. A session that dies
        # mid-take, or an app that never answers, still produces thousands of
        # frames — of a login form — and the run then offers to cut them. The
        # frames stay on disk either way (capture and cut are separate), so
        # this refuses the verdict, not the footage.
        done = sum(1 for e in take.events if e.kind == "step_ok")
        lost = any(e.kind == "session_lost" for e in take.events)
        planned = len(SCRIPTS[name])
        if lost or not done:
            why = "signed out mid-take" if lost else "no step completed"
            print(f"[{name}] FAILED — {why} ({done}/{planned} steps). Not a usable take.")
            return 1
        if done < planned:
            print(f"[{name}] partial: {done}/{planned} steps — the rest failed and were skipped.")
        if not args.cut:
            print(f"Now cut it:  python scripts/record_demo.py --cut {name}")
            return 0

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("No ffmpeg on PATH or in the Playwright browsers directory.")
        print("Install one:  winget install ffmpeg   (or brew install ffmpeg)")
        return 2

    take = load_take(out)
    edl_path = out / "edl.json"
    if args.fresh_edl or not edl_path.exists():
        segments = default_edl(take)
        save_edl(segments, out)
        print(f"[{name}] wrote {edl_path} — {len(segments)} segment(s). Edit it and re-cut.")
    else:
        segments = load_edl(out)
        print(f"[{name}] using your {edl_path} — {len(segments)} segment(s)")

    # ⚠ **One clip per question, from the SAME frames.** Reviewing a 4-minute
    # reel to judge four moments is the wrong unit of work, and re-recording to
    # get them separately would cost another 12 minutes of live agent turns for
    # footage already on disk. Capture and cut are separate programs precisely
    # so the edit is free — this just filters the segment list before rendering,
    # once per label.
    #
    # ⚠ It matches on the label PREFIX, so "q9" takes q9's typing, waiting,
    # answer and hold — the whole exchange — and deliberately not "q9" out of
    # "q19". A bare `in` test would silently splice two questions together and
    # the result would look like one long answer.
    wanted = [c.strip() for c in (args.clips or "").split(",") if c.strip()]
    if wanted:
        made = 0
        for label in wanted:
            picked = [s for s in segments if (s.label or "").split()[0] == label]
            if not picked:
                # Never render an empty selection: ffmpeg would write a 0-frame
                # file and the run would report a path that plays nothing.
                print(f"  ⚠ no segment labelled {label!r} — skipped")
                continue
            seq, rects, total = build_sequence(take, picked, out)
            video = render(seq, out, f"{name}-{label}", ffmpeg, rects)
            print(f"[{name}] {label}: {total:.1f}s  ->  {video}")
            made += 1
        return 0 if made else 1

    seq, rects, total = build_sequence(take, segments, out)
    video = render(seq, out, name, ffmpeg, rects)
    span = take.events[-1].t if take.events else 0.0
    print(f"[{name}] {span:.0f}s captured -> {total:.1f}s of video  ->  {video}")
    for s in segments:
        real = s.end - s.start
        print(f"    {s.label or s.name:24} {real:6.1f}s -> {real * s.rate:5.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
