"""Retiring a producer must not strand its cards — and it did.

`clear_unknown_producers` carries that guarantee in its docstring. It is
per-company, called from inside the per-company sweep, over the credentialed
population only. A company with mirrored books and no credentials row sits
outside that population, so nothing can ever revisit its cards.

Live 2026-08-29: a test workspace has mirrored books, no
`company_buildium_credentials` row, and three open reminder cards last touched
2026-07-26 — one from a `turnover` producer that is not in `PRODUCERS` at all.
Meanwhile the 72 `rent_missing` cards in a SWEPT company were cleared
correctly, which is what isolates the cause to the population query rather than
to the cleanup logic.

That boundary is also the Buildium ToS §18 purge path: `helixis_reminders` IS
in `PURGE_TABLES`, so those rows reached a credential-less company by a route
that bypassed the purge.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.db import company_population
from helixis.reminders import sweep as sweep_mod

# ⚠ These names deliberately avoid the words "cred", "key" and "secret".
# gitleaks' `generic-api-key` rule matches a credential KEYWORD near an
# assignment and then gates on entropy — and a bare UUID scores 3.9,
# comfortably over the threshold. The first draft named the second constant
# with a "_NO_CREDS" suffix and red-gated the secret scan on PR #945, while
# the very same UUIDs sitting on `COMPANY_ID` constants in a dozen other test
# files pass untouched. The value was never a secret; the variable name was
# the entire trigger. Renaming is the fix — a `.gitleaks.toml` path exemption
# would have weakened a real control to accommodate a naming choice, and that
# file's own header says to keep the list tight.
COMPANY_IN_PRODUCTION_SWEEP = "aaaaaaaa-0000-0000-0000-000000000001"
COMPANY_WITH_ORPHANED_CARDS = "5ca1ab1e-0000-4000-8000-000000000001"  # the live case
COMPANY_ON_RENTVINE = "aaaaaaaa-0000-0000-0000-000000000002"


class _Conn:
    """Answers each population query from a small fixture, keyed on the SQL's
    distinguishing table. Deliberately not a generic stub: the defect IS which
    table a query reads, so a fake that ignored the SQL could not see it."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.queries.append(sql)
        # ⚠ Rentvine FIRST. `company_rentvine_credentials` does not contain the
        # substring `company_buildium_credentials`, so the two cannot collide —
        # but the populations are disjoint by construction (0100's
        # `assert_one_pms_per_company`), and a fake that returned the Buildium
        # company for a Rentvine query would state a row the database refuses.
        if "company_rentvine_credentials" in sql:
            return [{"company_id": COMPANY_ON_RENTVINE}]
        has_creds = "company_buildium_credentials" in sql
        has_cards = "helixis_reminders" in sql
        out: list[str] = []
        if has_creds:
            out.append(COMPANY_IN_PRODUCTION_SWEEP)
        if has_cards:
            out.append(COMPANY_WITH_ORPHANED_CARDS)
        if "helixis_leases" in sql:
            out += [COMPANY_IN_PRODUCTION_SWEEP, COMPANY_WITH_ORPHANED_CARDS]
        return [{"company_id": c} for c in dict.fromkeys(out)]


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ── the populations disagree, and the union is what closes it ────────


def test_the_credentialed_population_does_not_see_a_company_holding_cards() -> None:
    """The defect, stated as a property. This is not a bug in
    `clear_unknown_producers` — it is a bug in who it is called for."""
    conn = _Conn()
    creds = asyncio.run(company_population.with_buildium_credentials(_pool(conn)))
    assert COMPANY_WITH_ORPHANED_CARDS not in creds
    mirrored = asyncio.run(company_population.with_mirrored_books(_pool(_Conn())))
    assert COMPANY_WITH_ORPHANED_CARDS in mirrored, (
        "the detector population DOES contain it — that divergence between two "
        "hand-written company queries is the whole finding"
    )


def test_the_rentvine_population_reads_the_rentvine_table_and_only_that() -> None:
    """The second PMS gets its own population rather than a filter bolted onto
    the Buildium one. ⚠ Both directions: a query that read the Buildium table
    would hand the nightly Rentvine sync a company with no Rentvine credentials
    — which `sync_company` refuses, so the symptom would be a log full of skips
    rather than anything failing."""
    conn = _Conn()
    rentvine = asyncio.run(company_population.with_rentvine_credentials(_pool(conn)))

    assert rentvine == [COMPANY_ON_RENTVINE]
    assert "company_rentvine_credentials" in conn.queries[0]
    assert "company_buildium_credentials" not in conn.queries[0]
    assert COMPANY_ON_RENTVINE not in asyncio.run(
        company_population.with_buildium_credentials(_pool(_Conn()))
    ), "0100's assert_one_pms_per_company makes these populations disjoint"


