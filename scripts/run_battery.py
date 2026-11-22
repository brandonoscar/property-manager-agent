#!/usr/bin/env python3
"""Run the live task battery and capture what each run ACTUALLY did.

Batteries v1-v4 were run by hand. That was survivable while the question was
"is the answer right", but v4's finding was not about answers — it was that
**three of twelve questions returned zero characters**, having spent 39% of the
battery's budget hitting the 240s wall clock (gotcha 32). Diagnosing that meant
opening Langfuse traces one at a time, because ``stop_reason`` and the two
budget ledgers were emitted there and nowhere else.

So v5 records the mechanism, not just the text. Each run now yields:

* ``answer`` / ``seconds`` / ``completed`` — what ``score_battery.py`` already
  consumes, unchanged, so the existing scorer keeps working;
* ``stop_reason`` — ``wall_clock_deadline`` is the specific failure v5 exists
  to count;
* ``call_budget`` — record-call count, refusals, memo hits (gotcha 25/32);
* ``specialist_budget`` — present only on questions the effort classifier
  recognised as one-fact (#874);
* ``tool_calls`` / ``handoffs`` — what it reached for, in order.

The last four come from ``orchestrator_runs`` (written in the run's ``finally``
so the deadline path is covered too), NOT from the SSE stream — the ledgers
live inside the specialist and never reach a client.

**A zero-character answer is a FAILURE, not a slow success.** v4's headline was
only visible because someone noticed three empty replies; this prints that
count first and refuses to bury it in an average.

Usage::

    python scripts/run_battery.py --out batteries/v5 --questions scripts/battery_questions.json
    python scripts/run_battery.py --out batteries/v5 --only 5,7,11   # re-run v4's losses
    python scripts/run_battery.py --out batteries/v5 --dry-run       # print the plan, spend nothing
    python scripts/run_battery.py --out batteries/v5 --preflight-only  # one question, not twelve

``--only`` is 1-indexed against ``battery_questions.json``, and those indexes
only line up with the v4 report while that file holds the CANONICAL v1-v4
questions. It does now; it did not between #877 and this commit.

Exit codes -- a battery that stops early must say WHY:
    0  ok                        2  token expired mid-run
    3  a question returned nothing
    5  pre-flight $0 -- deploy not rolled, OR company not seeded
    6  pre-flight request FAILED -- a backend error, NOT a stale deploy
    7  pre-flight COMPLETED but returned zero characters

Environment (all required unless --dry-run):
    HELIXIS_BATTERY_BASE_URL   e.g. https://your-app.vercel.app
    HELIXIS_BATTERY_TOKEN      a helixis-test JWT for the target company
    HELIXIS_BATTERY_COMPANY_ID the company to run against (AgentRequest.company_id)
    HELIXIS_SUPABASE__DB_URL   to read back the run diagnostics

Then score it::

    python scripts/score_battery.py batteries/v5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
# ⚠ ``scripts/`` is sys.path[0] when this is run as a script, but NOT when the
# unit tests load it by path via ``spec_from_file_location`` — so make the
# sibling import work in both, rather than only in the one that isn't tested.
sys.path.insert(0, str(_REPO / "scripts"))

from _console import force_utf8_console  # noqa: E402  — must follow the path setup

force_utf8_console()

#: Wall-clock ceiling per question, generous enough that WE are never the
#: binding constraint — the agent's own 240s deadline should always bite first.
#: If this one fires, the run hung somewhere the agent's watchdog doesn't cover
#: and that is itself the finding.
_HARNESS_TIMEOUT_S = 400.0

#: How long to wait for the run row's diagnostics to land after the stream
#: closes. The write happens in the run's ``finally``, which is after the
#: stream terminator — so a read that races it comes back empty and would look
#: exactly like "this run had no diagnostics".
_STATS_SETTLE_S = 6.0

#: The pre-flight question, verbatim from ``run_battery4.py``'s
#: ``PREFLIGHT_Q = QUESTIONS[1]``. Held as a literal AND checked against the
#: loaded questions file at startup, because the gate below is not generic: it
#: asks whether a NON-ZERO DOLLAR FIGURE came back, which is only meaningful for
#: a question that asks for one. Question 2 of a *different* battery file is not
#: interchangeable - PR #877 shipped a reconstructed ``battery_questions.json``
#: whose q02 was an open-ended portfolio health check, against which this gate
#: could never pass however healthy the deploy was.
_PREFLIGHT_Q = "What's my total unpaid vendor bill exposure right now?"

#: Dollar figures in free prose (``$1,234.56``). Ported from v4's ``money_in``.
_MONEY = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


class _TokenExpiredError(RuntimeError):
    """Auth died mid-battery. Fatal on purpose — see ``_ask``."""


# ── pure core (unit-tested; no network, no DB) ────────────────────────────


@dataclass
class StreamOutcome:
    """What consuming one turn's SSE stream produced."""

    answer: str = ""
    completed: bool = False
    #: The answer came back marked ``truncated`` — a deadline SYNTHESIS or a
    #: salvage, not a full reply. Kept separate from ``answer`` because
    #: counting it as a clean pass would inflate the battery exactly the way
    #: ``done_event``'s own docstring warns about.
    partial: bool = False
    errored: bool = False
    #: The stream ended with the ``data: [DONE]`` terminator rather than the
    #: body simply stopping.
    #:
    #: ⚠ WITHOUT THIS, A CUT PIPE AND A FINISHED RUN ARE THE SAME OBSERVATION.
    #: ``aiter_lines`` ends when the body ends and raises nothing, so a
    #: connection closed mid-turn produced ``answer=""``, ``error=None`` and was
    #: reported as an agent that said nothing. That is how battery v10-100 q63
    #: and q96 were recorded blank while ``orchestrator_runs`` held 331 and 1075
    #: characters. A transport failure must never score as an empty agent — the
    #: same reasoning that made ``partial`` its own field.
    saw_terminator: bool = False


