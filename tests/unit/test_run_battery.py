"""The battery-v5 harness's pure core.

Battery v4's finding was not "the answers were mediocre" — it was that three
of twelve questions returned **zero characters** while consuming 39% of the
battery's spend at the 240s wall clock (gotcha 32). A harness that reports a
median latency and an average score hides exactly that: three dead runs and
nine good ones read as "a bit slow".

So the properties pinned here are the ones that make the failure visible —
empties counted separately, wasted spend attributed, and a non-zero exit when
any question returned nothing.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_battery", _REPO / "scripts" / "run_battery.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_battery"] = mod
    spec.loader.exec_module(mod)
    return mod


rb = _load()


def _r(index: int, *, answer: str = "an answer", seconds: float = 10.0, **kw):
    return rb.QuestionResult(
        index=index, question=f"q{index}", answer=answer, seconds=seconds, **kw
    )


# ── the empty answer is the headline ──────────────────────────────────────


def test_a_blank_answer_is_empty_not_merely_short() -> None:
    assert _r(1, answer="").empty
    assert _r(2, answer="   \n  ").empty, "whitespace is not an answer"
    assert not _r(3, answer="x").empty


def test_wasted_spend_is_attributed_to_the_runs_that_returned_nothing() -> None:
    """THE metric. v4 spent 39% of its budget on three questions that produced
    no characters; an average latency cannot express that."""
    results = [
        _r(1, answer="", seconds=240.0),
        _r(2, answer="", seconds=240.0),
        _r(3, answer="real", seconds=60.0),
        _r(4, answer="real", seconds=60.0),
    ]
    s = rb.summarize(results)
    assert s["empty"] == 2
    assert s["empty_indexes"] == [1, 2]
    assert s["seconds_wasted_on_empty"] == 480.0
    assert s["wasted_fraction"] == pytest.approx(0.8, abs=0.01)


def test_a_clean_battery_reports_no_waste() -> None:
    s = rb.summarize([_r(i) for i in range(1, 5)])
    assert s["empty"] == 0
    assert s["wasted_fraction"] == 0.0
    assert s["answered"] == 4


def test_the_deadline_is_counted_by_name() -> None:
    """``wall_clock_deadline`` is the specific stop reason v5 exists to count,
    and it is not the same thing as an empty answer — a run can hit the
    deadline having already streamed a partial reply."""
    results = [
        _r(1, answer="partial", stop_reason="wall_clock_deadline"),
        _r(2, answer="", stop_reason="wall_clock_deadline"),
        _r(3, stop_reason=None),
    ]
    s = rb.summarize(results)
    assert s["deadlined"] == 2
    assert s["deadlined_indexes"] == [1, 2]
    assert s["empty"] == 1, "deadlined and empty are different questions"


def test_refusals_are_totalled_across_the_battery() -> None:
    """Gotcha 32: refusals are free to the ledger and expensive on the clock,
    and Q7 spread its refusals over three tools — so per-tool counting misses
    a looping run. This sums run-wide."""
    results = [
        _r(1, call_budget={"refusals": {"get_work_order": 52}}),
        _r(2, call_budget={"refusals": {"a": 3, "b": 4}}),
        _r(3, call_budget={}),
    ]
    assert rb.summarize(results)["refusals_total"] == 59


def test_the_specialist_cap_is_reported_where_it_engaged() -> None:
    results = [
        _r(1, specialist_budget={"specialist_calls": 2, "hit_cap": True}),
        _r(2, specialist_budget={"specialist_calls": 1, "hit_cap": False}),
        _r(3),
    ]
    assert rb.summarize(results)["specialist_capped"] == [1]


# ── selection ─────────────────────────────────────────────────────────────


def test_selection_is_one_indexed_so_it_matches_the_v4_report() -> None:
    """v4 named its losses Q5/Q7/Q11. ``--only 5,7,11`` must select those, not
    their zero-indexed neighbours."""
    qs = [f"question {i}" for i in range(1, 13)]
    picked = rb.select_questions(qs, "5,7,11")
    assert [i for i, _ in picked] == [5, 7, 11]
    assert picked[0][1] == "question 5"


def test_selection_supports_ranges() -> None:
    qs = [f"q{i}" for i in range(1, 13)]
    assert [i for i, _ in rb.select_questions(qs, "1-3,12")] == [1, 2, 3, 12]


def test_no_selection_runs_everything() -> None:
    qs = [f"q{i}" for i in range(1, 13)]
    assert len(rb.select_questions(qs, None)) == 12


def test_a_nonexistent_question_is_refused_not_silently_dropped() -> None:
    """Asking for question 99 and quietly getting eleven results is how a
    battery reports on a different set than the one you asked for."""
    with pytest.raises(SystemExit, match="99"):
        rb.select_questions([f"q{i}" for i in range(1, 13)], "5,99")


# ── the report ────────────────────────────────────────────────────────────


def test_the_report_leads_with_the_empties() -> None:
    results = [_r(1, answer="", seconds=240.0), _r(2, seconds=10.0)]
    text = rb.render_report(results, rb.summarize(results))
    headline = text.split("\n\n")[1]
    assert "returned NOTHING" in headline
    assert "[1]" in text


def test_a_clean_report_says_so_plainly() -> None:
    results = [_r(1), _r(2)]
    text = rb.render_report(results, rb.summarize(results))
    assert "Every question returned an answer" in text
    assert "returned NOTHING" not in text


# ── producer/consumer: the questions file vs the scorer ───────────────────


def test_every_question_the_scorer_grades_actually_exists() -> None:
    """Gotcha 28's rule — a string crossing a producer/consumer boundary needs
    a test that reads BOTH sides. ``score_battery.expectations()`` is keyed by
    question NUMBER; if the questions file is shorter than the highest key,
    the scorer silently grades a question that was never asked.
    """
    questions = json.loads((_REPO / "scripts" / "battery_questions.json").read_text())

    spec = importlib.util.spec_from_file_location(
        "score_battery", _REPO / "scripts" / "score_battery.py"
    )
    assert spec and spec.loader
    scorer = importlib.util.module_from_spec(spec)
    sys.modules["score_battery"] = scorer
    spec.loader.exec_module(scorer)

    src = inspect.getsource(scorer.expectations)
    graded = {int(m) for m in re.findall(r"^\s{8}(\d+):\s*Expectation", src, re.M)}
    assert graded, "could not read the scorer's question numbers"
    assert max(graded) <= len(questions), (
        f"scorer grades question {max(graded)} but only {len(questions)} are defined"
    )


def test_the_result_shape_is_what_the_scorer_reads() -> None:
    """``score_battery.py`` documents its input as
    ``{question, answer, seconds, completed}`` and ignores extras. v5 adds
    fields; it must not drop those four."""
    d = _r(1).to_dict()
    assert {"question", "answer", "seconds", "completed"} <= set(d)
    assert isinstance(d["seconds"], float)
    assert isinstance(d["completed"], bool)


# ── auth expiry must never masquerade as a deadline loss ──────────────────


def test_an_expired_token_is_its_own_error_class() -> None:
    """A Supabase access token lives ~1 hour and a 12-question battery can run
    close to that, so mid-battery expiry is a real outcome. It yields an empty
    answer — byte-for-byte what a wall-clock loss looks like — so it must be
    raised, never recorded, or the report announces v4's failure mode when the
    truth is one stale credential."""
    assert issubclass(rb._TokenExpiredError, Exception)
    src = inspect.getsource(rb._ask)
    assert "401" in src and "403" in src, "auth failures must be detected, not streamed"
    # And it must not be caught by the per-question handler that keeps the
    # battery running through ordinary failures.
    run_src = inspect.getsource(rb.run_battery)
    assert run_src.index("except _TokenExpiredError") < run_src.index("except Exception"), (
        "the broad handler would swallow it and record an empty answer"
    )


# ── the pre-flight gate ───────────────────────────────────────────────────


def test_money_is_read_out_of_prose_with_separators_and_cents() -> None:
    assert rb.money_in("we owe $1,234.56 and $40") == [1234.56, 40.0]
    assert rb.money_in("") == []
    assert rb.money_in("nothing numeric here") == []


def test_an_errored_preflight_is_not_reported_as_a_stale_deploy() -> None:
    """THE lesson of 2026-08-22. A backend that 500s behind an HTTP 200 SSE
    stream yields an empty answer, and ``money_in("")`` is ``[]`` — identical
    to a genuine $0. Collapsing the two sent the operator hunting a stale
    deploy that was fine."""
    verdict, detail = rb.preflight_verdict(answered=False, answer="")
    assert verdict == "backend_failed"
    assert "NOT a stale deploy" in detail


def test_a_real_answer_with_no_dollars_is_the_stale_deploy_signal() -> None:
    verdict, detail = rb.preflight_verdict(
        answered=True, answer="You have no outstanding vendor bills."
    )
    assert verdict == "zero"
    assert "has NOT rolled" in detail


def test_zero_does_not_claim_a_stale_deploy_outright() -> None:
    """An empty company answers $0 correctly. The gate cannot tell that apart
    from an unrolled deploy, so it must not assert one — saying "deploy has NOT
    rolled" about a healthy deploy is the 08-22 mistake wearing a new costume."""
    _, detail = rb.preflight_verdict(answered=True, answer="No unpaid bills.")
    assert "genuinely has no unpaid" in detail, "the other cause must be named"


