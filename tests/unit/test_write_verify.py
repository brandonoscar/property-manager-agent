"""The read-back verifiers — the half of the write harness that reads the
record instead of the receipt.

⚠ Why this file exists. On 2026-08-30 ``buildium_record_move_out`` returned
2xx, ``helixis_action_idempotency`` logged ``succeeded``, and NO move-out was
created: the one observable effect was ``lease_to_date`` moving. The harness
reported a clean write, and the agent told the operator the tenant was "flagged
as a former tenant effective today". Every layer said yes and the account said
no (CLAUDE.md gotcha 70).

``succeeded`` in that ledger means **Buildium returned 2xx**. It has never
meant the record changed. These verifiers are the only thing in the product
that asks the second question, so they are guarded here on both sides:

* the CORPUS side — every case the harness drives has a verifier, and every
  verifier names a tool that is really registered (gotcha 28's both-sides
  rule, and gotcha 45's read-the-corpus-not-the-function rule);
* the FIELD side — each verifier reads the field WHERE BUILDIUM ACTUALLY
  SURFACES IT. Gotcha 20 records that reading the wrong one reports every
  successful change as unconfirmed; this file pins the specific traps.

Every "could not check" outcome is asserted to be ``None``, never ``True``.
A verifier that cannot see must not report a pass — that is the same failure
this whole file exists to catch, one level up.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from helixis.agents.buildium_tools import buildium_tools

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(f"_wv_{name}", _SCRIPTS / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(_SCRIPTS))


_FX = {
    "property": "500 Woodlane St",
    "unit": "Unit 1",
    "vendor": "Volt & Vine Electric",
    "tenant": "Doyle Prather",
}


class _Obj:
    """A Buildium row. Attribute names are the SDK's snake_case."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeLeases:
    def __init__(self, leases: list[Any], move_outs: list[Any], txns: list[Any] | None = None):
        self._leases, self._move_outs, self._txns = leases, move_outs, txns or []

    async def list(self, *a: Any, **k: Any) -> list[Any]:
        return self._leases

    async def list_move_outs(self, lease_id: int) -> list[Any]:
        return self._move_outs

    async def list_transactions(self, lease_id: int) -> list[Any]:
        return self._txns


class _FakeApi:
    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)


def _lease_api(move_outs: list[Any], *, lease_to_date: str = "2026-09-14") -> _FakeApi:
    lease = _Obj(
        id=115938,
        lease_to_date=lease_to_date,
        unit=_Obj(address_line1="500 Woodlane St"),
    )
    return _FakeApi(leases=_FakeLeases([lease], move_outs))


# ── the corpus side ───────────────────────────────────────────────────


def test_every_harness_case_has_a_read_back_verifier() -> None:
    """A case with no verifier reports UNVERIFIED forever and nobody notices.

    ⚠ Read from ``build_cases`` rather than a hand-written list: a test fed
    its own inputs would agree with itself while a newly-added case silently
    went uncovered (gotcha 45).
    """
    harness = _load("test_buildium_writes")
    wv = _load("write_verify")
    tools = {c["tool"] for c in harness.build_cases(_FX)}
    assert tools, "build_cases returned nothing -- the scan is not reading the corpus"
    missing = sorted(tools - set(wv.VERIFIERS))
    assert not missing, (
        f"these write cases have no read-back verifier, so a 2xx-but-no-write "
        f"(gotcha 70) would pass silently: {missing}"
    )


def test_every_verifier_names_a_tool_that_is_actually_registered() -> None:
    """The other side. A verifier keyed to a renamed tool never runs, and a
    check that never runs is not a passing check (gotcha 35)."""
    wv = _load("write_verify")
    registered = {t.name for t in buildium_tools()}
    assert "buildium_record_move_out" in registered, "the registry read is broken"
    unknown = sorted(set(wv.VERIFIERS) - registered)
    assert not unknown, f"verifiers keyed to tools that are not registered: {unknown}"


