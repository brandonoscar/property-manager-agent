"""The demo recorder drives PRODUCTION, so its mistakes are customer-visible.

``e2e/demos`` records the landing-page clips against the deployed app with
real data. Two failure modes matter and neither shows up as a broken test in
the recorder's own language (it is JavaScript; this repo's CI is Python):

  1. **It approves a write.** The clips deliberately drive an ask that raises
     a confirm gate, because "it stops and asks a human" is the single best
     thing in the set. A recorder that clicked APPROVE would file a real work
     order into a real Buildium account, on camera, unattended.
  2. **A selector goes stale.** Every testid the recorder uses is a string
     resolved by a browser, not by anything that type-checks. A renamed
     testid turns into "no assistant reply after 240s" — a timeout that reads
     as a slow backend and costs the length of a full run to diagnose
     (CLAUDE.md gotcha 43, and the three rounds ``chat_probe`` already paid
     for in ``test_chat_probe_selectors.py``).

Both are checked by reading BOTH sides (gotcha 28): the recorder's strings
against the frontend's actual testids.

⚠ The never-approve check is **"the literal is absent from the whole file"**,
which is why ``record.mjs``'s own comment does not quote it. That form needs
no comment-stripping and cannot be defeated by reformatting — the two traps
that made gotcha 40's substring guard useless in both directions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEMOS = _ROOT / "e2e" / "demos"
_RECORD = _DEMOS / "record.mjs"
_SCENARIOS = _DEMOS / "scenarios.mjs"
_FRONTEND_SRC = _ROOT / "frontend" / "src"

#: The button that COMMITS a gated write. Verified present in the frontend by
#: `test_the_approve_button_this_forbids_is_real`, so the prohibition cannot
#: quietly start naming a control that no longer exists.
_APPROVE_TESTID = "confirm-gate-" + "confirm"  # split so this file is not a hit


def _frontend_testids() -> set[str]:
    ids: set[str] = set()
    for path in _FRONTEND_SRC.rglob("*.ts*"):
        ids |= set(re.findall(r"""["']([a-z0-9-]+)["']""", path.read_text(encoding="utf-8")))
    return ids


@pytest.fixture(scope="module")
def record_src() -> str:
    return _RECORD.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """Drop whole-line JS comments.

    ⚠ Needed because this recorder's header EXPLAINS the APIs it uses and the
    ones it forbids, so prose contains every literal a scan is looking for —
    and prose sorts before code, which makes a positional check (`is X before
    Y?`) answer about the paragraph rather than the call. Gotcha 40 from a
    third angle: the substring is not just a false positive, it is a false
    LOCATION.

    Deliberately line-based and conservative: a line whose first non-space
    character opens or continues a comment cannot also carry a statement. It
    never touches code, so it cannot hide a call — the blindness gotcha 40
    warns about comes from loosening the MATCH, which is why every caller here
    also matches a call shape rather than a bare name.
    """
    return "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith(("*", "//", "/*", "*/"))
    )


def test_the_recorder_never_approves_a_gated_write(record_src: str) -> None:
    """The one that would cost a customer a real record."""
    assert _APPROVE_TESTID not in record_src, (
        f"record.mjs names {_APPROVE_TESTID!r}. It drives production with real "
        "credentials and no human watching; approving a gate there files a real "
        "Buildium write. Show the gate and CANCEL it -- that frame is the point."
    )


def test_the_recorder_does_cancel_the_gate(record_src: str) -> None:
    """The other direction. A recorder that never touches the gate at all
    leaves it open on screen and blocks the rest of the run -- so absence of
    the approve string is only half the property."""
    assert "confirm-gate-cancel" in record_src, "record.mjs never cancels the confirm gate"


def test_the_approve_button_this_forbids_is_real() -> None:
    """⚠ A prohibition on a string that no longer exists is not a guard, it is
    a decoration -- and it passes forever. Same shape as gotcha 45: the control
    whose failure mode is 'silently permits more' needs its premise measured."""
    ids = _frontend_testids()
    assert _APPROVE_TESTID in ids, (
        f"{_APPROVE_TESTID!r} is not in the frontend any more, so the "
        "never-approve guard is now vacuous. Re-point it at whatever commits a "
        "gated write."
    )


