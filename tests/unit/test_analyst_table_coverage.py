"""The analyst could read everything Buildium sends and nothing Helixis knows.

Its allowlist held the 16 mirror tables and stopped there, so it could count
overdue tasks and average vendor spend but could not see a single risk flag,
reminder, renewal offer or turnover — every conclusion the product had already
reached about the portfolio was invisible to the thing whose job is reaching
conclusions about the portfolio.

⚠ **It could not read its own output.** The nightly analyst writes findings
into ``helixis_risk_flags`` with ``source='analyst'`` — 103 of the 1,033 rows
on file, measured 2026-08-29 — and had no way to read them back. "Did I
already flag this?" was unanswerable, so every run started blind. Same
write-without-read asymmetry as the Buildium tool surface, pointed at the
analyst's own table.

⚠ **The real artifact is the CENSUS, not the ten names.** A hand-maintained
allowlist goes stale the moment someone adds a table, and an unclassified
table is indistinguishable from a deliberately-excluded one — which is exactly
how ten operational tables sat unreachable without anyone deciding they should
be. So every company-scoped table a migration creates must appear in
``ALLOWED_TABLES`` or in ``EXCLUDED_TABLES`` with a reason, and a new table
fails here on the day it lands. Same mechanism as the ToS §18 purge
completeness test, which this deliberately mirrors.
"""

from __future__ import annotations

import pathlib
import re

from helixis.analysis.sql_guard import ALLOWED_TABLES, EXCLUDED_TABLES, guard_sql

REPO = pathlib.Path(__file__).resolve().parents[2]

_CREATE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_COMPANY_COL_RE = re.compile(r"company_id", re.IGNORECASE)


def _created_company_scoped_tables() -> set[str]:
    """Every ``helixis_*`` table the SQL sources create with a company_id.

    Deliberately the same crude parse the §18 purge census uses — two
    different parsers over the same migrations would eventually disagree, and
    the disagreement would be silent.
    """
    out: set[str] = set()
    sql_files = list((REPO / "supabase" / "migrations").glob("*.sql")) + list(
        (REPO / "src" / "helixis" / "db" / "schema").glob("*.sql")
    )
    for path in sql_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _CREATE_RE.finditer(text):
            name = match.group(1).lower()
            if not name.startswith("helixis_"):
                continue
            body = text[match.end() : match.end() + 4000].split(";", 1)[0]
            if _COMPANY_COL_RE.search(body):
                out.add(name)
    return out


# ── the census ───────────────────────────────────────────────────────


def test_the_scan_actually_finds_tables() -> None:
    """A walk that finds nothing reports exactly like a fully-classified
    schema (gotcha 49). The floor is far below the real count and only
    catches a broken glob or a changed migration style."""
    found = _created_company_scoped_tables()
    assert len(found) >= 20, f"migration scan looks broken — only found {sorted(found)}"


def test_every_company_scoped_table_is_classified() -> None:
    """THE census. A table nobody classified and a table someone deliberately
    withheld are the same absence from the outside — this is what makes them
    different."""
    unclassified = sorted(
        _created_company_scoped_tables() - set(ALLOWED_TABLES) - set(EXCLUDED_TABLES)
    )
    assert not unclassified, (
        "company-scoped table(s) the analyst can neither read nor has been "
        f"deliberately denied: {unclassified}. Add each to ALLOWED_TABLES, or "
        "to EXCLUDED_TABLES with the reason it is withheld."
    )


def test_nothing_is_classified_both_ways() -> None:
    """Allowed and excluded must partition. A table in both is a review that
    disagreed with itself, and the allowlist would silently win."""
    assert not (set(ALLOWED_TABLES) & set(EXCLUDED_TABLES))


def test_every_exclusion_carries_a_real_reason() -> None:
    """An empty or one-word reason is an absence wearing a decision's costume
    — the next reader cannot tell whether it was thought about."""
    for table, reason in EXCLUDED_TABLES.items():
        assert len(reason.split()) >= 5, f"{table} is excluded with no usable reason"


def test_the_exclusion_list_has_no_phantom_tables() -> None:
    """Both directions (gotcha 38). An exclusion naming a table that no longer
    exists is a justification nobody is applying, and it makes the list look
    more considered than it is."""
    created = _created_company_scoped_tables()
    phantom = sorted(t for t in EXCLUDED_TABLES if t not in created)
    assert not phantom, f"excluded but not created by any migration: {phantom}"


