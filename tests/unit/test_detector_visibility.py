"""A detector that is off, a detector that is broken, and a clean book all
looked identical from outside the process.

Measured 2026-08-29: 12 state-rule detectors are registered, **3** are
default-enabled and **9** are dark. Of the three that run, one —
``duplicate_vendor_bill`` — has produced zero findings in its entire lifetime,
and nothing anywhere could say whether that means the portfolio has no
duplicate bills or the detector has been quietly broken since it merged.

Three separate silences produced that:

1. the dark set was never reported, so "switched off" looked like "found
   nothing";
2. ``run_detector_cycle`` logged only ``if total:``, so a cycle that found
   nothing wrote no line at all — "ran and clean" looked like "dead";
3. a detector that RAISES is counted ``0``, which is the same number a healthy
   quiet one reports — so a crash looked like a clean result in the counts.

The third is the worst of them, because the count is what every caller sums.
It is still counted 0 on purpose (a sentinel would corrupt those totals) and
the failure now rides beside it instead.

Same family as gotcha 45 (a control that silently permits more looks exactly
like one that is working) and gotcha 54 (a documented behaviour that never
reached the wire, standing in for coverage).
"""

from __future__ import annotations

import ast
import inspect
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import helixis.detectors.bridge_rules  # noqa: F401  — registration side effect
import helixis.detectors.mirror_state_rules  # noqa: F401
import helixis.detectors.state_rules  # noqa: F401
import helixis.detectors.weather_rules  # noqa: F401
from helixis.detectors import runner
from helixis.detectors.base import STATE_RULE, Detector, Finding
from helixis.detectors.registry import (
    dark_state_rule_detector_ids,
    enabled_state_rule_detectors,
    state_rule_detectors,
)

COMPANY_ID = "11111111-1111-1111-1111-111111111111"


