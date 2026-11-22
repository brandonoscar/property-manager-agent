"""The model said it could not stand behind its answer, and nothing counted it.

``Confidence: unverified`` is the agent volunteering doubt about what it just
wrote. It sat in a footer — jargon a property manager may or may not read as
"do not act on this" — and no code, no row and no metric recorded it, so the
question "how often is Occupella unsure?" had no answer at all.

⚠ **What this does NOT do, stated because a half-closed gap that reads as
closed is worse than an open one.** It does not route the turn to a human.
Which turns escalate, to whom, and what the person sees are product decisions,
and #271's lesson is that a runtime gate on model prose needs a dogfood pass
before it ships. This is the measurable half; the routing half is held.

⚠ **The ordering is the load-bearing part.** Every caveat in ``claim_guard``
rewrites the Confidence line to ``unverified`` when it fires. Read the finished
reply and you count four different facts as one — three of which already file
their own reason code — producing a number that answers no question. The read
has to happen before the guards, and only a source-level check can see that.
"""

from __future__ import annotations

import ast
import pathlib

from helixis.agents.synthesis_contract import (
    CONFIDENCE_VOCABULARY,
    LOW_CONFIDENCE,
    volunteered_low_confidence,
)
from helixis.observability.reason_codes import REASON_CODES

ORCHESTRATOR = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "orchestrator.py"
)

_FOOTER = "Sources: buildium: nothing on file\nConfidence: {word} — {why}\nUnverified: {unv}"


def _reply(word: str, *, why: str = "no corroborating record", unv: str = "the whole claim") -> str:
    return f"The tenant is paid up.\n\n{_FOOTER.format(word=word, why=why, unv=unv)}"


def test_the_lowest_band_is_recognised() -> None:
    assert volunteered_low_confidence(_reply("unverified")) is True


def test_ordinary_calibration_is_not_a_signal() -> None:
    """⚠ ``likely`` is the system WORKING. An answer that hedges proportionately
    is what good calibration looks like, and counting it here would bury the
    real signal under normal turns — the gotcha 52 failure mode, where a guard
    that cries wolf is one people switch off."""
    assert volunteered_low_confidence(_reply("likely")) is False
    assert volunteered_low_confidence(_reply("confident")) is False


def test_a_reply_with_no_footer_is_not_a_signal() -> None:
    """Most replies carry no footer — it is a cross-source contract, not a
    universal one. Absent must not read as doubt."""
    assert volunteered_low_confidence("The rent is $2,400.") is False
    assert volunteered_low_confidence("") is False


def test_the_last_footer_wins_over_a_quoted_one() -> None:
    """A reply that QUOTES an example footer while explaining itself must be
    judged on its own last one, which is the rule ``parse_synthesis_footer``
    already applies. Same class as gotcha 65: a compliant reply naming the
    thing it is not doing must not trip the check.

    ⚠ The example footer has to sit AT LINE START, unprefixed, or this test
    proves nothing — and my first two fixtures both failed that. Inline
    (``ends like `Confidence: confident`.``) and markdown-quoted
    (``> Confidence: …``) are BOTH invisible to ``^\\s*Confidence:``: ``>`` is
    not whitespace. So the mutation that flips ``[-1]`` to ``[0]`` sailed
    through twice and the assertion was decoration. Verified against the real
    regex before trusting the third one.
    """
    quoted = (
        "Every cross-source answer ends with a line like\n\n"
        "Confidence: confident — read the lease myself\n\n"
        "Here is yours:\n\n"
        + _FOOTER.format(word="unverified", why="nothing to read", unv="all of it")
    )
    assert volunteered_low_confidence(quoted) is True


def test_the_low_band_is_in_the_vocabulary() -> None:
    """Both-sides (gotcha 28): the constant and the vocabulary it belongs to
    live in the same module and could still drift apart in an edit."""
    assert LOW_CONFIDENCE in CONFIDENCE_VOCABULARY