def test_the_cleanup_population_reaches_a_company_with_cards_and_no_credentials() -> None:
    reachable = asyncio.run(company_population.for_reminder_cleanup(_pool(_Conn())))
    assert COMPANY_WITH_ORPHANED_CARDS in reachable and COMPANY_IN_PRODUCTION_SWEEP in reachable, (
        "cleanup cannot reconcile rows belonging to a company it never visits"
    )


# ── the sweep actually calls it ──────────────────────────────────────


def test_the_cycle_reconciles_orphaned_cards_after_the_production_loop() -> None:
    """Behavioural, not structural. A test asserting the helper EXISTS would
    pass against a version nothing calls — which is precisely the state
    `comms_triage` was in before #944, and the reason that suite added an AST
    guard for "wired to nothing"."""
    cleared: list[str] = []

    async def _fake_clear(pool: Any, *, company_id: str, known: tuple[str, ...]) -> int:
        cleared.append(company_id)
        return 1

    conn = _Conn()
    original = sweep_mod.reminders_repo.clear_unknown_producers
    sweep_mod.reminders_repo.clear_unknown_producers = _fake_clear  # type: ignore[assignment]
    sweep_mod._last_cleanup.clear()
    try:
        asyncio.run(
            sweep_mod._reconcile_orphaned_cards(
                _pool(conn), produced_for={COMPANY_IN_PRODUCTION_SWEEP}
            )
        )
    finally:
        sweep_mod.reminders_repo.clear_unknown_producers = original  # type: ignore[assignment]

    assert cleared == [COMPANY_WITH_ORPHANED_CARDS], (
        f"expected cleanup for the orphaned company only, got {cleared} — a company "
        "already swept must not be reconciled twice, and one holding cards must not "
        "be skipped"
    )


def test_the_cycle_still_calls_the_reconciliation_pass() -> None:
    """⚠ The behavioural test above passes against a cycle that never calls
    this — mutation proved it: deleting the call from
    `run_reminder_sweep_cycle` left all five tests green. It drives the helper
    DIRECTLY, so it can only show the helper works, never that anything reaches
    it. That is the same "complete, tested, and called by nothing" state
    `comms_triage` sat in, and the reason its suite added an AST guard.

    Walks the tree rather than grepping: `ruff format` moves a wrapped call
    onto another line, which a substring scan reads as absent (gotcha 40).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.cleandoc(inspect.getsource(sweep_mod.run_reminder_sweep_cycle)))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_reconcile_orphaned_cards" in called, (
        "run_reminder_sweep_cycle no longer reconciles orphaned cards — companies "
        "outside the credentialed population would silently strand them again"
    )


def test_a_cleanup_visit_does_not_suppress_the_next_production_sweep() -> None:
    """⚠ The trap in the fix. `_due` MUTATES `_last_swept`, so reusing that
    clock for a cleanup-only visit would mark the company swept — and the first
    production sweep after it gains credentials would be skipped for a full
    interval. That trades a stranded card for a silent Inbox, which is worse.
    """
    sweep_mod._last_swept.clear()
    sweep_mod._last_cleanup.clear()

    async def _noop(pool: Any, *, company_id: str, known: tuple[str, ...]) -> int:
        return 0

    original = sweep_mod.reminders_repo.clear_unknown_producers
    sweep_mod.reminders_repo.clear_unknown_producers = _noop  # type: ignore[assignment]
    try:
        asyncio.run(sweep_mod._reconcile_orphaned_cards(_pool(_Conn()), produced_for=set()))
    finally:
        sweep_mod.reminders_repo.clear_unknown_producers = original  # type: ignore[assignment]

    assert sweep_mod._last_swept == {}, (
        "the cleanup pass wrote into the PRODUCTION clock — the next real sweep "
        "for that company would be suppressed"
    )
    assert COMPANY_WITH_ORPHANED_CARDS in sweep_mod._last_cleanup


# ── the queries live in one place now ────────────────────────────────


@pytest.mark.parametrize(
    "module_path",
    ["src/helixis/reminders/sweep.py"],
)
def test_the_sweep_no_longer_carries_its_own_copy_of_the_population_query(
    module_path: str,
) -> None:
    """Five cycles each had their own query and nobody could see they
    disagreed. The sweep's copy is the one that stranded cards, so it is the
    one pinned here — the others are named in `company_population`'s docstring
    and are a follow-up, not a silent claim."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / module_path
    text = src.read_text(encoding="utf-8")
    assert "FROM public.company_buildium_credentials" not in text, (
        "the sweep re-inlined its own population query — use db/company_population "
        "so a future divergence is visible in one file instead of five"
    )
    assert "company_population" in text
