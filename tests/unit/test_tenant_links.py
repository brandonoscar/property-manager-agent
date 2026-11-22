"""Which tenant-to-lease links mean "lives here now" (``db.tenant_links``).

``helixis_lease_tenants.status`` is the LINK's status. Buildium writes
``MovedOut`` for a co-tenant who left a lease the others still hold, and every
reader that joined the links with no status filter named that person as living
there: the renewal offer's recipient, the rent reminder's mail and promise
gates, the email-sender match, the property, unit and Operations tenant lists,
and the chat tools' tenant names.

The guard below walks every SQL string in ``src/helixis`` (string literals and
f-strings, from the AST; never the source text, so a comment quoting the defect
cannot trip it). Each read of the link table either carries
``current_link_sql(<its alias>)`` or is listed in ``_READS_EVERY_LINK`` with the
reason it means history.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sqlite3
from collections import Counter

import pytest

import helixis
from helixis.db.tenant_links import (
    CURRENT_LINK_STATUS,
    FUTURE_LINK_STATUS,
    MOVED_OUT_LINK_STATUS,
    current_link_sql,
    is_current_link,
)

_SRC = pathlib.Path(helixis.__file__).resolve().parent

#: Readers that read every link on purpose: (file, function) -> (how many link
#: reads in it carry no filter, why). The count makes a NEW unfiltered read in
#: one of these functions fail here instead of riding in on the old reason.
_READS_EVERY_LINK: dict[tuple[str, str], tuple[int, str]] = {
    ("agents/mirror_tools.py", "_delinquency_report_impl"): (
        1,
        "who owes the balance: a co-tenant who moved out can still owe it",
    ),
    ("agents/mirror_tools.py", "_lease_payment_history_impl"): (
        2,
        "history: finds a lease through anyone who was ever on it",
    ),
    ("db/repositories/cockpit.py", "get_tenant_detail"): (
        2,
        "the tenant's own leases and payments, past ones included",
    ),
}

#: The readers this rule was written for. The scan must keep finding each of
#: them filtered, or it has stopped looking (rule 10: a scan that finds nothing
#: is a claim to check).
_FILTERED_READERS = {
    ("db/repositories/renewals.py", "list_candidates"),
    ("webhooks/mirror_context.py", "gather_mirror_context"),
    ("reminders/producers.py", "rent_missing"),
    ("db/repositories/cockpit.py", "get_property_detail"),
    ("db/repositories/cockpit.py", "get_unit_detail"),
    ("db/repositories/cockpit.py", "get_tenant_detail"),
    ("db/repositories/operations.py", "<module>"),
    ("agents/mirror_tools.py", "_expiring_leases_impl"),
    ("agents/mirror_tools.py", "_rent_roll_impl"),
    ("agents/mirror_tools.py", "_contact_lookup_impl"),
}

_KEYWORDS = r"(?:ON|WHERE|JOIN|LEFT|RIGHT|INNER|USING|GROUP|ORDER|LIMIT)\b"
_LINK_READ = re.compile(
    rf"\b(?:FROM|JOIN)\s+helixis_lease_tenants\b(?:\s+(?:AS\s+)?(?!{_KEYWORDS})(\w+))?",
    re.IGNORECASE,
)
_FILTER = re.compile(r"\x00current:(\w+)\x00")
_WRITE = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _render(node: ast.AST) -> str:
    """The SQL text of a string literal or f-string. A ``current_link_sql()``
    call renders as a marker naming its alias; any other interpolation is
    opaque."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    assert isinstance(node, ast.JoinedStr)
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
            continue
        assert isinstance(value, ast.FormattedValue)
        call = value.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == current_link_sql.__name__
        ):
            args = [*call.args, *(k.value for k in call.keywords)]
            assert all(isinstance(a, ast.Constant) for a in args), (
                "current_link_sql() takes a literal alias, so the guard can match it"
            )
            alias = args[0].value if args else "lt"
            parts.append(f"\x00current:{alias}\x00")
        elif isinstance(call, ast.Name) and call.id == "LINK_TABLE":
            # ``pms/mirror.py`` names the table through a constant; a read
            # written that way must not hide from this guard.
            parts.append("helixis_lease_tenants")
        else:
            parts.append("\x00opaque\x00")
    return "".join(parts)


