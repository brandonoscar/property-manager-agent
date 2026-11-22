"""Deterministic guards for dependency pins that protect prod behavior.

These exist because a constraint in pyproject.toml is one Dependabot
"Update requirement" PR away from silently reverting a deliberate fix —
exactly what #737 did to the #713 langfuse pin (prod /agent/run crashed
with AttributeError on every traced call; CI never sees it because
Langfuse keys exist only in prod). A pin that guards runtime behavior
gets a test, so widening it fails CI with an explanation instead of
shipping a crash.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _dependency(name: str) -> str:
    data = tomllib.loads(_PYPROJECT.read_text())
    for dep in data["project"]["dependencies"]:
        if re.match(rf"{name}\b", dep.strip()):
            return dep.strip()
    raise AssertionError(f"{name} not found in [project.dependencies]")


def test_langfuse_stays_on_v2_line() -> None:
    """LiteLLM's langfuse callback reads ``langfuse.version``, removed in v3.

    The upper bound must exclude 3.0 until the callback integration is
    actually migrated (a code change, not a pin bump). See pyproject's
    comment block, Sentry PYTHON-FASTAPI-2X, and PRs #713 / #737.
    """
    dep = _dependency("langfuse")
    assert "<3" in dep.replace(" ", ""), (
        f"langfuse constraint {dep!r} no longer excludes v3+ — this reverts "
        "the #713 prod-crash fix (LiteLLM callback needs the v2 SDK). "
        "If you are deliberately migrating to the v3 SDK, update the "
        "LiteLLM callback integration and this test together."
    )


def test_litellm_is_exact_pinned() -> None:
    """litellm is exact-pinned (#535 class: an unpinned range broke prod).

    The langfuse guard above is calibrated against this exact version's
    callback behavior — if litellm floats, both assumptions drift.
    """
    dep = _dependency("litellm")
    assert re.search(r"litellm==\d", dep), (
        f"litellm constraint {dep!r} is no longer exact-pinned — "
        "see #535 and the langfuse guard in this file."
    )
