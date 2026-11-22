"""The two reads a property manager asks for and the product could not answer.

``buildium_list_unpaid_bills`` could say a bill was $2,400 and nothing could
say WHY — no tool opened a bill's lines, so "what GL account did that hit?" and
"which property does this belong to?" had no route at all. And nothing could
answer "what's in the operating account?": the bank-accounts endpoint has
existed since 2026-07-06 and had no agent tool.

Every money read here is a re-test of the field-shape family, because the raw
attributes on a Buildium bill LOOK populated and are not:

- ``TotalAmount`` absent on every production bill; the money is in the lines
  (gotcha 25 — the $0 that was reported against a real $26,000 exposure).
- ``IsPaid`` present-and-NULL on all 131 mirrored rows; ``PaidDate`` carries
  the answer (gotcha 57).
- ``GLAccountId`` / ``PropertyId`` present on every line and populated on none;
  the values live on the nested refs (gotcha 53).

A tool reading any of them directly renders a confident wrong number about
money, which is the exact defect this drill-down exists to make visible.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import BankAccount, Bill


class _ClientCM:
    async def __aenter__(self) -> _ClientCM:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


# ── the bill drill-down ──────────────────────────────────────────────────


def _bill(**overrides):
    """Production-shaped: no top-level total, no flat line ids, ownership and
    the GL account only on the nested refs."""
    payload: dict[str, object] = {
        "Id": 4821,
        "IsPaid": None,
        "Lines": [
            {
                "Amount": 1500.0,
                "PropertyId": None,
                "GLAccountId": None,
                "GLAccount": {"Id": 4120, "Name": "Repairs & Maintenance"},
                "AccountingEntity": {"Id": 77, "AccountingEntityType": "Rental"},
                "Memo": "roof patch",
            },
            {
                "Amount": 900.0,
                "PropertyId": None,
                "GLAccountId": None,
                "AccountingEntity": {"Id": 77, "AccountingEntityType": "Rental"},
            },
        ],
    }
    payload.update(overrides)
    return Bill.model_validate(payload)


def test_the_total_is_summed_from_the_lines() -> None:
    """gotcha 25. ``TotalAmount`` is absent on every production bill, so a
    tool reading it renders $0.00 — on the surface whose whole job is tracing
    a number to its parts."""
    out = bt._format_bill_detail(_bill())
    assert "$2,400.00" in out
    assert "$0.00" not in out


def test_each_line_is_shown_with_its_own_amount() -> None:
    out = bt._format_bill_detail(_bill())
    assert "2 line(s)" in out
    assert "$1,500.00" in out
    assert "$900.00" in out


def test_the_gl_account_is_read_off_the_nested_ref() -> None:
    """gotcha 53: ``GLAccountId`` is a present key with a null value on every
    production line. Reading it directly shows nothing on a line that does
    have an account."""
    out = bt._format_bill_detail(_bill())
    assert "Repairs & Maintenance" in out


def test_the_property_is_read_off_the_accounting_entity() -> None:
    """Same class, same gotcha. ``PropertyId`` is null 131/131; ownership is
    ``AccountingEntity.Id`` on ``Rental`` lines."""
    out = bt._format_bill_detail(_bill())
    assert "property 77" in out


def test_a_paid_bill_is_read_off_paid_date_not_is_paid() -> None:
    """gotcha 57: ``IsPaid`` is null even on bills that were paid. A bill the
    company already settled, reported as outstanding, is a wrong number about
    money on the screen where somebody decides whether to pay it again."""
    # Field-split, not a substring — "UNPAID" contains "PAID", so the naive
    # positive is satisfied by its own opposite (see the sibling test below).
    out = bt._format_bill_detail(_bill(PaidDate="2026-08-14T00:00:00"))
    header_fields = [f.strip() for f in out.splitlines()[0].split("—")]
    assert "PAID 2026-08-14" in header_fields
    assert "UNPAID" not in header_fields


def test_a_null_is_paid_with_no_paid_date_reads_unpaid() -> None:
    """The other direction, and this test replaced one whose PREMISE WAS
    FALSE. I first asserted a third "paid status not recorded" state; there is
    none — ``bill_is_paid`` is annotated ``bool | None`` and structurally
    cannot return None, so that arm was dead code (gotcha 58) and the test
    would have pinned a distinction that does not exist. Checking the premise
    is what found it: an assertion holds a defect in place as firmly as it
    catches one (gotchas 68/74/93/103/110).

    UNPAID is also the RIGHT reading — ``bill_is_paid`` owns this question and
    documents null-PaidDate as unpaid, and inventing a second answer here is
    exactly the two-homes drift gotcha 25 exists to stop."""
    # ⚠ Split on the separator rather than substring-testing for "PAID" —
    # "UNPAID" CONTAINS it, so the naive negative passes against the bug it
    # names and the naive positive fires on its own opposite (gotcha 40, one
    # more time, inside a guard written for a money field).
    out = bt._format_bill_detail(_bill())
    header_fields = [f.strip() for f in out.splitlines()[0].split("—")]
    assert "UNPAID" in header_fields
    assert "PAID" not in header_fields


def test_the_paid_reading_comes_from_the_helper_not_the_raw_flag() -> None:
    """gotcha 28, both sides. ``IsPaid`` is present-and-null on every
    production bill, so a formatter reading it directly is right by accident
    on the unpaid ones and wrong on every paid one."""
    import inspect

    src = inspect.getsource(bt._format_bill_detail)
    assert "bill_is_paid(bill)" in src
    assert 'getattr(bill, "is_paid"' not in src


def test_an_absent_lines_array_is_not_an_empty_bill() -> None:
    """gotcha 26's absence claim, pointed at money. A response that did not
    carry the lines is not a bill with no lines — and the total above the
    detail is DERIVED from them, so calling it empty contradicts the header."""
    out = bt._format_bill_detail(Bill.model_validate({"Id": 1, "IsPaid": None}))
    assert "no line detail on this record" in out
    assert "0 line(s)" not in out


def test_an_unpriced_line_is_stated_never_zero() -> None:
    bill = Bill.model_validate({"Id": 1, "IsPaid": None, "Lines": [{"Amount": None}]})
    out = bt._format_bill_detail(bill)
    assert "unpriced" in out
    assert "$0.00" not in out


def test_the_detail_points_at_the_payments_tool() -> None:
    """The mirror carries NO paid bills (gotcha 57), so payment history is
    only reachable live — and a drill-down that does not say where it ends is
    where the model starts guessing."""
    out = bt._format_bill_detail(_bill())
    assert "buildium_list_bill_payments" in out
    assert "bill_id=4821" in out


def test_the_impl_reads_the_bill_it_was_asked_for() -> None:
    api = MagicMock()
    api.bills.get = AsyncMock(return_value=_bill())
    with patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))):
        out = asyncio.run(bt._get_bill_impl(4821))
    api.bills.get.assert_awaited_once_with(4821)
    assert "Bill #4821" in out


# ── bank accounts ────────────────────────────────────────────────────────


def _account(**overrides):
    payload: dict[str, object] = {
        "Id": 12,
        "Name": "Operating",
        "BankAccountType": "Checking",
        "Balance": 48215.44,
        "IsActive": True,
    }
    payload.update(overrides)
    return BankAccount.model_validate(payload)


def test_an_account_renders_its_name_type_and_balance() -> None:
    out = bt._format_bank_accounts([_account()])
    assert "Operating" in out
    assert "Checking" in out
    assert "$48,215.44" in out


def test_no_account_or_routing_number_can_reach_the_reply() -> None:
    """⚠ NOT an oversight to fix later. ``BankAccount`` leaves those fields
    UNTYPED on purpose and the mirror strips them from ``raw`` before persist
    — no bank credentials at rest. A model reply is the one surface they must
    never reach, and ``extra='allow'`` means the wire values ARE on the object
    if the response carried them."""
    acct = BankAccount.model_validate(
        {
            "Id": 12,
            "Name": "Operating",
            "Balance": 1.0,
            "AccountNumber": "****6789",
            "AccountNumberUnmasked": "123456789",
            "RoutingNumber": "021000021",
        }
    )
    out = bt._format_bank_accounts([acct])
    assert "123456789" not in out
    assert "021000021" not in out
    assert "6789" not in out


def test_a_missing_balance_is_stated_never_zero() -> None:
    """The same rule as every other money read here (gotcha 25). "$0.00" in
    an operating account is a sentence somebody acts on."""
    out = bt._format_bank_accounts([_account(Balance=None)])
    assert "balance not reported" in out
    assert "$0.00" not in out


def test_inactive_accounts_are_hidden_and_counted() -> None:
    """Hidden is fine; hidden and unmentioned is gotcha 26 — the reader has no
    way to tell a filtered list from the whole set."""
    out = bt._format_bank_accounts([_account(), _account(Id=13, Name="Closed", IsActive=False)])
    assert "Closed" not in out
    assert "1 inactive account(s) not shown" in out


def test_an_all_inactive_account_set_is_shown_rather_than_reported_empty() -> None:
    """gotcha 55's fail-open, pointed at a filter: "no bank accounts" for a
    company that has them is a confident false claim, and the row says
    INACTIVE so the reader is not misled the other way either."""
    out = bt._format_bank_accounts([_account(IsActive=False)])
    assert "Operating" in out
    assert "INACTIVE" in out


def test_an_empty_account_list_is_honest() -> None:
    assert "No bank accounts found" in bt._format_bank_accounts([])


def test_the_impl_renders_what_the_endpoint_returned() -> None:
    api = MagicMock()
    api.bank_accounts.list = AsyncMock(return_value=[_account()])
    with patch.object(bt, "_open_api", AsyncMock(return_value=(_ClientCM(), api, None))):
        out = asyncio.run(bt._list_bank_accounts_impl())
    assert "Operating" in out


# ── both tools are registered and budgeted ───────────────────────────────


def test_both_tools_are_registered() -> None:
    """A tool nothing registers is a tool the model cannot reach — the
    ``comms_triage`` failure (gotcha 62), one registry over."""
    names = {t.name for t in bt.buildium_tools()}
    assert {"buildium_get_bill", "buildium_list_bank_accounts"} <= names


def test_the_per_record_read_is_capped_and_the_bulk_one_is_not() -> None:
    """gotcha 25's stated rule: adding a per-record tool means adding a
    ``PER_RECORD_CAPS`` entry, because a cap is the only thing that has ever
    stopped the N+1 loop. ⚠ And the bulk one must NOT be capped — capping a
    tool that answers a portfolio question in one call would refuse the
    behaviour the cap exists to steer toward."""
    from helixis.agents.call_budget import PER_RECORD_CAPS

    assert "buildium_get_bill" in PER_RECORD_CAPS
    assert "buildium_list_bank_accounts" not in PER_RECORD_CAPS


def test_a_stub_shaped_object_never_crashes_the_render() -> None:
    """A read tool is not load-bearing enough to take a turn down (gotcha 7).
    The formatters use getattr throughout; this is the proof, not the claim."""
    bare = SimpleNamespace(id=9)
    assert "Bill #9" in bt._format_bill_detail(bare)
    assert "[ref:9]" in bt._format_bank_accounts([bare])


def test_the_two_balance_tools_each_point_at_the_other() -> None:
    """⚠ A WELL-DOCUMENTED TOOL BEATS A ONE-LINE TOOL ON OVERLAPPING GROUND,
    and that is what shipping this one did.

    `buildium_list_bank_accounts` arrived with a description claiming "what's
    in the operating account?" and "how much cash do we have?"; the incumbent
    `buildium_get_gl_account_balances` said, in full, "Show general-ledger
    account balances as of a date." Golden 054 asks for balances on the
    "general ledger bank accounts" — its own note records that anchor phrase as
    what made the case reliable — and the model went to the new tool. CI run
    2111 named it in one line, which is the whole reason it was diagnosable.

    The two answer genuinely different questions (the ledger view at any date
    versus the banking view right now), so the fix is that each says which it
    owns. A pointer added to one side only is the shape gotchas 67/86/102 each
    record: the rule reaches one caller and not the other, and nothing fails.

    ⚠ READ FROM THE SOURCE AND THE SNAPSHOT, never from ``__doc__`` on the tool
    object. Under the real SDK ``@function_tool`` returns a ``FunctionTool``
    whose text lives on ``.description``, so a ``__doc__`` assertion passes
    against this container's stub and says nothing about CI (gotcha 117's trap
    from the other side). The snapshot is what actually reaches the model, so
    checking both is gotcha 28's both-sides rule.
    """
    import ast

    source = pathlib.Path("src/helixis/agents/buildium_tools.py").read_text(encoding="utf-8")
    docs = {
        n.name: (ast.get_docstring(n) or "")
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    snapshot = json.loads(
        pathlib.Path("tests/snapshots/buildium_tool_schemas.json").read_text(encoding="utf-8")
    )

    for surface, text_of in (
        ("source docstring", lambda k: docs[k]),
        ("schema snapshot", lambda k: snapshot[k]["description"]),
    ):
        gl = text_of("buildium_get_gl_account_balances").lower()
        bank = text_of("buildium_list_bank_accounts").lower()
        assert "buildium_list_bank_accounts" in gl, (
            f"[{surface}] the ledger tool must hand \"what's in the operating "
            'account" to the banking tool — otherwise the pointer exists on '
            "one side only"
        )
        assert "buildium_get_gl_account_balances" in bank, (
            f"[{surface}] the banking tool must hand general-ledger and "
            "as-of-date questions back, which is the half that keeps golden "
            "054 anchored"
        )
        assert "general ledger" in gl, f"[{surface}] the ledger tool must name the phrase it owns"