# ── the allowlist is structurally safe, not safe-by-attention ────────


def test_every_allowlisted_table_is_company_scoped() -> None:
    """⚠ THE tenant boundary. ``executor.py`` rewrites each referenced table
    into ``SELECT * FROM public.<t> WHERE company_id = $1::uuid`` — that
    rewrite IS the isolation, so a table without the column cannot be scoped
    at all.

    ``helixis_memories`` is the live example of why this is a test and not a
    convention: it is a real product table with no ``company_id``, and
    allowlisting it would produce a CTE that cannot compile — or, on a schema
    where it did compile, one that read every tenant's rows at once.
    """
    scoped = _created_company_scoped_tables()
    unscoped = sorted(t for t in ALLOWED_TABLES if t not in scoped)
    assert not unscoped, (
        f"allowlisted table(s) with no company_id column: {unscoped}. The "
        "executor's per-table CTE is the tenant boundary and cannot scope them."
    )


def test_every_allowlisted_table_actually_passes_the_guard() -> None:
    """⚠ Reads BOTH sides (gotcha 28). The guard has an allowlist AND an
    independent denylist of sensitive identifiers, and they are separate
    lists: a table can be allowlisted and still rejected outright by a
    denylist substring, which reads to the model as "that table does not
    exist" rather than as a bug. Adding a table named, say,
    ``helixis_chat_summaries`` would do exactly that."""
    rejected = [
        (t, guard_sql(f"SELECT count(*) FROM {t}").reason)
        for t in sorted(ALLOWED_TABLES)
        if not guard_sql(f"SELECT count(*) FROM {t}").allowed
    ]
    assert not rejected, f"allowlisted but refused by the guard's denylist: {rejected}"


def test_every_excluded_table_is_actually_refused() -> None:
    """The other direction, and the one that matters for safety: writing an
    exclusion down must not be mistaken for enforcing it."""
    accepted = [t for t in sorted(EXCLUDED_TABLES) if guard_sql(f"SELECT * FROM {t}").allowed]
    assert not accepted, f"listed as excluded but the guard allows it: {accepted}"


# ── the notes must describe the surface that exists ──────────────────


def test_the_schema_notes_do_not_deny_the_history_that_now_exists() -> None:
    """⚠ Widening the allowlist without correcting the notes ships a
    contradiction, and the notes win — they are what the model reads.

    The old note said the read surface holds "no prior versions and no change
    log, so 'what changed since <date>' has no answer here AT ALL". That was
    true of the Buildium mirror and became false the moment
    ``helixis_work_order_status_history`` — a literal change log — joined the
    allowlist. A false caveat is worse than a missing one: it steers the model
    away from a real answer AND teaches it to discount the true caveats
    sitting beside it (gotcha 57's rule about computed-not-hardcoded caveats,
    applied to prose).

    Gotcha 25's rule again — fix a change at every layer that reads it.
    """
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    notes = " ".join(_SCHEMA_NOTES)
    assert "no answer here at all" not in notes.lower(), (
        "the notes still deny history outright while a change log is allowlisted"
    )
    assert "helixis_work_order_status_history" in notes, (
        "the change log is readable and the notes never mention it — the model "
        "will not go looking for a table it has been told cannot exist"
    )


def test_the_notes_warn_against_citing_the_analyst_to_itself() -> None:
    """``helixis_risk_flags`` mixes deterministic detector output with the
    nightly LLM pass's own findings. An analyst that reads a ``source =
    'analyst'`` row as independent corroboration is citing itself — a
    confident conclusion built on a previous run's guess.

    ⚠ Asserts that ONE note carries the whole warning, not that the words
    appear somewhere across all of them. The joined-string version passed
    against a mutant that gutted the note's opening line, because "source" and
    "analyst" both survive elsewhere in the list — a scan loose enough to be
    satisfied by unrelated prose is satisfied by the absence it is checking
    for.
    """
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    matching = [
        n
        for n in _SCHEMA_NOTES
        if "helixis_risk_flags" in n and "analyst" in n.lower() and "corroboration" in n.lower()
    ]
    assert matching, (
        "no single note warns that a source='analyst' flag may be this "
        "analyst's own earlier conclusion rather than independent evidence"
    )


# ── the specific gap this closed ─────────────────────────────────────


