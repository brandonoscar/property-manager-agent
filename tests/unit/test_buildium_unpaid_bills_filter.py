"""``IsPaid`` is present-but-NULL on every production bill.

Live-verified across all 67 bills on account 695763 (2026-08-23): the key is
in the payload, the value is ``null``. The filter read
``getattr(b, "is_paid", True) is False``, so every row failed the test and the
tool reported **$0 against a real $26,000 exposure** — the very $0 that gotcha
25 fixed in ``bill_amount``, re-introduced one line earlier by the filter.

The ``True`` default was never the protection it looked like: the attribute
EXISTS, so the default never fires. Only the value is absent.

Fourth instance of one class — ``TotalAmount`` null (gotcha 25), ``Status``
null while ``TaskStatus`` carries it (gotcha 26), bill ownership living in
``Lines[].AccountingEntity.Id`` (gotcha 24), and now this. A present key is
not a present value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import Bill


def _keep(bills):
    """The shipped predicate. Kept in lockstep with buildium_tools by the
    wiring test below, so this file cannot quietly drift from the code."""
    return [b for b in bills if getattr(b, "is_paid", None) is not True]


def test_a_null_is_paid_is_kept() -> None:
    """THE regression. Production sends ``IsPaid: null``; dropping those rows
    is what produced $0 on a $26,000 account."""
    rows = [SimpleNamespace(id=i, is_paid=None) for i in range(67)]
    assert len(_keep(rows)) == 67


def test_an_explicitly_paid_bill_is_still_dropped() -> None:
    """The re-check must keep doing its job — this is a belt-and-braces guard
    against the API returning a paid row under paidstatus=Unpaid, not a
    licence to include everything."""
    rows = [
        SimpleNamespace(id=1, is_paid=False),
        SimpleNamespace(id=2, is_paid=True),
        SimpleNamespace(id=3, is_paid=None),
    ]
    assert [b.id for b in _keep(rows)] == [1, 3]


def test_a_missing_attribute_is_kept_too() -> None:
    """An older SDK model without the field at all must not empty the list
    either — the previous default silently claimed to cover this case."""
    assert len(_keep([SimpleNamespace(id=1)])) == 1


def test_the_shipped_filter_matches_this_one() -> None:
    """Gotcha 28's rule: read BOTH sides. If someone restores ``is False``,
    this fails rather than leaving the pin passing against a stale copy."""
    import inspect

    from helixis.agents import buildium_tools

    src = inspect.getsource(buildium_tools._list_unpaid_bills_impl)
    assert 'getattr(b, "is_paid", None) is not True' in src
    assert 'is_paid", True) is False' not in src, "the $0 filter is back"


# ── the property scope: the SAME class, one layer over ───────────────
#
# ⚠ ``PropertyId`` is present on every production bill LINE and populated on
# NONE of them (131/131 present, 0/131 populated, live-verified 2026-08-27).
# ``_passes`` compared the target property id against ``line.property_id``, so
# it compared a real integer against ``None`` for every bill and dropped them
# all — answering **"No unpaid bills found at <property>."** for a property
# with real money outstanding.
#
# Gotcha 53 fixed this for the MIRROR (`entities.upsert_bill` +
# migration 0077) and never reached this live agent tool, which reads the API
# directly. Gotcha 25's own rule — fix a field-shape bug at EVERY layer that
# reads it — applied to the fix for gotcha 25.


class _ClientCM:
    async def __aenter__(self) -> _ClientCM:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _bill(bill_id: int, amount: float, *, entity_id: int | None, entity_type: str = "Rental"):
    """A production-shaped bill: no top-level TotalAmount, no line PropertyId,
    ownership only on ``AccountingEntity``. Built through the real schema so
    the mock cannot drift from the wire shape it claims to imitate."""
    line: dict[str, object] = {"Amount": amount, "PropertyId": None}
    if entity_id is not None:
        line["AccountingEntity"] = {"Id": entity_id, "AccountingEntityType": entity_type}
    return Bill.model_validate({"Id": bill_id, "IsPaid": None, "Lines": [line]})


def _run(bills: list[Bill], properties: list[MagicMock], property_name: str | None):
    api = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)
    api.rentals.list_properties = AsyncMock(return_value=properties)
    with patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))):
        import asyncio

        return asyncio.run(bt._list_unpaid_bills_impl(property_name))


def _prop(pid: int, name: str) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.name = name
    p.address = None
    return p


def test_a_property_scoped_query_finds_its_bills() -> None:
    """THE regression. Ownership rides ``AccountingEntity.Id``; reading
    ``PropertyId`` matched nothing and reported a confident absence."""
    out = _run(
        [_bill(1, 265.0, entity_id=77), _bill(2, 900.0, entity_id=88)],
        [_prop(77, "Redbud Lane")],
        "Redbud",
    )
    assert "No unpaid bills" not in out, (
        "a property with real outstanding money was reported as having none — "
        "a fabricated absence, about money, stated with total confidence"
    )
    assert "1 unpaid bill" in out
    assert "265.00" in out


def test_bills_for_other_properties_are_still_excluded() -> None:
    """Both directions (gotcha 38). A fix that stopped filtering would answer
    every scoped question with the whole account and look just as green."""
    out = _run(
        [_bill(1, 265.0, entity_id=77), _bill(2, 900.0, entity_id=88)],
        [_prop(88, "Volt & Vine")],
        "Volt",
    )
    assert "1 unpaid bill" in out
    assert "900.00" in out
    assert "265" not in out


def test_a_company_overhead_line_is_not_attributed_to_a_property() -> None:
    """``AccountingEntityType`` is ``Company`` for overhead. Treating that id
    as a property id would attribute overhead to whatever property shares the
    number — inventing a fact rather than missing one (gotcha 53)."""
    out = _run(
        [_bill(1, 500.0, entity_id=77, entity_type="Company")],
        [_prop(77, "Redbud Lane")],
        "Redbud",
    )
    assert "No unpaid bills found at Redbud" in out


def test_no_attribution_anywhere_fails_open_and_says_so() -> None:
    """Gotcha 55. If not one line carries a property, the field cannot
    discriminate — return everything and label the scope as not held. An
    over-count is arguable and visible; an empty result is confident and
    wrong."""
    out = _run(
        [_bill(1, 265.0, entity_id=None), _bill(2, 900.0, entity_id=None)],
        [_prop(77, "Redbud Lane")],
        "Redbud",
    )
    assert "2 unpaid bills" in out
    assert "NOT scoped to Redbud" in out, (
        "an unscoped total presented under a scoped question is the confident "
        "false claim of gotcha 41, pointed at money"
    )
    assert "WHOLE account" in out


def test_an_unscoped_query_is_unchanged() -> None:
    out = _run([_bill(1, 265.0, entity_id=None)], [], None)
    assert "1 unpaid bill" in out
    assert "NOT scoped" not in out


def test_the_shipped_scope_reads_the_resolved_id() -> None:
    """Gotcha 28's both sides, and the pin that survives a rename: a future
    edit back to ``line.property_id`` restores a fabricated absence and
    nothing else in the suite would notice."""
    import inspect

    src = inspect.getsource(bt._bill_property_ids)
    assert "resolved_property_id" in src
    assert 'getattr(line, "property_id"' not in src, "the null-key read is back"


@pytest.mark.parametrize("entity_type", ["Rental", "rental", " Rental "])
def test_the_rental_check_is_forgiving_about_casing_and_space(entity_type: str) -> None:
    out = _run(
        [_bill(1, 265.0, entity_id=77, entity_type=entity_type)], [_prop(77, "Redbud")], "Redbud"
    )
    assert "1 unpaid bill" in out