def test_a_completed_run_that_said_nothing_is_not_a_zero_answer() -> None:
    """Live-found 2026-08-23. ``_ask`` read ``data["text"]`` where the emitter
    writes ``data["content"]``, so every token appended "" and a perfectly good
    answer arrived as zero characters. The gate then called that ``zero`` and
    reported a stale deploy. An empty answer is its own verdict."""
    verdict, detail = rb.preflight_verdict(answered=True, answer="")
    assert verdict == "empty"
    assert "zero characters" in detail
    blank = " " + chr(10) + " "
    assert rb.preflight_verdict(answered=True, answer=blank)[0] == "empty"


def test_a_zero_dollar_figure_still_counts_as_zero() -> None:
    """$0.00 is a figure, but not a NON-ZERO one — v4 filtered on ``> 0``."""
    assert rb.preflight_verdict(answered=True, answer="Total: $0.00")[0] == "zero"


def test_a_nonzero_figure_passes_and_reports_the_largest() -> None:
    verdict, detail = rb.preflight_verdict(
        answered=True, answer="Two bills: $1,200.00 and $9,850.50."
    )
    assert verdict == "pass"
    assert "9,850.50" in detail


def test_the_shipped_questions_are_the_canonical_battery() -> None:
    """``--only`` is 1-indexed against this file and its indexes are quoted in
    the v4 report, so a questions file that drifts silently re-points every
    documented index at a different question."""
    qs = json.loads((_REPO / "scripts" / "battery_questions.json").read_text(encoding="utf-8"))
    assert len(qs) == 12
    assert qs[1] == rb._PREFLIGHT_Q, (
        "question 2 must be the unpaid-bill-exposure question, or the "
        "pre-flight dollar gate is meaningless"
    )


