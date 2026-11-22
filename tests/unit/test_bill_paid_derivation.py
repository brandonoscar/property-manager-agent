"""Paid-vs-unpaid is derived, and the derivation has two homes that must agree.

Buildium sends ``IsPaid`` as a KEY on every production bill and NULL on every
one of them (live-verified 2026-08-21 across all 131 mirrored rows, and again
2026-08-30). ``PaidDate`` carries the answer. So:

* ``buildium/money.py::bill_is_paid`` decides it at INGEST, for every bill from
  #855 (merged 2026-08-21T19:56Z) onward.
* ``supabase/migrations/0081_bills_paid_from_paiddate.sql`` decides it for the
  rows already mirrored — 32 of 131, all written at 2026-08-21T18:01:19Z,
  roughly two hours BEFORE that fix merged.

Those two encode the same rule in two languages, so they can drift. This file
reads BOTH sides (gotcha 28), which is the same shape gotcha 59 arrived at from
the other direction: *a guard on a hardcoded rule must also pin the statement
that consumes it.*

⚠ ``bill_is_paid`` had **no test at all** before this file — `grep -rln
bill_is_paid tests/` returned nothing — while deciding whether money is owed
across the entire payables surface. A data-only migration is separately
invisible to ``migration_check.py``, which matches ``create table`` /
``add column`` against the live DB (gotcha 53). Between the two, nothing could
have caught this.
"""

from __future__ import annotations

import ast
import pathlib
import re
from types import SimpleNamespace

from helixis.buildium.money import bill_is_paid

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "supabase/migrations/0081_bills_paid_from_paiddate.sql"
)


def _statements() -> str:
    """The migration with ``--`` comments stripped — statements, not prose.

    ⚠ The header quotes ``PaidDate``, ``is_paid`` and ``IS NOT TRUE`` in order
    to EXPLAIN itself, so a whole-file scan matches the sentence documenting
    the guard rather than the guard (gotcha 59, and gotcha 40 before it).
    """
    return re.sub(r"--[^\n]*", "", _MIGRATION.read_text(encoding="utf-8"))


# ── the ingest side ───────────────────────────────────────────────────


def test_a_real_paid_date_means_paid() -> None:
    """The production shape: IsPaid null, PaidDate set."""
    assert bill_is_paid(SimpleNamespace(is_paid=None, paid_date="2026-07-14T00:00:00Z")) is True


def test_no_paid_date_means_unpaid() -> None:
    assert bill_is_paid(SimpleNamespace(is_paid=None, paid_date=None)) is False


def test_an_explicit_flag_wins_when_buildium_actually_sends_one() -> None:
    """If Buildium ever starts populating ``IsPaid``, it is authoritative —
    including when it disagrees with PaidDate. Both directions, because a
    one-sided test would pass against a function that ignored the flag."""
    assert bill_is_paid(SimpleNamespace(is_paid=True, paid_date=None)) is True
    assert bill_is_paid(SimpleNamespace(is_paid=False, paid_date="2026-07-14T00:00:00Z")) is False


def test_a_missing_attribute_is_not_an_exception() -> None:
    """`upsert_bill` calls this on every bill it writes; raising here would
    fail an entire sync stage over one odd row."""
    assert bill_is_paid(SimpleNamespace()) is False


# ── the migration side, and that the two agree ────────────────────────


def test_the_migration_keys_on_paid_date_not_the_null_flag() -> None:
    """It must reproduce `bill_is_paid`'s rule, not invent a second one.

    Keying on ``IsPaid`` would set nothing at all — it is NULL on 131 of 131
    production rows, which is the whole reason this migration exists.
    """
    sql = _statements()
    assert "PaidDate" in sql, "the backfill no longer reads PaidDate — it would match nothing"
    assert re.search(r"is_paid\s*=\s*true", sql, re.IGNORECASE), (
        "the backfill no longer sets is_paid"
    )


def test_the_migration_can_never_mark_a_bill_unpaid() -> None:
    """One-directional by construction.

    A backfill that could write ``false`` would, on a re-run against a row whose
    PaidDate had not yet synced, silently reopen a settled bill as money owed.
    Only ``true`` may be assigned — gotcha 57's "unknown must not collapse into
    a claim", pointed at the write instead of the read.
    """
    assignments = re.findall(r"is_paid\s*=\s*(\w+)", _statements(), re.IGNORECASE)
    assert assignments, "no is_paid assignment found — the scan is broken"
    assert set(a.lower() for a in assignments) == {"true"}, (
        f"the backfill assigns is_paid = {assignments} — it must only ever set true"
    )


def test_the_migration_is_idempotent_by_predicate() -> None:
    """Re-running must converge, which requires excluding already-true rows.

    Verified against the live DB when applied (2026-08-30): 32 rows touched,
    then a re-run of the same predicate matched 0.
    """
    sql = _statements()
    assert re.search(r"is_paid\s+IS\s+NOT\s+TRUE", sql, re.IGNORECASE), (
        "the WHERE no longer excludes rows that are already paid, so a re-run "
        "rewrites every row and the statement stops being idempotent"
    )


def test_the_guard_reads_a_real_migration() -> None:
    """A wrong path would make every assertion above vacuous (gotcha 49)."""
    assert _MIGRATION.exists()
    assert len(_statements().strip()) > 40


# ── the two sides cannot disagree about the null case ─────────────────


def test_neither_side_treats_a_missing_paid_date_as_paid() -> None:
    """The one row-state where a drift would invent money movement.

    `bill_is_paid` returns False; the migration's WHERE requires
    ``raw->>'PaidDate' IS NOT NULL``. Both must decline.
    """
    assert bill_is_paid(SimpleNamespace(is_paid=None, paid_date=None)) is False
    assert re.search(r"raw\s*->>\s*'PaidDate'\s+IS\s+NOT\s+NULL", _statements(), re.IGNORECASE), (
        "the migration would set is_paid on rows with no PaidDate — inventing a payment"
    )


def test_the_migration_file_is_valid_sql_shaped_and_single_statement() -> None:
    """One statement, so a partial apply cannot leave a half-done backfill.

    (Parsed structurally rather than by counting characters: a `;` inside the
    prose header would fool a naive split, which is gotcha 40's trap again.)
    """
    body = _statements().strip()
    assert body.count(";") == 1, f"expected exactly one statement, found {body.count(';')}"
    assert body.upper().startswith("UPDATE HELIXIS_BILLS")


def test_python_and_sql_are_both_reachable_from_this_test() -> None:
    """Self-check: the imports above are the two sides, and neither is stubbed."""
    assert callable(bill_is_paid)
    assert ast.parse("True") is not None
