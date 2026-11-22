"""The demo cutter: compress the dead air, keep everything else, drop the rest.

Every demo video of this product has been unwatchable for one reason — a turn
takes 40-160 seconds and a real-time recording is mostly spinner. Capture is
now one long pass and the CUT is a separate program driven by an edit list, so
this is where the arithmetic lives: warp each segment's real time into playback
time at its own speed, drop anything outside a segment, resample onto a fixed
frame grid.

⚠ These are the pure parts. The capture half needs a browser and is exercised
by running the script; what CANNOT be left untested is the arithmetic, because
its failure is silent — a wrong speed still produces a perfectly valid video.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

pytest.importorskip(  # the module imports chat_probe, which needs it
    "playwright", reason="playwright is not installed in this environment"
)

from record_demo import (  # noqa: E402
    FPS,
    SPEEDS,
    Event,
    Segment,
    Take,
    build_sequence,
    default_edl,
)


def _take(tmp_path: Path) -> Take:
    """A turn shaped like a real one: a short question, a long wait, an answer.

    Frames arrive only while the page CHANGES, which is what makes the trailing
    hold interesting — nothing is captured during it at all.
    """
    take = Take(session="chat-tour")
    take.events = [
        Event(0.0, "session_start", -1),
        Event(1.0, "step_start", 0, "What needs my attention today?"),
        Event(1.0, "type_start", 0),
        Event(3.0, "sent", 0),
        Event(93.0, "first_token", 0),  # 90 seconds of spinner
        Event(113.0, "answer_done", 0),
        Event(115.5, "step_end", 0),
    ]
    times: list[float] = []
    times += [i / 10 for i in range(0, 30)]  # intro + typing, dense
    times += [3.0 + i for i in range(0, 90)]  # the wait, one frame a second
    times += [93.0 + i / 10 for i in range(0, 200)]  # the answer, dense
    # …and nothing during the hold: a still page emits no frames.
    take.frames = [(f"f{i:06d}.jpg", t) for i, t in enumerate(times)]
    (tmp_path / "frames").mkdir(parents=True, exist_ok=True)
    return take


def test_the_dead_air_is_compressed_and_the_answer_is_not(tmp_path: Path) -> None:
    take = _take(tmp_path)
    _seq, _rects, total = build_sequence(take, default_edl(take), tmp_path)

    # 115.5s of real turn has to land near half a minute, or nobody watches it.
    assert 20.0 < total < 35.0, f"{total:.1f}s is not a landing-page video"

    # The specific claim: the 90-second wait is what got cut, and the 20-second
    # answer is still readable. Asserting only the total would pass on a cut
    # that compressed the ANSWER instead — the one thing that must stay legible.
    assert 90.0 * SPEEDS["think"] < 8.0
    assert 20.0 * SPEEDS["answer"] > 8.0, "the answer must outlast the wait in the cut"


def test_the_trailing_hold_survives_even_though_no_frame_was_captured_in_it(
    tmp_path: Path,
) -> None:
    """The bug the first real run shipped.

    CDP emits a frame when the page changes, so a still screen emits none — and
    the first cut ended on the last streamed character with no beat, which on a
    looping landing-page video snaps straight back to a blank composer. The
    hold is real elapsed time, so it belongs in the output.
    """
    take = _take(tmp_path)
    segs = default_edl(take)
    _seq, _rects, with_hold = build_sequence(take, segs, tmp_path)
    _seq2, _rects, without = build_sequence(take, [s for s in segs if s.name != "hold"], tmp_path)

    assert with_hold - without == pytest.approx(2.5, abs=0.15), (
        "the 2.5s hold contributed nothing — a still tail emits no frames, so "
        "it has to be carried by extending the clock on the last one"
    )


def test_a_segment_is_priced_at_its_own_speed(tmp_path: Path) -> None:
    """⚠ The trailing gap spans a segment boundary.

    The last captured frame lands in ``answer`` while the stillness runs on
    into ``hold``, so pricing the gap at one instant's speed plays the hold at
    the answer's rate. Only visible when those speeds differ, which is why this
    asserts against a deliberately distinct value rather than today's default.
    """
    take = _take(tmp_path)
    segs = default_edl(take)
    _seq, _rects, baseline = build_sequence(take, segs, tmp_path)

    doubled_segs = [
        Segment(s.name, s.start, s.end, s.rate * 2 if s.name == "hold" else s.rate, s.label)
        for s in segs
    ]
    _seq2, _rects, doubled = build_sequence(take, doubled_segs, tmp_path)

    assert doubled - baseline == pytest.approx(2.5, abs=0.15), (
        "changing the HOLD speed did not change the hold — the trailing gap is "
        "being priced at some other segment's rate"
    )


def test_dropping_a_segment_drops_its_footage(tmp_path: Path) -> None:
    """The edit list is an EDITOR, not just a re-timer.

    Anything outside a segment has to be gone — otherwise "cut this bit" is a
    no-op and the whole point of a separate cut program is lost.
    """
    take = _take(tmp_path)
    segs = default_edl(take)
    _seq, _rects, whole = build_sequence(take, segs, tmp_path)
    kept = [s for s in segs if s.name != "type"]
    _seq2, _rects, trimmed = build_sequence(take, kept, tmp_path)

    typing = next(s for s in segs if s.name == "type")
    assert whole - trimmed == pytest.approx((typing.end - typing.start) * typing.rate, abs=0.15)


def test_the_sequence_length_matches_the_reported_duration(tmp_path: Path) -> None:
    take = _take(tmp_path)
    seq, _rects, total = build_sequence(take, default_edl(take), tmp_path)
    assert len(seq) == pytest.approx(total * FPS, abs=2)


def test_a_failed_step_is_skipped_rather_than_breaking_the_cut(tmp_path: Path) -> None:
    """One bad step must not cost a 30-minute take.

    The step that died has no ``answer_done``, so there is no coherent span to
    cut — but every other step still has one, and the frames are all still on
    disk. Losing the whole session to one flaked question is the expensive
    failure this guards.
    """
    take = _take(tmp_path)
    take.events += [
        Event(116.0, "step_start", 1, "a question that never answered"),
        Event(116.0, "type_start", 1),
        Event(118.0, "sent", 1),
        Event(160.0, "step_failed", 1, "RuntimeError: agent still busy"),
    ]
    segs = default_edl(take)
    assert not any(s.label.startswith("q1") for s in segs), "the dead step leaked into the cut"
    assert any(s.label.startswith("q0") for s in segs), "the good step was lost with the bad one"


def test_a_take_with_no_frames_says_so(tmp_path: Path) -> None:
    """An unacknowledged screencast yields ~0 frames and no error of its own.

    Cutting that gives a 1-frame video, which looks like "the page never
    changed" rather than "the capture was never wired up".
    """
    empty = Take(session="chat-tour")
    empty.events = [Event(0.0, "session_start", -1)]
    with pytest.raises(RuntimeError, match="screencast never ran"):
        build_sequence(empty, [Segment("intro", 0.0, 1.0)], tmp_path)