def test_the_undo_case_names_a_verifier_for_the_undo_direction() -> None:
    """⚠ An undo is a write too. ``verify_update_task_status`` asserts
    InProgress, so pointing it at the restore-to-New undo would report a
    CORRECT undo as failed -- which is why that case carries ``undo_tool:
    None`` rather than reusing it."""
    harness = _load("test_buildium_writes")
    wv = _load("write_verify")
    by_tool = {c["tool"]: c for c in harness.build_cases(_FX)}
    assert by_tool["buildium_record_move_out"]["undo_tool"] == "buildium_undo_move_out"
    assert by_tool["buildium_record_move_out"]["undo_tool"] in wv.VERIFIERS
    assert by_tool["buildium_update_task_status"]["undo_tool"] is None


# ── the defect that started this ──────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_moveouts_collection_is_a_failed_write_not_a_pass() -> None:
    """The exact 2026-08-30 state: 2xx, ledger ``succeeded``, no move-out, and
    ``lease_to_date`` shortened. The verifier must say FALSE."""
    wv = _load("write_verify")
    ok, detail = await wv.verify_record_move_out(_lease_api([], lease_to_date="2026-08-30"), _FX)
    assert ok is False
    assert "NO move-out" in detail
    # The silent damage is named, because it is the only thing that DID change.
    assert "2026-08-30" in detail


@pytest.mark.asyncio
async def test_a_real_move_out_verifies() -> None:
    """The other direction. A verifier that only ever fails is as useless as
    one that only ever passes (gotcha 38/51)."""
    wv = _load("write_verify")
    ok, _ = await wv.verify_record_move_out(
        _lease_api([_Obj(tenant_id=229931, move_out_date="2026-08-30")]), _FX
    )
    assert ok is True


@pytest.mark.asyncio
async def test_the_undo_verifier_is_the_mirror_image() -> None:
    wv = _load("write_verify")
    gone, _ = await wv.verify_undo_move_out(_lease_api([]), _FX)
    assert gone is True
    still, _ = await wv.verify_undo_move_out(_lease_api([_Obj(tenant_id=1)]), _FX)
    assert still is False


@pytest.mark.asyncio
async def test_an_unresolvable_lease_is_unverifiable_never_a_pass() -> None:
    """⚠ The whole design rule in one assertion. ``None`` is a THIRD outcome:
    a verifier that cannot see the record must decline, and the harness must
    not read the decline as either a clean write or a proven failure."""
    wv = _load("write_verify")
    empty = _FakeApi(leases=_FakeLeases([], []))
    for fn in (wv.verify_record_move_out, wv.verify_undo_move_out, wv.verify_post_charge):
        ok, detail = await fn(empty, _FX)
        assert ok is None, f"{fn.__name__} reported {ok} when it could not resolve the lease"
        assert "could not resolve" in detail


@pytest.mark.asyncio
async def test_a_raising_read_is_unverifiable_never_a_pass() -> None:
    class _Boom:
        async def list(self, *a: Any, **k: Any) -> list[Any]:
            raise RuntimeError("network")

        async def list_notes(self, *a: Any, **k: Any) -> list[Any]:
            raise RuntimeError("network")

    wv = _load("write_verify")
    ok, _ = await wv.verify_add_vendor_note(_FakeApi(vendors=_Boom()), _FX)
    assert ok is None


@pytest.mark.asyncio
async def test_an_unregistered_tool_is_unverifiable_never_a_pass() -> None:
    wv = _load("write_verify")
    ok, detail = await wv.verify("buildium_not_a_tool", _FakeApi(), _FX)
    assert ok is None
    assert "no read-back verifier" in detail