class _StreamFolder:
    """Folds SSE events into a :class:`StreamOutcome`.

    ⚠ WHY THIS IS A CLASS AND NOT FOUR LOCALS IN ``_ask``
    ------------------------------------------------------
    Because the bug it fixes was invisible to every static check and cost a
    whole battery. On the v6 run (2026-08-27) five of twelve questions were
    reported as ZERO CHARACTERS at ``wall_clock_deadline``. They were not
    empty. ``orchestrator_runs`` holds all five, ``status=succeeded``,
    ``error_type=SoftDeadlineSynthesized``, carrying 987 to 4,129 characters
    — including a delinquency answer that was correct to the dollar
    ("$36,340 outstanding across 20 delinquent leases") with the debtor
    ladder in the right order. The product answered. The harness lost it.

    The mechanism: on a normal turn the prose streams as
    ``assistant_message_token`` deltas, so the terminal ``done`` event only
    has to flag completion. But ``deadline_synthesis`` is a FRESH model call
    made AFTER the streaming loop was aborted — its text is never tokenised
    onto the wire. It exists in exactly one place, ``done.summary``, and the
    old loop did ``elif etype == "done": completed = True`` and dropped it.

    So the harness manufactured, on the rescue path, precisely the finding it
    exists to count — and its own docstring already warned about this family
    ("a client that listens to the wrong one gets an empty string on a run
    that actually answered"). Gotcha 28's rule: a field crossing a
    producer/consumer boundary needs a test that reads BOTH sides. As four
    locals inside a network call there was nothing a test could reach; as a
    fold it is exercised directly, which is what makes the guard possible at
    all (gotcha 43 — a runtime bug is invisible to static analysis by
    construction).

    ⚠ PRECEDENCE MATCHES THE FRONTEND, deliberately. ``store.ts``'s
    ``finishAgentResponse`` uses ``summary`` ONLY when nothing streamed, so a
    good stream — which may exceed the backend's 8,000-char summary
    truncation — is never clobbered. The harness must measure what a user
    actually sees, so it applies the same rule rather than a better one.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._fallback: list[str] = []
        self._summary = ""
        self._in_msg = False
        self.outcome = StreamOutcome()

    def feed(self, evt: dict[str, Any]) -> bool:
        """Absorb one event. Returns False when the stream should stop."""
        etype = evt.get("type")
        data = evt.get("data") or {}
        # The payload key is ``content`` (see ``assistant_message_token_event``),
        # NOT ``text``. Reading the wrong key appends "" for every token, so a
        # run that answered perfectly reports zero characters -- and ``empty``
        # is this harness's headline metric, so the bug does not merely lose
        # the text, it manufactures the exact finding v5 exists to count.
        if etype == "assistant_message_start":
            self._in_msg = True
        elif etype == "assistant_message_token":
            self._parts.append(str(data.get("content", "")))
        elif etype == "assistant_message_end":
            self._in_msg = False
        elif etype == "token" and not self._in_msg:
            # Only outside an assistant turn, or the prose is counted twice on
            # paths that emit both.
            self._fallback.append(str(data.get("content", "")))
        elif etype == "done":
            self.outcome.completed = True
            # ⚠ THE LINE THAT WAS MISSING. On the synthesis path this is the
            # only copy of the answer that ever reaches a client.
            self._summary = str(data.get("summary", ""))
            self.outcome.partial = bool(data.get("truncated"))
        elif etype == "error":
            self.outcome.errored = True
            return False
        return True

    def finish(self) -> StreamOutcome:
        streamed = "".join(self._parts) or "".join(self._fallback)
        self.outcome.answer = streamed or self._summary
        # ``partial`` describes the ANSWER WE KEPT. A truncated done event
        # riding alongside a complete stream did not supply this text, so
        # flagging it would mislabel a full reply as a salvage.
        if streamed:
            self.outcome.partial = False
        return self.outcome


@dataclass
class QuestionResult:
    """One question's outcome, in the shape ``score_battery.py`` reads."""

    index: int
    question: str
    answer: str = ""
    seconds: float = 0.0
    completed: bool = False
    partial: bool = False
    #: A server ``error`` event arrived. The folder has computed this since v5
    #: and nothing carried it here, so an empty answer had three
    #: indistinguishable causes: the agent said nothing, the server errored, or
    #: the connection died.
    errored: bool = False
    #: The stream closed with ``data: [DONE]``. False means the body simply
    #: stopped — a cut pipe, not a silent agent. ``aiter_lines`` raises nothing
    #: on an early close, so without this the two are the same observation.
    saw_terminator: bool = False
    stop_reason: str | None = None
    call_budget: dict[str, Any] = field(default_factory=dict)
    specialist_budget: dict[str, Any] = field(default_factory=dict)
    synthesis: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[Any] = field(default_factory=list)
    handoffs: list[Any] = field(default_factory=list)
    error: str | None = None

    @property
    def empty(self) -> bool:
        """Zero characters back. The v4 failure mode, named explicitly so it
        can never be averaged into a latency number."""
        return not self.answer.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "seconds": round(self.seconds, 2),
            "completed": self.completed,
            "partial": self.partial,
            "errored": self.errored,
            "saw_terminator": self.saw_terminator,
            "stop_reason": self.stop_reason,
            "call_budget": self.call_budget,
            "specialist_budget": self.specialist_budget,
            "synthesis": self.synthesis,
            "tool_calls": self.tool_calls,
            "handoffs": self.handoffs,
            "error": self.error,
        }


