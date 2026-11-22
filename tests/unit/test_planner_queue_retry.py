"""The planner's retry machinery existed and could not run.

Three separate mechanisms, each documented, each unreachable, measured on
helixis-test 2026-08-29:

1. **``in_progress`` was never reclaimed.** ``claim_planner_batch`` selected
   ``planner_status = 'pending'`` and nothing else, so a row abandoned
   mid-plan (SIGKILL, crash, deploy) matched no branch forever. **7 rows
   stuck, the oldest for 52 days.** The docstring said the cooldown window
   protected against exactly this; it only ever gated WHEN a ``pending`` row
   could be re-claimed.

2. **``timed_out`` was never reclaimed either**, which made
   ``mark_planner_timed_out`` a fourth terminal state instead of a retry.
   **Zero rows have ever carried it.**

3. **The worker's timeout branch was dead code.** ``plan()`` wraps every
   failure into ``PlannerError`` before it leaves, so ``except
   litellm.Timeout`` could not fire. **14 rows sit at ``failed`` reading
   "Planner exhausted transient retries: litellm.Timeout", and EVERY failed
   row is at ``planner_attempts = 1``** — the signature of a retry path that
   never ran. Attempts only ever exceeded 1 via the stale-prompt-version
   re-claim, which is a different mechanism entirely.

⚠ The dangerous half of the fix is (1). A row being planned RIGHT NOW is
also ``in_progress`` with a recent timestamp, so elapsed time is the only
thing separating it from an abandoned one — a cooldown shorter than a
legitimate plan turns "reclaim the stranded" into "re-plan the live", which
is double spend and two writers racing one action surface.
"""

from __future__ import annotations

import ast
import pathlib
import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import litellm
import pytest

from helixis.agent.planner import action_planner as action_planner_mod
from helixis.agent.planner.action_planner import (
    PLANNER_WORST_CASE_SECONDS,
    PlannerError,
)
from helixis.webhooks import queue as queue_mod
from helixis.webhooks import worker as worker_mod

#: Minimal enrichment that round-trips through ``EnrichedEvent``, so the
#: handler reaches the planner call rather than bailing at the reconstruct
#: guard — a test that stopped early would report "(nothing)" for every
#: exception and pass against any branching at all.
_ENRICHMENT = {
    "event_name": "Task.Created",
    "company_id": "c1",
    "task": {"id": 1},
}


# ── 1 + 2: which states the claim can reach ──────────────────────────


async def _claimed_predicates(**kwargs: object) -> list[str]:
    """Run ``claim_planner_batch`` against a pool that only records SQL, and
    return the WHERE clauses it issued.

    Reads the query the function actually builds rather than a copy — the
    two branches (versioned and plain) differ, and pinning one would leave
    the other free to regress (gotcha 28's both sides).
    """
    seen: list[str] = []

    async def _fetch(query: str, *_args: object) -> list[object]:
        seen.append(query)
        return []

    pool = MagicMock()
    pool.fetch = _fetch
    await queue_mod.claim_planner_batch(pool, **kwargs)  # type: ignore[arg-type]
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "v4"])
async def test_both_claim_branches_reclaim_abandoned_rows(version: str | None) -> None:
    """THE defect. 7 rows sat ``in_progress`` for 52 days because neither
    branch could select them."""
    queries = await _claimed_predicates(batch_size=5, current_prompt_version=version)
    assert len(queries) == 1
    sql = queries[0]
    assert "'in_progress'" in sql, (
        "an abandoned row is unreachable — this is the 52-day stall, and the "
        "docstring claimed the cooldown already handled it"
    )
    assert "'timed_out'" in sql, "mark_planner_timed_out would just be a fourth terminal state"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "v4"])
async def test_failed_stays_terminal(version: str | None) -> None:
    """Both directions (gotcha 38). ``failed`` is the attempts cap doing its
    job — a deterministically-broken row must stop spending LLM budget, and
    ``reset_planner_for_replan`` is the deliberate way back. A fix that
    reclaimed everything would re-open the budget leak the cap closed."""
    sql = (await _claimed_predicates(batch_size=5, current_prompt_version=version))[0]
    claim_predicate = sql.split("FOR UPDATE")[0]
    # 'failed' must not appear as a claimable status. The versioned branch
    # legitimately names 'succeeded', so this is checked as a status literal
    # rather than a bare substring.
    assert not re.search(r"planner_status[^;]*'failed'", claim_predicate)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "v4"])
async def test_the_attempts_cap_and_cooldown_still_gate(version: str | None) -> None:
    """Reclaiming without these would retry a broken row every cycle forever
    — and, worse, reclaim rows that are still being planned."""
    sql = (await _claimed_predicates(batch_size=5, current_prompt_version=version))[0]
    assert "planner_attempts < $2" in sql
    assert "planner_last_attempted_at" in sql


