"""Buildium sends ``PropertyId`` on every bill line and a value on none of them.

WHAT HAPPENED
-------------
Battery v10-100 q48 — *"Which property is costing me the most in repairs?"* —
ran **177 seconds across six tool calls** and concluded::

    I could not determine which property is costing the most in repairs …
    the bills aren't linked to specific property addresses or units in what
    was retrieved.

They are. Live-verified against all 131 mirrored bill lines (2026-08-27):

===========================  =======  ==========
field                        present  populated
===========================  =======  ==========
``PropertyId``               131/131  **0/131**
``AccountingEntity.Id``      131/131  131/131
===========================  =======  ==========

``BillLine`` aliased ``PropertyId``. ``upsert_bill`` wrote it straight through.
So ``helixis_bill_lines.property_id`` was NULL on 100% of rows, and
``unpaid_bills``' property join — correct code, unchanged — silently produced
nothing for every property-scoped payables question ever asked.

⚠ THE WORST VERSION OF GOTCHA 25
---------------------------------
The rule is "never trust a Buildium field's presence from the SDK model or the
docs — confirm it on a live production row." This one defeats the *obvious*
way of confirming it: the key IS on every row. A presence check answers "yes,
we have PropertyId." Only reading the VALUE shows it is always null. Check
population, not presence.

And the class's own docstring asserted the opposite — *"carries the PropertyId
association needed for 'unpaid bills at <property>' filtering (PR #77)"* — a
claim that has never been true against production. A comment cannot fail, so
it went on being wrong for as long as nobody measured it (gotcha 48's shape).

⚠ ``Rental`` ONLY
------------------
``AccountingEntityType`` is ``Rental`` for a property and ``Company`` for an
overhead line. A ``Company`` entity id is not a property id, and treating it
as one would attribute overhead to whatever property happens to share that
number — inventing a fact rather than missing one.
"""

from __future__ import annotations

from helixis.buildium.schemas import BillLine


def test_the_property_comes_from_the_accounting_entity() -> None:
    """THE fix, on the real wire shape: ``PropertyId`` present and null,
    ``AccountingEntity`` carrying the value."""
    line = BillLine.model_validate(
        {
            "Id": 1,
            "PropertyId": None,
            "UnitId": None,
            "Amount": 265.0,
            "Memo": "Gutter cleaning — past due invoice",
            "AccountingEntity": {"Id": 37959, "AccountingEntityType": "Rental"},
        }
    )
    assert line.property_id is None, "the raw field is null on production — that is the premise"
    assert line.resolved_property_id == 37959


def test_an_explicit_property_id_still_wins() -> None:
    """⚠ ``PropertyId`` is checked FIRST, deliberately. It is in the SDK
    contract; if Buildium ever starts populating it, hard-coding the other
    field would then be the bug. Kept as a fallback, not deleted."""
    line = BillLine.model_validate(
        {"PropertyId": 111, "AccountingEntity": {"Id": 222, "AccountingEntityType": "Rental"}}
    )
    assert line.resolved_property_id == 111


def test_a_company_line_resolves_to_no_property() -> None:
    """⚠ Overhead belongs to no property. Returning the Company entity id here
    would attribute it to whatever property shares that number — a fabricated
    association, which is worse than the NULL this replaces."""
    line = BillLine.model_validate(
        {"Amount": 50.0, "AccountingEntity": {"Id": 900, "AccountingEntityType": "Company"}}
    )
    assert line.resolved_property_id is None


def test_a_line_with_neither_resolves_to_none() -> None:
    """No association is still a legitimate state and must not raise."""
    assert BillLine.model_validate({"Amount": 10.0}).resolved_property_id is None
    assert BillLine.model_validate({"AccountingEntity": {}}).resolved_property_id is None


def test_the_ingest_reads_the_resolved_field_not_the_raw_one() -> None:
    """⚠ Both sides (gotcha 28). Fixing the model and leaving the writer on
    ``line.property_id`` would keep the column NULL forever while every unit
    test above passed — the model would be correct and unused.

    Asserted on the AST rather than a substring so ``ruff format`` reflowing
    the call cannot turn this green (gotcha 40).
    """
    import ast
    import pathlib

    src = pathlib.Path("src/helixis/db/repositories/entities.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "line"
    }
    assert "resolved_property_id" in attrs, (
        "upsert_bill must write line.resolved_property_id — writing line.property_id is what "
        "left helixis_bill_lines.property_id NULL on 100% of rows"
    )
    assert "property_id" not in attrs, (
        "a remaining bare line.property_id write would re-open the bug on the rows it touches"
    )
