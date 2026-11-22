"""A ledger row is signed by its effect on the BALANCE — so a payment is
negative, and summing one un-negated puts a minus sign in front of income.

MEASURED on helixis-test 2026-09-10, with the control in the same result set
(which is what makes it evidence rather than a coincidence): ``Payment``
144/144 negative, ``Charge`` 619/619 positive; ``Credit`` 20/20 and ``Applied
Deposit`` 2/2 negative, ``Refund`` 3/3 positive.

THE DEFECT THIS EXISTS TO STOP, and why it is a corpus scan rather than a
unit test of one function. The rule was already written down, correctly, in
``reconciliation/analyst.py`` — whose comment says *"Negating (rather than
ABS) keeps a reversal honest: a positive Payment row means money went back
out"*. It never reached ``db/repositories/cockpit.py``, which summed the same
rows un-negated into a field named ``rent_collected_mtd`` and rendered it as
"$X collected this month" in the property cockpit AND in the agent's own
property snapshot. Two surfaces, a negative number where income belongs.
That is gotcha 67/25's shape: a rule documented at its first caller, invisible
to the second.

So the thing that has to be checked is not "does the helper work" — it did,
one module over, the whole time. It is **which statements in the tree read
these rows**, which is gotcha 45's rule: a control whose failure mode is
"silently permits more" needs a test that measures what it permitted.

⚠ SCOPED TO STATEMENTS, NEVER THE FILE (gotchas 40/59). ``money.py`` and
``cockpit.py`` now QUOTE the broken form in prose in order to explain it, so a
whole-file substring scan would fire on the sentence documenting the guard —
the exact trap that got a previous guard switched off. String literals are
read through the AST and a comment is never an ``ast.Constant``; a docstring
is one, so the SQL test requires the literal to actually look like SQL.
"""

from __future__ import annotations

import ast
import pathlib
import re

from helixis.buildium.money import payments_received_sql

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"

#: A statement that restricts to payment rows. Both spellings Buildium's
#: mirror uses appear in the tree.
_PAYMENT_FILTER = re.compile(r"transaction_type\s*(?:=|<>|!=)\s*'Payment'", re.I)

#: A SUM over the signed amount column.
_SUM_AMOUNT = re.compile(r"\bSUM\s*\(\s*-?\s*[\w.]*total_amount\s*\)", re.I)

#: The same, with the negation present. ``-SUM(x)`` and ``SUM(-x)`` are both
#: correct; ``ABS`` is NOT and must never be accepted here — a positive
#: ``Payment`` row is a reversal, money going back out, and ABS would count it
#: as income.
_NEGATED = re.compile(
    r"-\s*SUM\s*\(\s*[\w.]*total_amount\s*\)|\bSUM\s*\(\s*-\s*[\w.]*total_amount\s*\)", re.I
)


def _sql_literals() -> list[tuple[pathlib.Path, int, str]]:
    """Every string literal in the tree that is plausibly a SQL statement.

    f-strings are reconstructed as templates (holes as ``{}``) because an
    ``ast.Constant`` scan cannot see one, and its inner fragments are ALSO
    reached by ``ast.walk`` at the same lineno — judging both gives the
    fragment's verdict, which silently wins (gotcha 63).
    """
    out: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - the tree must parse
            continue
        inner_of_fstring: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for part in ast.walk(node):
                    if part is not node:
                        inner_of_fstring.add(id(part))
        for node in ast.walk(tree):
            if id(node) in inner_of_fstring:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "{}"
                    for p in node.values
                )
            else:
                continue
            low = text.lower()
            if "select" in low and "from" in low:
                out.append((path, node.lineno, text))
    return out


def test_the_scan_finds_the_statements_it_is_meant_to_judge() -> None:
    """Guard the guard. A bad glob or a changed CWD collects zero statements
    and reports green — a corpus scan that finds nothing is a claim to check,
    not a result (gotchas 49/100/101)."""
    sql = _sql_literals()
    assert len(sql) > 50, f"only {len(sql)} SQL literals found — the scan is not reading the tree"
    payment_sql = [s for s in sql if _PAYMENT_FILTER.search(s[2])]
    assert payment_sql, "no payment-filtered SQL found — the scan cannot be judging anything"


def test_no_statement_sums_payments_without_negating_them() -> None:
    """FLOOR: any statement that restricts to ``Payment`` rows and sums
    ``total_amount`` must negate the sum.

    If this fails, the named statement produces a NEGATIVE number for money
    received. Fix it with ``buildium.money.payments_received_sql()`` rather
    than by hand — that helper carries the ``COALESCE`` too, and a bare
    ``SUM`` over no rows is ``NULL``, which compares falsy and silently stops
    a gate firing on exactly the population it exists for.
    """
    offenders: list[str] = []
    for path, lineno, text in _sql_literals():
        if not _PAYMENT_FILTER.search(text):
            continue
        if not _SUM_AMOUNT.search(text):
            continue
        if _NEGATED.search(text):
            continue
        rel = path.relative_to(SRC.parents[1])
        offenders.append(f"{rel}:{lineno}")
    assert offenders == [], (
        "these statements sum Payment rows without negating, so they report "
        f"money received as a negative number: {offenders}. Payments are signed "
        "by their effect on the balance — use buildium.money.payments_received_sql()."
    )


def test_the_helper_carries_both_halves() -> None:
    """Both narrowings are fatal in OPPOSITE directions, so both must be in
    the one place callers copy from: without the negation every figure is
    inverted; without the COALESCE a ``SUM`` over no rows is ``NULL`` and
    every comparison against it is falsy."""
    sql = payments_received_sql()
    assert _NEGATED.search(sql), f"the helper must negate: {sql!r}"
    assert "coalesce" in sql.lower(), f"the helper must COALESCE: {sql!r}"
    assert "abs(" not in sql.lower(), (
        "ABS is wrong, not merely redundant: a POSITIVE Payment row is a "
        "reversal — money going back out — and must subtract, not add."
    )
    assert payments_received_sql("x.amount").endswith("(-SUM(x.amount), 0)")


def test_the_two_live_readers_agree() -> None:
    """The two statements that actually read this rule in production must
    both negate — ``analyst.py`` always did, ``cockpit.py`` did not, and the
    whole point is that they can no longer disagree."""
    analyst = (SRC / "reconciliation" / "analyst.py").read_text()
    cockpit = (SRC / "db" / "repositories" / "cockpit.py").read_text()
    assert "-SUM(total_amount) AS payments_3mo" in analyst, (
        "analyst.py stopped negating — it is the module that got this right first"
    )
    assert "payments_received_sql()" in cockpit, (
        "cockpit.py's rent_collected_mtd must read the shared rule, not re-derive it"
    )
