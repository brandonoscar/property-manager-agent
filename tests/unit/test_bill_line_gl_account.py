"""The ledger category lives in the NESTED object, not the flat key.

Live-verified across all 131 mirrored bill lines (2026-08-28):

    GLAccountId    present 131/131, populated   0/131
    GLAccount.Id   present 131/131, populated 131/131

``upsert_bill`` wrote the flat field, so every mirrored line carried a NULL
category and ``spending_report``'s GL grouping rendered ONE bucket —
"Uncategorized" — for the whole payables book. Backfilled by migration
0078 (applied 2026-08-28: 131/131 categorised).

This is the SECOND column on THIS TABLE with the identical defect. 0077 did
property_id from ``AccountingEntity.Id`` a day earlier. Both are gotcha 53's
shape: **a Buildium field can be present as a KEY on every row and populated
on none, so check the VALUE, never the key.**

⚠ VALIDATION TRAP, and it is why this file names a second company. Blue Ridge
— the tenant every battery and probe runs against — books **67 of 67 lines to
"Bank Fees"**. A correct fix therefore produces one row there and reads as "it
didn't work". "My Company" carries the real spread (Cleaning and Maintenance
$8,496.00, Repairs $6,847.92, Insurance $2,787.44, and eight more). Validate
on data that can show the difference.
"""

from __future__ import annotations

import ast
import inspect

from helixis.buildium.schemas import BillLine
from helixis.db.repositories import entities


def _line(**raw) -> BillLine:
    return BillLine.model_validate({"Id": 1, "Amount": 265.0, **raw})


# ── the resolver ─────────────────────────────────────────────────────────


def test_the_nested_object_is_read_when_the_flat_key_is_null() -> None:
    """The production shape: flat key present and null, nested populated."""
    line = _line(GLAccountId=None, GLAccount={"Id": 4021, "Name": "Repairs"})
    assert line.gl_account_id is None, "the raw field is null on every production row"
    assert line.resolved_gl_account_id == 4021
    assert line.resolved_gl_account_name == "Repairs"


def test_the_flat_key_still_wins_when_buildium_populates_it() -> None:
    """⚠ Order matters. The flat field is checked FIRST so a future Buildium
    fix wins automatically — hard-switching to the nested object would make
    the day they fix it the day we break. Same reasoning as
    ``resolved_property_id``."""
    line = _line(GLAccountId=999, GLAccount={"Id": 4021, "Name": "Repairs"})
    assert line.resolved_gl_account_id == 999


def test_neither_present_resolves_to_none_not_zero() -> None:
    """Unknown must stay unknown. A 0 here would join to no GL account and
    render as a real category with a plausible-looking name."""
    line = _line()
    assert line.resolved_gl_account_id is None
    assert line.resolved_gl_account_name is None


def test_a_nested_object_with_no_id_is_not_a_category() -> None:
    line = _line(GLAccount={"Name": "Repairs"})
    assert line.resolved_gl_account_id is None
    # The name survives — it is what a manager reads, and having it without an
    # id is strictly more than having neither.
    assert line.resolved_gl_account_name == "Repairs"


def test_the_gl_resolver_does_not_copy_the_property_resolver_s_rental_guard() -> None:
    """⚠ The two resolvers are deliberately NOT symmetric. ``resolved_property_id``
    refuses a ``Company`` entity because a company id is not a property id and
    treating it as one invents a fact. A GL account is a GL account on an
    overhead line too, so guarding it here would drop real categories."""
    line = _line(GLAccount={"Id": 4021, "Name": "Bank Fees"})
    assert line.resolved_gl_account_id == 4021


# ── the importer ─────────────────────────────────────────────────────────


def test_upsert_bill_writes_the_resolved_id_not_the_raw_field() -> None:
    """⚠ Reads the SHIPPED source. The resolver being correct is worth nothing
    if the importer still passes ``line.gl_account_id`` — which is exactly the
    state this table was in for property_id until yesterday, with a working
    resolver nothing called.

    AST, not a substring: ``ruff format`` reflows these argument lists and a
    text scan then matches the wrong thing (gotcha 40).
    """
    tree = ast.parse(inspect.getsource(entities.upsert_bill))
    attrs = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "line"
    }
    assert "resolved_gl_account_id" in attrs, (
        "upsert_bill is not writing the resolved category — every mirrored "
        "line will carry a NULL gl_account_id and spend will read as "
        "'Uncategorized' for the whole book"
    )
    assert "gl_account_id" not in attrs, (
        "upsert_bill still passes the RAW gl_account_id, which Buildium leaves "
        "null on every production line"
    )
    # The sibling column must not regress while this one is edited.
    assert "resolved_property_id" in attrs and "property_id" not in attrs


def test_the_migration_only_fills_empty_rows() -> None:
    """A backfill that overwrites is a backfill you cannot re-run. If Buildium
    ever populates the flat key, the stored value is authoritative and a second
    run of 0078 must not clobber it."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[2]
        / "supabase"
        / "migrations"
        / "0078_bill_lines_gl_from_nested.sql"
    ).read_text(encoding="utf-8")
    assert "bl.gl_account_id IS NULL" in sql, "the backfill would overwrite real values"
    assert "GLAccount' ->> 'Id'" in sql, "the backfill reads the wrong key"
    # Company-scoped join (gotcha 47) — a bill_id is only unique within a company.
    assert "bl.company_id = sub.company_id" in sql
