"""One company's flood must not take every slot in every batch.

MEASURED on helixis-test 2026-09-10, deliberately cross-tenant because queue
fairness is a property of the DISPATCHER rather than of one company's books
(gotcha 47): company X enqueued 185 events in nine minutes on
2026-07-25, peaking at 38 in one minute, against ``worker_batch_size = 10`` and
a claim ordered ``received_at ASC`` across every company. A second company's
event arriving mid-burst sat behind ~150 rows belonging to somebody else — and
for the planner and summarizer claims, each of those rows is an LLM call, so the
flood spent the shared model budget first too.

The end-to-end evidence lives in ``webhooks/fair_queue``'s docstring: the same
seed through the same UPDATE gave ``AAAAAAAAAA`` under the old ordering and
``AAAAAAAABC`` under this one.

⚠ **What these tests can and cannot see.** They read SQL TEXT, because the
behaviour under test is an ordering that only Postgres can evaluate and this
repo's unit suite has no database. That is a real limit, so the split is
deliberate: the DATABASE proved the ordering (by effect, rolled back), and these
guards pin that every claim path actually uses the proven shape — which is the
half a behavioural test could never see (gotchas 62/66/69: a well-tested helper
that a call site quietly ignores).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from helixis.webhooks.fair_queue import fair_claim_cte

_QUEUE = Path("src/helixis/webhooks/queue.py")

#: Every function in queue.py that claims a batch of rows for processing.
#: Adding a seventh claim path means adding it here — the corpus test below
#: fails if a function looks like a claim and is not listed.
CLAIM_FUNCTIONS = ("claim_batch", "claim_planner_batch", "claim_summary_batch")


def _fn(name: str) -> ast.AST:
    tree = ast.parse(_QUEUE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from queue.py")


# ── the shape itself ─────────────────────────────────────────────────────


def test_the_batch_is_ordered_by_company_rank_not_by_arrival() -> None:
    """The one line that makes the difference. ``row_number`` per company, then
    that rank first in the ORDER BY — so the batch takes each company's oldest
    row before it takes anyone's second."""
    sql = fair_claim_cte(table="t", where="status = 'pending'", order_column="received_at")
    assert "PARTITION BY company_id ORDER BY received_at" in sql
    assert "ORDER BY _fair_rank, _fair_order" in sql


def test_the_window_function_is_not_in_the_locking_select() -> None:
    """⚠ Postgres REFUSES ``0A000: FOR UPDATE is not allowed with window
    functions`` — probed against the live server, not recalled. The obvious
    one-line fix (append the row_number to the existing claim) cannot run, so
    the two-CTE split is load-bearing and not stylistic. This test is what
    stops somebody folding it back into one SELECT and discovering that on the
    worker, in production."""
    sql = fair_claim_cte(table="t", where="status = 'pending'", order_column="received_at")
    ranked = sql.split("claimed AS")[0]
    assert "row_number()" in ranked
    assert "FOR UPDATE" not in ranked, (
        "the ranking SELECT now carries FOR UPDATE — Postgres rejects that "
        "combination outright (0A000) and every claim will raise at runtime"
    )
    assert "FOR UPDATE OF b SKIP LOCKED" in sql, "the claim no longer locks the rows it claims"


def test_the_limit_placeholder_stays_dollar_one() -> None:
    """Every call site passes ``batch_size`` first and continues its own
    placeholders from ``$2``. Renumbering here silently shifts every caller's
    arguments by one — asyncpg would bind ``max_attempts`` as the batch size."""
    sql = fair_claim_cte(table="t", where="x", order_column="y")
    assert "LIMIT $1" in sql
    assert "$2" not in sql and "$3" not in sql


def test_one_company_is_ordered_exactly_as_before() -> None:
    """⚠ The property that makes this safe to ship, stated where a reader will
    look for it. ``row_number`` is assigned in ``<col>`` order within a
    partition, so with ONE partition ``ORDER BY _fair_rank, _fair_order`` is
    plain ``ORDER BY <col>``. Proven by effect on the database (ids 1..10
    identical under both orderings); asserted here as the INTENT, so a rewrite
    that broke it has to argue with a named test rather than slip through.
    """
    sql = fair_claim_cte(table="t", where="x", order_column="received_at")
    rank_expr = re.search(r"ORDER BY (\w+), (\w+)", sql)
    assert rank_expr is not None
    assert rank_expr.group(2) == "_fair_order", (
        "the tie-break is no longer the ordering column, so a single company's "
        "rows can be reordered against each other — FIFO within a company is "
        "the guarantee this change was not allowed to touch"
    )


# ── it is on the path every claim already takes ──────────────────────────


@pytest.mark.parametrize("name", CLAIM_FUNCTIONS)
def test_every_claim_path_uses_the_fair_ordering(name: str) -> None:
    """⚠ Reads the CALL SITE, which is the half a behavioural test cannot see.
    A claim that quietly reverted to global FIFO would still drain the queue,
    still pass every existing queue test, and simply starve whichever company
    was not flooding — silent by construction, which is how this shipped."""
    calls = [
        n
        for n in ast.walk(_fn(name))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "fair_claim_cte"
    ]
    assert calls, f"{name} builds its claim SQL without fair_claim_cte"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert {"table", "where", "order_column"} <= kw, (
            f"{name} calls fair_claim_cte without all three arguments — a "
            "missing order_column would silently rank by the wrong column"
        )


def test_no_claim_orders_a_limited_batch_by_a_bare_timestamp() -> None:
    """The CORPUS check (gotcha 45 — the failure mode here is 'silently permits
    more'). A seventh claim path written the old way is invisible to the
    per-function tests above, because they only look at the three functions that
    exist today.

    ⚠ Scoped to STATEMENTS, never the whole file: this module's docstrings and
    comments now QUOTE ``ORDER BY received_at ASC`` in order to explain why it
    was wrong, and a file-wide scan fires on the sentence documenting the guard
    (gotchas 40/59, arriving in SQL).
    """
    src = _QUEUE.read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(src)):
        # A SQL literal is a Constant, or the fragments of an f-string
        # (JoinedStr) — gotcha 63: scanning Constant alone misses the f-strings,
        # which is what every templated claim in this file is.
        texts: list[str] = []
        if isinstance(node, ast.JoinedStr):
            texts = [v.value for v in node.values if isinstance(v, ast.Constant)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts = [node.value]
        for text in texts:
            if "LIMIT $1" not in text or "FOR UPDATE" not in text:
                continue
            if "_fair_rank" in text:
                continue
            offenders.append(text.strip()[:120])
    assert not offenders, (
        "a claim SELECT limits and locks a batch without ranking by company — "
        "one company's burst will take every slot in every batch:\n" + "\n".join(offenders)
    )


def test_the_claim_function_list_is_not_stale() -> None:
    """⚠ An allowlist needs a staleness test (gotcha 63, which caught its own
    author). If a claim function is renamed or removed, the parametrized test
    above silently covers nothing while still reporting green."""
    src = _QUEUE.read_text(encoding="utf-8")
    named = {
        n.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name.startswith("claim_")
    }
    assert named == set(CLAIM_FUNCTIONS), (
        f"queue.py's claim functions are {sorted(named)} but this file guards "
        f"{sorted(CLAIM_FUNCTIONS)} — re-point the list, never shrink it"
    )
