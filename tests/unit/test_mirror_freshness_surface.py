"""Every screen showing mirrored data says how old it is.

Chat has carried an "as of" footer since migration 0057. The PAGES rendering
the same rows carried nothing — so a property manager reading the Operations
grid could not tell a sync from an hour ago from one that died last week, and
gotcha 57 is two engineering sessions misreading a sync window as a missing
feature with far more context than a user has.

⚠ The property that matters most is that the DECISION is shared with the chat
footer. Two copies of "is this stale" end with the grid and the footer beneath
it contradicting each other about the same account on the same screen —
gotcha 78's second copy of the statute, and this repo has shipped that exact
shape twice.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

from helixis.db.repositories.mirror_sync_state import (
    CORE_TABLES,
    STALE_AFTER_HOURS,
    assess,
)

_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _ago(hours: float) -> datetime:
    return _NOW - timedelta(hours=hours)


# ── the shared verdict ───────────────────────────────────────────────


def test_as_of_is_the_newest_and_stalest_is_the_oldest() -> None:
    """⚠ BOTH, because either alone misleads. The newest is the flattering
    number — a lease row synced a minute ago hides day-old bills behind it,
    which is the honesty gap 0057 exists to close. The oldest alone says
    "yesterday" about an account that is current apart from one table."""
    verdict = assess(
        {"leases": _ago(0.5), "bills": _ago(40), "properties": _ago(2)},
        now=_NOW,
    )
    assert verdict.as_of == _ago(0.5)
    assert verdict.stalest_table == "bills"
    assert verdict.stale_hours == 40


def test_a_table_behind_the_threshold_is_stale() -> None:
    assert assess({"bills": _ago(STALE_AFTER_HOURS + 1)}, now=_NOW).is_stale


def test_ordinary_nightly_jitter_is_not_stale() -> None:
    """⚠ 26 rather than 24 on purpose. A nightly sync at 03:00 that slips an
    hour is not a problem, and a warning that fires on ordinary jitter is one
    people learn to scroll past (gotcha 52)."""
    assert not assess({"bills": _ago(25)}, now=_NOW).is_stale
    assert STALE_AFTER_HOURS > 24


def test_a_never_synced_account_reports_no_data_rather_than_a_time() -> None:
    """Unknown must not render as current — gotcha 26's class. `as_of=None` is
    what makes the caption say "not synced" instead of inventing a date."""
    verdict = assess({}, now=_NOW)
    assert verdict.as_of is None
    assert not verdict.has_data
    assert not verdict.is_stale


def test_reference_tables_alone_still_report_something() -> None:
    """⚠ An account mid-first-sync has reference tables and no core ones yet.
    Reporting "never synced" there is a false claim about a sync visibly in
    progress, so non-core rows are the fallback rather than nothing."""
    verdict = assess({"gl_accounts": _ago(1)}, now=_NOW)
    assert verdict.has_data
    # No CORE table has synced, so there is no core staleness to claim.
    assert verdict.stalest_table is None


def test_a_naive_timestamp_does_not_crash_the_verdict() -> None:
    """asyncpg returns tz-aware, but a fixture or an older row may not, and a
    caption that raises takes the page down with it."""
    verdict = assess({"bills": datetime(2026, 9, 1, 12, 0)}, now=_NOW)
    assert verdict.stale_hours is not None and verdict.stale_hours > 0


def test_only_core_tables_drive_the_staleness_warning() -> None:
    """Reference data refreshes on its own cadence; warning about it is noise
    that trains the reader to ignore the line that matters."""
    verdict = assess({"leases": _ago(1), "gl_accounts": _ago(500)}, now=_NOW)
    assert verdict.stalest_table == "leases"
    assert "gl_accounts" not in CORE_TABLES


# ── one decision, two audiences ──────────────────────────────────────


def test_the_chat_footer_reads_the_shared_verdict() -> None:
    """⚠ The footer must not re-derive staleness. It had its own inline
    threshold, `max()` and `stalest_core()` call; the API would have been a
    SECOND implementation of the same rule, and the two would drift into the
    grid and the footer beneath it disagreeing.

    AST rather than a substring (gotcha 40): the docstrings on both sides now
    discuss `assess` in prose, so a text scan proves nothing about the code.
    """
    from helixis.agents import mirror_tools

    tree = ast.parse(pathlib.Path(mirror_tools.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "assess"
    ]
    assert calls, (
        "the chat footer no longer calls sync_state.assess — it is deriving "
        "staleness itself again, and will drift from the page caption"
    )


def test_the_route_reads_the_shared_verdict_too() -> None:
    from helixis.api.routes import mirror as mirror_route

    tree = ast.parse(pathlib.Path(mirror_route.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "assess"
    ]
    assert calls, "the freshness route computes its own verdict"


def test_the_threshold_has_exactly_one_definition() -> None:
    """A second hardcoded 26 is how the two surfaces start disagreeing. Scans
    the product source for a bare threshold comparison outside the module that
    owns it."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"
    owner = root / "db" / "repositories" / "mirror_sync_state.py"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != owner and ">= 26" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, f"a second staleness threshold appeared in: {offenders}"


# ── the page actually renders it ─────────────────────────────────────


def test_operations_page_mounts_the_caption() -> None:
    """⚠ The whole point, and a behavioural test of the component cannot see
    it (gotcha 62): a perfectly correct `DataFreshness` that no page renders
    is the `comms_triage` failure — complete, tested, and wired to nothing."""
    page = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "OperationsPage.tsx"
    ).read_text(encoding="utf-8")
    assert "import { DataFreshness }" in page
    assert "<DataFreshness" in page, (
        "OperationsPage imports the caption and never renders it — every "
        "number on that page is still a cached copy that says nothing"
    )
