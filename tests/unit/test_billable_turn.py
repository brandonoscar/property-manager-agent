"""A turn that gave the user nothing must not appear on their invoice.

⚠ **THE DEFECT THIS EXISTS FOR WAS A DOCSTRING THAT CITED THE RULE AND A QUERY
THAT DID NOT IMPLEMENT IT.** `billing/gate.py::_turns_used` explained, at
length, that a run is blank only when `final_text` is empty AND no CARD tool
ran (gotcha 56) — and then counted `jsonb_array_length(tool_calls) > 0`, i.e.
any tool at all. A deadline death that spent four minutes on specialist
consults and returned zero characters satisfies that, so the population gotcha
37 measured at **22% of runs** was billed by the query quoting the paragraph
that forbids it. Gotcha 48: a comment cannot fail.

The correct implementation already existed, once, in `evals/cost_drift.answered`
— a module about measurement, not money. Gotcha 25/67's fan-out: a rule
documented at its first caller, invisible to the second, and the second was the
one deciding what a customer pays.

⚠ **THE PRE-EXISTING GUARD PASSED AGAINST THE DEFECT AND IS RE-POINTED, NOT
DELETED** (gotchas 68/74/93/103/110). `test_a_blank_turn_is_not_billable`
asserted `"final_text" in sql and "tool_calls" in sql` — both strings are in
the broken query, so the assertion was satisfied by the bug it was written to
catch. Gotcha 40's substring trap, inside the guard for this exact property.
It asserts the named-set form now.

⚠ **NO DATABASE THIS SESSION**, so nothing here proves Postgres evaluates the
`exists (… jsonb_array_elements …)` predicate as intended — stated rather than
implied (gotcha 106's precedent). What IS measured: the query names the set
rather than counting anything that ran, it BINDS that set rather than carrying
a second copy of the names, both consumers read one set, and the set stays in
step with the tools that actually render.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.billing.gate import _turns_used
from helixis.common.answered import CARD_TOOLS, turn_answered
from tests.unit._pg_bind_contract import reject_unbindable

_SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"
_GATE = _SRC / "billing" / "gate.py"
_VISUAL_TOOLS = _SRC / "agents" / "visual_tools.py"

#: Visual tools deliberately outside the billable-answer set. Each one DOES put
#: something on screen, so leaving it out under-counts — the customer-favouring
#: direction, and the one `billing/gate.py` takes everywhere (its rule 1).
#: Adding either also widens the cost-drift latency sample, which moves a
#: measurement baseline; that is a decision to take with the numbers in front
#: of you, not a side effect of a billing fix (gotcha 58: say which gaps are
#: choices).
_DELIBERATELY_NOT_BILLABLE = {
    "request_details": "renders a form; answering it is the user's next turn, not this one",
}


def _sql_of(function: str, path: Path) -> str:
    """The SQL literal inside ``function``.

    ⚠ Scoped to the FUNCTION, never the file. `_turns_used`'s docstring now
    quotes `jsonb_array_length(tool_calls) > 0` in order to explain why it was
    wrong, so a whole-file scan fires on the sentence documenting the fix —
    gotchas 40/59/91/114, arriving in SQL for the third time.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function:
            found = [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and "orchestrator_runs" in n.value
            ]
            assert found, f"no SQL literal inside {function} — has the query moved?"
            return "\n".join(found)
    raise AssertionError(f"{function} not found in {path.name}")