@pytest.mark.asyncio
async def test_a_verifier_that_raises_is_caught_as_unverifiable() -> None:
    """``verify`` never propagates. A broken check must not lose a run that
    has already written to a real account."""
    wv = _load("write_verify")

    async def _explode(api: Any, fx: dict[str, str]):
        raise ValueError("boom")

    wv.VERIFIERS["buildium_record_move_out"] = _explode
    try:
        ok, detail = await wv.verify("buildium_record_move_out", _FakeApi(), _FX)
    finally:
        wv.VERIFIERS["buildium_record_move_out"] = wv.verify_record_move_out
    assert ok is None
    assert "ValueError" in detail


# ── the field-location traps (gotcha 20 / 26) ─────────────────────────


class _FakeTasks:
    def __init__(self, rows: list[Any], history: list[Any] | None = None):
        self._rows, self._history = rows, history or []

    async def list(self, *a: Any, **k: Any) -> list[Any]:
        return self._rows

    async def list_history(self, task_id: int) -> list[Any]:
        return self._history


@pytest.mark.asyncio
async def test_task_status_is_read_from_task_status_field_not_status() -> None:
    """⚠ ``Status`` is NULL on all 179 production task rows while
    ``TaskStatus`` carries the value (gotcha 26). A verifier reading ``Status``
    reports every successful change as unconfirmed -- gotcha 20's
    false-NEGATIVE, which is how a working write gets 'fixed'."""
    wv = _load("write_verify")
    production_shape = _Obj(
        id=39431,
        status=None,  # exactly what production sends
        task_status="InProgress",
        property=_Obj(name="500 Woodlane St"),
    )
    ok, _ = await wv.verify_update_task_status(_FakeApi(tasks=_FakeTasks([production_shape])), _FX)
    assert ok is True, "the verifier is reading Status, which is NULL in production"


@pytest.mark.asyncio
async def test_a_task_still_new_is_reported_as_not_changed() -> None:
    wv = _load("write_verify")
    ok, _ = await wv.verify_update_task_status(
        _FakeApi(
            tasks=_FakeTasks([_Obj(id=1, task_status="New", property=_Obj(name="500 Woodlane St"))])
        ),
        _FX,
    )
    assert ok is False


class _FakeWorkOrders:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    async def list(self, *a: Any, **k: Any) -> list[Any]:
        return self._rows


@pytest.mark.asyncio
async def test_work_order_close_reads_the_top_level_status() -> None:
    """Closing propagates to the WO's own ``Status`` -- live-verified on
    WO 9015 (gotcha 20)."""
    wv = _load("write_verify")
    wo = _Obj(id=9015, status="Completed", task=_Obj(priority="Low"), title="500 Woodlane St tap")
    ok, _ = await wv.verify_close_work_order(_FakeApi(work_orders=_FakeWorkOrders([wo])), _FX)
    assert ok is True


@pytest.mark.asyncio
async def test_work_order_update_does_not_verify_on_top_level_priority() -> None:
    """⚠ Priority moves on the NESTED ``WorkOrder.Task.Priority`` while the
    top-level one stays frozen at creation (live dogfood, WO 9006). Checking
    the top level false-negatives every successful change -- so the vendor
    reassignment, which IS top-level truth, is what this verifier reads."""
    wv = _load("write_verify")
    wo = _Obj(
        id=9006,
        title="500 Woodlane St tap",
        priority="Normal",  # frozen at creation -- a trap, not a signal
        task=_Obj(priority="High"),
        vendor=_Obj(name="Volt & Vine Electric"),
    )
    ok, detail = await wv.verify_update_work_order(_FakeApi(work_orders=_FakeWorkOrders([wo])), _FX)
    assert ok is True, "the verifier is reading the frozen top-level Priority"
    assert "Volt & Vine" in detail

    src = (_SCRIPTS / "write_verify.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "verify_update_work_order"
    )
    # AST, not a substring scan: the docstring QUOTES the trap in order to
    # explain it, so a text search fires on the sentence documenting the guard
    # (gotcha 40).
    reads = {
        n.args[1].value
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "getattr"
        and len(n.args) >= 2
        and isinstance(n.args[1], ast.Constant)
    }
    assert "priority" not in reads, (
        "verify_update_work_order reads a `priority` attribute -- the top-level "
        "one is frozen at creation and the nested one is not what this tool changes"
    )


