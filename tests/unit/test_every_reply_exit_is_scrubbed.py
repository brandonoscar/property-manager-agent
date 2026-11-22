"""Every path that hands text to a user must scrub it. All of them.

WHAT HAPPENED (live cross-source battery, 2026-08-26)
-----------------------------------------------------
Six real answers. The leak correlation was exact:

    stop_reason = completed                -> 0 identifier leaks  (4 of 4)
    stop_reason = SoftDeadlineSynthesized  -> leaks               (2 of 2)

The leaking answers carried ``**buildium_agent**`` and, worse, *"two open
repairs at property 37975"* — a raw Buildium record number in prose, the exact
thing #897/#899 exist to prevent.

The scrubber was fine. It simply was not called. ``run_agent_streaming`` has
six writers of ``final_text`` and the deadline-synthesis rescue path was the
one that did not scrub — added by #899, guarded on its INPUT
(``partial_text=scrub_reply_text(...)``) and not on its OUTPUT, even though
synthesis is a fresh model call over raw tool material.

That placement is why nobody caught it: the rescue path only runs when a run is
already going badly, so it is the least-exercised and least-watched exit in the
function.

⚠ WHY THIS IS AN AST WALK AND NOT A UNIT TEST OF THE SCRUBBER
--------------------------------------------------------------
Testing ``scrub_reply_text`` proves the rule works; it cannot prove the rule is
APPLIED. Both leaking strings pass cleanly through the scrubber today — that
was never in doubt. The defect lives in the wiring, so the guard has to read
the wiring (gotcha 28's both-sides rule).

And it walks the tree rather than grepping, because ``ruff format`` reflows a
long call across lines and a substring guard silently stops matching — gotcha
40, which cost this repo a blind guard once already.
"""

from __future__ import annotations

import ast
import pathlib

SOURCE = pathlib.Path("src/helixis/agents/orchestrator.py")

#: Functions whose return value is text a user reads or a row we store as the
#: answer. A value reaching ``final_text=`` or ``done_event(...)`` must have
#: passed through one of these.
_SCRUBBERS = {"scrub_reply_text"}

#: Callables that WRAP already-scrubbed text and are safe to see in between —
#: the caveat appliers append a footer to text that was scrubbed upstream.
_PASSTHROUGH = {
    "apply_scope_caveat",
    "apply_source_caveat",
    "apply_no_evidence_caveat",
    "str",
}


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _scrubbed_names(tree: ast.Module) -> set[str]:
    """Names ever assigned the result of a scrubber (directly or via a
    passthrough wrapper around one)."""
    scrubbed: set[str] = set()
    # Two passes: a name can be scrubbed then re-wrapped by a caveat applier.
    for _ in range(3):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not node.targets:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if _is_scrubbed_expr(node.value, scrubbed):
                scrubbed.add(target.id)
    return scrubbed


def _is_scrubbed_expr(node: ast.AST, scrubbed: set[str]) -> bool:
    """Does this expression evaluate to scrubbed text?"""
    if isinstance(node, ast.Call):
        fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fn in _SCRUBBERS:
            return True
        if fn in _PASSTHROUGH:
            return any(_is_scrubbed_expr(a, scrubbed) for a in node.args)
        return False
    if isinstance(node, ast.Name):
        return node.id in scrubbed
    if isinstance(node, ast.IfExp):
        # ⚠ BOTH branches. The bug next door to this one is line 2682's
        # ``str(final_output) if final_output else scrub_reply_text(...)``,
        # where the FALLBACK is scrubbed and the primary is not — which reads
        # like care and is a hole.
        return _is_scrubbed_expr(node.body, scrubbed) and _is_scrubbed_expr(node.orelse, scrubbed)
    if isinstance(node, ast.BinOp):  # synthesized + PARTIAL_NOTE
        return _is_scrubbed_expr(node.left, scrubbed) or _is_scrubbed_expr(node.right, scrubbed)
    if isinstance(node, ast.Subscript):  # (text)[:8000]
        return _is_scrubbed_expr(node.value, scrubbed)
    if isinstance(node, ast.BoolOp):  # scrub(x) or None
        return any(_is_scrubbed_expr(v, scrubbed) for v in node.values)
    return False


def test_every_final_text_written_is_scrubbed() -> None:
    """⚠ THE guard. Six writers of ``final_text``; one of them leaked a
    Buildium property id to a live user because it did not scrub."""
    tree = _tree()
    scrubbed = _scrubbed_names(tree)
    unscrubbed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "final_text":
            continue
        if not _is_scrubbed_expr(node.value, scrubbed):
            unscrubbed.append(ast.unparse(node.value)[:90])
    assert not unscrubbed, (
        f"these final_text values reach the run row without scrub_reply_text: {unscrubbed}. "
        f"A record number stored here is a record number the dev-runs view and any future "
        f"replay will show. Scrub at the assignment, not at each call site."
    )


def test_the_guard_can_actually_fail() -> None:
    """⚠ Assert the probe RUNS (gotcha 33). An AST walker that resolves nothing
    reports zero findings and looks identical to a clean codebase — which is
    exactly how the ``AnnAssign`` bug in the freshness guard went unnoticed."""
    scrubbed = _scrubbed_names(ast.parse("a = scrub_reply_text(x)\nb = raw_text\n"))
    assert "a" in scrubbed, "the walker cannot see a direct scrub — it resolves nothing"
    assert "b" not in scrubbed, "the walker calls unscrubbed text scrubbed"

    # And the specific shape that bit us: a ternary whose fallback is scrubbed.
    half = ast.parse("c = str(raw) if raw else scrub_reply_text(other)\n")
    assert not _is_scrubbed_expr(half.body[0].value, set()), (
        "a half-scrubbed ternary must NOT count as scrubbed — that is the live bug"
    )
