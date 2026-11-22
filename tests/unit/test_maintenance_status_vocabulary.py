"""One definition of "open maintenance", not two that disagree.

Gotcha 50 recorded the split and deliberately left it: ``buildium_tools``
allowlisted OPEN against the live API — work orders ``("New", "InProgress")``,
tasks ``("New", "InProgress", "Deferred")`` — while the mirror denylisted
CLOSED as ``("Completed", "Closed", "Cancelled")``. A ``Deferred`` work order
was therefore open in one and closed in the other, so ``portfolio_overview``
and ``buildium_open_maintenance_by_unit`` could answer the same question with
different numbers.

⚠ **The deeper problem was the SHAPE, not the contents.** An allowlist and a
denylist drift in opposite directions: under an allowlist an unrecognised
status is CLOSED, under a denylist it is OPEN. So the day Buildium adds a
status — and its vocabularies do change (gotcha 15) — the API path would
silently start DROPPING that work from every "what's open" answer while the
mirror kept counting it. Under-reporting looks like a clean answer;
over-reporting looks like a number to argue with. This product's worst failure
mode is the first (gotcha 26).

CLOSED is now the single source of truth, open is its complement, and every
decision about a row in hand goes through ``is_open`` — so an unknown status is
open by construction rather than by someone remembering to update two lists.
"""

from __future__ import annotations

import ast
import pathlib
import re