def _function_tools(path: Path) -> set[str]:
    """Every ``@function_tool``-decorated name in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "function_tool":
                out.add(node.name)
    return out


# ── the rule itself ───────────────────────────────────────────────────────


def test_a_run_that_only_consulted_a_specialist_is_not_answered() -> None:
    """The 22% population, stated as an input. Four minutes of `buildium_agent`
    and `gmail_agent` with nothing streamed is a deadline death (gotcha 92
    measured both of the trailing week's failures at exactly this), and the old
    any-tool test called it a billable turn."""
    assert turn_answered(has_prose=False, tool_names=("buildium_agent", "gmail_agent")) is False


def test_a_card_only_turn_is_answered() -> None:
    """Cards never reach `final_text` (gotcha 56 measured it on five of six
    questions), so prose-only counting drops exactly the turns that worked."""
    assert turn_answered(has_prose=False, tool_names=("show_table",)) is True


def test_prose_answers_on_its_own() -> None:
    assert turn_answered(has_prose=True, tool_names=()) is True


def test_a_run_that_did_nothing_at_all_is_not_answered() -> None:
    assert turn_answered(has_prose=False, tool_names=()) is False


# ── the query ─────────────────────────────────────────────────────────────


def test_the_billing_query_names_card_tools_instead_of_counting_any_tool() -> None:
    sql = _sql_of("_turns_used", _GATE)
    assert "jsonb_array_length" not in sql, (
        "`jsonb_array_length(tool_calls) > 0` counts ANY tool, so a deadline "
        "death that only made specialist consults bills as a real turn. The "
        "second arm has to name the tools whose output reaches the user."
    )
    assert "->> 'name'" in sql, "the predicate must read each call's name"
    assert "company_id = $1" in sql, "gotcha 47: this table is multi-tenant"


@pytest.mark.asyncio
async def test_the_query_binds_the_shared_set_rather_than_naming_tools_inline() -> None:
    """A hardcoded name list in the SQL is a second copy of the set one import
    away, and its failure mode is silent in the expensive direction: a renamed
    card tool stops matching and every card-only turn starts billing as blank.
    """
    captured: dict[str, object] = {}
    pool = MagicMock()

    async def _fetchrow(sql, *args):
        reject_unbindable(sql, args)
        captured["sql"] = sql
        captured["args"] = args
        return {"n": 3}

    pool.fetchrow = AsyncMock(side_effect=_fetchrow)
    with patch("helixis.billing.gate.db_engine.get_pool", return_value=pool):
        used = await _turns_used("c1", datetime.now(UTC))

    assert used == 3
    assert captured["args"][2] == sorted(CARD_TOOLS), (
        "the card set must arrive as a BOUND parameter, so there is one place "
        "a new card tool has to be added"
    )
    sql = str(captured["sql"])
    for name in CARD_TOOLS:
        assert name not in sql, f"{name!r} is spelled into the SQL — that is the second copy"


# ── one set, two consumers ────────────────────────────────────────────────


def test_the_latency_sample_and_the_meter_read_the_same_set() -> None:
    """`evals/cost_drift` re-exports rather than redefining. Two sets that
    agree today are two sets that disagree the day somebody adds a card tool to
    one of them — and the disagreement is invisible, because each is internally
    consistent (gotcha 28's both-sides rule, applied to a constant)."""
    from helixis.evals import cost_drift

    assert cost_drift.CARD_TOOLS is CARD_TOOLS
    assert cost_drift.answered(cost_drift.RunRecord(latency_ms=1, tool_names=("show_table",)))
    assert not cost_drift.answered(
        cost_drift.RunRecord(latency_ms=1, tool_names=("buildium_agent",))
    )


# ── the corpus ────────────────────────────────────────────────────────────


def test_every_visual_tool_is_either_billable_or_a_named_exemption() -> None:
    """The failure mode is "silently bills more" in one direction and
    "silently bills less" in the other, so this reads the CORPUS rather than a
    hand-typed list (gotcha 45). A ninth visual tool added without a decision
    fails here instead of quietly changing what a customer pays."""
    tools = _function_tools(_VISUAL_TOOLS)
    assert len(tools) >= 7, (
        f"only {len(tools)} @function_tool definitions found in visual_tools.py — "
        "a scan that finds too little is a claim to check, not a result "
        "(gotchas 49/100/101/114)"
    )
    unaccounted = tools - CARD_TOOLS - set(_DELIBERATELY_NOT_BILLABLE)
    assert not unaccounted, (
        f"{sorted(unaccounted)} render something for the user and are neither "
        "counted as an answer nor exempted with a reason. Decide which, here."
    )


def test_no_exemption_names_a_tool_that_no_longer_exists() -> None:
    """A stale exemption is a licence nobody needs that makes the rule read as
    more permissive than it is (gotcha 63, which caught its own author)."""
    stale = set(_DELIBERATELY_NOT_BILLABLE) - _function_tools(_VISUAL_TOOLS)
    assert not stale, f"{sorted(stale)} is exempted and is not a visual tool any more"


def test_the_set_names_no_tool_that_does_not_exist() -> None:
    """The other direction: a name in `CARD_TOOLS` that nothing registers can
    never match, so the turns it was meant to cover bill as blank — and the set
    still reads complete."""
    missing = CARD_TOOLS - _function_tools(_VISUAL_TOOLS)
    assert not missing, f"{sorted(missing)} is counted as an answer and is not a live tool"
