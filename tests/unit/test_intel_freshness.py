"""A property's flood zone was fetched once and described as live Buildium data.

`helixis_property_intel` caches FEMA / USFS / EPA / HUD answers against a
property's coordinates. Two defects, one shape — the derived artifact outliving
the thing it describes, which is the same family as the stale risk cards
(coverage #244) and the reminder that could not age (gotcha 88).

**It was never refreshed.** `_PENDING_SQL`'s work-list sentinel was "this
property has no flood row", so a property left the list the first time it was
enriched and never returned. `fetched_at` was written by the upsert and read by
nothing. MEASURED on helixis-test 2026-09-10, deliberately cross-tenant because
a refresh cadence is a property of the product rather than of one company's
books: **109 rows across two sources, every one stamped 2026-08-17..08-21**,
nothing re-fetched in the twenty days since, and by construction nothing ever
would be.

**And the footer lied about it twice.** The read appended
`mirror_tools._as_of_line`, which produced, verbatim, on a FEMA flood zone:

    (from Occupella's synced Buildium data, updated 552h ago)

Buildium has never seen this data, and `_as_of_line` renders everything past
two hours in hours — so a year-old row reads "9600h ago", which is true and is
also a way of not saying "over a year".
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helixis.agents.mirror_tools import _INTEL_SOURCES, _intel_as_of_line
from helixis.geo.intel_freshness import (
    DEFAULT_REFRESH_AFTER,
    RECRAWL_AFTER,
    REFRESH_AFTER,
    describe_age,
    is_stale,
    refresh_after,
    stale_sources,
)

_BACKFILL = Path("src/helixis/geo/intel_backfill.py")
_MIRROR = Path("src/helixis/agents/mirror_tools.py")

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# ── is this row past its window ──────────────────────────────────────────


def test_a_row_inside_its_window_is_fresh() -> None:
    """The twin. Without it every assertion here passes against a function
    that calls everything stale and re-crawls the whole account daily."""
    assert not is_stale("flood", _ago(30), now=NOW)


def test_a_row_past_its_window_is_stale() -> None:
    assert is_stale("flood", _ago(400), now=NOW)


def test_the_windows_are_per_source_not_one_number() -> None:
    """⚠ The sources age on very different schedules and a single TTL is wrong
    in both directions. HUD republishes Fair Market Rent every October — a
    stale FMR is a wrong number about money, on a known date — while the EPA
    radon map is a static 1993 product bundled into this repo, so a fetch
    cannot learn anything new about it."""
    old = _ago(200)
    assert is_stale("fmr", old, now=NOW), "the sharpest source is being trusted too long"
    assert not is_stale("radon", old, now=NOW), "a bundled static table is being re-fetched"


def test_an_undated_row_is_stale() -> None:
    """⚠ Deliberately the opposite of `company_clock`'s fail-open. There,
    refusing to answer costs a company its whole Inbox cycle; here being wrong
    costs one HTTP call against public data, while trusting an undateable row
    means a hazard answer presented as current that nothing can vouch for."""
    assert is_stale("flood", None, now=NOW)


def test_an_unknown_source_gets_the_shortest_window_not_the_longest() -> None:
    """A source nobody classified is a source nobody reasoned about. Falling
    back to `radon`'s three years would silently grant the longest trust to the
    one entry we know least about."""
    assert refresh_after("some_source_added_later") == DEFAULT_REFRESH_AFTER
    assert DEFAULT_REFRESH_AFTER == min(REFRESH_AFTER.values())


def test_stale_sources_names_only_the_overdue_ones_in_a_stable_order() -> None:
    got = stale_sources({"flood": _ago(400), "radon": _ago(400), "fmr": _ago(10)}, now=NOW)
    assert got == ["flood"], (
        "radon's window is three years and fmr was checked last week — naming "
        "either would train the reader to discount the warning"
    )


def test_two_overdue_sources_come_back_in_a_stable_order() -> None:
    """⚠ Found by MUTATION, and only on the SECOND battery (gotcha 90). The
    test above passes reverse-order input and still yields ONE stale source, so
    sorted, unsorted and truncated outputs are byte-identical — the mutation
    applied, compiled, ran, and could not change any result. The sort is only
    observable with two.

    It matters because the SQL feeding this has no ORDER BY, so dict insertion
    order is row order and can differ between two reads of unchanged data. A
    footer that reshuffles itself reads to a model as new information.
    """
    got = stale_sources({"superfund": _ago(400), "flood": _ago(400)}, now=NOW)
    assert got == ["flood", "superfund"], f"unstable order: {got}"


def test_the_recrawl_cadence_is_derived_from_the_windows() -> None:
    """⚠ One pass re-queries EVERY source for a property, so the crawl has to
    run at the shortest window or the sharpest source ages past its own while a
    longer-lived one holds the property off the work-list. Deriving it means
    adding a shorter-lived source tightens the crawl automatically — the
    `maintenance_status.OPEN_STATUSES` precedent."""
    assert RECRAWL_AFTER == min(REFRESH_AFTER.values())
    assert all(RECRAWL_AFTER <= w for w in REFRESH_AFTER.values())


def test_every_source_the_product_surfaces_has_a_window() -> None:
    """⚠ Reads BOTH sides (gotcha 28). `_INTEL_SOURCES` is what the hazard tool
    renders; a source present there and absent here silently takes the default
    and nobody ever decides how long it should be trusted."""
    missing = [s for s, _, _ in _INTEL_SOURCES if s not in REFRESH_AFTER]
    assert not missing, f"no refresh window declared for {missing}"


# ── saying the age in a unit somebody can act on ─────────────────────────


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "today"), (1, "yesterday"), (23, "23 days ago")],
)
def test_recent_ages_read_plainly(days: int, expected: str) -> None:
    assert describe_age(_ago(days), now=NOW) == expected


def test_a_year_old_row_says_years_not_hours() -> None:
    """⚠ THE MEASURED DEFECT. `_as_of_line` rendered the live table's 23-day-old
    rows as "552h ago" and would render this one as "9600h ago". Nobody reads
    that as "over a year"."""
    assert describe_age(_ago(400), now=NOW) == "about 13 months ago"
    got = describe_age(_ago(800), now=NOW)
    assert "h ago" not in got, f"still rendering an age in hours: {got!r}"
    assert got == "about 2 years ago"


def test_an_undated_row_does_not_get_an_invented_age() -> None:
    assert describe_age(None, now=NOW) == "at an unrecorded time"


# ── the footer the model actually reads ──────────────────────────────────


def test_the_footer_does_not_call_government_data_buildium_data() -> None:
    """⚠ The exact false claim measured on 2026-09-10: a FEMA flood zone
    footed with "(from Occupella's synced Buildium data …)". Buildium has never
    seen this table."""
    line = _intel_as_of_line({"flood": _ago(23)}, now=NOW)
    assert "Buildium" not in line, f"still attributing FEMA/EPA/HUD data to Buildium: {line!r}"
    assert "FEMA" in line and "HUD" in line


def test_a_fresh_footer_carries_no_warning() -> None:
    """The twin — without it the caveat could fire on everything, and a caveat
    that always fires is one readers learn to skip (gotcha 52's shape)."""
    line = _intel_as_of_line({"flood": _ago(23), "radon": _ago(23)}, now=NOW)
    assert "⚠" not in line
    assert "23 days ago" in line


def test_an_overdue_footer_names_which_sources() -> None:
    """ "Some of this is old" is not actionable when the sources age on
    schedules three years apart."""
    line = _intel_as_of_line({"flood": _ago(400), "radon": _ago(400)}, now=NOW)
    assert "Past its refresh window: flood" in line
    assert "radon" not in line.split("Past its refresh window:")[1]


def test_an_overdue_footer_still_reports_the_age_rather_than_withholding() -> None:
    """⚠ Stale data is still the best answer available. A flood zone from
    fourteen months ago beats no flood zone, and suppressing it would be a
    fabricated absence — this product's worst failure mode (gotchas 26/55/57).
    The reader gets the finding AND its age."""
    line = _intel_as_of_line({"flood": _ago(400)}, now=NOW)
    assert "about 13 months ago" in line
    assert "confirm against the source" in line


# ── the work list actually asks for stale rows ───────────────────────────


def _backfill_src() -> str:
    return _BACKFILL.read_text(encoding="utf-8")


def _pending_sql() -> str:
    for node in ast.walk(ast.parse(_backfill_src())):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "_PENDING_SQL" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError("_PENDING_SQL is gone from intel_backfill")


def test_the_work_list_includes_rows_that_have_aged_out() -> None:
    """⚠ Scoped to the STATEMENT, never the file — the comments above it now
    quote the old sentinel in order to explain it, and a whole-file scan would
    pass on the sentence describing the bug (gotchas 40/59)."""
    sql = _pending_sql()
    assert "fetched_at" in sql, (
        "the work list no longer looks at how old the intel is, so a property "
        "leaves it the first time it is enriched and never comes back — the "
        "measured defect: 109 rows, none re-fetched in twenty days"
    )
    assert "i.buildium_property_id IS NULL" in sql, (
        "the never-crawled arm is gone, so a brand-new property is never enriched at all"
    )


def test_never_crawled_properties_sort_ahead_of_refreshes() -> None:
    """⚠ Refreshes and first-time enrichments share ONE work-list under a
    LIMIT. Without the leading sort key a company with many ageing properties
    spends its whole budget re-crawling rows that already have an answer while
    a newly added property waits behind them — a staleness fix that starves the
    case it was built on."""
    sql = _pending_sql()
    order = sql.split("ORDER BY")[1]
    assert "(i.buildium_property_id IS NOT NULL)" in order.split(",")[0], (
        "never-crawled properties no longer sort first, so refreshes can crowd "
        "out the first enrichment of a new property"
    )


def test_the_backfill_passes_the_derived_cadence() -> None:
    """⚠ A behavioural test of `is_stale` cannot see a query that stopped being
    given an interval — the SQL would simply never match a stale row, which is
    indistinguishable from "nothing is stale". Gotchas 62/66/69 are three
    separate instances of a well-tested helper a caller quietly ignored."""
    for node in ast.walk(ast.parse(_backfill_src())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fetch"
            and any(getattr(a, "id", "") == "_PENDING_SQL" for a in node.args)
        ):
            assert any(getattr(a, "id", "") == "RECRAWL_AFTER" for a in node.args), (
                "the work-list query is no longer given the refresh cadence, so "
                "its stale arm can never match"
            )
            return
    raise AssertionError("nothing fetches _PENDING_SQL any more")


def test_the_hazard_tool_uses_the_intel_footer_not_the_mirror_one() -> None:
    """⚠ The provenance bug, pinned at the CALL SITE. Both functions return a
    plausible string, so a behavioural test of either one passes while the
    hazard answer goes back to calling FEMA data "synced Buildium data"."""
    tree = ast.parse(_MIRROR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and "intel" in ast.unparse(node).lower()
        ):
            src = ast.unparse(node)
            if "helixis_property_intel" not in src:
                continue
            assert "_intel_as_of_line" in src, (
                "the property-intel read is footing government data with the "
                "Buildium mirror's freshness line again"
            )
            return
    raise AssertionError("no function reads helixis_property_intel in mirror_tools")