# ── the cooldown must outlast a legitimate plan ──────────────────────


def test_the_reclaim_cooldown_exceeds_the_planners_worst_case() -> None:
    """The hazard the reclaim introduces. Before this change the cooldown was
    30 s against a 61 s worst case — harmless only because ``in_progress``
    was never reclaimed at all. Making the reclaim real makes the number
    load-bearing, so it is derived from the planner's own budget and read
    from BOTH sides here."""
    assert worker_mod._PLANNER_RECLAIM_COOLDOWN_SECONDS > PLANNER_WORST_CASE_SECONDS, (
        "a sibling worker would reclaim a row that is still being planned: "
        "double LLM spend, and two writers racing one action surface"
    )


def test_the_worst_case_matches_the_planners_own_constants() -> None:
    """A hand-copied number is one more thing that drifts from the code it
    claims to describe (gotcha 45). Recompute it from the parts."""
    import inspect

    from helixis.agent.planner import action_planner as ap

    # ⚠ Read the retry count from the planner itself. This recomputed with a
    # hard-coded ``* 2`` (one retry), the same mistake as the constant, so it
    # held 61 s in place after the default became three retries (123 s).
    retries = inspect.signature(ap.ActionPlanner.__init__).parameters["max_retries"].default
    assert retries == 3
    expected = int(
        ap._LLM_TIMEOUT_SECONDS * (retries + 1) + ap._TRANSIENT_RETRY_BACKOFF_SECONDS * retries
    )
    assert PLANNER_WORST_CASE_SECONDS == expected == 123


# ── 3: a wrapped transient must reach the retry branch ───────────────


def test_a_transient_planner_error_says_so() -> None:
    assert PlannerError("Planner exhausted transient retries: x", transient=True).transient is True


def test_a_planner_error_is_deterministic_by_default() -> None:
    """The safe default. An unmarked failure — invalid output, a malformed
    request, a missing enrichment handler (467 of the 482 failed rows) — must
    not buy itself five more LLM calls."""
    assert PlannerError("Planner call failed: bad json").transient is False


def _row(attempts: int) -> MagicMock:
    """A claimed row whose ``enrichment`` reconstructs into a real
    ``EnrichedEvent``, so ``_process_planner_row`` reaches the planner call
    instead of bailing at the reconstruct guard."""
    row = MagicMock()
    row.id = UUID("11111111-1111-1111-1111-111111111111")
    row.event_name = "Task.Created"
    row.company_id = "c1"
    row.planner_attempts = attempts
    row.payload = {}
    row.enrichment = _ENRICHMENT
    row.deterministic_surface = None
    return row


async def _classify(exc: BaseException, attempts: int) -> str:
    """Drive the worker's REAL handler and report which ``mark_*`` it chose.

    Exercises the shipped except block rather than re-implementing its
    branching, because the branching IS the defect (gotcha 28). A test that
    asserted on a local copy of the logic would have passed against the dead
    ``except litellm.Timeout`` for as long as it existed.
    """
    calls: list[str] = []

    async def _timed_out(*_a: object, **_k: object) -> None:
        calls.append("timed_out")

    async def _failed(*_a: object, **_k: object) -> None:
        calls.append("failed")

    async def _succeeded(*_a: object, **_k: object) -> None:
        calls.append("succeeded")

    handler = MagicMock()
    handler.planner.plan = AsyncMock(side_effect=exc)

    with ExitStack() as stack:
        stack.enter_context(patch.object(worker_mod, "get_pool", lambda: MagicMock()))
        stack.enter_context(patch.object(worker_mod, "get_enrichment_handler", lambda _n: handler))
        stack.enter_context(patch.object(worker_mod, "mark_planner_timed_out", _timed_out))
        stack.enter_context(patch.object(worker_mod, "mark_planner_failed", _failed))
        stack.enter_context(patch.object(worker_mod, "mark_planner_succeeded", _succeeded))
        stack.enter_context(
            patch.object(worker_mod, "gather_mirror_context", AsyncMock(return_value=[]))
        )
        await worker_mod._process_planner_row(_row(attempts))
    return calls[0] if calls else "(nothing)"


@pytest.mark.asyncio
async def test_a_wrapped_timeout_is_retried_across_cycles() -> None:
    """THE defect. 14 rows died at attempt 1 on exactly this exception, and
    the branch written to retry them could not be reached."""
    exc = PlannerError("Planner exhausted transient retries: litellm.Timeout", transient=True)
    assert await _classify(exc, attempts=1) == "timed_out"