def test_every_selector_the_recorder_uses_still_exists(record_src: str) -> None:
    """A renamed testid surfaces as a 240s timeout, far from its cause."""
    used = set(re.findall(r"""getByTestId\(\s*["']([^"']+)["']""", record_src))
    assert used, "no getByTestId calls found -- this scan is not reading the recorder"
    missing = sorted(used - _frontend_testids())
    assert not missing, (
        f"record.mjs uses testid(s) the frontend no longer defines: {missing}. "
        "The recorder would hang until its timeout and report it as a slow reply."
    )


# ── the catalogue ─────────────────────────────────────────────────────


def _scenarios() -> list[dict]:
    """Parse the catalogue without a JS engine.

    ⚠ Rewritten when scenarios became SCRIPTS rather than single prompts. The
    old parser matched a flat ``{ ... }`` block, which nested step arrays break
    outright — and a parser that silently returns nothing makes every
    assertion below vacuously true. Hence the not-empty self-check.
    """
    src = _SCENARIOS.read_text(encoding="utf-8")
    body = src[src.index("export const SCENARIOS") :]
    # Split on the id line: each scenario runs until the next one starts.
    chunks = re.split(r"\n    id: ", body)[1:]
    out = []
    for chunk in chunks:
        entry: dict = {}
        m = re.match(r"(['\"])(.*?)\1", chunk)
        if not m:
            continue
        entry["id"] = m.group(2)
        for key in ("title", "hook", "shows", "target"):
            km = re.search(rf"\b{key}:\s*(['\"])(.*?)(?<!\\)\1", chunk, re.S)
            if km:
                entry[key] = km.group(2)
        # Step verbs actually used, in order.
        entry["acts"] = re.findall(r"act:\s*'([a-z]+)'", chunk)
        entry["has_preroll"] = bool(re.search(r"\bpreroll:\s*\[", chunk))
        out.append(entry)
    return out


def test_the_catalogue_parses_and_is_not_empty() -> None:
    """Self-check. Every assertion below is vacuous against an empty list, and
    a scan that silently reads nothing reports green (gotcha 49)."""
    scenarios = _scenarios()
    assert len(scenarios) >= 3
    assert all(s.get("acts") for s in scenarios), (
        "a scenario has no steps -- the parser is not reading the script arrays"
    )


def test_scenario_ids_are_unique() -> None:
    """The id names the .webm, the .mp4 and the poster. A duplicate silently
    overwrites the earlier clip's files."""
    ids = [s["id"] for s in _scenarios()]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate scenario id(s) would overwrite each other's video: {dupes}"


def test_every_scenario_says_what_it_is_evidence_for() -> None:
    """`shows` is the reason a clip is publishable. If it cannot be named, the
    clip is footage rather than a demo -- and a landing page full of footage is
    what this catalogue exists to avoid."""
    thin = [
        s["id"] for s in _scenarios() if len(s.get("shows", "")) < 25 or len(s.get("hook", "")) < 25
    ]
    assert not thin, f"scenario(s) with no real hook/shows copy: {thin}"


def test_the_default_set_still_demonstrates_the_confirm_gate() -> None:
    """⚠ The safety clip is the most valuable one in the set. Marking it
    `optional` -- or dropping it -- would quietly ship a demo reel where the
    product only ever answers questions and never acts, which undersells it and
    hides the guardrail that makes acting safe."""
    scenarios = _scenarios()
    assert any("gate" in s["acts"] for s in scenarios), (
        "no scenario reaches a confirm gate -- the reel no longer shows the "
        "product asking before it writes, which is its best thirty seconds"
    )


def test_no_scenario_types_the_question_on_camera() -> None:
    """⚠ THE defect this catalogue was rewritten to fix.

    The first version was one prompt per clip, and the footage was somebody
    typing a sentence and waiting. Nothing was solved on screen. The structural
    remedy is that the question is asked in ``preroll`` -- before the camera --
    so frame one is the agent already working.

    An ``ask`` inside ``steps`` puts the typing back on camera, which is the
    whole regression.
    """
    for s in _scenarios():
        if "ask" not in s["acts"]:
            continue
        assert s["has_preroll"], (
            f"{s['id']} asks a question but has no preroll -- the typing is on camera again"
        )
        src = _SCENARIOS.read_text(encoding="utf-8")
        chunk = src.split(f"id: '{s['id']}'", 1)[1].split("steps:", 1)
        assert "act: 'ask'" in chunk[0], (
            f"{s['id']} has its `ask` in `steps` rather than `preroll` -- "
            "recording would start on an empty composer"
        )