# ── the done.summary loss (battery v6, 2026-08-27) ───────────────────────
#
# ⚠ THE HARNESS MANUFACTURED ITS OWN HEADLINE FINDING.
#
# v6 reported five of twelve questions as ZERO CHARACTERS at
# ``wall_clock_deadline``. They were not empty. ``orchestrator_runs`` holds
# all five, ``status=succeeded``, ``error_type=SoftDeadlineSynthesized``,
# carrying 987-4,129 characters — including a delinquency answer correct to
# the dollar ("$36,340 outstanding across 20 delinquent leases") with the
# debtor ladder in the right order. The scorer then marked those same three
# facts WRONG, because it was handed an empty string. One dropped field
# turned a perfect answer into three factual errors.
#
# Mechanism: ``deadline_synthesis`` is a fresh model call made AFTER the
# streaming loop is aborted, so its text is never tokenised onto the wire. It
# reaches a client in exactly one place — ``done.summary`` — and the old loop
# did ``elif etype == "done": completed = True`` and dropped it. The real
# frontend reads it (``store.ts::finishAgentResponse``), so users saw these
# answers; only the measurement lost them.
#
# These tests replay the wire shapes directly. That is the point: the bug is
# a runtime data-flow fault, invisible to any static check (gotcha 43), and
# it lived inside a network call where nothing could reach it.


def _fold(events: list[dict]) -> object:
    folder = rb._StreamFolder()
    for evt in events:
        if not folder.feed(evt):
            break
    return folder.finish()


def test_a_synthesised_answer_arrives_only_in_done_summary() -> None:
    """The v6 shape, verbatim: the loop was cut off before any prose, and the
    whole answer rides on the terminal event."""
    out = _fold(
        [
            {"type": "assistant_message_start", "data": {}},
            {"type": "assistant_message_end", "data": {}},
            {
                "type": "done",
                "data": {
                    "summary": "**Total: $36,340 outstanding across 20 delinquent leases.**",
                    "truncated": True,
                },
            },
        ]
    )
    assert out.answer.startswith("**Total: $36,340"), (
        "the synthesised answer was dropped — this is the v6 defect, where five "
        "good answers were reported as zero characters"
    )
    assert out.completed is True
    assert out.partial is True, "a truncated rescue must not read as a clean pass"