def summarize(results: list[QuestionResult]) -> dict[str, Any]:
    """The battery-level verdict. Pure, so the reporting is testable without
    spending a cent on live model calls.

    ``wasted_fraction`` is the number v4 was really about: seconds spent on
    runs that returned nothing, over seconds spent in total. An average
    latency hides it completely — three dead runs and nine good ones look
    like "a bit slow".
    """
    total = len(results)
    if not total:
        return {"questions": 0}
    empties = [r for r in results if r.empty]
    deadlined = [r for r in results if r.stop_reason == "wall_clock_deadline"]
    spent = sum(r.seconds for r in results)
    wasted = sum(r.seconds for r in empties)
    answered = [r for r in results if not r.empty]
    # ⚠ Answered, but RESCUED. These are deadline-synthesis or salvage replies
    # — real text, written from what the run had gathered when the clock cut
    # it off, not what it meant to say. Counting them silently as clean passes
    # would swing this report from the v6 failure (calling five good answers
    # empty) straight into the opposite lie. Both are measurements that do not
    # describe the run.
    partials = [r for r in answered if r.partial]
    times = sorted(r.seconds for r in results)
    return {
        "questions": total,
        "answered": len(answered),
        "partial": len(partials),
        "partial_indexes": [r.index for r in partials],
        "empty": len(empties),
        "empty_indexes": [r.index for r in empties],
        "deadlined": len(deadlined),
        "deadlined_indexes": [r.index for r in deadlined],
        "errors": [r.index for r in results if r.error],
        "seconds_total": round(spent, 1),
        "seconds_wasted_on_empty": round(wasted, 1),
        "wasted_fraction": round(wasted / spent, 3) if spent else 0.0,
        "median_seconds": round(times[total // 2], 1),
        "slowest_seconds": round(times[-1], 1),
        "refusals_total": sum(
            int(r.call_budget.get("refusals", 0) or 0)
            if isinstance(r.call_budget.get("refusals"), int)
            else sum((r.call_budget.get("refusals") or {}).values())
            for r in results
        ),
        "specialist_capped": [r.index for r in results if r.specialist_budget.get("hit_cap")],
        # The per-run memo's whole job, totalled. Zero across a battery means
        # no duplicate read was ever collapsed — which is either a corpus that
        # never repeats a lookup or a memo that has stopped working, and the
        # figure is the only thing that tells them apart.
        "memo_hits_total": sum(int(r.call_budget.get("memo_hits", 0) or 0) for r in results),
        "hit_refusal_ceiling": [
            r.index for r in results if r.call_budget.get("hit_refusal_ceiling")
        ],
    }


def render_report(results: list[QuestionResult], summary: dict[str, Any]) -> str:
    """A scannable mechanism report. Deliberately leads with the empties."""
    lines: list[str] = ["# Battery v5 — run report", ""]

    if summary.get("empty"):
        lines += [
            f"## {summary['empty']} of {summary['questions']} returned NOTHING",
            "",
            f"Questions {summary['empty_indexes']} produced zero characters, "
            f"consuming {summary['seconds_wasted_on_empty']}s "
            f"({summary['wasted_fraction']:.0%} of the battery's {summary['seconds_total']}s).",
            "",
        ]
    else:
        lines += ["## Every question returned an answer", ""]

    if summary.get("partial"):
        # Named, never folded into "answered". A rescued reply is written from
        # what the run had when the clock cut it off — it can be correct (v6's
        # q3 was, to the dollar) and it can also be openly incomplete. Either
        # way the reader has to be able to tell it apart from a clean pass.
        lines += [
            f"## {summary['partial']} answer(s) came back RESCUED",
            "",
            f"Questions {summary['partial_indexes']} hit the soft deadline and were "
            "answered by synthesis over what had been gathered — real text, marked "
            "`truncated` on the wire, not a full reply. Count them as answered; do "
            "not count them as clean.",
            "",
        ]

    hit = summary.get("hit_refusal_ceiling") or []
    _ceiling_note = f" — ceiling hit on {hit}" if hit else ""
    lines += [
        f"- deadlined: **{summary['deadlined']}** {summary['deadlined_indexes'] or ''}",
        f"- median: {summary['median_seconds']}s · slowest: {summary['slowest_seconds']}s",
        f"- read refusals across the battery: {summary['refusals_total']}{_ceiling_note}",
        f"- memo hits (duplicate reads collapsed): {summary['memo_hits_total']}",
        f"- specialist cap engaged on: {summary['specialist_capped'] or 'none'}",
        "",
        "## Per question",
        "",
        # ⚠ memo/ceiling ADDED 2026-08-27. Both numbers were already being
        # fetched into ``call_budget`` and simply never rendered, so two
        # sessions concluded in a row that "neither battery tool emits
        # memo_hits" and that #920 was unverified — while v7 q6 had thirteen
        # of them sitting in the row. A measurement you collect and do not
        # print is indistinguishable from one you never took.
        "| # | s | chars | stop | reads | refused | memo | specialists |",
        "|---:|---:|---:|---|---:|---:|---:|---|",
    ]
    for r in results:
        cb = r.call_budget or {}
        refused = cb.get("refusals")
        refused_n = refused if isinstance(refused, int) else sum((refused or {}).values())
        sb = r.specialist_budget or {}
        lines.append(
            f"| {r.index} | {r.seconds:.0f} | {len(r.answer)}"
            f"{' *(rescued)*' if r.partial else ''} | "
            f"{r.stop_reason or ('ok' if r.completed else '—')} | "
            f"{cb.get('record_calls', '—')} | {refused_n or 0}"
            f"{' ⚠' if cb.get('hit_refusal_ceiling') else ''} | "
            f"{cb.get('memo_hits', '—')} | "
            f"{sb.get('specialist_calls', '—')}{' (capped)' if sb.get('hit_cap') else ''} |"
        )
    return "\n".join(lines) + "\n"


def money_in(text: str) -> list[float]:
    """Every dollar figure in ``text``. Behaviour ported from v4's ``money_in``."""
    out: list[float] = []
    for m in _MONEY.finditer(text or ""):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def preflight_verdict(*, answered: bool, answer: str) -> tuple[str, str]:
    """Classify a pre-flight result into ONE of three distinct causes.

    These must never be collapsed. ``money_in("")`` returns ``[]`` for a run
    that ERRORED exactly as it does for a genuine ``$0`` -- and on 2026-08-22
    collapsing them printed "deploy has NOT rolled" while chat was actually
    500ing behind an HTTP 200 SSE stream, which sent the operator hunting a
    stale deploy that was fine.

    ⚠ ``zero`` carries an ASSUMPTION the caller must check: that the company
    HAS unpaid vendor bills. Against a genuinely empty company, "$0" is the
    CORRECT answer and this returns ``zero`` -- reporting "the deploy has NOT
    rolled" about a deploy that is fine, which is the 08-22 mistake in a new
    costume. The gate is only meaningful against a seeded company. For the
    50-state test bed that holds (67 unpaid bills, ~$26,000 exposure); confirm
    it before trusting a ``zero`` anywhere else.

    Returns ``(verdict, detail)``; verdict is ``backend_failed``, ``empty``,
    ``zero`` or ``pass``.
    """
    if not answered:
        return "backend_failed", (
            "the backend FAILED this request. This is NOT a stale deploy -- pull "
            "the Render traceback for this session before rerunning."
        )
    if not answer.strip():
        # ``done`` arrived but no prose did. This is v4's headline failure and
        # it is NOT a $0 answer: money_in("") is [] for a run that said nothing
        # exactly as for one that said "zero", so calling it ``zero`` reports a
        # stale deploy for a run that never spoke.
        return "empty", (
            "the run COMPLETED but returned zero characters. Not a $0 answer and "
            "not a stale deploy -- check the stream parser and the wall clock."
        )
    amounts = [a for a in money_in(answer) if a > 0]
    if not amounts:
        return "zero", (
            "a REAL answer came back carrying no non-zero figure. Either the "
            "deploy has NOT rolled, or this company genuinely has no unpaid "
            "vendor bills -- check that the company is seeded before concluding "
            "the former."
        )
    return "pass", f"non-zero exposure reported: ${max(amounts):,.2f}"


def load_sets(questions_path: Path) -> list[dict[str, Any]]:
    """Per-question set metadata, if the questions file has a sidecar.

    ``battery_questions_100.json`` pairs with ``battery_sets_100.json``: one
    entry per question carrying ``set`` / ``set_name`` / ``scoring`` and the
    two skip flags. Absent (the canonical 12) → no sets, and every set-aware
    path below no-ops.
    """
    sidecar = questions_path.with_name(questions_path.stem.replace("questions", "sets") + ".json")
    if not sidecar.exists():
        return []
    meta: list[dict[str, Any]] = json.loads(sidecar.read_text(encoding="utf-8"))
    return meta


def unattended_skips(meta: list[dict[str, Any]]) -> set[int]:
    """Question numbers a normal run must NOT execute.

    Two kinds, both skipped by DEFAULT rather than deleted, because the
    questions are real and belong in the corpus:

    * ``requires_attachment`` — the document sets. ``/agent/run`` takes text,
      so these cannot be asked over this harness at all; running them would
      score the product on a question it never received.
    * ``sends_real_email`` — the compose set. On approval these send REAL mail
      to REAL people. Nobody should discover that from a battery report.
    """
    return {int(m["n"]) for m in meta if m.get("requires_attachment") or m.get("sends_real_email")}


def select_questions(
    questions: list[str],
    only: str | None,
    *,
    sets: str | None = None,
    meta: list[dict[str, Any]] | None = None,
    skip: set[int] | None = None,
) -> list[tuple[int, str]]:
    """1-indexed selection, so ``--only 5,7,11`` names the same questions the
    v4 report did rather than off-by-one neighbours.

    ``--set`` selects by SET instead: ``--set 17`` runs the three contradiction
    questions, which is the unit the 100-question battery is meant to be run
    and re-run in. A set is ~7 minutes and ~$1, comfortably inside one
    Supabase token's ~1 hour life — the reason the corpus is grouped in threes
    rather than run as one 4-hour pass that the token could not survive.
    """
    pairs = list(enumerate(questions, start=1))
    if sets:
        if not meta:
            raise SystemExit(f"--set needs a sets sidecar next to {len(questions)} questions")
        wanted_sets = _expand(sets)
        known = {int(m["set"]) for m in meta}
        if missing := sorted(wanted_sets - known):
            raise SystemExit(f"--set names sets that do not exist: {missing}")
        in_sets = {int(m["n"]) for m in meta if int(m["set"]) in wanted_sets}
        pairs = [(i, q) for i, q in pairs if i in in_sets]
    if skip:
        pairs = [(i, q) for i, q in pairs if i not in skip]
    if not only:
        return pairs
    wanted = _expand(only)
    missing = sorted(wanted - {i for i, _ in pairs})
    if missing:
        raise SystemExit(f"--only names questions that do not exist: {missing}")
    return [(i, q) for i, q in pairs if i in wanted]


def _expand(spec: str) -> set[int]:
    """``"5,7,11"`` / ``"1-4"`` → the integers. Shared by --only and --set so
    the two selectors cannot drift into different range syntaxes."""
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = (int(x) for x in chunk.split("-", 1))
            out.update(range(lo, hi + 1))
        else:
            out.add(int(chunk))
    return out


# ── live side ─────────────────────────────────────────────────────────────


async def _ask(
    base_url: str, token: str, question: str, session_id: str, company_id: str
) -> StreamOutcome:
    """POST the question, consume the SSE stream, return the outcome.

    Reads the assistant-message tokens rather than raw ``token`` events: the
    live chat path streams prose through the assistant-message trio, and a
    client that listens to the wrong one gets an empty string on a run that
    actually answered (gotcha 4's family). The event folding itself lives in
    :class:`_StreamFolder` so it can be tested without a network — see that
    class for the ``done.summary`` bug this shape exists to prevent.
    """
    import httpx

    folder = _StreamFolder()
    # ⚠ httpx's ``timeout`` is PER-READ, not per-request, and on a streaming
    # response that distinction is the whole bug. Once the server started
    # sending a keepalive comment every 15s, every one of them reset this
    # clock — so a stream delivering nothing could never time out. Battery v11
    # q96 sat on the socket for 2,948 seconds (49 minutes) against a stated
    # 400s ceiling, ate most of a 60-minute token by itself, and took q97/q98/
    # q100 down with it: three questions never ran, including the
    # release-blocking fair-housing one.
    #
    # So the wall clock is enforced HERE, over the whole read loop, where
    # keepalives cannot touch it.
    async with httpx.AsyncClient(timeout=_HARNESS_TIMEOUT_S) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/api/v1/agent/run",
            headers={"Authorization": f"Bearer {token}"},
            # ``company_id`` is REQUIRED by ``AgentRequest`` and is what
            # ``require_company_access`` authorises against. Omitting it does not
            # fail open or fall back to the token's own company -- FastAPI
            # rejects the body with 422 before the route runs, so every request
            # this harness made returned an empty answer that looked exactly
            # like a wall-clock loss. ``source`` tags the run in
            # ``orchestrator_runs`` telemetry the same way v1-v4 did.
            json={
                "task": question,
                "company_id": company_id,
                "source": "chat",
                "session_id": session_id,
            },
        ) as resp:
            # A Supabase access token lives ~1 hour, and a 12-question battery
            # can run close to that — so expiry MID-BATTERY is a real outcome,
            # not a hypothetical. An expired token yields an empty answer,
            # which is byte-for-byte what a wall-clock loss looks like: the
            # battery would report v4's exact failure mode and be wrong about
            # it. Raise instead, so it can never be mistaken for a finding.
            if resp.status_code in (401, 403):
                raise _TokenExpiredError(
                    f"HTTP {resp.status_code} — HELIXIS_BATTERY_TOKEN is expired or "
                    "wrong-company. Re-grab it and re-run the remaining questions "
                    "with --only; do NOT read the empty answers as deadline losses."
                )
            resp.raise_for_status()
            deadline = time.monotonic() + _HARNESS_TIMEOUT_S
            async for line in resp.aiter_lines():
                if time.monotonic() > deadline:
                    # Keepalives keep the socket warm; they are not progress.
                    # Leaving the loop preserves whatever the folder collected,
                    # and ``saw_terminator`` stays False so the report can tell
                    # this from a clean close.
                    break
                if not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    folder.outcome.saw_terminator = True
                    break
                try:
                    evt = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if not folder.feed(evt):
                    break
    return folder.finish()


async def _fetch_stats(db_url: str, session_id: str) -> dict[str, Any]:
    """Read back what the run recorded. Returns {} rather than raising — a
    missing row must not lose an answer we already have in hand."""
    import asyncpg

    try:
        conn = await asyncpg.connect(db_url, statement_cache_size=0)
    except Exception:
        return {}
    try:
        row = await conn.fetchrow(
            """
            SELECT raw, tool_calls, handoffs, status
              FROM public.orchestrator_runs
             WHERE session_id = $1
             ORDER BY started_at DESC
             LIMIT 1
            """,
            session_id,
        )
    except Exception:
        return {}
    finally:
        await conn.close()
    if row is None:
        return {}

    def _j(v: Any, default: Any) -> Any:
        if v is None:
            return default
        return json.loads(v) if isinstance(v, str) else v

    raw = _j(row["raw"], {}) or {}
    return {
        "stop_reason": raw.get("stop_reason"),
        "call_budget": raw.get("call_budget") or {},
        # What the deadline rescue did, when it ran at all. Absent means it
        # never ran — a different diagnosis from "ran and produced nothing".
        "synthesis": raw.get("synthesis") or {},
        "specialist_budget": raw.get("specialist_budget") or {},
        "tool_calls": _j(row["tool_calls"], []) or [],
        "handoffs": _j(row["handoffs"], []) or [],
        "status": row["status"],
    }


async def run_battery(
    pairs: list[tuple[int, str]],
    *,
    base_url: str,
    token: str,
    db_url: str,
    company_id: str,
    out: Path,
) -> list[QuestionResult]:
    """Sequential on purpose. Concurrent runs would contend for the same
    Buildium rate limit (10 req/s) and the same 20-sandbox E2B ceiling, and a
    latency number measured under self-inflicted contention answers a question
    nobody asked."""
    out.mkdir(parents=True, exist_ok=True)
    results: list[QuestionResult] = []
    for idx, question in pairs:
        session_id = f"battery-v5-q{idx:02d}-{int(time.time())}"
        res = QuestionResult(index=idx, question=question)
        started = time.monotonic()
        try:
            outcome = await _ask(base_url, token, question, session_id, company_id)
            res.answer, res.completed, res.partial = (
                outcome.answer,
                outcome.completed,
                outcome.partial,
            )
            # ⚠ BOTH of these were computed by the folder and thrown away here.
            # ``errored`` is set on a server ``error`` event; ``saw_terminator``
            # separates a clean close from a cut pipe. Without them an empty
            # answer has three indistinguishable causes — the agent said
            # nothing, the server errored, or the connection died — and the
            # battery reported all three as the first one.
            res.errored, res.saw_terminator = outcome.errored, outcome.saw_terminator
        except _TokenExpiredError as exc:
            # Deliberately NOT swallowed. Every remaining question would record
            # an empty answer and the report would announce a battery-wide
            # collapse that is actually one expired credential.
            print(f"\n[q{idx:02d}] ABORTING: {exc}", file=sys.stderr)
            if results:
                print(
                    f"{len(results)} question(s) completed before this; "
                    f"re-run the rest with --only",
                    file=sys.stderr,
                )
            raise SystemExit(2) from None
        except Exception as exc:  # noqa: BLE001 — one bad question must not end the battery
            res.error = f"{type(exc).__name__}: {exc}"
        res.seconds = time.monotonic() - started

        # The stats write lands in the run's finally, i.e. AFTER the stream
        # terminator. Reading immediately races it and returns empty, which is
        # indistinguishable from "no diagnostics" — so settle first.
        await asyncio.sleep(_STATS_SETTLE_S)
        stats = await _fetch_stats(db_url, session_id)
        res.stop_reason = stats.get("stop_reason")
        res.call_budget = stats.get("call_budget", {})
        res.specialist_budget = stats.get("specialist_budget", {})
        res.synthesis = stats.get("synthesis", {})
        res.tool_calls = stats.get("tool_calls", [])
        res.handoffs = stats.get("handoffs", [])

        (out / f"q{idx:02d}.json").write_text(json.dumps(res.to_dict(), indent=2), encoding="utf-8")
        flag = "EMPTY" if res.empty else f"{len(res.answer)} chars"
        print(
            f"[q{idx:02d}] {res.seconds:6.1f}s  {flag:>12}  "
            f"stop={res.stop_reason or '-'}  {question[:52]}"
        )
        results.append(res)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the live task battery (v5).")
    p.add_argument("--out", required=True, type=Path, help="directory for qNN.json + report")
    p.add_argument(
        "--questions",
        type=Path,
        default=_REPO / "scripts" / "battery_questions.json",
        help="JSON list of question strings",
    )
    p.add_argument("--only", help="1-indexed selection, e.g. 5,7,11 or 1-4")
    p.add_argument(
        "--set",
        dest="sets",
        help="select by SET, e.g. 17 or 17,29 or 1-16 (needs a sets sidecar). "
        "One set is 3 questions, ~7 min, ~$1 — the unit this corpus is run in.",
    )
    p.add_argument(
        "--include-unattended",
        action="store_true",
        help="also run the questions skipped by default: the document sets "
        "(need a file /agent/run cannot carry) and the compose set (sends REAL "
        "email on approval).",
    )
    p.add_argument("--dry-run", action="store_true", help="print the plan, spend nothing")
    p.add_argument(
        "--preflight-only",
        action="store_true",
        help="run ONLY the pre-flight question and stop (one request, not twelve)",
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip the deploy-rolled gate; only for a re-run you have already gated",
    )
    args = p.parse_args(argv)

    if not args.questions.exists():
        raise SystemExit(f"no question file at {args.questions}")
    questions = json.loads(args.questions.read_text())
    meta = load_sets(args.questions)
    skip = set() if args.include_unattended else unattended_skips(meta)
    if skip:
        print(f"[sets] skipping {len(skip)} question(s) — attachment-only or real-send.")
    pairs = select_questions(questions, args.only, sets=args.sets, meta=meta, skip=skip)
    if not pairs:
        raise SystemExit("selection matched no questions — check --set / --only.")

    if args.dry_run:
        print(f"would run {len(pairs)} question(s) into {args.out}:")
        for i, q in pairs:
            print(f"  q{i:02d}  {q}")
        return 0

    missing = [
        v
        for v in (
            "HELIXIS_BATTERY_BASE_URL",
            "HELIXIS_BATTERY_TOKEN",
            "HELIXIS_BATTERY_COMPANY_ID",
            "HELIXIS_SUPABASE__DB_URL",
        )
        if not os.environ.get(v)
    ]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")

    # -- PRE-FLIGHT --------------------------------------------------------
    # One question before twelve. A battery run against an unrolled deploy
    # spends the whole budget and produces a report that is confidently wrong
    # rather than obviously broken.
    if not args.skip_preflight:
        # ⚠ The gate's real requirement is that the pre-flight question ASKS FOR
        # A DOLLAR FIGURE — it checks a non-zero one came back. It is NOT that
        # the question sits at index 1. That was a proxy, correct while one
        # 12-question file existed and wrong the moment a second arrived:
        # battery_questions_100.json puts the same question at 25, because it
        # belongs to the "unpaid bills" set. Requiring position would have
        # forced the 100 into an order that breaks its own set structure to
        # satisfy a check that never cared about order.
        #
        # So: find it ANYWHERE, and keep the real requirement (it must be this
        # question, whose answer the gate below knows how to read).
        if _PREFLIGHT_Q not in questions:
            raise SystemExit(
                f"pre-flight needs {_PREFLIGHT_Q!r} somewhere in {args.questions} "
                "and it is not there. The gate checks for a non-zero DOLLAR "
                "figure and is meaningless against a question that does not ask "
                "for one. Add it, or pass --skip-preflight knowingly."
            )
        print("===== PRE-FLIGHT =====")
        print(_PREFLIGHT_Q)
        pf_session = f"battery-v5-preflight-{int(time.time())}"
        try:
            pf_outcome = asyncio.run(
                _ask(
                    os.environ["HELIXIS_BATTERY_BASE_URL"],
                    os.environ["HELIXIS_BATTERY_TOKEN"],
                    _PREFLIGHT_Q,
                    pf_session,
                    os.environ["HELIXIS_BATTERY_COMPANY_ID"],
                )
            )
            pf_answer, pf_completed = pf_outcome.answer, pf_outcome.completed
        except _TokenExpiredError as exc:
            raise SystemExit(f"pre-flight: {exc}") from None
        except Exception as exc:  # noqa: BLE001 - reported, not raised, so the
            # transport failure is classified as backend_failed like any other.
            pf_answer, pf_completed = "", False
            print(f"  transport error: {type(exc).__name__}: {exc}")

        verdict, detail = preflight_verdict(answered=pf_completed, answer=pf_answer)
        print(f"  dollar figures found: {money_in(pf_answer)}")
        print(f"  session: {pf_session}")
        print(f"  answer: {pf_answer[:300]!r}")
        if verdict == "backend_failed":
            print(f"!!! PRE-FLIGHT FAILED -- {detail}")
            print("    STOPPING before the battery.")
            return 6
        if verdict == "empty":
            print(f"!!! PRE-FLIGHT EMPTY -- {detail}")
            print("    STOPPING before the battery.")
            return 7
        if verdict == "zero":
            print(f"!!! PRE-FLIGHT $0 -- {detail}")
            print("    STOPPING before the battery.")
            return 5
        print(f"  PRE-FLIGHT PASS -- {detail}")
        if args.preflight_only:
            return 0
    elif args.preflight_only:
        raise SystemExit("--preflight-only and --skip-preflight are contradictory")

    results = asyncio.run(
        run_battery(
            pairs,
            base_url=os.environ["HELIXIS_BATTERY_BASE_URL"],
            token=os.environ["HELIXIS_BATTERY_TOKEN"],
            db_url=os.environ["HELIXIS_SUPABASE__DB_URL"],
            company_id=os.environ["HELIXIS_BATTERY_COMPANY_ID"],
            out=args.out,
        )
    )
    summary = summarize(results)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = render_report(results, summary)
    # ⚠ encoding= IS NOT OPTIONAL. Without it Python uses the platform default,
    # which on Windows is cp1252 — and #923 added a ⚠ to this report. The run
    # crashed with UnicodeEncodeError AFTER every question had executed,
    # exiting non-zero on a run that had actually succeeded. Every write in
    # this file is now explicit (gotcha 25: fix a bug at EVERY layer that has
    # it, not just the line that happened to blow up).
    (args.out / "run-report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"wrote {args.out}/run-report.md — now: python scripts/score_battery.py {args.out}")
    # A battery that returned nothing on any question is a failed battery,
    # whatever the surviving answers scored.
    return 1 if summary["empty"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
