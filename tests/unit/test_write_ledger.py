"""Occupella could post a charge and leave no trace a manager could read.

MEASURED on helixis-test 2026-09-10, deliberately cross-tenant because which
provenance values the product writes is a property of the CODE rather than of
one company's books: **528 activity rows** — 452 `webhook` (Buildium telling us
what somebody else changed), 53 `chat/web_search`, 21 `manual`. Not one row for
a Buildium write Occupella made.

`grep -c write_activity` returned **0** for both `agents/buildium_tools.py` and
`agents/sms_tools.py`, while every read tool logged. So the product had recorded
"Occupella searched the web" fifty-three times and had never recorded "Occupella
posted a charge to a tenant's ledger" (coverage checklist #361).

The thing that existed instead was `helixis_action_idempotency`, and gotcha 70
says plainly what a `succeeded` row there means: *Buildium returned 2xx*. It is
a dedup ledger with no description and no reader.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from helixis.agents.write_ledger import LABEL_PREFIX, RECORDED_OUTCOMES, record_write

_IDEMPOTENCY = Path("src/helixis/agents/idempotency.py")

CONFIRM = {
    "action": "buildium_post_charge",
    "title": "Post a charge in Buildium?",
    "details": "Post a $2,400.00 charge to Aaron Tillman's ledger at 200 E Colfax Ave.",
}


def _written(**kw: object) -> list:
    """Drive `record_write` with the activity writer spied on."""
    seen: list = []

    async def _spy(_pool: object, entry: object) -> None:
        seen.append(entry)

    with (
        patch("helixis.db.repositories.activity.write_activity", new=_spy),
        patch("helixis.db.engine.get_pool", return_value=object()),
    ):
        asyncio.run(record_write(**kw))  # type: ignore[arg-type]
    return seen


# ── what earns a row ─────────────────────────────────────────────────────


def test_a_completed_write_is_recorded() -> None:
    """The whole point. Before this, a $2,400 charge left nothing in the feed."""
    (entry,) = _written(
        company_id="co-1", action_type="buildium_post_charge", confirm=CONFIRM, outcome="executed"
    )
    assert entry.action_type == "buildium_post_charge"
    assert "$2,400.00 charge to Aaron Tillman" in entry.description
    assert entry.source_label.startswith(LABEL_PREFIX)


def test_a_write_that_could_not_be_confirmed_is_recorded_and_says_so() -> None:
    """⚠ The most important row this feed carries. `blocked_needs_review` means
    the side effect DID fire and the read-back could not confirm it — gotcha
    70's 200-but-lie, where Buildium may hold a change nobody can account for.
    Omitting it would leave the least explainable event as the only invisible
    one."""
    (entry,) = _written(
        company_id="co-1",
        action_type="buildium_record_move_out",
        confirm=CONFIRM,
        outcome="blocked_needs_review",
    )
    assert "could not be confirmed" in entry.description
    assert "check Buildium" in entry.description


@pytest.mark.parametrize(
    "outcome", ["replayed", "not_approved", "already_pending", "blocked_store_unavailable"]
)
def test_an_outcome_that_changed_nothing_writes_no_row(outcome: str) -> None:
    """⚠ `replayed` is the one worth naming: a second approval of the same key
    returns the PRIOR result without touching Buildium, so a row would make the
    feed claim two charges where there was one. The other three never reached
    the side effect at all. A feed that records non-events is one people stop
    reading, which is how the real row gets scrolled past (gotcha 84)."""
    assert outcome not in RECORDED_OUTCOMES
    assert (
        _written(
            company_id="co-1", action_type="buildium_post_charge", confirm=CONFIRM, outcome=outcome
        )
        == []
    )


def test_no_company_means_no_row_rather_than_an_unscoped_one() -> None:
    """Every `helixis_*` write carries a company (gotcha 47). An audit row with
    no tenant is one that could surface in somebody else's feed."""
    assert (
        _written(
            company_id="", action_type="buildium_post_charge", confirm=CONFIRM, outcome="executed"
        )
        == []
    )


# ── auto vs manual, which is what #361 actually asked for ────────────────


def test_an_approved_write_records_that_a_human_was_at_the_gate() -> None:
    (entry,) = _written(
        company_id="co-1", action_type="buildium_post_charge", confirm=CONFIRM, outcome="executed"
    )
    assert entry.raw["approved_by_human"] is True
    assert entry.source == "chat"


def test_an_unattended_write_records_that_nobody_was() -> None:
    """⚠ The distinction the checklist item asked for, and `run_guarded` is the
    ONE place that knows it: a confirm payload means a person answered a gate,
    and its absence means the autonomous path ran it. Inferring that anywhere
    downstream would be a guess about accountability."""
    (entry,) = _written(
        company_id="co-1", action_type="notes.autonomous", confirm=None, outcome="executed"
    )
    assert entry.raw["approved_by_human"] is False
    assert entry.source == "scheduled"
    assert entry.source != "chat", "an unattended write is being filed as a conversation"


