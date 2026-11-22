"""A `_todo` that says a primitive is missing must be right about that.

Gotcha 34 wrote the rule verbatim — *"A stale TODO saying a check doesn't work,
when it does, is worse than none: it invites the next reader to write the case
off as toothless"* — and then corrected exactly ONE case (122) while two more
went on asserting the same falsehood. Nothing reads case metadata, so prose in
a JSON file cannot fail (gotcha 48's shape).

Measured 2026-08-29 across all 143 golden cases. Six carry a `_todo`; three
claimed a missing primitive and two of those claims were false:

* **123** — *"MISSING PRIMITIVE: no handoff_trail_must_contain"*. It is
  implemented, it landed in the same W2 S5 "#676 _todo closures" batch, and the
  case **already used it**: `expected.handoff_trail_must_contain` is right
  there in the same file.
* **121** — *"no Gmail tool_mocks seam in `_MOCK_TARGETS`"*. `GMAIL_FETCH_EMAILS`
  is registered there. (Its second claim, that routing has no quality-tier
  negative reply primitive, is TRUE and stays.)
* **124** — *"no routing-side trail cap"*. Over-broad rather than wrong:
  `specialist_call_count_max` exists and gates as SAFETY, but it caps
  SPECIALIST dispatches, so a retry storm inside one specialist is still
  invisible. Narrowed rather than deleted.

THE CONVENTION THIS ENFORCES
----------------------------
A `_todo` may name a primitive that exists — 122's corrected note does exactly
that, on purpose, to say it exists. What it may not do is name one *silently*.
So: if a note mentions a primitive available to that case's builder, it must
also carry an explicit acknowledgement (`CORRECTED`, `RESOLVED`, `is
implemented`, `exists`, `landed`).

That rule was chosen over parsing negation, and the reason is worth keeping:
proximity-based negation detection FALSELY FLAGS 122, whose corrected note
reads *"was a MISSING PRIMITIVE; it is not — `specialist_call_count_max` is
implemented"*. The phrase "MISSING PRIMITIVE" sits ~30 characters before the
key it is explicitly denying. A guard that fires on the best-written note in
the corpus is a guard someone deletes.

⚠ **Per builder, never a union.** `tool_call_count_max` exists in `agent_flow`
and NOT in `orchestrator_routing`, so 124 — a routing case — is correct that it
has no routing-side equivalent. Checking against the union of both builders
would manufacture a finding against a true statement.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GOLDEN = _REPO / "src" / "helixis" / "evals" / "golden"
_BUILDERS = {
    "orchestrator_routing": _REPO / "src" / "helixis" / "evals" / "orchestrator_routing.py",
    "agent_flow": _REPO / "src" / "helixis" / "evals" / "agent_flow.py",
}

#: A key is "assertion-shaped" if it reads like a golden expectation rather
#: than an ordinary dict lookup. Deliberately loose — a false include costs a
#: note one acknowledgement word; a false exclude lets a stale claim through.
_ASSERTION_KEY = re.compile(r"_(must|should|count|trail|max|min)_?")

#: Saying "yes, this exists" — any one of these clears the claim.
_ACKNOWLEDGED = re.compile(
    r"CORRECTED|RESOLVED|is implemented|are implemented|exists|landed|it is not",
    re.I,
)


def _implemented_keys(path: Path) -> set[str]:
    """Every `expected.get("X")` / `input.get("X")` key the builder reads.

    Read from the AST, not a hand-written list (gotcha 45): a list here would
    describe what someone believed the builder supports, and the whole defect
    class is a belief about the builder outliving the builder.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    return {k for k in keys if _ASSERTION_KEY.search(k)}


def _cases_with_todos() -> list[tuple[str, str, str]]:
    """(filename, case_type, todo) for every golden case carrying a `_todo`."""
    out = []
    for path in sorted(_GOLDEN.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        todo = data.get("_todo")
        if todo:
            out.append((path.name, str(data.get("case_type") or ""), str(todo)))
    return out


# ── the scan is actually reading the corpus ──────────────────────────


def test_the_builders_expose_primitives_and_the_corpus_has_todos() -> None:
    """A bad path or a changed call shape would collect nothing and report
    green — the same self-check gotcha 49's guard needed."""
    for name, path in _BUILDERS.items():
        assert len(_implemented_keys(path)) >= 5, f"{name}: found almost no assertion keys"
    assert len(_cases_with_todos()) >= 3, "no _todos found — did the corpus move?"


# ── the rule ─────────────────────────────────────────────────────────


def test_no_todo_silently_names_a_primitive_that_exists() -> None:
    stale: list[str] = []
    for filename, case_type, todo in _cases_with_todos():
        builder = _BUILDERS.get(case_type)
        if builder is None:
            continue  # a type with no assertion builder makes no such claims
        if _ACKNOWLEDGED.search(todo):
            continue  # the note says out loud that the primitive exists
        for key in sorted(_implemented_keys(builder)):
            if key in todo:
                stale.append(f"{filename} ({case_type}) names {key!r} — its builder implements it")
    assert not stale, (
        "a _todo names a primitive its own builder provides, without saying so:\n  "
        + "\n  ".join(stale)
        + "\n\nEither the claim is stale (gotcha 34: a TODO saying a check doesn't work when it "
        "does invites the next reader to call the case toothless), or the note genuinely means to "
        "reference it — in which case say so with CORRECTED / RESOLVED / 'is implemented'."
    )


# ── both directions (gotcha 38: test the catch AND the spare) ────────


def test_the_guard_would_catch_a_freshly_stale_claim() -> None:
    """Against a synthetic note of the exact shape 123 carried."""
    routing = _implemented_keys(_BUILDERS["orchestrator_routing"])
    assert "handoff_trail_must_contain" in routing
    fake = "MISSING PRIMITIVE: no handoff_trail_must_contain in orchestrator_routing"
    assert not _ACKNOWLEDGED.search(fake)
    assert any(k in fake for k in routing), "the guard would not see this stale claim"


def test_the_guard_spares_a_note_that_correctly_says_the_primitive_exists() -> None:
    """122's corrected note is the best-written one in the corpus and names an
    implemented key on purpose. A guard that flags it gets switched off — and a
    proximity-based negation check DOES flag it, which is why this file keys on
    acknowledgement instead."""
    corrected = next((t for name, _ct, t in _cases_with_todos() if name.startswith("122")), None)
    assert corrected, "122's _todo disappeared — it is this file's worked example"
    assert "specialist_call_count_max" in corrected
    assert _ACKNOWLEDGED.search(corrected), (
        "122 names an implemented primitive and no longer acknowledges it — the guard would now "
        "flag the one note that was already correct"
    )


@pytest.mark.parametrize("case_prefix", ["121", "123", "124"])
def test_the_three_corrected_notes_stay_corrected(case_prefix: str) -> None:
    """Pins the fix. Without this, a future edit could quietly restore the
    false claim and nothing would fail — which is how they survived gotcha 34's
    own correction pass in the first place."""
    todo = next((t for name, _ct, t in _cases_with_todos() if name.startswith(case_prefix)), None)
    assert todo, f"case {case_prefix} lost its _todo"
    assert _ACKNOWLEDGED.search(todo), f"{case_prefix} no longer records what was corrected"
