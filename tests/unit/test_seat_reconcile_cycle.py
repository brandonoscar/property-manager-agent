"""The seat reconciler runs, and the worker actually calls it.

⚠ BOTH HALVES, because they fail differently and one is invisible to the
other. A behavioural test of ``run_seat_reconcile_cycle`` cannot see a worker
that never invokes it — gotcha 62 caught exactly that (a reconciliation call
deleted from a cycle left all five guards green), and gotcha 69 caught four
route-level misses for the same reason: the resolver was thoroughly tested and
nothing pinned that the callers used it.

The AST guard is therefore not decoration. For one commit
``reconcile_company_seats`` existed, was documented as "what makes it
eventually correct", and had no caller anywhere — which is `comms_triage`'s
failure and `notes/policy`'s failure, arriving a third time.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.billing.seats_cycle import run_seat_reconcile_cycle

_WORKER = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/webhooks/worker.py"


def _settings(*, key: str = "sk_test_x") -> Any:
    s = MagicMock()
    s.billing.stripe_secret_key = key
    return s


class _Pool:
    def __init__(self, company_ids: list[str]) -> None:
        self._ids = company_ids
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.queries.append(sql)
        return [{"company_id": c} for c in self._ids]


@pytest.mark.asyncio
async def test_it_visits_every_subscribed_company(monkeypatch) -> None:
    seen: list[str] = []

    async def _fake(_pool: Any, _settings: Any, company_id: str) -> bool:
        seen.append(company_id)
        return True

    monkeypatch.setattr("helixis.billing.seats_cycle.reconcile_company_seats", _fake)
    visited = await run_seat_reconcile_cycle(_Pool(["a", "b", "c"]), _settings())

    assert visited == 3
    assert seen == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_it_only_looks_at_companies_that_have_a_subscription() -> None:
    """A trial company has no Stripe quantity to correct, so visiting one costs
    a query and can never change anything. Deriving the population from the
    thing being repaired is gotcha 62's other half — that finding was a sweep
    whose population query silently excluded the companies that needed it."""
    pool = _Pool([])
    await run_seat_reconcile_cycle(pool, _settings())
    assert "stripe_subscription_id is not null" in pool.queries[0]


@pytest.mark.asyncio
async def test_no_stripe_key_is_a_no_op_not_a_crash() -> None:
    """The normal state of a dev box and of CI."""
    pool = _Pool(["a"])
    assert await run_seat_reconcile_cycle(pool, _settings(key="")) == 0
    assert pool.queries == [], "it queried despite having no key to act on"


@pytest.mark.asyncio
async def test_one_bad_company_does_not_end_the_sweep(monkeypatch) -> None:
    seen: list[str] = []

    async def _explode(_pool: Any, _settings: Any, company_id: str) -> bool:
        if company_id == "b":
            raise RuntimeError("stripe is having a night")
        seen.append(company_id)
        return True

    monkeypatch.setattr("helixis.billing.seats_cycle.reconcile_company_seats", _explode)
    visited = await run_seat_reconcile_cycle(_Pool(["a", "b", "c"]), _settings())

    assert visited == 3
    assert seen == ["a", "c"], "a failure on one company stopped the fleet"


@pytest.mark.asyncio
async def test_an_unreadable_billing_table_returns_zero(monkeypatch) -> None:
    class _Broken:
        async def fetch(self, *_a: Any, **_k: Any) -> list[dict[str, str]]:
            raise RuntimeError("pool is gone")

    assert await run_seat_reconcile_cycle(_Broken(), _settings()) == 0


def test_the_worker_actually_calls_the_cycle() -> None:
    """The half no behavioural test can reach."""
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    called = {
        c.func.id
        for c in ast.walk(tree)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "run_seat_reconcile_cycle" in called, (
        "the worker no longer runs the seat reconciler — every seat sync is "
        "best-effort, so without this pass a Stripe outage during one invite "
        "mis-bills that company indefinitely with nothing to notice."
    )


def test_the_cycle_has_its_own_handler_not_just_the_loops() -> None:
    """The handler has to be LOCAL to this cycle, and that distinction is the
    whole test.

    ⚠ The first version of this asserted only "some enclosing try exists", and
    a mutation removing the cycle's own try/except SURVIVED it — because the
    worker's loop body sits inside one big `except (Exception, SystemExit)`
    that spans everything. Checking behaviour first (gotcha 58) showed the
    mutation was not harmless: with only the outer handler, a seat-reconcile
    failure unwinds past `claim_batch`, so a billing repair erroring would
    silently skip webhook processing for that whole iteration. Every other
    cycle in that loop carries its own try for exactly this reason.

    So the assertion is that the INNERMOST enclosing try is tight around the
    cycle — it must not also contain the batch claim.
    """
    src = _WORKER.read_text(encoding="utf-8")
    tree = ast.parse(src)

    call_line = next(
        (
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "run_seat_reconcile_cycle"
        ),
        None,
    )
    assert call_line is not None, "the worker no longer calls the cycle at all"

    enclosing = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Try) and n.lineno <= call_line <= (n.end_lineno or 0)
    ]
    assert enclosing, "the cycle is not inside any try/except"

    innermost = max(enclosing, key=lambda n: n.lineno)
    body = ast.get_source_segment(src, innermost) or ""
    assert "claim_batch" not in body, (
        "the only try/except around the seat cycle is the loop's own — an error "
        "there would unwind past claim_batch and skip webhook processing for "
        "that iteration. Give the cycle its own handler, as a2p_cycle has."
    )
    assert innermost.handlers, "the enclosing try has no except clause"
