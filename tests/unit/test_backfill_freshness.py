"""A stage that FAILED must never be stamped fresh.

``_record_stage_freshness`` writes per-table sync timestamps that the mirror
rollups surface to the model as "last synced". It used to decide which stages
had failed by parsing the human-readable error string —
``{e.split(":", 1)[0] for e in result.errors}`` — which is correct for
``"units: <exc>"`` and silently wrong for the three sites that qualify their
prefix: ``bills(unpaid)``, ``bills(paid)``, ``transactions(lease 123)``.

Neither qualified prefix ever equals ``"bills"`` or ``"transactions"``, so a
failed bills fetch stamped bills fresh. The mirror then told the model its
payables were current at a moment the fetch had errored — a fabricated
freshness claim on exactly the two stages already known to be incomplete
(gotcha 57: zero paid bills on file).

⚠ These guards read the SOURCE and drive the REAL function (gotchas 40/45).
A test feeding hand-written error strings to the old set-comprehension would
have passed forever: the comprehension was self-consistent and correct on its
own terms, and only the strings the module actually produces expose it.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from helixis.buildium import backfill as backfill_mod
from helixis.buildium.backfill import BackfillResult, _record_stage_freshness

_SOURCE = pathlib.Path(backfill_mod.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


# ── the behaviour ────────────────────────────────────────────────────


class _Recorder:
    """Captures every ``record_sync`` the freshness pass makes."""

    def __init__(self) -> None:
        self.stamped: list[str] = []

    async def __call__(self, _pool: Any, **kwargs: Any) -> None:
        self.stamped.append(kwargs["table_name"])


@pytest.fixture
def stamps(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from helixis.db.repositories import mirror_sync_state

    rec = _Recorder()
    monkeypatch.setattr(mirror_sync_state, "record_sync", rec)
    return rec


@pytest.mark.asyncio
async def test_a_qualified_failure_still_suppresses_its_stage(stamps: _Recorder) -> None:
    """The whole defect, in one assertion. ``bills(unpaid)`` is a BILLS
    failure, and before this the mirror stamped bills fresh anyway."""
    result = BackfillResult()
    result.record_failure("bills", RuntimeError("422 FromPaidDate"), detail="unpaid")

    await _record_stage_freshness(object(), company_id="c1", result=result)

    assert "bills" not in stamps.stamped, (
        "a failed bills fetch was stamped fresh — the mirror would tell the "
        "model its payables are current at the moment the fetch errored"
    )
    # Every OTHER stage still stamps: stage isolation is the design, and a
    # fix that suppressed everything would be a different bug wearing the
    # same green.
    assert "properties" in stamps.stamped
    assert "transactions" in stamps.stamped


@pytest.mark.asyncio
async def test_the_per_lease_transactions_failure_suppresses_transactions(
    stamps: _Recorder,
) -> None:
    """``transactions(lease 99797)`` is the other qualified prefix, and its
    detail carries a NUMBER — so any fix that special-cased the two literal
    bills suffixes would still get this one wrong."""
    result = BackfillResult()
    result.record_failure("transactions", RuntimeError("timeout"), detail="lease 99797")

    await _record_stage_freshness(object(), company_id="c1", result=result)

    assert "transactions" not in stamps.stamped
    assert "bills" in stamps.stamped


@pytest.mark.asyncio
async def test_a_plain_failure_suppresses_its_stage(stamps: _Recorder) -> None:
    """The case that always worked — pinned so a fix cannot trade one
    direction for the other (gotcha 38)."""
    result = BackfillResult()
    result.record_failure("units", RuntimeError("boom"))

    await _record_stage_freshness(object(), company_id="c1", result=result)

    assert "units" not in stamps.stamped
    assert "properties" in stamps.stamped


@pytest.mark.asyncio
async def test_a_clean_run_stamps_every_stage(stamps: _Recorder) -> None:
    await _record_stage_freshness(object(), company_id="c1", result=BackfillResult())
    assert len(stamps.stamped) == 14
    assert len(set(stamps.stamped)) == len(stamps.stamped)


def test_the_message_format_is_unchanged() -> None:
    """``errors`` is read by callers outside this module (the connector route
    surfaces the first five to the user). The stage travels BESIDE the
    message now, not instead of it."""
    result = BackfillResult()
    result.record_failure("bills", "422", detail="paid")
    result.record_failure("units", "boom")
    assert result.errors == ["bills(paid): 422", "units: boom"]
    assert result.failed_stages == {"bills", "units"}
    assert result.ok is False


# ── the source, so a new stage cannot reopen the hole ────────────────


def _recorded_stages() -> set[str]:
    """Every stage name passed to ``record_failure`` in the module source."""
    out: set[str] = set()
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record_failure"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.add(node.args[0].value)
    return out


def _stamped_stages() -> set[str]:
    """The stage tuple ``_record_stage_freshness`` iterates, read off the AST
    rather than duplicated here — a hand-copied list is one more thing that
    can drift from the code it claims to describe."""
    for node in ast.walk(_TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_record_stage_freshness":
            for inner in ast.walk(node):
                if isinstance(inner, ast.For) and isinstance(inner.iter, ast.Tuple):
                    return {
                        e.value
                        for e in inner.iter.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
    raise AssertionError("could not find the stage tuple — the scan is stale, not the code")


def test_every_recorded_stage_is_a_stage_the_freshness_pass_knows() -> None:
    """gotcha 28's both-sides rule. A stage recorded under a name the
    freshness loop never checks is a failure that stamps itself fresh — the
    same defect this file exists for, arriving by a typo instead of a
    parenthesis."""
    recorded, stamped = _recorded_stages(), _stamped_stages()
    assert recorded, "the AST scan found no record_failure call sites — it is broken, not clean"
    assert recorded <= stamped, f"recorded but never checked: {sorted(recorded - stamped)}"


def test_no_call_site_appends_to_errors_directly() -> None:
    """``record_failure`` is the only way in. A direct
    ``result.errors.append(...)`` records the message and NOT the stage, which
    is exactly the state this module was in — and it would be invisible,
    because the stage still stamps and nothing raises.

    ⚠ AST, not a substring scan (gotcha 40): the docstring above quotes the
    forbidden form, and a substring guard would fire on the sentence
    explaining the guard. A comment is never a ``Call`` node.
    """
    offenders = [
        node.lineno
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "errors"
        and not _inside_record_failure(node)
    ]
    assert not offenders, f"errors.append outside record_failure at line(s) {offenders}"


def _inside_record_failure(target: ast.AST) -> bool:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "record_failure":
            if any(child is target for child in ast.walk(node)):
                return True
    return False
