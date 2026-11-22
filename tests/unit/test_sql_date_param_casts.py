"""`date ± $n` needs a cast, or the query raises and the caller swallows it.

asyncpg sends a bare parameter as ``unknown`` and lets Postgres infer the
type. That works almost everywhere and fails for date arithmetic, because
``date + unknown`` has three candidate operators — ``date + integer``,
``date + interval``, ``date + time`` — and Postgres refuses to guess:

    ERROR:  42725: operator is not unique: date + unknown
    HINT:  Could not choose a best candidate operator.

Verified directly against the live database, not recalled.

**Every caller of these queries catches broadly and degrades**, which is
correct for a background sweep and is what made this invisible. Measured on
helixis-test 2026-08-29:

* ``bill_due`` had emitted **0 reminder cards, ever**, against **80
  qualifying bills** across three companies. Six sibling producers held 102
  cards between them — they compare against a bare ``current_date`` with no
  parameter, which is exactly why they work.
* Six detector rules carried the identical shape and had **0 findings each**:
  ``duplicate_vendor_bill``, ``promise_to_pay_broken``,
  ``invoice_no_work_order``, ``utilities_owner_paid_occupied``,
  ``invoice_above_norm``, ``same_day_trip_charges``. Every rule that HAS
  fired (``rent_not_collected``, ``rent_under_charged``, ``analyst``) is free
  of it. The correlation is total.

⚠ This scans the SOURCE TREE rather than the seven known sites (gotcha 45).
Fixing the sites found by one investigation leaves the next one free to
reopen the hole, and the failure mode is silence — a rule that never fires
looks exactly like a rule with nothing to report.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"

#: ``current_date``/``now()``/a date column, then ``+``/``-``, then a bare
#: ``$n`` NOT followed by a cast. The negative lookahead is the whole point:
#: ``$2::int`` is the fix and must not be flagged.
_UNCAST = re.compile(
    r"""
    (?:current_date | CURRENT_DATE | now\(\))   # a date/timestamp source
    \s* [+-] \s*                                # arithmetic
    \$\d+                                       # a bound parameter
    (?! \s* :: )                                # ...with no cast after it
    """,
    re.VERBOSE,
)

#: The same shape written the other way round — ``$n + current_date``.
_UNCAST_REVERSED = re.compile(
    r"\$\d+ (?! \s* ::) \s* [+-] \s* (?:current_date|CURRENT_DATE|now\(\))",
    re.VERBOSE,
)


def _offenders() -> list[str]:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if _UNCAST.search(line) or _UNCAST_REVERSED.search(line):
                rel = path.relative_to(SRC.parent.parent)
                hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


def test_no_query_does_uncast_date_parameter_arithmetic() -> None:
    assert not _offenders(), (
        "Postgres cannot resolve `date ± unknown` (42725, verified live), so "
        "each of these raises at execution and its caller swallows it — a rule "
        "that never fires is indistinguishable from a rule with nothing to "
        "report. Add an explicit cast (`$n::int`) or use `make_interval(days "
        "=> $n)`, which is the idiom already used elsewhere in this repo:\n  "
        + "\n  ".join(_offenders())
    )


def test_the_scan_can_actually_see_the_defect() -> None:
    """⚠ A guard that finds nothing because its regex is wrong reports exactly
    like a clean tree (gotcha 49). Feed it the real pre-fix line and the real
    post-fix line and require it to separate them."""
    before = "   AND b.bill_date >= current_date - $3"
    after = "   AND b.bill_date >= current_date - $3::int"
    assert _UNCAST.search(before), "the scan cannot see the bug it exists for"
    assert not _UNCAST.search(after), "the scan flags the fix — it would be switched off"


def test_the_scan_spares_the_make_interval_idiom() -> None:
    """``make_interval(days => $2)`` types its parameter through the function
    signature, so it resolves. It is already used in ``producers.py`` and
    ``mirror_tools.py`` and must not be flagged — a guard that cries wolf on
    correct code gets disabled (gotcha 49, from the other direction)."""
    line = "AND tv.move_out_date >= current_date - make_interval(days => $2)"
    assert not _UNCAST.search(line)
    assert not _UNCAST_REVERSED.search(line)


def test_the_scan_spares_ordinary_parameter_use() -> None:
    """Bare ``$n`` is fine everywhere else — only date arithmetic is
    ambiguous. Flagging every parameter would make the guard useless."""
    for ok in (
        "WHERE b.company_id = $1::uuid AND b.total_amount > $2",
        "AND b.due_date <= $2",
        "LIMIT $1",
        "AND created_at >= now() - make_interval(days => $3)",
    ):
        assert not _UNCAST.search(ok), ok
        assert not _UNCAST_REVERSED.search(ok), ok


def test_the_scan_catches_the_reversed_spelling() -> None:
    """``$2 + current_date`` is the same bug typed the other way round."""
    assert _UNCAST_REVERSED.search("AND d < $2 + current_date")
    assert not _UNCAST_REVERSED.search("AND d < $2::int + current_date")


def test_the_scan_reaches_the_files_that_carried_it() -> None:
    """The tree walk must actually cover the four modules this was found in —
    a wrong root or a bad glob collects zero files and reports green."""
    scanned = {p.name for p in SRC.rglob("*.py")}
    for name in (
        "producers.py",
        "state_rules.py",
        "bridge_rules.py",
        "mirror_state_rules.py",
    ):
        assert name in scanned, f"{name} is not being scanned — the walk is broken"