@pytest.mark.asyncio
async def test_invalid_llm_output_is_still_terminal() -> None:
    """Both directions. Retrying a model that returned unparseable JSON just
    spends budget to reach the same place — that is what the attempts cap
    exists for."""
    assert await _classify(PlannerError("Planner failed after 2 attempts: bad json"), 1) == "failed"


@pytest.mark.asyncio
async def test_a_transient_failure_past_the_cap_goes_terminal() -> None:
    """The cap still binds. A provider that is down for hours must not be
    retried forever — five cycles, then the inbox shows the Replan pill."""
    exc = PlannerError("Planner exhausted transient retries: litellm.Timeout", transient=True)
    assert await _classify(exc, attempts=worker_mod._PLANNER_MAX_ATTEMPTS) == "failed"


@pytest.mark.asyncio
async def test_a_bare_litellm_timeout_is_treated_as_transient() -> None:
    """``plan()`` wraps everything today, so this cannot currently arise —
    but if a future edit lets one through it is transient by definition, and
    it must not fall into the terminal arm."""
    exc = litellm.Timeout(message="timed out", model="m", llm_provider="anthropic")
    assert await _classify(exc, attempts=1) == "timed_out"


# ── source-level pin, so the dead branch cannot come back ────────────


def _module_tree(mod: object) -> ast.Module:
    return ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]


