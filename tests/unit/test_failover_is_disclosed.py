"""A backup model answered and the reply did not say so.

The failover itself works (``test_call_policy`` pins that, and the ledger
tests there pin that it gets recorded). This file covers the other half: the
answer the user reads, and the wiring that connects the two.

**Why it is worth a footer at all.** The reply is the same SHAPE whichever SKU
wrote it, so a silent substitution is indistinguishable from the primary
having answered — there is no wording tell, which is the same problem gotcha
47 describes for a mis-scoped query. And it is not a hypothetical difference:
gotcha 86 MEASURED it. ``411_fair_housing_safe_area_steering`` red-gates on
claude-haiku-4-5, the failover SKU, and the runtime steering scanner had to be
widened from 6/10 to 9/10 recall to catch what that model actually says.

⚠ **Three guards here are AST walks and that is deliberate.** The caveat
helper can be perfect and reached by nobody — this repo has that defect on
record three separate times (gotchas 62, 66, 69: a well-tested helper whose
one production call site quietly ignored it, invisible to every behavioural
test of the helper). A behavioural test of ``run_agent_streaming`` cannot run
here either way; the Agents SDK does not install in this container.
"""

from __future__ import annotations

import ast
import pathlib

from helixis.agents.claim_guard import apply_failover_caveat
from helixis.observability.reason_codes import REASON_CODES

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"
ORCHESTRATOR = SRC / "agents" / "orchestrator.py"
PROMPT_CACHE = SRC / "llm" / "prompt_cache.py"


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for call in _calls(tree):
        fn = call.func
        if isinstance(fn, ast.Name):
            names.add(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.add(fn.attr)
    return names


def _function(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name} — did it get renamed?")


# ── the sentence the user reads ──────────────────────────────────────


def test_the_caveat_says_a_backup_answered_and_why_that_matters() -> None:
    out = apply_failover_caveat("The rent is $2,400.")

    assert "The rent is $2,400." in out, "the body is never rewritten — only annotated"
    lowered = out.lower()
    assert "backup" in lowered, "the reader has to be told a different model answered"
    assert "re-check" in lowered, "and what to do about it"


def test_the_caveat_does_not_put_a_model_identifier_on_the_screen() -> None:
    """⚠ The split this whole design turns on, and the direction that gets it
    wrong is the tempting one: naming the SKU looks more honest.

    A property manager can do nothing with ``anthropic/claude-haiku-4-5``, and
    a reply is not where internal identifiers belong — the same reasoning
    ``reply_hygiene`` applies to record numbers. The actionable half goes to
    the operator instead, via ``model_failover``'s reason-code detail, exactly
    as ``fair_housing_flagged`` already splits.

    Enforced on the SIGNATURE too: a helper that accepted a model name is a
    helper somebody will pass one to.
    """
    import inspect

    out = apply_failover_caveat("The rent is $2,400.").lower()
    for token in ("anthropic", "claude", "haiku", "sonnet", "gpt", "gemini", "sku"):
        assert token not in out, f"{token!r} reached the user-facing caveat"

    params = list(inspect.signature(apply_failover_caveat).parameters)
    assert params == ["answer"], (
        "apply_failover_caveat gained a parameter — if it is a model name, the "
        "reason code is where that belongs (see claim_guard._FAILOVER_CAVEAT)"
    )


def test_an_empty_answer_is_left_alone() -> None:
    """Nothing to annotate. A bare footer with no answer above it would be the
    only thing a user saw on a failed turn."""
    assert apply_failover_caveat("") == ""


def test_the_caveat_merges_into_an_existing_footer() -> None:
    """A synthesis footer is edited in place — confidence downgraded and the
    reason recorded on ``Unverified`` — rather than a note stapled underneath.

    ⚠ The caveat legitimately appears TWICE here, and asserting once was my
    first draft and wrong. ``_apply_caveat`` is shared by all four guards and
    writes both lines by design: ``Confidence:`` says how much to trust the
    answer and ``Unverified:`` says what is unproven, and a reader who only
    scans one still gets the sentence. Pinning "exactly one" would have been
    pinning my assumption over the module's actual contract.
    """
    answer = "The rent is $2,400.\n\nConfidence: confident\nUnverified: none"
    out = apply_failover_caveat(answer)

    assert "Confidence: unverified" in out
    assert "Unverified: none" not in out, "the placeholder should have been replaced"
    assert "_Note:" not in out, "a footer exists, so nothing should be appended below it"


# ── the wiring: is any of that reached? ──────────────────────────────


def test_the_streaming_run_binds_the_ledger_and_reads_it() -> None:
    """Bind, apply, disclose, unbind — all four, in ``run_agent_streaming``.

    The ledger must be bound BEFORE the run for the contextvar reason the
    orchestrator comments at the binding site: a failover fires inside
    ``litellm.acompletion``, usually inside a nested specialist Task, and a
    Task copies the context.
    """
    fn = _function(ORCHESTRATOR, "run_agent_streaming")
    names = _called_names(fn)

    for required in (
        "bind_failover_ledger",
        "apply_failover_caveat",
        "reset_failover_ledger",
    ):
        assert required in names, (
            f"run_agent_streaming no longer calls {required} — a failover would "
            "be recorded and never shown to anyone"
        )


def test_the_failover_files_a_reason_code_with_the_model_in_it() -> None:
    """The operator-facing half. Checked as a CALL with the literal slug,
    because a reason code recorded under a drifted spelling fragments the
    dashboard silently (``record_reason_code`` logs and carries on)."""
    fn = _function(ORCHESTRATOR, "run_agent_streaming")
    slugs = {
        call.args[0].value
        for call in _calls(fn)
        if isinstance(call.func, ast.Name)
        and call.func.id == "record_reason_code"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert "model_failover" in slugs
    assert "model_failover" in REASON_CODES, (
        "the slug is emitted but not in the vocabulary — record_reason_code "
        "would warn and the dashboard would not chart it (gotcha 28's "
        "both-sides rule, applied to a string crossing two modules)"
    )


def test_the_wrapper_records_only_on_the_success_branch() -> None:
    """⚠ A structural check, because the behavioural one cannot see WHERE the
    call sits — only that the totals came out right.

    ``record_failover`` must live in the ``else`` of the sibling ``try``. In
    the ``try`` body it would still be correct today and would silently start
    recording failed siblings the moment anything is appended after it; in the
    ``except`` it would name a model that contributed nothing to the answer,
    making the one sentence whose job is to be true about how the reply was
    produced itself false.

    ⚠ **Membership has to be resolved on the NEAREST enclosing ``Try``, not by
    walking each one.** The failover loop lives inside the OUTER try's
    ``except``, so ``ast.walk`` from that handler reaches the inner try's
    ``else`` too — a naive "is it anywhere under a handler" check reports
    True against the correct code, which is exactly what my first draft did.
    Same trap as gotchas 40/59, arriving in a tree walk instead of a substring
    scan: the enclosing structure quotes the thing being forbidden.
    """
    fn = _function(PROMPT_CACHE, "configure_prompt_caching")

    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    targets = [
        c for c in _calls(fn) if isinstance(c.func, ast.Name) and c.func.id == "record_failover"
    ]
    assert targets, "record_failover is not called at all — nothing is ever disclosed"

    branches: set[str] = set()
    for call in targets:
        node: ast.AST | None = call
        while node is not None:
            enclosing = parent.get(node)
            if isinstance(enclosing, ast.Try):
                for field in ("body", "orelse", "finalbody"):
                    if node in getattr(enclosing, field):
                        branches.add(field)
                if node in enclosing.handlers or any(node in h.body for h in enclosing.handlers):
                    branches.add("handler")
                break
            node = enclosing

    assert branches == {"orelse"}, (
        f"record_failover sits in {sorted(branches) or ['no try at all']}; it belongs "
        "only on the success branch of the sibling call"
    )
