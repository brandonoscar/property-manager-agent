"""The move-out tool reports what it KNOWS, and 344's mock stays in step with it.

⚠ THE SENTENCE THIS EXISTS TO KEEP DELETED. Until 2026-09-04 the tool returned
"Recorded a move-out for X on Y in Buildium." after a bare 2xx. Gotcha 70
live-disproved exactly that on 2026-08-31: the call returned 2xx, the
idempotency ledger logged ``succeeded``, and lease 115938 still read
``LeaseStatus: Active`` with ``MoveOutData: []``. The agent then reported the
tenant "flagged as a former tenant effective today… no further action needed".

Golden case 344 was catching the MODEL for repeating that — while the tool was
the thing asserting it. A tool that claims completion it cannot know hands the
model a false premise; the fix belongs in the tool.
"""

from __future__ import annotations

import json
import pathlib
import re

from helixis.agents.buildium_tools import _move_out_accepted

_GOLDEN = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/evals/golden"
_CASE = _GOLDEN / "344_write_readback_contradiction.json"


def test_the_tool_string_does_not_trip_the_case_it_is_mocked_into() -> None:
    """The load-bearing one, and it reads BOTH sides (gotcha 28).

    344's prohibitions are affirmation-shaped and applied to the REPLY, but a
    tool result carrying one of those phrases puts it in the transcript for the
    model to echo. Sourcing the patterns from the case file rather than
    retyping them means the two cannot drift into disagreeing — retyping would
    only prove this file agrees with itself (gotcha 45).
    """
    case = json.loads(_CASE.read_text(encoding="utf-8"))
    text = _move_out_accepted("Doyle Prather", "lease 115938")

    for pattern in case["expected"]["reply_must_not_match_regex"]:
        assert not re.search(pattern, text, re.IGNORECASE), (
            f"the move-out tool's own success line matches {pattern!r}, which 344 "
            "forbids in the reply — the model is being handed the phrase to echo."
        )


def test_it_does_not_assert_the_write_took_effect() -> None:
    lowered = _move_out_accepted("A Tenant", "lease 1").lower()
    assert "accepted" in lowered, "the string no longer says what it actually knows"
    for banned in ("recorded a move-out", "successfully", "former tenant"):
        assert banned not in lowered, f"the completion claim is back: {banned!r}"


def test_it_tells_the_reader_to_read_the_lease_back() -> None:
    """Naming the next step is the difference between an honest caveat and a
    shrug. Gotcha 70's harness half (scripts/write_verify.py) does exactly this
    for the operator; this is the same instruction for the model."""
    lowered = _move_out_accepted("A Tenant", "lease 1").lower()
    assert "read the lease back" in lowered


def test_it_forbids_re_sending_the_write() -> None:
    """344's other safety assertion caps buildium_record_move_out at ONE call.
    Re-firing to 'make sure it took' is how a real account ends up with two
    move-outs — the same shape as two charges."""
    assert "do not re-send" in _move_out_accepted("A Tenant", "lease 1").lower()


def test_the_golden_mocks_are_the_tools_real_output() -> None:
    """A mock drifted from its tool tests fiction (gotcha 41).

    Both cases quote this string, and 344's whole subject is what the model
    does when the tool and the read-back disagree — so a stale mock there would
    be exercising a shape the product no longer produces, which is gotcha 35's
    'a case that has never run is not a passing case' wearing a mock's costume.
    """
    for name, tenant, lease in (
        ("344_write_readback_contradiction", "Doyle Prather", "lease 115938"),
        ("126_relative_date_resolved_not_asked", "Marcus Whitfield", "lease 115934"),
    ):
        case = json.loads((_GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
        mocked = case["input"]["tool_mocks"]["buildium_record_move_out"][0]["return"]
        assert mocked == _move_out_accepted(tenant, lease), (
            f"{name}'s move-out mock has drifted from the tool. Regenerate it from "
            "_move_out_accepted rather than hand-editing."
        )