def test_every_clip_ends_on_something_to_look_at() -> None:
    """A clip that ends the instant an action fires cuts before the payoff.
    The last step must be a hold or a gate -- something that leaves a rendered
    result on screen long enough to read."""
    for s in _scenarios():
        assert s["acts"], s["id"]
        assert s["acts"][-1] in ("hold", "gate"), (
            f"{s['id']} ends on '{s['acts'][-1]}' -- the clip cuts before the result is readable"
        )


def test_every_verb_the_catalogue_uses_is_implemented_by_the_recorder(record_src: str) -> None:
    """⚠ The producer/consumer string boundary, read from BOTH sides (gotcha 28).

    A step verb is a bare string in ``scenarios.mjs`` resolved by a ``switch``
    in ``record.mjs``. Nothing type-checks the pair, and the two files are
    edited for different reasons — the catalogue when the story changes, the
    recorder when the mechanics do. This exact drift shipped once: the
    catalogue was rewritten to use ``caption`` while the recorder still only
    knew ``chapter``, so the first live run would have thrown ``unknown step
    verb`` after the browser was open and the session spent.

    ``runStep`` throws on an unknown verb rather than skipping it, which is
    right — a typo that silently does nothing produces a clip missing its
    point — but it throws at RUN time, against production, minutes in. This
    fails in milliseconds instead.

    ⚠ Full-line ``//`` comments are stripped first. A commented-out step does
    not run, so requiring an implementation for it would be a false RED — and
    a guard that cries wolf is one somebody switches off (gotcha 40, both
    directions).
    """
    src = _SCENARIOS.read_text(encoding="utf-8")
    live = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))
    used = set(re.findall(r"act:\s*'([a-z]+)'", live))
    assert used, "no step verbs found -- this scan is not reading the catalogue"

    implemented = set(re.findall(r"case\s+'([a-z]+)':", record_src))
    assert implemented, "no switch cases found -- this scan is not reading the recorder"

    missing = sorted(used - implemented)
    assert not missing, (
        f"scenarios.mjs uses step verb(s) record.mjs cannot run: {missing}. "
        f"The recorder implements {sorted(implemented)}. runStep throws on an "
        "unknown verb, so this would fail mid-run against production."
    )


def test_the_camera_honours_start_when_working(record_src: str) -> None:
    """The SECOND string crossing the same boundary, and the one whose failure
    is silent rather than loud.

    An unknown step verb throws. ``startWhen`` is read with a plain property
    access, so a recorder that stopped honouring it would record every clip
    perfectly — starting on an empty composer, with the typing back on camera.
    That is the exact regression ``test_no_scenario_types_the_question_on_
    camera`` exists to prevent, and that test cannot see it: moving the ``ask``
    into ``preroll`` only helps if the camera then WAITS for the work.

    So: if the catalogue declares it, the recorder must read it, and the read
    must happen before the capture starts.
    """
    src = _SCENARIOS.read_text(encoding="utf-8")
    if "startWhen" not in src:
        pytest.skip("no scenario asks the camera to wait")
    code = _code_only(record_src)
    assert re.search(r"startWhen\s*===?\s*'working'", code), (
        "scenarios.mjs declares `startWhen: 'working'` and record.mjs never "
        "reads it -- the camera would roll on an empty composer and the clip "
        "opens on somebody typing, which is the defect the catalogue was "
        "rewritten to fix"
    )
    before, sep, after = code.partition("screencast.start(")
    assert sep, "no screencast.start( call found -- this scan is not reading the recorder"
    assert "startWhen" in before, (
        "record.mjs reads `startWhen` only AFTER the capture starts -- the wait "
        "has to gate the camera, not happen behind it"
    )


def test_the_recorder_captions_in_the_page_not_the_chapter_overlay(record_src: str) -> None:
    """⚠ `page.screencast.showChapter` does NOT render into captured frames —
    verified twice, absent from both `page.screenshot` and the `onFrame` JPEGs.
    A caption built on it looks correct in a live browser window and is simply
    not there in the finished mp4, which is the worst shape of bug for a thing
    nobody watches until it is published.

    So captions are a DOM overlay, and going back to the overlay API is named
    here as forbidden rather than left to whoever reads the diff.

    ⚠ Matched as a CALL, on comment-stripped source — twice over, so neither
    half is load-bearing alone. A bare-substring form fires on the sentence
    above, which explains the ban by quoting it: gotcha 40, arriving in this
    file for the third time. The other two prohibitions here dodge it by never
    writing their literal down, and that only works while nobody needs to
    explain them. Two defences instead: `screencast.showChapter(` is a shape
    prose does not take, and the comment lines are gone before the scan sees
    them.
    """
    assert not re.search(r"screencast\s*\.\s*showChapter\s*\(", _code_only(record_src)), (
        "record.mjs is drawing captions with the screencast chapter overlay, "
        "which does not appear in captured frames -- the clips would ship with "
        "no captions while looking correct on screen during the run"
    )
    assert "__demo_caption__" in record_src, "record.mjs no longer draws a caption into the page"


