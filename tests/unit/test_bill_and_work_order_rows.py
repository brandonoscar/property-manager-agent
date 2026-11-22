"""A rollup that answers half a question sends the model to enumerate the rest.

Two list tools returned less than the question needed, and both are gotcha 50's
shape — *a rollup missing ONE field the question needs is an N+1 loop with extra
steps*:

- ``buildium_list_unpaid_bills`` returned ONE LINE — ``"12 unpaid bills, $8,400
  total."``. "Which ones are overdue?" and "how much do we owe Redbud?" are not
  answerable from it, so the only route to them is one read per bill. That is
  the fan-out ``PER_RECORD_CAPS`` refuses, and a cap converts a loop into a
  BLANK rather than an answer (gotcha 50: the 16:21 run refused 15 reads, hit
  the ceiling at 107s of its 240s, and produced zero characters).
- ``buildium_list_open_work_orders`` took NO arguments, so a keyword question
  had to be answered from a truncated list — the exact input that made the
  agent state a task "does not exist anywhere in the portfolio" while it sat in
  the hidden tail (gotcha 26, battery v2 q9).

Every field the bill rows needed was already on the objects we had fetched and
thrown away (gotchas 41/50/51/88/92/93/94/98/101/104/106/109's shape). The
vendor NAME is the one exception and is why ``vendor_names`` exists: a row
reading ``vendor 214`` re-creates the per-record loop one level over.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import Bill

TODAY = date(2026, 9, 13)


class _ClientCM:
    async def __aenter__(self) -> _ClientCM:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _bill(bill_id: int, amount: float | None, *, due: str | None, vendor_id: int | None = None):
    """A production-shaped bill, built through the real schema.

    ⚠ NO top-level ``TotalAmount`` — production carries none on any bill
    (0 of 67, gotcha 25), so a fixture that sets one would test a shape the
    wire never sends and hide the exact defect ``bill_amount`` exists for.
    """
    payload: dict[str, object] = {"Id": bill_id, "IsPaid": None}
    if amount is not None:
        payload["Lines"] = [{"Amount": amount, "PropertyId": None}]
    if due is not None:
        payload["DueDate"] = due
    if vendor_id is not None:
        payload["VendorId"] = vendor_id
    return Bill.model_validate(payload)


# ── bills: rows, not a summary ───────────────────────────────────────────


def test_every_bill_gets_a_row() -> None:
    """THE regression. A summary alone cannot answer "which ones", so the
    model opens bills one at a time until the cap kills the run."""
    out = bt._format_unpaid_bills(
        [_bill(1, 265.0, due="2026-09-01"), _bill(2, 900.0, due="2026-09-20")],
        TODAY,
        header="2 unpaid bills, $1,165.00 total:",
    )
    assert "[ref:1]" in out
    assert "[ref:2]" in out
    assert out.count("\n- ") == 2


def test_the_most_overdue_bill_is_first() -> None:
    """ "What's most urgent" has to be answerable by reading down the list."""
    out = bt._format_unpaid_bills(
        [
            _bill(1, 100.0, due="2026-09-20"),  # future
            _bill(2, 100.0, due="2026-07-01"),  # very overdue
            _bill(3, 100.0, due="2026-09-10"),  # just overdue
        ],
        TODAY,
        header="h:",
    )
    assert out.index("[ref:2]") < out.index("[ref:3]") < out.index("[ref:1]")


def test_a_bill_with_no_due_date_sorts_last_and_says_so() -> None:
    """Unknown must not render as a date, and must not jump the queue."""
    out = bt._format_unpaid_bills(
        [_bill(1, 100.0, due=None), _bill(2, 100.0, due="2026-09-10")],
        TODAY,
        header="h:",
    )
    assert out.index("[ref:2]") < out.index("[ref:1]")
    assert "no due date" in out


def test_a_missing_amount_is_stated_never_rendered_as_zero() -> None:
    """gotcha 25: a missing amount must never launder into a real-looking
    figure. ``$0.00`` on a bill is a claim about money that nothing supports."""
    out = bt._format_unpaid_bills([_bill(1, None, due="2026-09-01")], TODAY, header="h:")
    assert "amount not recorded" in out
    assert "$0.00" not in out


def test_the_row_carries_the_money_not_just_the_header() -> None:
    """⚠ A MUTATION FOUND THIS HOLE AND REVIEW DID NOT (gotcha 58, answered
    "the test is blind"). Pointing the row at ``b.total_amount`` instead of
    ``bill_amount(b)`` makes EVERY production row read "amount not recorded" —
    ``TotalAmount`` is absent on all 67 — and the whole battery stayed green,
    because the summary line above the rows is computed separately and still
    carried the figure. Every existing money assertion was satisfied by the
    HEADER while the rows it was supposed to guard had gone blank.

    So assert on the row specifically: the money has to be where the model
    reads it per bill, not only in a total it cannot attribute."""
    out = bt._format_unpaid_bills([_bill(1, 265.0, due="2026-09-01")], TODAY, header="h:")
    row = next(line for line in out.splitlines() if line.startswith("- "))
    assert "$265.00" in row
    assert "amount not recorded" not in row


def test_overdue_days_are_counted_from_the_caller_s_today() -> None:
    out = bt._format_unpaid_bills([_bill(1, 100.0, due="2026-09-01")], TODAY, header="h:")
    assert "overdue 12d" in out


def test_a_bill_due_today_is_not_called_overdue() -> None:
    """Both directions (gotcha 38). gotcha 91 is the live instance of this
    exact off-by-one: a bill due TODAY reported as OVERDUE, about money."""
    out = bt._format_unpaid_bills([_bill(1, 100.0, due="2026-09-13")], TODAY, header="h:")
    assert "due today" in out
    assert "overdue" not in out


# ── bills: the vendor name, and what happens without it ──────────────────


def test_a_row_names_the_vendor_when_we_know_it() -> None:
    out = bt._format_unpaid_bills(
        [_bill(1, 265.0, due="2026-09-01", vendor_id=214)],
        TODAY,
        vendor_names={214: "Redbud Landscaping"},
        header="h:",
    )
    assert "Redbud Landscaping" in out


def test_an_unknown_vendor_is_words_never_a_bare_id() -> None:
    """A bare ``vendor 214`` is a number the model looks up — which is the
    per-record fan-out this whole change removes, arriving one level over."""
    out = bt._format_unpaid_bills(
        [_bill(1, 265.0, due="2026-09-01", vendor_id=214)],
        TODAY,
        vendor_names={},
        header="h:",
    )
    assert "vendor not recorded" in out
    assert "214" not in out


# ── bills: the impl wiring ───────────────────────────────────────────────


def _prop(pid: int, name: str) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.name = name
    p.address = None
    return p


def _vendor(vid: int, name: str) -> MagicMock:
    v = MagicMock()
    v.id = vid
    v.company_name = name
    return v


def _run_bills(bills, vendors=None, *, vendors_raise: bool = False):
    api = MagicMock()
    api.bills.list = AsyncMock(return_value=bills)
    api.rentals.list_properties = AsyncMock(return_value=[])
    if vendors_raise:
        from helixis.buildium.errors import BuildiumApiError

        api.vendors.list = AsyncMock(side_effect=BuildiumApiError(500, "boom"))
    else:
        api.vendors.list = AsyncMock(return_value=vendors or [])
    with patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))):
        return asyncio.run(bt._list_unpaid_bills_impl()), api


def test_vendor_names_cost_one_call_however_many_bills() -> None:
    """The bulk fetch is the whole point: per-bill vendor resolution is the
    loop being removed, not a smaller version of it."""
    out, api = _run_bills(
        [_bill(i, 100.0, due="2026-09-01", vendor_id=214) for i in range(1, 13)],
        [_vendor(214, "Redbud Landscaping")],
    )
    assert api.vendors.list.await_count == 1
    assert out.count("Redbud Landscaping") == 12


def test_a_failing_vendor_lookup_costs_the_names_never_the_bills() -> None:
    """gotcha 7: an enhancement is never load-bearing. The bills are the
    answer; the names are a nicety, and losing them must not lose the answer."""
    out, _ = _run_bills([_bill(1, 265.0, due="2026-09-01", vendor_id=214)], vendors_raise=True)
    assert "265.00" in out
    assert "[ref:1]" in out
    assert "vendor not recorded" in out


def test_no_vendor_ids_means_no_vendor_call_at_all() -> None:
    """Do not spend a round trip resolving names nothing will use."""
    _, api = _run_bills([_bill(1, 265.0, due="2026-09-01")])
    assert api.vendors.list.await_count == 0


def test_the_summary_line_survives_above_the_rows() -> None:
    """The count and total were the whole result and are still the headline —
    the rows are additive, so a caller that only read the first line is not
    broken by this."""
    out, _ = _run_bills([_bill(1, 265.0, due="2026-09-01"), _bill(2, 900.0, due="2026-09-02")])
    assert out.splitlines()[0].startswith("2 unpaid bills, $1,165.00 total")


# ── work orders: the filters ─────────────────────────────────────────────


def _wo(wo_id: int, title: str, *, property_id: int | None = None, due: str | None = None):
    return SimpleNamespace(
        id=wo_id,
        title=title,
        description="",
        property_id=property_id,
        unit_id=None,
        priority=None,
        due_date=date.fromisoformat(due) if due else None,
        effective_due_date=None,
    )


def test_a_search_matches_all_words_in_any_order() -> None:
    """``_task_matches``' contract, and for its reason: the model folds the
    place into the phrase, and a verbatim-phrase match then reports a real
    record as absent while claiming it searched everything."""
    wo = _wo(1, "URGENT: water heater leaking into the closet")
    assert bt._work_order_matches(wo, "water heater")
    assert bt._work_order_matches(wo, "heater water")
    assert not bt._work_order_matches(wo, "roof")


def test_the_location_is_in_the_search_haystack() -> None:
    """ "the water heater at Montgomery" must match — the property label is not
    on the work order, so without this the phrase can never hit."""
    wo = _wo(1, "water heater leaking")
    assert bt._work_order_matches(wo, "Montgomery water heater", location="120 4th St, Montgomery")
    assert not bt._work_order_matches(wo, "Montgomery water heater")


def test_the_impl_actually_hands_the_location_to_the_matcher() -> None:
    """⚠ A MUTATION FOUND THIS AND THE TEST ABOVE COULD NEVER HAVE.

    Dropping ``location=`` at the call site left the whole battery green: the
    helper is exercised DIRECTLY, so it is thoroughly covered and its one
    production caller was invisible to it — gotchas 62/66/69/103, the fourth
    recorded instance of a well-tested helper a route quietly ignores.

    This drives the real impl with a phrase carrying a place name, which is
    the input the two spellings disagree on and the one a model actually
    sends (it folds the location into the phrase however the docstring is
    worded)."""
    out = _run_work_orders(
        [_wo(1, "water heater leaking", property_id=77), _wo(2, "roof", property_id=88)],
        [_prop(77, "120 4th St, Montgomery"), _prop(88, "Redbud Lane")],
        search="Montgomery water heater",
    )
    assert "[ref:1]" in out
    assert "[ref:2]" not in out


def _run_work_orders(work_orders, properties=None, *, search="", property_name=""):
    api = MagicMock()
    api.work_orders.list = AsyncMock(return_value=work_orders)
    api.rentals.list_properties = AsyncMock(return_value=properties or [])
    with patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))):
        return asyncio.run(bt._list_open_work_orders_impl(search, property_name))


def test_an_empty_filtered_result_says_what_it_searched() -> None:
    """gotcha 26. A filtered miss is the only honest "no" this tool can give,
    and it has to name the set it ran over or the answer reads as a
    portfolio-wide absence when it was never one."""
    out = _run_work_orders([_wo(1, "roof leak"), _wo(2, "gutter clean")], search="water heater")
    assert "searched all 2 open" in out
    assert "matching 'water heater'" in out


def test_an_empty_unfiltered_result_keeps_the_plain_sentence() -> None:
    """Both directions. "Searched all 0" would be a strange thing to tell
    somebody whose portfolio simply has nothing open."""
    out = _run_work_orders([])
    assert out == "No open work orders in Buildium right now."


def test_a_search_narrows_and_the_header_says_so() -> None:
    out = _run_work_orders(
        [_wo(1, "water heater leaking"), _wo(2, "roof shingles")], search="water heater"
    )
    assert "[ref:1]" in out
    assert "[ref:2]" not in out
    assert "matching 'water heater'" in out


def test_a_property_scope_filters_client_side() -> None:
    """Production silently IGNORES ``?propertyids`` on some endpoints and
    returns the whole account, 200, no error (gotcha 24) — so the filter has
    to run here, on rows the server may not have narrowed at all."""
    out = _run_work_orders(
        [_wo(1, "leak", property_id=77), _wo(2, "leak", property_id=88)],
        [_prop(77, "Redbud Lane"), _prop(88, "Volt & Vine")],
        property_name="Redbud",
    )
    assert "[ref:1]" in out
    assert "[ref:2]" not in out


def test_a_blind_property_column_fails_open() -> None:
    """gotcha 55, and it is the whole reason this scopes on ``property_id``
    rather than ``unit_id``. If NO row carries the field, filtering on it
    cannot discriminate — so it returns everything rather than nothing. An
    over-count is arguable and visible; a false absence is confident, wrong,
    and is this product's worst failure mode."""
    out = _run_work_orders(
        [_wo(1, "leak"), _wo(2, "leak")],  # property_id None on every row
        [_prop(77, "Redbud Lane")],
        property_name="Redbud",
    )
    assert "[ref:1]" in out
    assert "[ref:2]" in out
    assert "No open work orders" not in out