def test_an_unattended_write_still_gets_a_readable_description() -> None:
    """There is no approval text to inherit, and a blank description renders as
    an empty row — which reads as nothing having happened."""
    (entry,) = _written(
        company_id="co-1", action_type="notes.autonomous", confirm=None, outcome="executed"
    )
    assert entry.description.strip()
    assert "notes.autonomous" in entry.description


def test_a_long_approval_is_truncated_rather_than_dropped() -> None:
    long_confirm = {"details": "x" * 5000}
    (entry,) = _written(
        company_id="co-1", action_type="a", confirm=long_confirm, outcome="executed"
    )
    assert 0 < len(entry.description) <= 1000
    assert entry.description.endswith("…")


def test_a_failing_activity_write_costs_the_row_not_the_turn() -> None:
    """⚠ The write has ALREADY happened by the time this runs. Raising here
    would make the tool report a failure for a change that did occur, which is
    a worse lie than the missing row."""

    async def _boom(_pool: object, _entry: object) -> None:
        raise RuntimeError("pool is gone")

    with (
        patch("helixis.db.repositories.activity.write_activity", new=_boom),
        patch("helixis.db.engine.get_pool", return_value=object()),
    ):
        asyncio.run(
            record_write(company_id="co-1", action_type="a", confirm=CONFIRM, outcome="executed")
        )


def test_no_pool_costs_the_row_not_the_turn() -> None:
    """CI, tests and browser degraded mode have no pool at all."""
    with patch("helixis.db.engine.get_pool", side_effect=RuntimeError("no pool")):
        asyncio.run(
            record_write(company_id="co-1", action_type="a", confirm=CONFIRM, outcome="executed")
        )


# ── it is on the path every write already takes ──────────────────────────


def _fn(name: str) -> ast.AST:
    for node in ast.walk(ast.parse(_IDEMPOTENCY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from idempotency.py")


def test_the_guard_audits_on_both_of_its_exits() -> None:
    """⚠ `run_guarded` returns from TWO places: the passthrough branch (no
    durable store, `idempotency.required` false) and the delegation to
    `guarded_action`. The passthrough one performs a REAL write against a real
    Buildium account, so an audit that only covered the stored path would go
    missing exactly where the dedup already had.

    A behavioural test of `record_write` cannot see a call site that stopped
    calling it — the feed would simply stay empty, which is indistinguishable
    from "no writes happened". Gotchas 62/66/69 are three separate instances.
    """
    fn = _fn("run_guarded")
    audits = [
        n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_audit"
    ]
    assert len(audits) == 2, (
        f"run_guarded audits {len(audits)} of its 2 exits — a Buildium write on "
        "the uncovered path leaves no trace in the activity feed"
    )


def test_the_audit_is_not_wired_through_the_per_caller_record_hook() -> None:
    """⚠ `record=` was already on `run_guarded` and used by NOTHING. Routing the
    audit through it would have re-created the fan-out this fix exists to
    remove: nineteen call sites, each of which has to remember, and the one that
    forgets is silently exempt — which is how the gap was born."""
    assert "record_write" in ast.unparse(_fn("_audit"))
    # ⚠ WALK BOTH CALLS. This was a substring test — `"_audit(company_id,
    # action_type, confirm" in fn` — and a MUTATION walked past it: replacing
    # `confirm` with `None` on the DELEGATED exit (the normal production path)
    # left the passthrough exit's identical text satisfying the check, so every
    # real write would have filed as unattended with `approved_by_human: False`
    # and the guard stayed green. Gotcha 40, in the guard meant to catch this.
    for call in (
        n
        for n in ast.walk(_fn("run_guarded"))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_audit"
    ):
        assert getattr(call.args[2], "id", None) == "confirm", (
            "an _audit call no longer receives the confirm payload, so writes on "
            "that path file as unattended and the auto-vs-manual split inverts"
        )


def test_the_write_tools_do_not_log_activity_themselves() -> None:
    """⚠ Reads the OTHER side. If a tool starts logging its own row while the
    guard also logs, the feed double-counts every write — and a duplicated audit
    entry is a worse artifact than a missing one, because it looks like two
    changes to the same ledger."""
    for path in ("src/helixis/agents/buildium_tools.py", "src/helixis/agents/sms_tools.py"):
        src = Path(path).read_text(encoding="utf-8")
        calls = {
            getattr(n.func, "id", "") or getattr(n.func, "attr", "")
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
        }
        assert "write_activity" not in calls, (
            f"{path} logs its own activity row while run_guarded also does — "
            "every write it makes now appears in the feed twice"
        )