# ── the harness wiring (a helper nothing calls is the real bug) ────────


def test_the_harness_actually_calls_the_verifier() -> None:
    """⚠ Gotcha 62: a test that drives a helper directly cannot see that
    nothing calls it. ``comms_triage`` was complete, unit-tested and wired to
    nothing for months. Pin the call site.

    ⚠ And COUNTING call sites is not enough on its own. The mutation battery
    for this file disabled the undo re-read with ``if False and ...``; the
    call node survived, the count stayed at two, and the guard reported green.
    A call that cannot execute is a call that does not exist, so reachability
    is asserted separately below.
    """
    src = (_SCRIPTS / "test_buildium_writes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "check"
    ]
    assert len(calls) >= 2, (
        "the harness does not call the read-back on both the write and its undo "
        "-- an undo is a write too, and 'the gate appeared' is exactly the claim "
        "gotcha 70 caught being wrong"
    )
    assert "import write_verify" in src, "the harness never imports the verifiers"


def test_no_read_back_call_is_switched_off_by_a_constant() -> None:
    """The reachability half. Walk from each ``.check(`` call up to the module
    root and refuse any ancestor ``if`` whose test is a literal — that is how
    a call site is disabled while still reading as wired."""
    src = (_SCRIPTS / "test_buildium_writes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _tests_above(node: ast.AST) -> list[ast.expr]:
        out, cur = [], node
        while cur in parent:
            up = parent[cur]
            if isinstance(up, (ast.If, ast.IfExp)) and cur is not up.test:
                out.append(up.test)
            cur = up
        return out

    checked = 0
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "check"
        ):
            continue
        checked += 1
        for test in _tests_above(node):
            literals = [
                sub
                for sub in ast.walk(test)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, bool)
            ]
            assert not literals, (
                "a read-back call is guarded by a literal boolean -- the call site "
                "still parses but can never run"
            )
    assert checked >= 2, "the reachability scan found no read-back calls to check"


def test_only_a_verified_write_is_undone_automatically() -> None:
    """⚠ Undoing a CONTRADICTED write is a fresh unverified write onto an
    unknown state -- which is how three ambiguous writes became six. The undo
    gate must admit WROTE-VERIFIED (and bare WROTE when read-back is off),
    and nothing else."""
    src = (_SCRIPTS / "test_buildium_writes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    landed = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "landed" for t in n.targets)
    )
    admitted = {
        c.value
        for n in ast.walk(landed)
        if isinstance(n, ast.Compare)
        for c in n.comparators
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }
    assert admitted == {"WROTE-VERIFIED", "WROTE"}, admitted
    for forbidden in ("WROTE-CONTRADICTED", "WROTE-UNVERIFIED", "WROTE-UNCONFIRMED"):
        assert forbidden not in admitted


def test_a_contradicted_write_still_counts_as_having_reached_the_gate() -> None:
    """The gate worked and the WRITE failed. Filing it under 'never gated'
    points an operator at the wrong half of the system."""
    harness = _load("test_buildium_writes")
    assert "WROTE-CONTRADICTED" in harness._GATE_REACHED
    assert "WROTE-VERIFIED" in harness._GATE_REACHED
    assert "ERROR" not in harness._GATE_REACHED


def test_the_read_back_never_falls_back_to_env_credentials() -> None:
    """⚠ The env Buildium pair points at apisandbox.buildium.com -- a
    DIFFERENT account. A read-back against it would confidently verify the
    wrong books, which is worse than not verifying (gotcha 47's shape)."""
    src = (_SCRIPTS / "test_buildium_writes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_BuildiumReadBack"
    )
    attrs = {
        n.attr
        for n in ast.walk(cls)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    assert "buildium" not in attrs, (
        "_BuildiumReadBack reads settings.buildium -- that is the sandbox account, "
        "not the company whose write we just made"
    )