def test_an_unknown_property_name_is_honest() -> None:
    out = _run_work_orders(
        [_wo(1, "leak", property_id=77)], [_prop(77, "Redbud")], property_name="Garden Row"
    )
    assert "couldn't find a property matching 'Garden Row'" in out


def test_an_unfiltered_call_is_byte_for_byte_what_it_was() -> None:
    """The filters are additive. Every caller that passes nothing — which is
    every existing one — must get exactly today's result."""
    rows = [_wo(1, "leak", property_id=77, due="2026-09-01")]
    today = bt.datetime.now(bt.UTC).date()
    assert _run_work_orders(rows) == bt._format_open_work_orders(rows, today)


# ── the truncation footer names parameters that EXIST ────────────────────


def test_the_truncation_hint_names_real_parameters() -> None:
    """``_truncate_footer``'s own note: do not promise a narrowing parameter
    the tool does not have, or the model invents one. It has both now, and
    this reads BOTH sides (gotcha 28) so the hint cannot outlive them."""
    out = bt._format_open_work_orders([_wo(i, "leak") for i in range(300)], TODAY)
    assert "search=" in out
    assert "property_name=" in out
    params = inspect.signature(bt._list_open_work_orders_impl).parameters
    assert {"search", "property_name"} <= set(params)


def test_the_truncation_footer_forbids_concluding_absence() -> None:
    """A truncated list is NEVER evidence of absence (gotcha 26)."""
    out = bt._format_open_work_orders([_wo(i, "leak") for i in range(300)], TODAY)
    assert "TRUNCATED" in out