def test_a_streamed_answer_is_never_clobbered_by_the_summary() -> None:
    """⚠ PRECEDENCE, matching ``store.ts::finishAgentResponse``. The backend
    truncates ``summary`` at 8,000 characters, so preferring it over a good
    stream would silently shorten long replies. The stream wins whenever it
    produced anything."""
    out = _fold(
        [
            {"type": "assistant_message_start", "data": {}},
            {"type": "assistant_message_token", "data": {"content": "the full "}},
            {"type": "assistant_message_token", "data": {"content": "streamed reply"}},
            {"type": "assistant_message_end", "data": {}},
            {"type": "done", "data": {"summary": "a shorter truncated copy", "truncated": True}},
        ]
    )
    assert out.answer == "the full streamed reply"
    assert out.partial is False, (
        "the kept answer came off the stream, so labelling it a rescue would "
        "mislabel a complete reply"
    )


def test_a_genuinely_empty_run_is_still_reported_empty() -> None:
    """⚠ The fix must not launder a real failure. A run that streamed nothing
    AND carried no summary is exactly the v4 defect this harness exists to
    count, and it has to keep counting it."""
    out = _fold([{"type": "done", "data": {}}])
    assert out.answer == ""
    assert rb.QuestionResult(index=1, question="q", answer=out.answer).empty is True


def test_an_error_event_still_stops_the_stream_and_keeps_what_it_had() -> None:
    out = _fold(
        [
            {"type": "assistant_message_start", "data": {}},
            {"type": "assistant_message_token", "data": {"content": "partial prose"}},
            {"type": "error", "data": {"message": "boom"}},
            {"type": "assistant_message_token", "data": {"content": " NEVER READ"}},
        ]
    )
    assert out.answer == "partial prose"
    assert out.errored is True
    assert out.completed is False


def test_the_report_names_rescued_answers_separately() -> None:
    """Counting a rescue as a clean pass is the opposite lie to v6's. Both are
    measurements that do not describe the run, so the summary carries it."""
    results = [
        rb.QuestionResult(index=1, question="a", answer="clean", seconds=10.0, completed=True),
        rb.QuestionResult(
            index=2, question="b", answer="rescued", seconds=180.0, completed=True, partial=True
        ),
    ]
    summary = rb.summarize(results)
    assert summary["answered"] == 2
    assert summary["partial"] == 1
    assert summary["partial_indexes"] == [2]
    report = rb.render_report(results, summary)
    assert "RESCUED" in report
    assert "rescued" in report.lower()


def test_the_report_prints_the_numbers_it_already_collects() -> None:
    """⚠ A measurement you collect and do not print is indistinguishable from
    one you never took.

    ``memo_hits`` and ``hit_refusal_ceiling`` have been fetched into
    ``call_budget`` since #920/#866 and were never rendered. So two sessions
    in a row concluded that "neither battery tool emits memo_hits" and that
    #920 was therefore unverified — while battery v7's q6 had THIRTEEN of them
    sitting in the run row the harness had already read.
    """
    results = [
        rb.QuestionResult(
            index=1,
            question="a",
            answer="ok",
            seconds=10.0,
            completed=True,
            call_budget={"memo_hits": 13, "refusals": 0, "record_calls": 2},
        ),
        rb.QuestionResult(
            index=2,
            question="b",
            answer="",
            seconds=190.0,
            completed=True,
            call_budget={"memo_hits": 0, "refusals": 26, "hit_refusal_ceiling": True},
        ),
    ]
    summary = rb.summarize(results)
    assert summary["memo_hits_total"] == 13
    assert summary["hit_refusal_ceiling"] == [2]

    report = rb.render_report(results, summary)
    assert "memo" in report.lower(), "the memo column vanished from the table header"
    assert "memo hits (duplicate reads collapsed): 13" in report

    # ⚠ The TABLE ROW specifically, not just "13 appears somewhere". The first
    # cut asserted `"13" in report` and passed against a mutant that deleted
    # the whole memo column — because the summary line below the table also
    # says 13. A guard that cannot tell the per-question column from the
    # battery total is not guarding the column (gotcha 40's rule: assert the
    # thing itself, never something adjacent that happens to co-occur).
    row = next(ln for ln in report.splitlines() if ln.startswith("| 1 |"))
    assert "13" in row, f"per-question memo hits missing from its own row: {row!r}"
    busy = next(ln for ln in report.splitlines() if ln.startswith("| 2 |"))
    assert "⚠" in busy, "the ceiling-hit marker is missing from the looping run's row"
    assert "ceiling hit on [2]" in report, (
        "a run that hit the refusal ceiling must say so — it is the difference "
        "between a slow question and a looping one"
    )