from helixis.buildium.maintenance_status import (
    CLOSED_STATUSES,
    KNOWN_STATUSES,
    OPEN_STATUSES,
    closed_statuses_sql,
    is_open,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"


# ── the vocabulary itself ────────────────────────────────────────────


def test_open_is_the_complement_of_closed() -> None:
    """Computed, never written out — that is what stops the two drifting."""
    assert set(OPEN_STATUSES) == set(KNOWN_STATUSES) - set(CLOSED_STATUSES)
    assert not set(OPEN_STATUSES) & set(CLOSED_STATUSES)


def test_an_unknown_status_counts_as_open() -> None:
    """THE property. A status Buildium adds tomorrow must not silently vanish
    from "what maintenance is open" — an omission reads as a clean answer,
    which is how a manager stops looking."""
    assert is_open("SomethingBuildiumAddsIn2027")
    assert is_open("Reopened")


def test_a_missing_status_counts_as_open() -> None:
    """``None`` means Buildium did not say, not that the work is finished —
    gotcha 55's decline-to-exclude, pointed at a status instead of a filter."""
    assert is_open(None)


def test_the_finished_states_are_closed() -> None:
    """Both directions (gotcha 38). A definition that called everything open
    would pass every test above and be useless."""
    for status in CLOSED_STATUSES:
        assert not is_open(status), status
    for status in OPEN_STATUSES:
        assert is_open(status), status


def test_whitespace_does_not_reopen_a_finished_job() -> None:
    """A stray space is a wire artefact, not a different status."""
    assert not is_open("  Completed  ")


def test_the_sql_list_is_built_not_repr_ed() -> None:
    """gotcha 50: ``repr(tuple)[1:-1]`` is a correct-looking SQL list until the
    tuple has one entry — ``('X',)`` renders ``'X',`` and Postgres answers
    42601 on a live chat turn. Feed the builder the one input the two
    implementations disagree on."""
    assert closed_statuses_sql() == "'Completed', 'Closed', 'Cancelled'"

    import helixis.buildium.maintenance_status as mod

    original = mod.CLOSED_STATUSES
    try:
        mod.CLOSED_STATUSES = ("OnlyOne",)  # type: ignore[misc]
        assert mod.closed_statuses_sql() == "'OnlyOne'"
        assert not mod.closed_statuses_sql().endswith(",")
    finally:
        mod.CLOSED_STATUSES = original  # type: ignore[misc]


# ── nobody keeps a private second copy ───────────────────────────────


def _string_tuples_in(path: pathlib.Path) -> list[tuple[str, ...]]:
    """Every tuple/list of string literals in a module."""
    out: list[tuple[str, ...]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Tuple | ast.List) and node.elts:
            values = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if len(values) == len(node.elts):
                out.append(tuple(values))
    return out


def test_no_module_hardcodes_its_own_status_vocabulary() -> None:
    """⚠ Reads the SOURCE TREE, not the constants (gotcha 45). Asserting that
    the two known lists agree would pass forever while a third quietly
    appeared in a new rollup — which is exactly how the second one appeared.

    A module may name these statuses in a tuple only if it IS the definition.
    Everyone else imports.
    """
    definition = SRC / "buildium" / "maintenance_status.py"
    interesting = {frozenset(CLOSED_STATUSES), frozenset(OPEN_STATUSES)}
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == definition:
            continue
        for tup in _string_tuples_in(path):
            if frozenset(tup) in interesting:
                offenders.append(f"{path.relative_to(SRC.parent.parent)}: {tup}")
    assert not offenders, (
        "a private copy of the maintenance-status vocabulary — the two copies "
        "that existed before disagreed about `Deferred`, and nothing said so:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_would_actually_notice_a_copy() -> None:
    """A tree walk that matches nothing reports exactly like a clean tree
    (gotcha 49). Prove the matcher recognises the real shape."""
    assert frozenset(("Completed", "Closed", "Cancelled")) == frozenset(CLOSED_STATUSES)
    assert frozenset(("New", "InProgress", "Deferred")) == frozenset(OPEN_STATUSES)
    fake = SRC / "buildium" / "maintenance_status.py"
    assert _string_tuples_in(fake), "the tuple extractor found nothing in the definition module"


# ── both readers actually use it ─────────────────────────────────────


def test_the_live_api_tools_read_the_shared_vocabulary() -> None:
    from helixis.agents import buildium_tools as bt

    assert bt._OPEN_WORK_ORDER_STATUSES == OPEN_STATUSES
    assert bt._OPEN_TASK_STATUSES == OPEN_STATUSES


def test_the_mirror_reads_the_shared_vocabulary() -> None:
    from helixis.agents import mirror_tools

    assert mirror_tools.CLOSED_MAINTENANCE_STATUSES == CLOSED_STATUSES
    assert closed_statuses_sql() in mirror_tools._OPEN_MAINTENANCE_SELECT


def test_a_deferred_work_order_is_open_on_both_sides() -> None:
    """The one status the two spellings disagreed about, asserted through the
    real readers rather than the constant — so a future edit that re-splits
    them fails here rather than in a battery answer."""
    from helixis.agents import buildium_tools as bt

    assert "Deferred" in bt._OPEN_WORK_ORDER_STATUSES
    assert is_open("Deferred")
    assert "Deferred" not in closed_statuses_sql()


# ── SQL is a copy too ────────────────────────────────────────────────

#: A status allowlist written into SQL. The tuple scan above never saw these:
#: twelve queries said ``status IN ('New', 'InProgress')``, so Rentvine's
#: "On Hold" and Buildium's "Deferred" were open in the chat and missing from
#: the cockpit, the Operations page, the first scan and the morning brief.
_SQL_ALLOWLIST = re.compile(r"\bstatus\s+IN\s*\(\s*'[A-Za-z ]+'(?:\s*,\s*'[A-Za-z ]+')*\s*\)")


def _sql_allowlists_in(source: str) -> list[int]:
    """Lines of status allowlists in the source's string literals (never its comments)."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _SQL_ALLOWLIST.finditer(node.value):
                statuses = set(re.findall(r"'([^']+)'", match.group(0)))
                # Only a list of maintenance statuses: lease or bill status
                # filters are their own vocabularies.
                if statuses & set(KNOWN_STATUSES) and statuses <= set(KNOWN_STATUSES):
                    hits.append(node.lineno)
    return hits


def test_no_query_hardcodes_which_statuses_are_open() -> None:
    offenders = [
        f"{path.relative_to(SRC.parent.parent)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in _sql_allowlists_in(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "a query lists the OPEN maintenance statuses itself; use "
        "`status NOT IN ({closed_statuses_sql()})` so a status it doesn't know "
        "(Rentvine's 'On Hold') still counts as open:\n  " + "\n  ".join(offenders)
    )


def test_the_sql_scan_would_notice_a_copy() -> None:
    """A scan that matches nothing reports exactly like a clean tree (gotcha 49)."""
    planted = (
        "SQL = \"SELECT 1 FROM helixis_work_orders wo WHERE wo.status IN ('New', 'InProgress')\"\n"
    )
    assert _sql_allowlists_in(planted) == [1]
    # The comment explaining the rule quotes the defect; it must not fire (rule 17).
    assert not _sql_allowlists_in("# was: status IN ('New', 'InProgress')\nX = 1\n")
    # A lease-status filter is not a maintenance vocabulary.
    assert not _sql_allowlists_in("SQL = \"WHERE l.status IN ('Active')\"\n")


def test_rentvine_on_hold_counts_as_open() -> None:
    from helixis.rentvine.mapper import WORK_ORDER_STATUS

    assert is_open(WORK_ORDER_STATUS[4]) and WORK_ORDER_STATUS[4] == "On Hold"
    assert not is_open(WORK_ORDER_STATUS[3])  # "Closed"