def test_the_analyst_can_read_its_own_findings() -> None:
    """It writes ``source='analyst'`` rows into ``helixis_risk_flags`` and
    could not read one of them, so it could not tell a fresh problem from one
    it flagged last night."""
    assert "helixis_risk_flags" in ALLOWED_TABLES
    assert guard_sql(
        "SELECT rule_key, count(*) FROM helixis_risk_flags "
        "WHERE source = 'analyst' GROUP BY rule_key"
    ).allowed


def test_the_operational_tables_are_reachable() -> None:
    """The product's own conclusions — what it is nudging about, which
    renewals are out, which units are turning over — were all invisible."""
    for table in (
        "helixis_reminders",
        "helixis_renewal_offers",
        "helixis_turnovers",
        "helixis_payment_promises",
        "helixis_work_order_status_history",
    ):
        assert table in ALLOWED_TABLES, f"{table} is still unreachable"


def test_the_sensitive_surfaces_stay_shut() -> None:
    """Widening an allowlist is exactly where a leak gets added by accident,
    so the four that must never open are asserted by name rather than left to
    the completeness test's bookkeeping."""
    for table in (
        "helixis_chat_messages",
        "helixis_sms_consent",
        "helixis_sms_opt_outs",
        "helixis_sms_log",
        "helixis_action_idempotency",
    ):
        assert table not in ALLOWED_TABLES
        assert not guard_sql(f"SELECT * FROM {table}").allowed


# ── the hidden-column set (ADR 0010 Decision (a), migration 0101) ─────────────


def test_the_discriminator_is_withheld_and_nothing_else_is() -> None:
    """A Buildium customer's analyst must not gain a column nobody added for
    them — and a filter that hides too much is the worse bug, because a missing
    column is invisible to the reader while an extra one is merely noise."""
    from helixis.analysis.sql_guard import hidden_columns, visible_columns

    schema = ["id", "company_id", "buildium_id", "pms", "total_amount", "ledger_type"]
    assert visible_columns(schema, "buildium") == (
        "id",
        "company_id",
        "buildium_id",
        "total_amount",
    )
    # Rentvine keeps ledger_type: Decision (b) — without it the money totals
    # are WRONG, which is a worse outcome than seeing a column it cannot use.
    assert visible_columns(schema, "rentvine") == (
        "id",
        "company_id",
        "buildium_id",
        "total_amount",
        "ledger_type",
    )
    assert "pms" in hidden_columns("buildium")
    assert "pms" in hidden_columns("rentvine")


def test_an_unreadable_pms_gets_the_buildium_set_not_an_empty_one() -> None:
    """The fallback direction is a choice. Empty would leak the discriminator on
    exactly the deployments that could not answer the question."""
    from helixis.analysis.sql_guard import hidden_columns

    for unknown in (None, "", "   ", "yardi", "BUILDIUM"):
        assert "pms" in hidden_columns(unknown), unknown


def test_the_hidden_set_is_a_named_list_never_a_pattern() -> None:
    """ "Hide what I do not recognise" would silently blind the analyst to every
    column added from now on. Pinned so the shortcut cannot arrive quietly."""
    from helixis.analysis.sql_guard import visible_columns

    for ordinary in ("pms_notes", "ledger", "some_column_added_later", "pmsx"):
        assert ordinary in visible_columns([ordinary], "buildium"), ordinary


def test_every_column_migration_0101_adds_to_a_readable_table_is_withheld() -> None:
    """Reads the MIGRATION, not a list in a test (gotcha 45).

    The failure mode here is "silently permits more": a second discriminator
    column added to the same tables — ADR 0010 Decision (b)'s `ledger_id` /
    `ledger_type` are the ones already planned — would reach a Buildium
    customer's analyst with nothing failing. This is what notices.
    """
    import re
    from pathlib import Path

    from helixis.analysis.sql_guard import hidden_columns

    sql = (
        Path(__file__).resolve().parents[2]
        / "supabase/migrations/0101_mirror_pms_discriminator.sql"
    ).read_text()

    added = set(re.findall(r"add column if not exists (\w+)", sql))
    assert added, "the scan found no added columns — check the pattern, not the migration"

    touched = set(re.findall(r"'(helixis_\w+|buildium_staff)'", sql)) & ALLOWED_TABLES
    assert len(touched) >= 20, f"expected ~20 analyst-readable tables, found {len(touched)}"

    withheld = hidden_columns("buildium")
    assert added <= withheld, f"migration 0101 adds {sorted(added - withheld)} with no filter"
