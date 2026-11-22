"""The five capability screenshots — and the fixture facts they depend on.

⚠ **A screenshot question is only as good as the data behind it.** Each of
these was checked against the LIVE Blue Ridge fixture on 2026-09-07 before it
was added, because a thin answer photographed for a landing page is worse than
no photograph: it ships as a claim.

What these guards can and cannot see:

- They CAN see that the list is wired to the flag, that it is not empty, and
  that no question re-introduces the radius that the fixture cannot answer.
- They CANNOT see whether the live data still supports the questions. That is
  a property of the account, so the preconditions are written into
  `chat_probe.FEATURE_SHOTS`'s own comment with the numbers and the date —
  re-measure before trusting them (gotcha 48: a justification is prose and
  cannot fail).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SRC = (_SCRIPTS / "chat_probe.py").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("chat_probe", _SCRIPTS / "chat_probe.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chat_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load()


def test_the_feature_flag_actually_selects_the_feature_questions() -> None:
    """⚠ A curated list nothing reads is this repo's most repeated defect
    (gotchas 62/66/79): the helper is correct, thoroughly tested, and no
    caller uses it. A behavioural test of `FEATURE_SHOTS` cannot see that.

    So walk the source for the branch that assigns it, and for the argparse
    flag that reaches the branch — both halves, because either alone leaves a
    dead option (`--features` that selects nothing, or a list with no flag).
    """
    tree = ast.parse(_SRC)

    selects = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id == "FEATURE_SHOTS"
    ]
    assert selects, (
        "nothing assigns FEATURE_SHOTS to the questions the probe runs — "
        "`--features` would fall through to the single --ask default"
    )

    flags = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and any(isinstance(a, ast.Constant) and a.value == "--features" for a in node.args)
    ]
    assert flags, "FEATURE_SHOTS exists but no --features flag can reach it"


def test_every_shot_is_a_real_question() -> None:
    """Five, non-empty, no duplicates. A duplicate wastes a live turn and
    produces two screenshots of the same capability."""
    shots = cp.FEATURE_SHOTS
    assert len(shots) == 5, f"expected five capability shots, got {len(shots)}"
    assert all(isinstance(s, str) and s.strip().endswith("?") for s in shots), (
        f"a shot is not a question: {shots}"
    )
    assert len(set(shots)) == len(shots), "two shots ask the same thing"


@pytest.mark.parametrize(
    ("capability", "needle"),
    [
        ("contextual memory", "already decide"),
        ("jurisdiction lookup", "return their deposit"),
        ("fair housing", "family situation"),
        ("hazard / geo", "flood zone"),
        ("radius search", "miles of"),
    ],
)
def test_each_named_capability_still_has_a_shot(capability: str, needle: str) -> None:
    """⚠ Both sides (gotcha 28). The founder asked for FOUR named capabilities
    by name; this pins that editing the list cannot silently drop one, which
    is how a screenshot set ends up missing the feature it was made for.
    """
    assert any(needle in s for s in cp.FEATURE_SHOTS), (
        f"no shot covers {capability} — the {needle!r} question was removed or reworded"
    )


def test_no_shot_asks_for_a_radius_the_fixture_cannot_answer() -> None:
    """⚠ THE MEASURED CONSTRAINT, and the one most likely to be undone by
    somebody making the question sound better.

    Blue Ridge is one property per US state — 50 state capitals — so the
    CLOSEST PAIR is Boston-Providence at **32.8 miles** (measured on the live
    mirror 2026-09-07 with PostGIS `st_distance`). A 5-mile or 10-mile radius
    returns nothing, and an empty answer photographed as "geocoding" claims
    the opposite of what it shows.

    The bar is 40 miles, above the closest real pair rather than at it: a
    radius that only just reaches one property is a demo of a single row.
    Lower it the day the fixture holds clustered properties, and re-measure
    rather than assuming this comment is still true.
    """
    import re

    for shot in cp.FEATURE_SHOTS:
        for raw in re.findall(r"(\d+)\s*miles?", shot):
            assert int(raw) >= 40, (
                f"{raw}-mile radius: Blue Ridge's closest pair is 32.8 miles apart, "
                f"so this returns nothing — {shot!r}"
            )