def test_the_helper_cannot_forward_the_reason_clause() -> None:
    """⚠ Enforced by the RETURN TYPE, not by a rule somebody remembers.

    ``confidence_reason`` is model prose about the user's own portfolio —
    "no payment from Doyle Prather since March" — and ``record_reason_code``'s
    contract is short internal strings, never a message body. A helper that
    returned the reason is one a caller will eventually pass to ``detail``.
    """
    out = volunteered_low_confidence(_reply("unverified", why="no record for Doyle Prather"))
    assert out is True
    assert isinstance(out, bool)


# ── wiring ───────────────────────────────────────────────────────────


def _run_agent_streaming() -> ast.AST:
    tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_agent_streaming":
            return node
    raise AssertionError("run_agent_streaming not found — renamed?")


def _first_lineno(fn: ast.AST, predicate: object) -> int:
    """Line of the first call matching ``predicate``, or -1."""
    hits = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and predicate(n)  # type: ignore[operator]
    ]
    return min(hits) if hits else -1


def test_the_signal_is_read_and_filed() -> None:
    fn = _run_agent_streaming()
    names = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "volunteered_low_confidence" in names, (
        "nothing reads the model's own calibration — the word is back to sitting "
        "in a footer nobody counts"
    )

    slugs = {
        n.args[0].value
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "record_reason_code"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
    }
    assert "low_confidence_answer" in slugs
    assert "low_confidence_answer" in REASON_CODES


def test_the_read_happens_before_the_guards_rewrite_the_line() -> None:
    """⚠ The one assertion that cannot be made behaviourally here, and the one
    the whole design turns on.

    ``apply_scope_caveat`` / ``apply_source_caveat`` / ``apply_no_evidence_
    caveat`` / ``apply_failover_caveat`` all rewrite ``Confidence:`` to
    ``unverified``. If the read moved below any of them, every downgrade this
    turn applied would also register as the model volunteering doubt — four
    facts collapsed into one, three of them already counted elsewhere, and the
    metric would silently become meaningless rather than wrong-looking.
    """
    fn = _run_agent_streaming()

    read_at = _first_lineno(
        fn,
        lambda n: isinstance(n.func, ast.Name) and n.func.id == "volunteered_low_confidence",
    )
    assert read_at > 0

    for guard in (
        "apply_scope_caveat",
        "apply_source_caveat",
        "apply_no_evidence_caveat",
        "apply_failover_caveat",
    ):
        guard_at = _first_lineno(
            fn, lambda n, g=guard: isinstance(n.func, ast.Name) and n.func.id == g
        )
        assert guard_at > 0, f"{guard} is no longer called at all"
        assert read_at < guard_at, (
            f"the confidence read moved BELOW {guard}, which rewrites the same "
            "line — the counter would start including guard-applied downgrades"
        )


def test_the_reason_detail_is_the_word_and_not_model_prose() -> None:
    """``detail=LOW_CONFIDENCE`` — a constant, not a captured string. Pinned as
    a NAME node so a future edit that passes ``footer.confidence_reason`` (or
    any other local) fails here rather than quietly shipping a tenant's name to
    third-party telemetry."""
    fn = _run_agent_streaming()
    for call in ast.walk(fn):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "record_reason_code"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "low_confidence_answer"
        ):
            continue
        detail = next((kw.value for kw in call.keywords if kw.arg == "detail"), None)
        assert isinstance(detail, ast.Name) and detail.id == "LOW_CONFIDENCE", (
            "the low-confidence reason code's detail is no longer the bare "
            "constant — the reason clause is model prose and must not reach "
            "the telemetry channel"
        )
        return
    raise AssertionError("no low_confidence_answer call found")


def test_the_run_row_records_both_kinds_of_doubt() -> None:
    """A ``confidence`` key on ``orchestrator_runs.raw``, because logs are not
    always reachable and the row always answers (the reasoning the ``synthesis``
    key already records). Two fields, not one boolean: an answer the model stood
    behind but a BACKUP SKU wrote is a different risk from one the model itself
    would not vouch for."""
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    assert '_run_stats["confidence"]' in src
    for field in ('"model"', '"failover"'):
        assert field in src.split('_run_stats["confidence"]')[1][:400]