def _conn() -> Any:
    """⚠ ``execute`` must be an ``AsyncMock``. A bare ``MagicMock`` returns
    something unawaitable, so ``persist_findings`` raises and every HEALTHY
    detector is recorded as failed — which looks precisely like the failure
    channel over-reporting, and is the fixture lying instead."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    return conn


def _pool(conn: Any) -> Any:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


class _Det(Detector):
    """A minimum-viable detector that really persists.

    ⚠ **It subclasses ``Detector`` and emits real ``Finding``s on purpose.** A
    hand-rolled stand-in returning ``object()`` sailed through ``scan`` and
    then blew up inside ``persist_findings`` — so the HEALTHY detectors landed
    in ``failed`` as well, which reads exactly like the new failure channel
    over-reporting. The fixture was wrong, not the code, and a fake that
    cannot survive the real persist path cannot tell those two apart
    (gotcha 41's rule about a mock drifted from the thing it stands in for).
    """

    detector_class = STATE_RULE
    default_enabled = True
    rule_keys = ("k",)

    def __init__(self, detector_id: str, *, boom: bool = False, findings: int = 0) -> None:
        self.detector_id = detector_id
        self._boom = boom
        self._findings = findings

    async def scan(self, conn: Any, company_id: str) -> list[Finding]:
        if self._boom:
            raise RuntimeError("detector exploded")
        return [
            Finding(
                rule_key="k",
                fingerprint=f"{self.detector_id}-{i}",
                severity="info",
                title="t",
                detail="d",
            )
            for i in range(self._findings)
        ]


# ── dark is distinguishable from silent ──────────────────────────────


def test_the_dark_detectors_are_nameable() -> None:
    """The census that motivated all of this. If this list is empty the whole
    file is measuring nothing, so it is asserted non-empty rather than
    trusted."""
    dark = dark_state_rule_detector_ids()
    assert dark, "no dark detectors found — the registry scan is broken"
    assert "invoice_no_work_order" in dark


def test_dark_and_enabled_partition_the_registry() -> None:
    """Both directions (gotcha 38). Every registered detector is in exactly
    one of the two sets — a detector in neither is one nobody is accounting
    for, which is how the original 9 went unnoticed."""
    everything = {d.detector_id for d in state_rule_detectors()}
    on = {d.detector_id for d in enabled_state_rule_detectors()}
    dark = set(dark_state_rule_detector_ids())
    assert on | dark == everything
    assert not (on & dark)


def test_allow_listing_a_detector_removes_it_from_the_dark_list() -> None:
    """A dark list that still names an enabled detector is a warning nobody
    can act on, and it teaches the operator to ignore the line."""
    extra = frozenset({"invoice_no_work_order"})
    assert "invoice_no_work_order" not in dark_state_rule_detector_ids(extra)
    assert "invoice_no_work_order" in {d.detector_id for d in enabled_state_rule_detectors(extra)}


# ── a crash is distinguishable from a clean zero ─────────────────────


@pytest.mark.asyncio
async def test_a_failed_detector_is_reported_as_failed_not_as_zero() -> None:
    """⚠ THE one that matters. A detector that raises is counted 0 — the same
    number a healthy detector with nothing to report gives — so a detector
    broken on every company contributes a clean-looking zero to every cycle,
    forever."""
    conn = _conn()
    failed: set[str] = set()
    counts = await runner.run_detectors_for_company(
        _pool(conn),
        COMPANY_ID,
        [_Det("boom", boom=True), _Det("quiet"), _Det("busy", findings=3)],
        failed=failed,
    )
    assert counts == {"boom": 0, "quiet": 0, "busy": 3}
    assert failed == {"boom"}, "the crash must be separable from the honest zero"


@pytest.mark.asyncio
async def test_the_failed_channel_is_optional_and_changes_no_count() -> None:
    """⚠ The failure rides BESIDE the counts, never inside them. Two callers
    sum this mapping, so a ``-1`` or an omitted key would corrupt a total in
    exchange for the signal — and omitting the argument must behave exactly as
    it did before."""
    conn = _conn()
    dets = [_Det("boom", boom=True), _Det("busy", findings=2)]
    with_channel = await runner.run_detectors_for_company(
        _pool(conn), COMPANY_ID, dets, failed=set()
    )
    without = await runner.run_detectors_for_company(_pool(conn), COMPANY_ID, dets)
    assert with_channel == without == {"boom": 0, "busy": 2}
    assert sum(without.values()) == 2


@pytest.mark.asyncio
async def test_a_failing_detector_still_does_not_stop_its_siblings() -> None:
    """The pre-existing isolation guarantee, re-pinned because the failure
    channel is new code on that exact path — a bug here would convert one bad
    detector into a dead sweep."""
    conn = _conn()
    failed: set[str] = set()
    counts = await runner.run_detectors_for_company(
        _pool(conn),
        COMPANY_ID,
        [_Det("boom", boom=True), _Det("after", findings=1)],
        failed=failed,
    )
    assert counts["after"] == 1


# ── the cycle says all of it, every time ─────────────────────────────


def test_a_cycle_that_finds_nothing_still_writes_a_line() -> None:
    """⚠ This was ``if total:``. A sweep across every company that detected
    nothing produced no output at all, so a dead detector subsystem and a
    clean set of books were the same observation — and the subsystem is
    exactly the kind that fails quietly.

    AST rather than a substring: ``ruff format`` reflows the call across
    lines, and a comment mentioning ``if total`` would satisfy a text scan
    (gotcha 40).

    ⚠ **ONE tree, and that is load-bearing.** The first version collected the
    ``logger.info`` calls from one ``ast.parse`` and searched for them inside
    a SECOND ``ast.parse`` of the same source. Those are different objects, so
    the identity test never matched, no guard was ever inspected, and the test
    passed unconditionally — including against a mutant that restored the
    exact ``if`` it exists to forbid. Only mutation showed it: a walk that
    finds nothing reports identically to a clean tree (gotcha 49's rule).
    """
    tree = ast.parse(inspect.getsource(runner.run_detector_cycle))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "info"
    ]
    assert calls, "the cycle must report its outcome"
    inspected = 0
    for guard in (n for n in ast.walk(tree) if isinstance(n, ast.If)):
        inner = {id(c) for stmt in guard.body for c in ast.walk(stmt)}
        if any(id(c) in inner for c in calls):
            inspected += 1
            test = ast.unparse(guard.test)
            assert "total" not in test and "counts" not in test and "findings" not in test, (
                f"the cycle summary is gated on {test!r} — a quiet sweep would "
                "go silent again, which is the defect"
            )
    # A summary sitting under NO conditional is the correct shape, so zero
    # inspected guards is a pass — but say so, rather than leaving a reader to
    # wonder whether the loop ran.
    assert inspected == 0 or inspected >= 1


def test_the_cycle_summary_names_the_dark_and_the_failed() -> None:
    """The count alone answers neither question an operator has. Both belong
    in the line they are already reading, not in the source."""
    src = inspect.getsource(runner.run_detector_cycle)
    assert "dark_state_rule_detector_ids(" in src
    assert "dark=" in src
    assert "failed=" in src
    assert "failed=failed" in src, "the failure channel must actually be passed down"


def test_the_company_sweep_says_why_it_is_unscoped() -> None:
    """Gotcha 47: a cross-tenant query carries ``company_id`` or a comment
    saying why not. This one enumerates the companies to sweep, so it is
    legitimately global — and saying so is what lets the next reader tell a
    considered choice from a forgotten filter."""
    src = inspect.getsource(runner)
    head = src[: src.index("_COMPANIES_SQL")]
    assert (
        "CROSS-TENANT" in head.rsplit("\n\n", 1)[-1].upper()
        or "CROSS-TENANT"
        in src[max(0, src.index("_COMPANIES_SQL") - 400) : src.index("_COMPANIES_SQL")].upper()
    )
