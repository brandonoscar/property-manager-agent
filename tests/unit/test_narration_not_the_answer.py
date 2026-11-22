"""Narration is not an answer, and a deadline salvage must never ship it as one.

`current_text` accumulated EVERY streamed delta and then served as the
deadline salvage, the `final_output` fallback, and the `final_text` column.
Text the model streams *before* a tool call is it saying what it is about to
do; the answer is what it writes *after* the last one. Those were the same
string.

That was survivable only because the model rarely narrates. Battery v3's Q5
still managed it -- 190 characters of *"Now let me check open maintenance at
each of these units…"* shipped as a completed answer, and every scorer counted
the question as answered. `_is_deliverable` exists because of that run.

The orchestrator prompt is about to ask the model to narrate before every tool
call. Shipping that trigger without this split would make the Q5 shape the
NORMAL outcome of any run that hits the deadline.

So `current_text` resets at each `tool_called`, and a new `full_text` keeps the
whole transcript for the diagnostics that genuinely want it (the Langfuse
turn summaries). This file pins which is which.

⚠ The split is only worth anything if EVERY answer-shaped consumer got
narrowed and every transcript-shaped one did not -- gotcha 25's every-layer
rule. My first pass missed four sites because a `grep | head -12` truncated
them, so the corpus check below reads the whole file rather than a sample.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ORCH = Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "orchestrator.py"
_SRC = _ORCH.read_text(encoding="utf-8")


# ── the behaviour ─────────────────────────────────────────────────────


def _accumulate(stream: list[tuple[str, str]]) -> tuple[str, str]:
    """Replay the orchestrator's accumulator rules over a token/tool stream.

    Mirrors the real loop: a delta appends to both, a tool call resets only
    the answer. Kept in step with the source by
    `test_the_reset_lives_at_the_tool_called_flush_site`, which asserts the
    real code still does this -- a model of the rules that has drifted from
    the rules proves nothing.
    """
    current, full = "", ""
    for kind, text in stream:
        if kind == "delta":
            current += text
            full += text
        elif kind == "tool_called":
            current = ""
    return current, full


def test_a_deadline_salvage_contains_no_pre_tool_narration() -> None:
    """THE regression. A run that narrates, calls tools, and dies before
    writing an answer must salvage NOTHING -- not its own plan."""
    current, full = _accumulate(
        [
            ("delta", "Let me check Buildium for both spellings of Bennett. "),
            ("tool_called", "buildium_agent"),
            ("delta", "Nothing under Bennet. Now checking the mirror. "),
            ("tool_called", "mirror_lookup"),
        ]
    )
    assert current == "", f"salvage would ship narration: {current!r}"
    assert "Let me check Buildium" in full, "the transcript lost the narration entirely"
    assert "Now checking the mirror" in full


def test_the_real_answer_survives() -> None:
    """The other direction, and the one that makes the split worth having.
    Text after the LAST tool call is the answer and must reach the user."""
    current, _full = _accumulate(
        [
            ("delta", "Let me look that up. "),
            ("tool_called", "buildium_agent"),
            ("delta", "Bennett owes $1,240 across two leases."),
        ]
    )
    assert current == "Bennett owes $1,240 across two leases."


def test_a_run_that_calls_no_tools_is_unchanged() -> None:
    """A trivia answer never hits a `tool_called`, so nothing resets and the
    behaviour is byte-for-byte what it was before the split."""
    current, full = _accumulate([("delta", "Rent is due on the 1st.")])
    assert current == full == "Rent is due on the 1st."


def test_narration_between_two_calls_does_not_leak_into_the_answer() -> None:
    """The middle segment is the easiest one to get wrong -- it is neither the
    first thing streamed nor the last."""
    current, _ = _accumulate(
        [
            ("tool_called", "a"),
            ("delta", "That returned nothing, so I'll widen the search. "),
            ("tool_called", "b"),
            ("delta", "Found it: unit 4B."),
        ]
    )
    assert current == "Found it: unit 4B."
    assert "widen the search" not in current


# ── the wiring, read off the real source ──────────────────────────────


def test_the_reset_lives_at_the_tool_called_flush_site() -> None:
    """The model above is only honest if the real loop resets there too.

    Asserted on the AST rather than a substring: this file's own docstrings
    quote `current_text` repeatedly, and `ruff format` moves assignments
    across lines (gotcha 40).
    """
    tree = ast.parse(_SRC)
    resets = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "current_text" for t in n.targets)
        and isinstance(n.value, ast.Constant)
        and n.value.value == ""
    ]
    # Outer init, per-run init, and the tool_called reset.
    assert len(resets) >= 3, (
        f"only {len(resets)} `current_text = ''` assignments -- the tool_called "
        "reset is gone, so narration is back in the salvage"
    )


def _read_sites() -> dict[str, list[int]]:
    """Every line that READS an accumulator, split by which one."""
    out: dict[str, list[int]] = {"current": [], "full": []}
    for i, line in enumerate(_SRC.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        if "scrub_reply_text(current_text)" in line or "else current_text" in line:
            out["current"].append(i)
        if "scrub_reply_text(full_text)" in line:
            out["full"].append(i)
    return out


def test_every_answer_consumer_reads_the_narrowed_text() -> None:
    """The salvage, `final_output`'s fallback, every `final_text=` column, and
    the deadline synthesizer's `partial_text` are ANSWER-shaped. The
    synthesizer's output reaches the user directly, so it does not get
    narration as input either."""
    sites = _read_sites()
    assert len(sites["current"]) >= 8, (
        f"only {len(sites['current'])} answer-side reads found -- either the scan "
        "is broken or a consumer was silently switched to the transcript"
    )
    # The salvage specifically. If this one drifts, Q5's shape is back.
    salvage = [i for i in sites["current"] if "salvage = " in _SRC.split("\n")[i - 1]]
    assert salvage, "the deadline salvage no longer reads current_text"


def test_the_transcript_consumers_still_see_the_narration() -> None:
    """The Langfuse turn summaries are the ONLY record of what a stalled run
    was doing. Narrowing them would make a run's reasoning invisible in the one
    place built to explain it -- and all four are `_emit_loop_turn_summary`."""
    sites = _read_sites()
    assert len(sites["full"]) == 4, (
        f"expected 4 transcript reads, found {len(sites['full'])} -- a loop "
        "summary was narrowed (diagnostics lost) or an answer site was widened "
        "(narration back in the reply)"
    )
    lines = _SRC.split("\n")
    for i in sites["full"]:
        window = "\n".join(lines[max(0, i - 3) : i + 1])
        assert "_emit_loop_turn_summary" in window, (
            f"line {i} reads full_text outside a loop summary -- the transcript "
            "is reaching something that should get the answer"
        )


def test_both_accumulators_are_written_on_every_delta() -> None:
    """A split where only one side accumulates is a silent data-loss bug in
    whichever direction was forgotten."""
    delta_block = re.search(
        r"current_text \+= delta\s*\n\s*full_text \+= delta\s*\n\s*segment_text \+= delta", _SRC
    )
    assert delta_block, (
        "the three accumulators are no longer written together on a delta -- "
        "one of them is going to be silently empty"
    )


def test_the_reset_is_unconditional_not_guarded_by_segment_state() -> None:
    """The reset must fire on EVERY `tool_called` -- a four-call run resets
    four times.

    It sits beside the `thinking_event` publish, which IS conditional
    (`if segment_text.strip():`). Moving the reset inside that guard would look
    like a tidy-up and would silently restore the bug for any call the model
    made without narrating first -- exactly the calls a run makes when it is
    working fast, which is most of them.
    """
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and "tool_called" in ast.dump(node.test)):
            continue
        # Direct children of the tool_called branch -- NOT nested inside the
        # `if segment_text.strip():` that precedes it.
        top_level = [
            t.id
            for stmt in node.body
            if isinstance(stmt, ast.Assign)
            for t in stmt.targets
            if isinstance(t, ast.Name)
        ]
        if "segment_text" in top_level:
            assert "current_text" in top_level, (
                "the current_text reset is no longer a direct child of the "
                "tool_called branch -- if it moved inside `if segment_text.strip()` "
                "then a tool call the model did not narrate before leaves the "
                "previous narration in the answer"
            )
            return
    raise AssertionError("tool_called branch not found -- the scan is stale")


def test_the_transcript_is_accumulated_per_delta_not_derived_at_flush() -> None:
    """`full_text` must append on every delta, not be rebuilt from segments at
    the flush. Derived loses the tail on a mid-segment death -- which is
    precisely the run whose transcript anyone would want to read."""
    tree = ast.parse(_SRC)
    appends = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "full_text"
        and isinstance(n.value, ast.Name)
        and n.value.id == "delta"
    ]
    assert len(appends) == 1, (
        f"expected exactly one `full_text += delta`, found {len(appends)} -- the "
        "transcript is being derived rather than accumulated, and a run that "
        "dies mid-segment loses its tail"
    )