def test_the_recorder_does_not_use_the_low_quality_encoder(record_src: str) -> None:
    """⚠ Playwright's `recordVideo` is hardcoded to a low-bitrate realtime VP8
    preset. No option changes it, and it puts visible mosquito noise around
    text -- which is exactly the "quality was not good" complaint that caused
    this rewrite. `page.screencast` hands us raw frames so the encoder is ours.

    Reverting to recordVideo would silently restore the bad output, so it is
    named here as forbidden rather than left to memory.
    """
    assert "recordVideo" not in record_src, (
        "record.mjs is back on Playwright's built-in recordVideo, whose encoder "
        "is a hardcoded low-bitrate VP8 preset -- the original quality problem"
    )
    assert "screencast.start" in record_src, "record.mjs no longer captures frames"


def test_package_scripts_are_wired() -> None:
    pkg = json.loads((_DEMOS / "package.json").read_text(encoding="utf-8"))
    for script in ("login", "record", "render", "review"):
        assert script in pkg["scripts"], f"npm run {script} is missing"
    # ffmpeg-static is what makes `npm install` the whole setup on Windows.
    assert "ffmpeg-static" in pkg["dependencies"]


# ── the review page ───────────────────────────────────────────────────

_REVIEW = _DEMOS / "review.mjs"


def test_the_review_page_cannot_send_the_footage_anywhere() -> None:
    """⚠ This page exists so footage recorded against PRODUCTION is watched
    before anything is published. Its whole value is being the step BEFORE
    upload, so it must never be able to upload — and its own header says so in
    those words.

    A promise in a docstring cannot fail (gotcha 48). This is the mechanical
    version: nothing in the generator, and therefore nothing in the page it
    writes, may reach the network.
    """
    src = _REVIEW.read_text(encoding="utf-8")
    forbidden = ("fetch(", "XMLHttpRequest", "navigator.sendBeacon", "WebSocket", "EventSource")
    found = sorted(f for f in forbidden if f in src)
    assert not found, (
        f"review.mjs contains {found} — the review page is the step before anything "
        "is published, and real tenant data is on screen in it. It must not be able "
        "to transmit."
    )
    # An absolute http(s) reference would pull a third party into a page that is
    # displaying a resident's name. Relative paths only.
    remote = re.findall(r"""["'(]https?://[^"')\s]+""", src)
    assert not remote, f"review.mjs references remote URL(s): {remote}"


def test_render_actually_builds_the_review_page() -> None:
    """⚠ Gotcha 62: a helper nothing calls is the real bug, and a test that
    drives it directly cannot see that. `render.mjs` finishing without writing
    the page would leave the operator with a folder of mp4s and no prompt to
    watch them — which is precisely the step this is here to force.

    ⚠ Assert the CALL, not the identifier. Checking that ``buildReview``
    appears anywhere is satisfied by the import line alone, so deleting the
    call left this green — measured, not assumed. It is the same weakness the
    recorder's own battery found in counting call sites: presence of a name is
    not evidence that anything runs.
    """
    render = (_DEMOS / "render.mjs").read_text(encoding="utf-8")
    assert "from './review.mjs'" in render, "render.mjs no longer imports the review builder"
    assert re.search(r"\bbuildReview\s*\(", render), (
        "render.mjs imports buildReview but never CALLS it — nothing then tells "
        "the operator to watch the clips before publishing"
    )


def test_the_review_page_still_warns_about_both_hazards() -> None:
    """The two things a viewer of these clips must be told, and the two that
    are easy to quietly drop while editing copy: the footage carries real
    resident data, and the waiting is compressed. The second is what keeps an
    edit from reading as a claim about how fast the product is."""
    src = _REVIEW.read_text(encoding="utf-8").lower()
    assert "real data" in src or "real tenant" in src, "the PII warning is gone from the page"
    assert "compressed" in src, "the page no longer says the waiting time is compressed"