class _SqlStrings(ast.NodeVisitor):
    """Every string literal and f-string with the function that holds it,
    docstrings and the pieces of an f-string excluded."""

    def __init__(self) -> None:
        self.found: list[tuple[str, str]] = []
        self._func = ["<module>"]
        self._skip: set[int] = set()

    def _skip_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            self._skip.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._skip_docstring(node)
        self._func.append(node.name)
        self.generic_visit(node)
        self._func.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self.found.append((self._func[-1], _render(node)))
        # The pieces are part of this string, not strings of their own; an
        # interpolated expression may hold a string of its own.
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self._skip:
            self.found.append((self._func[-1], node.value))


def _tally(sql: str) -> Counter[str]:
    """How many reads of the link table in one statement carry a filter on
    their own alias ("filtered") and how many do not ("unfiltered"). A write
    counts as neither."""
    tally: Counter[str] = Counter()
    if _WRITE.match(sql):
        return tally
    filters = Counter(_FILTER.findall(sql))
    for match in _LINK_READ.finditer(sql):
        alias = match.group(1) or "helixis_lease_tenants"
        if filters[alias] > 0:
            filters[alias] -= 1
            tally["filtered"] += 1
        else:
            tally["unfiltered"] += 1
    return tally


def _link_reads() -> dict[tuple[str, str], Counter[str]]:
    """(file, function) -> the summed :func:`_tally` of its statements."""
    out: dict[tuple[str, str], Counter[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        visitor = _SqlStrings()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        rel = path.relative_to(_SRC).as_posix()
        for func, sql in visitor.found:
            tally = _tally(sql)
            if tally:
                out.setdefault((rel, func), Counter()).update(tally)
    return out


def test_every_connector_writes_the_word_the_readers_keep() -> None:
    """Rule 15: the readers keep ``Current``; Rentvine, Rent Manager and
    Propertyware write one fixed word for every link they keep, and DoorLoop
    writes it for a CURRENT lease tenant. If a mapper's word drifted, every one
    of that system's tenants would vanish from every "who lives here" list at
    once."""
    from helixis.doorloop import mapper as dl
    from helixis.propertyware import mapper as pw
    from helixis.rentmanager import mapper as rm
    from helixis.rentvine import mapper as rv

    for mapper in (rv, rm, pw, dl):
        assert mapper.LEASE_TENANT_STATUS == CURRENT_LINK_STATUS, mapper.__name__
    # DoorLoop's other two words are the readers' too (``db.tenant_links``).
    assert dl.LINK_STATUS == {
        "CURRENT": CURRENT_LINK_STATUS,
        "FUTURE": FUTURE_LINK_STATUS,
        "PAST": MOVED_OUT_LINK_STATUS,
    }


def test_the_filter_is_the_constant_on_a_bare_alias() -> None:
    assert current_link_sql() == "COALESCE(lt.status, 'Current') = 'Current'"
    assert current_link_sql("lt2") == "COALESCE(lt2.status, 'Current') = 'Current'"
    assert CURRENT_LINK_STATUS not in (MOVED_OUT_LINK_STATUS, FUTURE_LINK_STATUS)
    for bad in ("", "lt.status", "lt; DROP TABLE x", "lt --"):
        with pytest.raises(ValueError):
            current_link_sql(bad)


@pytest.mark.parametrize(
    "status",
    [
        None,
        CURRENT_LINK_STATUS,
        MOVED_OUT_LINK_STATUS,
        FUTURE_LINK_STATUS,
        "",
        "current",
        " Current",
    ],
)
def test_the_sql_filter_and_the_python_check_agree(status: str | None) -> None:
    """Rule 15: the lists filter in SQL and ``contact_lookup`` words the line in
    Python; the same link must read the same way in both. SQLite evaluates the
    shipped condition (``COALESCE`` and ``=`` on text behave as in Postgres)."""
    with sqlite3.connect(":memory:") as db:
        (kept,) = db.execute(
            f"SELECT {current_link_sql('t')} FROM (SELECT ? AS status) t",  # noqa: S608
            (status,),
        ).fetchone()
    assert bool(kept) is is_current_link(status)


def _unexplained(
    reads: dict[tuple[str, str], Counter[str]],
    allowed: dict[tuple[str, str], tuple[int, str]] = _READS_EVERY_LINK,
) -> dict[tuple[str, str], int]:
    """Unfiltered reads the history list does not account for, exactly."""
    return {
        key: tally["unfiltered"]
        for key, tally in reads.items()
        if tally["unfiltered"] and allowed.get(key, (0, ""))[0] != tally["unfiltered"]
    }


def test_every_reader_of_the_links_filters_or_says_why() -> None:
    unexplained = _unexplained(_link_reads())
    assert not unexplained, (
        "these read helixis_lease_tenants with no status filter, which names a "
        "tenant who moved out as living there. Add current_link_sql(<alias>) to "
        "the read, or list it in _READS_EVERY_LINK with why it means history: "
        f"{unexplained}"
    )


def test_the_scan_still_finds_the_filtered_readers() -> None:
    reads = _link_reads()
    filtered = {key for key, tally in reads.items() if tally["filtered"]}
    missing = _FILTERED_READERS - filtered
    assert not missing, f"the scan no longer sees these filtered reads: {sorted(missing)}"


def test_a_history_reader_that_grows_another_unfiltered_read_is_caught() -> None:
    """The listed reason covers the reads it was written for, not the next one."""
    allowed = {("a.py", "f"): (1, "history")}
    assert _unexplained({("a.py", "f"): Counter(unfiltered=2)}, allowed) == {("a.py", "f"): 2}
    assert _unexplained({("a.py", "f"): Counter(unfiltered=1)}, allowed) == {}
    assert _unexplained({("b.py", "g"): Counter(unfiltered=1, filtered=1)}, allowed) == {
        ("b.py", "g"): 1
    }
    assert _unexplained({("b.py", "g"): Counter(filtered=2)}, allowed) == {}


def test_every_listed_history_reader_still_reads_every_link() -> None:
    """A stale entry would excuse whatever unfiltered read lands there next."""
    reads = _link_reads()
    for key, (count, _why) in _READS_EVERY_LINK.items():
        assert reads.get(key, Counter())["unfiltered"] == count, key


@pytest.mark.parametrize(
    ("sql", "filtered", "unfiltered"),
    [
        ("SELECT 1 FROM helixis_lease_tenants lt WHERE \x00current:lt\x00", 1, 0),
        ("SELECT 1 FROM helixis_lease_tenants lt WHERE lt.company_id = $1", 0, 1),
        # The filter must name THIS read's alias: an outer ``lt`` does not
        # cover an inner ``lt2``.
        ("SELECT (SELECT 1 FROM helixis_lease_tenants lt2) WHERE \x00current:lt\x00", 0, 1),
        # One filter does not cover two reads.
        (
            "SELECT (SELECT 1 FROM helixis_lease_tenants lt WHERE \x00current:lt\x00),"
            " (SELECT 1 FROM helixis_lease_tenants lt)",
            1,
            1,
        ),
        ("SELECT 1 FROM helixis_lease_tenants WHERE x", 0, 1),
        # A write is neither.
        ("DELETE FROM helixis_lease_tenants lt WHERE lt.company_id = $1", 0, 0),
    ],
)
def test_the_counter_matches_each_read_to_its_own_filter(
    sql: str, filtered: int, unfiltered: int
) -> None:
    """The guard's own logic, on SQL it must catch and SQL it must spare."""
    got = _tally(sql)
    assert (got["filtered"], got["unfiltered"]) == (filtered, unfiltered)


@pytest.mark.parametrize(
    ("source", "filtered", "unfiltered"),
    [
        ('f"SELECT 1 FROM {LINK_TABLE} lt WHERE {current_link_sql()}"', 1, 0),
        ('f"SELECT 1 FROM {LINK_TABLE} lt"', 0, 1),
        ("f\"SELECT 1 FROM helixis_lease_tenants x WHERE {current_link_sql('x')}\"", 1, 0),
        ("f\"SELECT 1 FROM helixis_lease_tenants x WHERE {current_link_sql(alias='x')}\"", 1, 0),
        ('f"SELECT 1 FROM helixis_lease_tenants x WHERE {other()}"', 0, 1),
    ],
)
def test_the_renderer_reads_the_table_and_the_filter_through_interpolation(
    source: str, filtered: int, unfiltered: int
) -> None:
    node = ast.parse(source, mode="eval").body
    got = _tally(_render(node))
    assert (got["filtered"], got["unfiltered"]) == (filtered, unfiltered)


def test_the_analyst_is_told_which_links_mean_lives_here() -> None:
    """The nightly analyst writes its own SQL against the same table; one note
    has to carry the whole rule, not words scattered across several."""
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    notes = [
        n
        for n in _SCHEMA_NOTES
        if "helixis_lease_tenants.status" in n
        and f"'{CURRENT_LINK_STATUS}'" in n
        and f"'{MOVED_OUT_LINK_STATUS}'" in n
        and "Active" in n
    ]
    assert notes, "no single note says a MovedOut link can sit on an Active lease"
    assert current_link_sql("helixis_lease_tenants") in notes[0]