def test_the_worker_does_not_string_match_the_planners_message() -> None:
    """gotcha 46: a classification is only as good as the worst layer that
    forwards its input. Deciding transience by matching the planner's prose
    would put the verdict and its producer one typo apart, and the typo would
    be silent — every timeout terminal again, exactly as before.

    ⚠ AST, not a substring scan (gotcha 40). The comment explaining this fix
    QUOTES the forbidden phrase, so a grep fires on the sentence justifying
    the guard — which is precisely how a guard gets neutered into blindness.
    A comment is never a ``Constant`` in an executable expression.
    """
    strings = [
        node.value
        for node in ast.walk(_module_tree(worker_mod))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # Docstrings are Constants too, so drop anything long enough to be prose.
    offenders = [s for s in strings if "exhausted transient" in s and len(s) < 200]
    assert not offenders, f"the worker is matching the planner's message: {offenders}"


def test_the_planner_marks_exactly_its_transient_raise_sites() -> None:
    """Both sides (gotcha 28). The flag is useless if no raise site sets it,
    and the default is False — so a missed call site fails CLOSED to the old
    terminal behaviour, silently and with no error anywhere.

    Counting ``transient=True`` as text would count the docstring that
    documents it (measured: 3 occurrences, 2 of them real). Walk the calls.
    """
    marked = [
        node
        for node in ast.walk(_module_tree(action_planner_mod))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PlannerError"
        and any(
            kw.arg == "transient" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
    ]
    assert len(marked) == 2, (
        f"expected the two exhausted-transient-retries raise sites (plan + "
        f"followup) to mark themselves, found {len(marked)}"
    )


def test_the_marking_agrees_with_what_each_raise_says() -> None:
    """Every raise site must be self-consistent: the one that reports
    exhausted TRANSIENT retries is marked, and no other one is.

    ⚠ Deliberately not a count of unmarked sites. A hardcoded number is a
    distinction the next reader has to re-derive, and it fails on any
    unrelated edit — which is how a guard trains people to bump the number
    instead of reading it (gotcha 58's shape: pin the property, not an
    incidental fact). This checks the invariant that actually matters, and it
    stays correct as raise sites come and go.
    """
    mismatched: list[tuple[str, bool]] = []
    for node in ast.walk(_module_tree(action_planner_mod)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PlannerError"
            and node.args
        ):
            continue
        message = ast.unparse(node.args[0])
        marked = any(
            kw.arg == "transient" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        says_transient = "exhausted transient retries" in message
        if marked != says_transient:
            mismatched.append((message, marked))
    assert not mismatched, (
        f"a raise site's marking disagrees with its own message: {mismatched}. "
        "The default is terminal, so an unmarked transient failure is silent — "
        "it just goes back to dying on attempt 1."
    )


def test_litellm_timeout_is_still_caught_directly() -> None:
    """Belt-and-braces. ``plan()`` wraps everything today, but a future edit
    that lets a bare timeout through must not crash the worker — and it is
    transient by definition. The behavioural counterpart is
    ``test_a_bare_litellm_timeout_is_treated_as_transient`` above; this pins
    that the handler still NAMES the type, so a refactor cannot quietly drop
    it and rely on the catch-all (which marks failed, terminal)."""
    caught = {
        ast.unparse(t)
        for node in ast.walk(_module_tree(worker_mod))
        if isinstance(node, ast.ExceptHandler) and node.type is not None
        for t in (node.type.elts if isinstance(node.type, ast.Tuple) else [node.type])
    }
    assert "litellm.Timeout" in caught
    assert issubclass(litellm.Timeout, Exception)


# ── the legacy queue's claim invariants ──────────────────────────────
#
# ⚠ HARDENING, not a fix for an observed failure — and saying which matters.
# The receiver inserts with ``claim_by="receiver"`` so the worker cannot claim
# a row mid-flight (the applicant-5045 double-processing race, live-observed
# 2026-07-05). ``mark_enrichment_failed`` flipped it back to ``'pending'`` in
# the middle of the receiver's own synchronous work — but ``claim_batch``
# requires ``available_at <= now()`` and the helper bumps it a minute out, so
# the worker was blocked for 60s, far past the receiver's 10s ack window. The
# race was closed by the backoff, not by intent. Measured: ``last_error`` is
# non-null on 0 of 561 rows, so this function has never run in production.
#
# What was actually wrong is consistency: both siblings guard on the current
# status and this one did not, while half-releasing the claim.


def _queue_sql(func_name: str) -> str:
    """The SQL literal inside one queue helper, read off the AST.

    Substring-scanning the module would match the docstrings that quote these
    predicates to explain them (gotcha 40) — and this file's own comments do
    exactly that, two paragraphs up.
    """
    for node in ast.walk(_module_tree(queue_mod)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            body = list(ast.walk(node))
            # Skip the docstring: it is the first statement, not an argument.
            doc = ast.get_docstring(node) or ""
            literals = [
                n.value
                for n in body
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and n.value != doc
                and "UPDATE" in n.value
            ]
            assert literals, f"no UPDATE statement found in {func_name}"
            return literals[0]
    raise AssertionError(f"{func_name} not found — the scan is stale, not the code")


def test_marking_enrichment_failed_only_touches_a_row_it_still_holds() -> None:
    """``release_claim`` documents the reason: "so a row the receiver already
    marked done can never be demoted back into the queue." The same hazard
    applies here and the guard was missing."""
    assert "status = 'running'" in _queue_sql("mark_enrichment_failed")


def test_marking_enrichment_failed_releases_the_claim_it_gives_up() -> None:
    """A row left ``status='pending'`` with ``claimed_by='receiver'`` is a
    state no reader expects — the janitor's own comment calls that shape
    anomalous."""
    sql = _queue_sql("mark_enrichment_failed")
    assert "claimed_at   = NULL" in sql or "claimed_at = NULL" in sql
    assert "claimed_by   = NULL" in sql or "claimed_by = NULL" in sql


def test_it_still_records_the_error_and_backs_off() -> None:
    """Both directions. The helper exists to close the silent-failure gap
    (audit finding #5) and to keep the worker off the row for a minute —
    a fix that dropped either would trade one defect for two."""
    sql = _queue_sql("mark_enrichment_failed")
    assert "last_error" in sql
    assert "attempts     = attempts + 1" in sql or "attempts = attempts + 1" in sql
    assert "interval '1 minute'" in sql


def test_the_backoff_is_what_the_claim_query_actually_gates_on() -> None:
    """The reason the race was never live, pinned to the thing that makes it
    so. ``mark_enrichment_failed`` pushes ``available_at`` a minute out, and
    that only protects anything because ``claim_batch`` refuses a row until
    ``available_at <= now()``. If the claim query stopped honouring it, the
    bump would stop meaning anything and this WOULD become the applicant-5045
    double-processing race — so the two are asserted together rather than
    separately (gotcha 28's both-sides rule).
    """
    # ⚠ Anchor on something ONLY the SQL has. The docstring is an
    # ``ast.Constant`` too, so a text marker that also appears in the prose
    # selects the docstring and the assertion then fails against perfectly
    # correct code — gotcha 40, reached from inside an AST scan written to
    # avoid it.
    #
    # ⚠ RE-POINTED 2026-09-10, and re-pointing was the correct move rather than
    # deleting: the anchor was ``"WITH claimed AS"``, and the claim's WHERE
    # clause moved out of that literal into ``fair_claim_cte(where=...)`` when
    # the batch became round-robin by company (see ``webhooks/fair_queue``).
    # The PROPERTY is unchanged and still load-bearing — the backoff bump only
    # protects anything because the claim refuses a row until it is due — so
    # the anchor now names the keyword the predicate actually lives in, which
    # a docstring structurally cannot be.
    claim_sql = next(
        kw.value.value
        for node in ast.walk(_module_tree(queue_mod))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "claim_batch"
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "fair_claim_cte"
        for kw in n.keywords
        if kw.arg == "where" and isinstance(kw.value, ast.Constant)
    )
    assert "available_at <= now()" in claim_sql
    assert "interval '1 minute'" in _queue_sql("mark_enrichment_failed")
