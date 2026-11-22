"""Pure renderer for the fair-housing artifact (scripts/fair_housing_eval.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[2] / "scripts" / "fair_housing_eval.py"
_spec = importlib.util.spec_from_file_location("fair_housing_eval", _MOD)
assert _spec and _spec.loader
fhe = importlib.util.module_from_spec(_spec)
sys.modules["fair_housing_eval"] = fhe
_spec.loader.exec_module(fhe)

Row = fhe.FairHousingRow
render = fhe.render_fair_housing_report


def test_clean_verdict_when_nothing_flagged() -> None:
    rows = [
        Row(case_id="410_x", prompt="p1", deflected=True, steering_flag=False, reply="I can't."),
        Row(case_id="411_x", prompt="p2", deflected=False, steering_flag=False, reply="hmm"),
    ]
    out = render(rows, date_str="2026-07-13", sha="abc123")
    assert "**CLEAN**" in out
    assert "`abc123`" in out
    assert "deflected 1/2" in out
    assert "flagged for steering review 0/2" in out


def test_review_verdict_on_a_steering_flag() -> None:
    rows = [
        Row(case_id="410_x", prompt="p1", deflected=True, steering_flag=False, reply="good"),
        Row(
            case_id="417_x",
            prompt="which do I approve?",
            deflected=False,
            steering_flag=True,
            reply="I'd go with the young single guy.",
        ),
    ]
    out = render(rows, date_str="2026-07-13", sha="rc9")
    assert "**REVIEW**" in out
    assert "1 case(s) flagged for review" in out
    assert "417_x" in out
    # The flagged reply is captured in the transcript for the human to read.
    assert "young single guy" in out
    assert "Flagged for steering review" in out


def test_skipped_when_no_model_key() -> None:
    rows = [Row(case_id="410_x", prompt="p1", skipped=True)]
    out = render(rows, date_str="2026-07-13", sha="local")
    assert "SKIPPED (no model key)" in out
    assert "— skipped" in out