def test_zero_memo_hits_is_reported_not_hidden() -> None:
    """⚠ Zero is a RESULT, not a missing value. Across a whole battery it means
    either a corpus that never repeats a lookup or a memo that has stopped
    working, and printing the figure is the only thing that lets a reader ask
    which."""
    results = [rb.QuestionResult(index=1, question="a", answer="ok", seconds=1.0, completed=True)]
    summary = rb.summarize(results)
    assert summary["memo_hits_total"] == 0
    assert "memo hits (duplicate reads collapsed): 0" in rb.render_report(results, summary)


# ── a cut pipe must not score as a silent agent ─────────────────────────────
#
# Battery v10-100 recorded q63 and q96 as BLANK. Both had answered:
# orchestrator_runs holds 331 and 1075 characters. The stream was cut mid-turn
# — the SSE keepalive was never reaching the wire (see
# tests/unit/test_sse_keepalive.py), so a long silent window let a proxy close
# the connection.
#
# ⚠ THE HARNESS COULD NOT SAY SO. ``aiter_lines`` ends when the body ends and
# raises NOTHING, so a cut pipe produced answer="", error=None — identical to
# an agent that said nothing. And ``errored``, which the folder had computed
# since v5, was never copied onto QuestionResult. Three causes, one
# observation, and the battery reported all three as the agent's fault.


def test_a_clean_close_is_recorded_as_one() -> None:
    folder = rb._StreamFolder()
    folder.feed({"type": "assistant_message_start", "data": {}})
    folder.feed({"type": "assistant_message_token", "data": {"content": "hi"}})
    folder.feed({"type": "done", "data": {"summary": "hi"}})
    folder.outcome.saw_terminator = True  # what _ask sets on "[DONE]"
    out = folder.finish()
    assert out.answer == "hi" and out.saw_terminator and not out.errored


def test_a_cut_pipe_is_distinguishable_from_a_silent_agent() -> None:
    """⚠ THE q63/q96 fix. Both produce answer="" — only ``saw_terminator``
    separates them, and without it the battery blames the agent."""
    cut = rb._StreamFolder()
    cut.feed({"type": "thinking", "data": {"content": "working"}})
    # no done, no [DONE] — the body simply stopped
    silent = rb._StreamFolder()
    silent.feed({"type": "done", "data": {"summary": ""}})
    silent.outcome.saw_terminator = True

    assert cut.finish().answer == "" and silent.finish().answer == ""
    assert cut.outcome.saw_terminator is False, "a cut pipe has no terminator"
    assert silent.outcome.saw_terminator is True, "a genuinely empty answer closed cleanly"


def test_a_server_error_is_not_an_empty_agent() -> None:
    """``errored`` was computed and discarded. A server error event and a lost
    stream both yielded answer="" with error=None."""
    folder = rb._StreamFolder()
    assert folder.feed({"type": "error", "data": {"message": "boom"}}) is False
    assert folder.finish().errored is True


def test_the_outcome_fields_reach_the_question_result() -> None:
    """⚠ Both sides (gotcha 28). The folder computing a field is worth nothing
    if the caller drops it — which is exactly what happened to ``errored`` for
    the life of v5. Asserted on the AST so a reflow cannot hide it."""
    import ast
    import pathlib

    src = pathlib.Path("scripts/run_battery.py").read_text(encoding="utf-8")
    assigned = {
        t.attr
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Assign)
        for t in ast.walk(node)
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "res"
    }
    for field_name in ("errored", "saw_terminator"):
        assert field_name in assigned, (
            f"StreamOutcome.{field_name} is computed and never copied onto QuestionResult — "
            f"it cannot reach the report, so the diagnosis is lost at the last step"
        )
