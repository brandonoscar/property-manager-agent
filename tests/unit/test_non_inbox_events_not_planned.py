"""Events the Inbox excludes must never be planned or summarized.

The receiver already gates enrichment and routing on `surfaces_in_inbox`, but
the queue row landed at the column default `'pending'` and NEITHER claim query
carries an allowlist — so the worker claimed every non-inbox event anyway.

Live 2026-08-29 (global; pipeline health is cross-tenant): 400 of 565 rows are
non-inbox families, and they are **437 of the 484** `planner_status='failed'`
rows, every one reading "no enrichment handler registered". So 90% of the one
status an operator would check is events that were never eligible, wearing the
same word as the ~47 that are genuinely broken.

These guards read BOTH sides in two places, because this change has two ways to
rot silently:

- the SQL migration hardcodes the allowlist prefixes, which can drift from
  `INBOX_EVENT_PREFIXES` — a new prefix in Python would leave its family
  stamped ineligible by a migration nobody re-runs;
- the terminal value must match NEITHER claim predicate, which is the entire
  reason no claim query needed changing. If someone widens a claim to include
  it, the spend comes straight back with nothing failing.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from helixis.webhooks.inbox_filter import INBOX_EVENT_PREFIXES, surfaces_in_inbox
from helixis.webhooks.queue import NOT_APPLICABLE, enqueue

#: Inbox eligibility is ``$9`` in the enqueue INSERT — 0-indexed, args[8].
_ELIGIBILITY_ARG = 8

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "supabase" / "migrations" / "0080_buildium_events_not_applicable.sql"
_QUEUE = _REPO / "src" / "helixis" / "webhooks" / "queue.py"


def _backfill_statements() -> list[str]:
    """The two UPDATE bodies, comments stripped and whitespace collapsed.

    ⚠ Scoped to the statements, never the file. The header prose QUOTES the
    wrong operator in order to explain the error it caused, so a whole-file
    scan fires on the sentence documenting the guard — gotcha 40's trap,
    arriving in SQL instead of Python. Comments are stripped inside each
    statement too, and whitespace collapsed, so a reflowed `not like\\n all`
    still reads as one token sequence.
    """
    sql = _MIGRATION.read_text(encoding="utf-8")
    # [1:] drops everything BEFORE the first update — that is where the prose
    # lives, including its quotation of the wrong operator.
    bodies = [b.split(";")[0] for b in sql.split("update public.buildium_events")[1:]]
    out = []
    for body in bodies:
        lines = [ln.split("--")[0] for ln in body.splitlines()]
        out.append(re.sub(r"\s+", " ", " ".join(lines)).strip().lower())
    return out


# ── the value itself ─────────────────────────────────────────────────


def test_the_terminal_value_matches_neither_claim_predicate() -> None:
    """The load-bearing property. `not_applicable` is inert because no claim
    selects it — that is why this change needed no claim-query edit. A future
    widening would silently restore the spend, so assert it against the REAL
    predicates in the source rather than against a list written here."""
    sql = _QUEUE.read_text(encoding="utf-8")

    planner_claims = re.findall(r"planner_status IN \(([^)]*)\)", sql)
    summary_claims = re.findall(r"summary_status IN \(([^)]*)\)", sql)
    assert planner_claims, "no planner claim predicate found — did the query move?"
    assert summary_claims, "no summary claim predicate found — did the query move?"

    for clause in planner_claims + summary_claims:
        assert NOT_APPLICABLE not in clause, (
            f"a claim predicate now selects {NOT_APPLICABLE!r} ({clause.strip()}) — "
            "non-inbox events would be planned again, and nothing else would fail"
        )


# ── both sides: the migration's prefixes vs the Python allowlist ─────


def test_the_migration_prefix_list_matches_the_python_allowlist() -> None:
    """The migration decides which historical rows get stamped, using a
    hardcoded copy of the prefixes. If Python gains a prefix and the SQL does
    not, that family is retro-classified as ineligible by a migration nobody
    will re-read. Compare them as SETS, not as text — formatting differs."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    in_sql = set(re.findall(r"'([A-Za-z]+)\.%'", sql))
    in_python = {p.rstrip(".") for p in INBOX_EVENT_PREFIXES}

    assert in_sql == in_python, (
        "migration 0080's prefix list has drifted from INBOX_EVENT_PREFIXES\n"
        f"  only in SQL:    {sorted(in_sql - in_python)}\n"
        f"  only in Python: {sorted(in_python - in_sql)}"
    )


def test_the_migration_never_overwrites_a_succeeded_row() -> None:
    """A row that was actually planned keeps its history even if its family is
    ineligible today — 50 such summaries exist. Losing them would destroy the
    only evidence of what the old behaviour produced."""
    updates = _backfill_statements()
    assert len(updates) == 2, f"expected 2 backfill statements, found {len(updates)}"
    for block in updates:
        assert "'succeeded'" in block, "a backfill statement does not exempt succeeded rows"


