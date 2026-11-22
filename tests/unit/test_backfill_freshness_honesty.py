"""A freshness stamp is a claim that a stage RAN — and one stage could earn it
without ever asking Buildium anything.

CLAUDE.md gotcha 57 leans on this by name: *"a freshness stamp is evidence a
stage did not error — check it before spending an API call."* A whole
diagnosis was settled that way (Blue Ridge genuinely has no paid bills,
established from the mirror alone). The rule is true of every stage that calls
its own endpoint inside its own ``try`` — and ``transactions`` does not.

``transactions`` iterates ``active_lease_ids``, a list built inside the
``leases`` try-block. If ``api.leases.list()`` raises, that list is empty or
partial, the per-lease loop body never executes, **nothing raises**, so
``transactions`` never enters ``failed_stages`` — and it was stamped fresh with
``row_count 0``. "We fetched the ledger and it was empty" and "we never asked"
then read identically, in the direction that matters: the zero looks like an
answer. That is gotcha 26's fabricated-absence class pointed at the metadata
instead of at the data, and it silently falsifies the guarantee gotcha 57
depends on.

⚠ THE PER-LEASE ``except`` IS NOT THE HOLE. It DOES call
``record_failure("transactions", …)`` — a hypothesis worth disconfirming
rather than assuming (gotcha 66: test the cheap precondition first). The hole
is strictly the ZERO-ITERATION case, where there is no exception to record.

Two kinds of guard, because either alone is blind:

* **Behavioural** — drive the real function and assert what is and is not
  stamped. It cannot see a second derived stage arriving later.
* **Structural** — read ``backfill_company``'s own loops and require every
  stage whose work-list is a NAME (rather than a fresh ``await``) to be
  declared. That is gotcha 45's rule: the failure mode here is "silently
  stamps more", so something has to measure what it permitted.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
from unittest.mock import MagicMock

from helixis.buildium import backfill as backfill_mod
from helixis.buildium.backfill import (
    _STAGE_WORK_LIST_FROM,
    BackfillResult,
    _record_stage_freshness,
)

SRC = pathlib.Path(backfill_mod.__file__)


def _stamped(monkeypatch, result: BackfillResult) -> dict[str, int | None]:
    """Run the real stamping pass and return {table_name: row_count}."""
    calls: dict[str, int | None] = {}

    async def _fake_record_sync(_pool, *, company_id, table_name, row_count, source):  # noqa: ANN001
        calls[table_name] = row_count

    # The import is INSIDE the function, so patch it at its home module.
    import helixis.db.repositories.mirror_sync_state as mss

    monkeypatch.setattr(mss, "record_sync", _fake_record_sync)
    asyncio.run(_record_stage_freshness(MagicMock(), company_id="c1", result=result))
    return calls


def test_a_stage_whose_work_list_failed_upstream_is_not_stamped(monkeypatch) -> None:
    """The defect itself: leases errored, so the transactions loop never ran,
    so its 0 is not a measurement and must not be recorded as one."""
    result = BackfillResult()
    result.record_failure("leases", RuntimeError("502 from Buildium"))
    result.transactions = 0

    calls = _stamped(monkeypatch, result)

    assert "transactions" not in calls, (
        "transactions was stamped fresh with row_count 0 while its work-list "
        "(active lease ids) came from the leases stage, which failed — so "
        "'the ledger is empty' and 'we never asked' are indistinguishable, "
        "which is exactly what gotcha 57 reads this stamp to rule out."
    )
    assert "leases" not in calls, "a stage that errored must keep its previous timestamp"


def test_the_control_the_other_stages_still_stamp(monkeypatch) -> None:
    """The CONTROL that makes the assertion above evidence rather than a
    function that simply stamps nothing: every stage NOT implicated by the
    leases failure is still recorded, with its real count."""
    result = BackfillResult()
    result.record_failure("leases", RuntimeError("502"))
    result.properties = 7
    result.bills = 3

    calls = _stamped(monkeypatch, result)

    assert calls.get("properties") == 7
    assert calls.get("bills") == 3
    assert len(calls) >= 10, f"only {len(calls)} stages stamped — the pass is not running"


def test_a_genuine_zero_is_still_stamped(monkeypatch) -> None:
    """The opposite direction, and the one that would make this fix a
    regression if it were wrong. A company with leases and no ledger rows in
    the window HAS been measured, and suppressing that stamp would send the
    next reader to spend an API call on a question already answered."""
    result = BackfillResult()
    result.leases = 12
    result.transactions = 0

    calls = _stamped(monkeypatch, result)

    assert "transactions" in calls, (
        "a real zero must still stamp — leases succeeded, so the loop ran and "
        "found nothing, which is a measurement"
    )
    assert calls["transactions"] == 0


def test_a_per_lease_failure_still_marks_the_stage(monkeypatch) -> None:
    """Disconfirming the hypothesis that ALMOST shipped as the fix: the
    per-lease except already records the failure, so that path was never the
    hole and must keep working unchanged."""
    result = BackfillResult()
    result.leases = 12
    result.record_failure("transactions", RuntimeError("timeout"), detail="lease 99")

    calls = _stamped(monkeypatch, result)

    assert "transactions" not in calls
    assert "transactions" in result.failed_stages


def _backfill_tree() -> ast.AST:
    return ast.parse(inspect.getsource(backfill_mod))


def _try_ancestors(tree: ast.AST) -> dict[int, list[ast.Try]]:
    """Every node's enclosing ``try`` blocks, innermost last.

    The distinction this whole guard turns on is not "is the loop's iterable a
    NAME" — ``bank_accounts = await api.bank_accounts.list()`` is a name too,
    and it is fine, because it is fetched INSIDE the same ``try`` that wraps
    its loop, so a failure there is caught and recorded for that stage. What
    makes ``transactions`` different is that its work-list is produced under a
    DIFFERENT failure-isolation boundary, where a failure it will never see
    has already been absorbed.

    ⚠ My first cut asserted on the node type alone and flagged
    ``bank_accounts`` — gotcha 103's lesson, that a scan matching too broadly
    is as useless as one matching too little, and the red baseline is what
    caught it.
    """
    out: dict[int, list[ast.Try]] = {}

    def walk(node: ast.AST, stack: list[ast.Try]) -> None:
        out[id(node)] = list(stack)
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.Try) and child in node.body:
                walk(child, [*stack, node])
            else:
                walk(child, stack)

    walk(tree, [])
    return out


def _stages_touched(node: ast.AST) -> set[str]:
    """Which ``result.<stage>`` attributes this subtree writes to."""
    out: set[str] = set()
    for sub in ast.walk(node):
        target = None
        if isinstance(sub, ast.AugAssign):
            target = sub.target
        elif isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            target = sub.targets[0]
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "result"
        ):
            out.add(target.attr)
    return out


def test_every_derived_work_list_is_declared() -> None:
    """THE LOAD-BEARING GUARD (gotcha 45 — the failure mode is "silently
    stamps more"). A stage that fetches its own work-list does so inside the
    ``try`` that also wraps its loop, so a failure is caught and recorded
    against that stage. A stage that INHERITS one loops over a name produced
    under a different boundary, where a failure it can never see has already
    been absorbed — and that stage must appear in ``_STAGE_WORK_LIST_FROM``.

    A behavioural test cannot see a SEVENTH stage written the derived way —
    it would simply be stamped, silently, exactly as transactions was.
    """
    tree = _backfill_tree()
    tries = _try_ancestors(tree)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "backfill_company":
            fn = node
    assert fn is not None, "backfill_company not found — the scan is reading the wrong module"

    # Where each local name is bound, and whether that binding is a FETCH.
    # An empty initialiser (``active_lease_ids: list[int] = []``) sits at the
    # same level as its loop and is filled somewhere else entirely, so "same
    # boundary" alone is not the test — the value has to be produced there.
    fetched_in: dict[str, list[ast.Try | None]] = {}
    for node in ast.walk(fn):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or not any(isinstance(n, ast.Await) for n in ast.walk(value)):
            continue
        scope = tries[id(node)]
        for t in targets:
            if isinstance(t, ast.Name):
                fetched_in.setdefault(t.id, []).append(scope[-1] if scope else None)

    derived: dict[str, str] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.For | ast.AsyncFor) and isinstance(node.iter, ast.Name)):
            continue
        loop_try = tries[id(node)][-1] if tries[id(node)] else None
        if any(scope is loop_try for scope in fetched_in.get(node.iter.id, [])):
            continue  # fetched under this loop's own failure boundary
        for stage in _stages_touched(node):
            derived[stage] = node.iter.id

    assert derived, (
        "the scan found no derived-work-list loop at all — transactions is one, "
        "so a zero here means the walk is broken, not that the code is clean "
        "(gotchas 49/100/101: a corpus scan that finds nothing is a claim to check)"
    )
    undeclared = sorted(set(derived) - set(_STAGE_WORK_LIST_FROM))
    assert undeclared == [], (
        f"these stages loop over a work-list built elsewhere {undeclared} "
        f"(from {[derived[s] for s in undeclared]}) but are not in "
        "_STAGE_WORK_LIST_FROM — so if the stage that BUILT that list fails, "
        "they will be stamped fresh with a row count of 0 that means "
        "'never asked'. Declare the upstream stage."
    )


def test_the_map_is_actually_consulted_by_the_stamping_loop() -> None:
    """A map nothing reads is the ``comms_triage`` failure — a complete,
    well-tested helper wired to nothing (gotchas 62/66/69, three prior
    instances in this repo). The behavioural tests above drive the function
    directly and would pass against a map that was declared and ignored only
    if the ``continue`` were also removed; this pins the wiring itself."""
    for node in ast.walk(_backfill_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_record_stage_freshness":
            reads = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Name) and n.id == "_STAGE_WORK_LIST_FROM"
            ]
            assert reads, "_record_stage_freshness no longer consults _STAGE_WORK_LIST_FROM"
            return
    raise AssertionError("_record_stage_freshness not found")


def test_the_map_names_real_stages_on_both_sides() -> None:
    """A typo on either side silently exempts nothing — the key would never
    match a stage name and the value would never match a failed one, so the
    guard would read as installed and do nothing (gotcha 70's shape)."""
    fields = set(BackfillResult.__dataclass_fields__)
    for stage, upstream in _STAGE_WORK_LIST_FROM.items():
        assert stage in fields, f"{stage!r} is not a BackfillResult stage"
        assert upstream in fields, f"{upstream!r} is not a BackfillResult stage"
        assert stage != upstream, f"{stage!r} cannot derive its work-list from itself"
