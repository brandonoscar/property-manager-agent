"""``?propertyids`` comes back ignored, and an unscoped count reads as a fact.

WHAT HAPPENED
-------------
Battery v10-100 q95 — *"Post a $450 charge to the lease at MT — 1301 E 6th
Ave."* Occupella refused, correctly, and explained why::

    Which lease? Buildium shows 50 active leases attached to that single-unit
    property, which looks like a data mismatch.

The mirror says that property has **one** active lease. Fifty is the whole
account. So ``api.leases.list(propertyids=[prop.id], ...)`` returned every
lease Blue Ridge owns, 200, no error — the silent-ignore gotcha 24 records for
``/tasks`` and ``/bills`` while explicitly asserting ``/leases`` was fine.
It is not.

⚠ THE REFUSAL WAS SAFE AND THE PREMISE WAS FALSE
-------------------------------------------------
Nothing bad happened here: the agent declined a write it could not target.
But it declined it for a reason that was not true, and the same reasoning
pointed the other way is a wrong write — gotcha 41's exact lesson. A 50x-wrong
count is indistinguishable from a real one at the call site.

⚠ WHY RE-FILTERING IS ALWAYS CORRECT
-------------------------------------
It is a no-op when the server filter works and a correction when it does not,
so it cannot make anything worse. That asymmetry is why this belongs on every
property-scoped list read rather than only the endpoints known to be broken —
the endpoints known to be broken is a list that has now grown twice, and both
times the discovery cost a wrong answer first.
"""

from __future__ import annotations

from typing import Any

from helixis.agents.buildium_tools import _only_for_property


class _Row:
    def __init__(self, property_id: Any) -> None:
        self.property_id = property_id


def test_rows_from_other_properties_are_dropped() -> None:
    """THE q95 fix: the account-wide response is cut back to this property."""
    rows = [_Row(41), _Row(99), _Row(41), _Row(7)]
    assert len(_only_for_property(rows, 41)) == 2


def test_a_correctly_filtered_response_is_untouched() -> None:
    """⚠ The no-op half. If this ever removed rows the server filtered
    correctly, the fix would be worse than the bug — it would under-report,
    which is the false-absence failure (gotcha 26) rather than the
    over-report we started with."""
    rows = [_Row(41), _Row(41), _Row(41)]
    assert len(_only_for_property(rows, 41)) == 3


def test_a_row_with_no_property_is_dropped_not_kept() -> None:
    """⚠ Unattributable is not "belongs here". ``Unit.property_id`` is already
    documented as occasionally absent on draft rows, so this is live. Counting
    such a row toward this property is precisely the false claim being
    removed."""
    kept = _only_for_property([_Row(None), _Row(41)], 41)
    assert [r.property_id for r in kept] == [41]


def test_string_and_int_ids_compare_equal() -> None:
    """Buildium ids arrive as ints, but a caller passing the string form must
    not silently empty the list — which would read as "this property has
    nothing"."""
    assert len(_only_for_property([_Row("41"), _Row(41)], "41")) == 2


def test_an_unusable_target_returns_the_rows_untouched() -> None:
    """⚠ Fails OPEN, deliberately, and this is the one judgement call here.

    With no id to scope against, returning [] would report "no leases at this
    property" — a confident absence manufactured by our own guard, which is a
    worse failure than the over-count it replaces. Returning the rows keeps
    the pre-existing behaviour and leaves the over-count visible.
    """
    rows = [_Row(1), _Row(2)]
    assert _only_for_property(rows, None) == rows
    assert _only_for_property(rows, "not-an-id") == rows


def test_both_property_reads_are_scoped() -> None:
    """⚠ Both sides (gotcha 28), on the AST. Scoping the leases and leaving
    the units unscoped fixes the number q95 happened to surface and leaves the
    identical bug one line above it — and a unit count is what "single-unit
    property" in that same reply was based on."""
    import ast
    import pathlib

    src = pathlib.Path("src/helixis/agents/buildium_tools.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_only_for_property"
    ]
    assert len(calls) >= 2, (
        f"expected the property detail read to scope BOTH its units and its leases, "
        f"found {len(calls)} scoped call(s)"
    )


def test_an_unpopulated_field_fails_open_rather_than_reporting_nothing() -> None:
    """⚠ THE bug my own first cut shipped, caught by a pre-existing test.

    ``WorkOrder.unit_id`` is declared and Buildium leaves it NULL — the mirror
    has it null on 60 of 60 rows. Filtering on it turned "60 work orders,
    wrongly unfiltered" into **"123 Main St, Unit 4B has no work orders on
    record."**

    Those are not the same severity. An over-count is arguable and visible; a
    false absence is confident and wrong, and gotcha 26 calls that this
    product's worst failure mode. Trading the first for the second is a
    regression wearing a fix's clothes.

    So when NO row carries the field, the filter cannot discriminate and
    declines to act — the same principle as the unusable-TARGET branch. A guard
    that cannot see must say so by doing nothing, never by reporting emptiness.
    """
    blind = [_Row(None), _Row(None), _Row(None)]
    assert _only_for_property(blind, 41) == blind, (
        "a field that is null on every row must not empty the list — that is a "
        "fabricated absence, strictly worse than the over-count it replaces"
    )


def test_one_populated_row_is_enough_to_filter() -> None:
    """⚠ The boundary, and it must NOT be 'most rows'. If even one row carries
    the field, the field works and the nulls are genuinely unattributable —
    keeping them would re-introduce the false-attribution bug. Only a
    completely blind field fails open."""
    mixed = [_Row(None), _Row(41), _Row(99)]
    assert [r.property_id for r in _only_for_property(mixed, 41)] == [41]