def test_the_backfill_excludes_on_all_the_prefixes_not_merely_one() -> None:
    """The operator, not the list — and this is the one that actually shipped
    broken.

    `x NOT LIKE ANY (arr)` is true when x fails to match *at least one*
    pattern. No event name matches all twelve prefixes, so ANY is true for
    EVERY row: the predicate reads like a filter and selects the whole table.
    `NOT LIKE ALL` is the operator that means "matches none of these".

    This was applied to helixis-test as ANY on 2026-08-29 and stamped all 484
    non-succeeded rows, including 83 inbox-eligible ones whose genuine 'failed'
    status and planner_error text were overwritten (repaired via
    `planner_attempts > 0`; the error strings were not recoverable).

    The prefix-list test above passed against that SQL the whole time — it
    compares WHICH prefixes appear and is blind to what is done with them.
    A guard on a hardcoded list must also pin the operator that consumes it.
    """
    for block in _backfill_statements():
        assert "not like all (array[" in block, (
            "a backfill statement does not use `not like all` — with `not like any`, "
            "or a bare `not like`, the predicate matches every row and the backfill "
            "stamps inbox-eligible failures as not_applicable"
        )
        assert "not like any" not in block, (
            "`not like any` is true whenever the value fails ANY one pattern, i.e. "
            "for every row — this is the exact defect that shipped on 2026-08-29"
        )


# ── the enqueue-time decision ────────────────────────────────────────


@pytest.mark.parametrize(
    "event_name",
    ["LeaseTransaction.Created", "OwnershipAccountTransaction.Created", "GLAccount.Updated"],
)
def test_non_inbox_families_are_not_inbox_eligible(event_name: str) -> None:
    """The two families that produced the 437 rows, plus one more, must all
    read ineligible — this is the input `enqueue` stamps on."""
    assert surfaces_in_inbox(event_name) is False


@pytest.mark.parametrize(
    "event_name",
    [
        "Task.Created",
        "WorkOrder.Created",
        "Email.Received",
        "Risk.FlagRaised",
        "Comms.SmsReceived",
        "Helixis.TeamTask",
    ],
)
def test_every_producer_we_enqueue_from_stays_eligible(event_name: str) -> None:
    """The blast radius check. Five non-Buildium producers enqueue through the
    same function; if any of their event names read ineligible, this change
    would silently stop planning real Inbox cards — a far worse outcome than
    the spend it saves."""
    assert surfaces_in_inbox(event_name) is True


@pytest.mark.parametrize(
    ("event_name", "expected"),
    [("Task.Created", True), ("LeaseTransaction.Created", False)],
)
@pytest.mark.asyncio
async def test_enqueue_binds_the_eligibility_the_sql_reads(event_name: str, expected: bool) -> None:
    """The runtime half. The SQL guards above read TEXT — they cannot see what
    value actually reaches `$9`, and a stamp bound to the wrong thing looks
    identical in the diff. So drive the real `enqueue` and read the argument.

    ⚠ Addressed by PLACEHOLDER ($9 → args[8]), not `args[-1]`: "last" is what
    `test_receiver_claim_race` used for the claim arg, and appending this very
    parameter silently redirected both of its assertions onto a bool.
    """
    captured: list[tuple[Any, ...]] = []

    class _Conn:
        async def fetchrow(self, _sql: str, *args: Any) -> dict[str, Any]:
            captured.append(args)
            return {"id": uuid4()}

    @asynccontextmanager
    async def _acquire():
        yield _Conn()

    pool = MagicMock()
    pool.acquire = _acquire

    await enqueue(
        pool,
        event_name=event_name,
        event_time=datetime.now(UTC),
        account_id=652932,
        entity_ids={},
        payload={"EventName": event_name},
        company_id="5ca1ab1e-0000-4000-8000-000000000004",
    )
    assert captured[0][_ELIGIBILITY_ARG] is expected, (
        f"{event_name} bound the wrong eligibility — the CASE on $9 decides both "
        "planner_status and summary_status for the row's whole life"
    )


def test_enqueue_decides_eligibility_once_at_the_insert() -> None:
    """Stamped in `enqueue`, not in each of the five callers. A caller-side
    decision is one a sixth producer would forget, and its failure mode is
    invisible: the row is planned, costs money, renders nowhere."""
    src = _QUEUE.read_text(encoding="utf-8")
    insert = src[src.index("INSERT INTO buildium_events") : src.index("ON CONFLICT (dedup_key)")]
    assert "planner_status" in insert and "summary_status" in insert, (
        "the enqueue INSERT no longer sets both status columns — they would "
        "fall back to the 'pending' default and be claimed again"
    )
    assert "surfaces_in_inbox(event_name)" in src, (
        "enqueue no longer computes inbox eligibility — the stamp cannot be right"
    )
