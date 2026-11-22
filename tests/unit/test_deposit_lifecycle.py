"""The move-out / deposit lifecycle, which was built in disconnected pieces.

Five things existed and did not reach each other: ``security_deposit`` parsed
off every lease read and stored nowhere; ``deposit_return_clock`` computing the
statutory deadline behind an env gate nothing sets; ``jurisdiction_lookup``
holding the caps; ``helixis_turnovers`` feeding the clock; and
``Leases.undo_move_out`` — wrapped, tested, and callable by nothing.

Two of those are load-bearing and are guarded here.

⚠ **The deposit figure.** Live-verified 2026-08-29 across all 134 mirrored
leases: Buildium populates ``SecurityDeposit`` on every row, and one account
reads 0.00 on all 75 of its leases while the other two carry nonzero values up
to $1,800. So ``0.00`` is a VALUE — Buildium was asked and answered zero — and
an account at zero throughout is ONE fact about how that account is set up, not
seventy-five facts about seventy-five tenants. Rendering it the second way is
the fabricated-absence failure (gotchas 26/55) pointed at money people are owed
back.

⚠ **The undo.** ``buildium_record_move_out`` is registered confirm-only, with
no Manager+ floor, and its own docstring gives the reason: *"REVERSIBLE via
``Leases.undo_move_out``"*. That reversal had no tool. A safety argument
resting on a capability nobody can invoke is the shape of gotcha 54's
documented-but-unwired keepalive — stated, load-bearing, and absent.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from helixis.agents import mirror_tools
from helixis.agents.mirror_tools import _deposit_note
from helixis.buildium.schemas import Lease

# ── the deposit is persisted at all ──────────────────────────────────


def test_the_lease_model_exposes_the_deposit_beside_the_rent() -> None:
    """It was parsed into ``AccountDetails`` and never surfaced, exactly like
    ``rent`` was before PR #74 — the fetched-and-discarded shape."""
    lease = Lease.model_validate(
        {"Id": 1, "AccountDetails": {"Rent": 2400.0, "SecurityDeposit": 1800.0}}
    )
    assert lease.security_deposit == 1800.0
    assert lease.rent == 2400.0


def test_a_missing_account_block_is_unknown_not_zero() -> None:
    """⚠ The distinction the whole change turns on. ``None`` means we never
    got the block; ``0.0`` means Buildium said zero. Collapsing the first into
    the second invents a fact about a lease we know nothing about."""
    assert Lease.model_validate({"Id": 1}).security_deposit is None
    assert (
        Lease.model_validate({"Id": 1, "AccountDetails": {"SecurityDeposit": 0.0}}).security_deposit
        == 0.0
    )


def test_the_upsert_writes_the_deposit_column() -> None:
    """The column must be in the INSERT list, the ON CONFLICT update AND the
    parameter list. Two of the three is not a smaller version of the fix: land
    it on INSERT but not on conflict and the deposit is written once and then
    frozen, so a lease whose deposit changes keeps the old figure forever
    while looking perfectly synced.

    ⚠ **Asserts on the SQL LITERAL, not on the function's source text.** The
    first version counted ``"security_deposit"`` occurrences across the whole
    source and required three — and passed with the ON CONFLICT clause
    deleted, because the DOCSTRING mentions the column too. Caught by mutation
    only. That is gotcha 40 exactly: a substring guard over source that a
    comment can satisfy is a guard the comment explaining it can satisfy.
    """
    import ast as _ast

    from helixis.db.repositories import entities

    tree = _ast.parse(inspect.getsource(entities.upsert_lease))
    sql = max(
        (
            n.value
            for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str)
        ),
        key=len,
    )
    assert "helixis_leases" in sql, "found the docstring, not the statement"
    insert_list, _, conflict = sql.partition("ON CONFLICT")
    assert "security_deposit" in insert_list, "not in the INSERT column list"
    assert "security_deposit = EXCLUDED.security_deposit" in conflict, (
        "not updated ON CONFLICT — the deposit would be written once and then "
        "frozen against every later sync of the same lease"
    )
    assert "lease.security_deposit" in inspect.getsource(entities.upsert_lease), (
        "the value is never passed as a parameter"
    )


def test_the_migration_backfills_what_is_already_mirrored() -> None:
    """The value has been in ``raw`` the whole time, so the backfill is exact
    rather than a guess — the same shape as 0073 (bill totals) and 0077 (bill
    line properties). ⚠ It must be NULL-only, or a re-run overwrites values
    the live ingest has since written."""
    sql = (
        pathlib.Path(__file__).resolve().parents[2]
        / "supabase"
        / "migrations"
        / "0079_leases_security_deposit.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS security_deposit" in sql
    assert "SecurityDeposit" in sql
    assert "security_deposit IS NULL" in sql, "re-running must not clobber ingested values"


# ── what the number is allowed to claim ──────────────────────────────


def _rows(*deposits: float | None) -> list[dict[str, object]]:
    return [{"security_deposit": d} for d in deposits]


def test_a_portfolio_of_zeros_is_reported_as_one_fact_not_fifty() -> None:
    """⚠ THE finding. Blue Ridge reads 0.00 on all 75 leases. Fifty rows each
    saying "$0.00" reads as fifty tenants who owe no deposit; it is one fact
    about an account that does not record deposits in Buildium."""
    note = _deposit_note(_rows(0.0, 0.0, 0.0))
    assert note is not None
    assert "not evidence that each tenant owes nothing" in note


def test_one_real_deposit_switches_the_note_to_what_the_figure_means() -> None:
    """Both directions (gotcha 38). Where deposits ARE recorded the systemic
    caveat would be a fresh false claim, and the useful warning is the other
    one: this is the agreed amount, not the money held."""
    note = _deposit_note(_rows(0.0, 1800.0))
    assert note is not None
    assert "lease agreement" in note
    assert "not confirmation it was paid" in note


def test_the_note_is_computed_never_hardcoded() -> None:
    """The day an account starts recording deposits the zero caveat becomes
    false, and a false caveat teaches the model to discount the true ones —
    the paid-bills gap notice learned this the same way (gotcha 57)."""
    assert _deposit_note(_rows(0.0)) != _deposit_note(_rows(500.0))


def test_unknown_deposits_are_excluded_rather_than_counted_as_zero() -> None:
    """⚠ Unknown must not collapse into zero. A partial sync — rows with no
    ``AccountDetails`` block at all — must not manufacture the "records no
    deposits" claim about a company that simply has not finished syncing."""
    assert _deposit_note(_rows(None, None)) is None
    assert _deposit_note([]) is None
    mixed = _deposit_note(_rows(None, 900.0))
    assert mixed is not None and "lease agreement" in mixed


def test_a_zero_deposit_never_prints_on_a_row() -> None:
    """Per-row silence plus one footer, deliberately: the rows are dense, and
    "$0.00" repeated down a list is exactly what gets read as a finding."""
    # ⚠ Not ``inspect.cleandoc`` — it re-indents the block and ``ast.parse``
    # dies on the result. The function is module-level, so its source already
    # starts at column 0.
    tree = ast.parse(inspect.getsource(mirror_tools._expiring_leases_impl))
    guarded = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.IfExp) and "deposit" in ast.unparse(n).lower()
    ]
    assert guarded, "the deposit must render conditionally, not unconditionally"
    assert any("> 0" in ast.unparse(n.test) for n in guarded), (
        "a zero deposit must be silent on the row — the footer carries that meaning"
    )


# ── the undo the write was shipped on ────────────────────────────────


def test_the_move_out_undo_is_reachable_from_the_agent() -> None:
    """⚠ ``buildium_record_move_out`` is confirm-only — no Manager+ floor —
    and the reason in its own docstring is that the move-out is reversible.
    Until now nothing could reach the reversal, so a move-out filed against
    the wrong tenant could only be repaired in the Buildium UI: the gate's
    justification was a capability the product did not expose."""
    from helixis.agents.buildium_tools import buildium_tools

    names = {t.name for t in buildium_tools()}
    assert "buildium_record_move_out" in names
    assert "buildium_undo_move_out" in names, (
        "the write ships on a reversibility claim; the reversal must be callable"
    )


def test_the_undo_is_confirm_gated_and_claims_an_idempotency_key() -> None:
    """It is a write like any other. ``run_guarded`` gives it the confirm card
    and the idempotency claim, so a double-approval replays instead of firing
    a second DELETE."""
    from helixis.agents import buildium_tools as bt

    src = inspect.getsource(bt._undo_move_out_impl)
    assert "run_guarded" in src
    assert "derive_key" in src
    assert 'action_type="buildium_undo_move_out"' in src


def test_the_undo_carries_no_role_floor_and_that_is_deliberate() -> None:
    """⚠ Asserted as a POSITIVE choice, not left to drift. Flooring the
    correction while the mistake itself needs only a confirm would make the
    damage strictly easier to do than to repair — the opposite of what the
    Manager+ floor exists for on the payment / charge / close writes, which
    are floored precisely because they CANNOT be undone (gotchas 20/21).

    ⚠ Asserted on the PRIMITIVE, not on the helper's name. ``post_charge`` and
    ``close_work_order`` call the shared ``_require_manager_role``, while
    ``record_payment`` keeps the inline floor it shipped with in July — the
    helper's own docstring says it mirrors that one. A guard spelled as
    ``"_require_manager_role" in source`` reads the immutable money write as
    UNFLOORED and reports a release-blocking regression that does not exist;
    it did exactly that on the first run here. Both spellings reach
    ``role_at_least``, so that is what the contrast is measured on.
    """
    from helixis.agents import buildium_tools as bt

    def floored(fn: object) -> bool:
        # ⚠ The UNION of both spellings, and neither alone is sufficient:
        # `record_payment` inlines `role_at_least` and never names the helper,
        # while `post_charge` / `close_work_order` call the helper and never
        # name `role_at_least`. Checking either one on its own reports a
        # different pair of live money writes as unguarded — both directions
        # were observed here, one after the other.
        src = inspect.getsource(fn)  # type: ignore[arg-type]
        return "role_at_least" in src or "_require_manager_role" in src

    assert not floored(bt._undo_move_out_impl)
    assert not floored(bt._record_move_out_impl), (
        "the move-out pair is confirm-only by design — if one gains a floor "
        "the other must too, or the mistake gets cheaper than the repair"
    )
    for irreversible in (
        bt._record_payment_impl,  # immutable (gotcha 21)
        bt._post_charge_impl,  # non-deletable (gotcha 21)
        bt._close_work_order_impl,  # one-way latch (gotcha 20)
    ):
        assert floored(irreversible), (
            f"{irreversible.__name__} lost its Manager+ floor — this test is only "
            "meaningful while the reversible/irreversible contrast holds"
        )


def test_the_undo_names_the_tenant_it_read_not_the_one_the_model_typed() -> None:
    """Same discipline as the record path: the confirm card is built from the
    fetched lease, so it cannot name one tenant while acting on another — the
    Whitfield misattribution (gotcha 41) at a write gate."""
    from helixis.agents import buildium_tools as bt

    src = inspect.getsource(bt._undo_move_out_impl)
    assert "api.leases.get(" in src
    assert "_gate_label(" in src


# ── the producer that is dark, and says so ───────────────────────────


def test_a_dark_producer_is_distinguishable_from_a_silent_one() -> None:
    """⚠ A producer gated off returns ``[]`` and records ``count = 0`` — which
    is byte-for-byte what a producer that ran and found nothing records. So
    "switched off" and "switched on and silent" were the same observation, and
    that is the condition under which a registered producer reaches production
    emitting nothing and nobody notices (gotcha 45's class)."""
    from helixis.reminders import producers

    assert "deposit_return_clock" in producers.DARK_BY_DEFAULT
    assert "deposit_return_clock" in producers.dark_producer_ids()


def test_enabling_a_producer_clears_it_from_the_dark_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions. A dark list that still names an enabled producer is a
    warning nobody can act on, and it would make the operator distrust the
    whole line."""
    from helixis.reminders import producers

    monkeypatch.setenv("HELIXIS_REMINDERS__ENABLED_IDS", "deposit_return_clock")
    assert "deposit_return_clock" not in producers.dark_producer_ids()


def test_every_dark_producer_is_actually_registered() -> None:
    """Registration checked separately from the declaration (gotcha 35's third
    trap): a name in the dark set that no longer exists in ``PRODUCERS`` would
    report a producer as switched off forever after it was deleted."""
    from helixis.reminders import producers

    assert producers.DARK_BY_DEFAULT <= set(producers.PRODUCERS)


def test_the_producer_keeps_its_own_gate() -> None:
    """Defence in depth. The declared set is for REPORTING; a direct caller
    that never consults it must still not be able to run a dark producer."""
    from helixis.reminders import producers

    assert "_extra_enabled_producer_ids()" in inspect.getsource(producers.deposit_return_clock)


def test_a_quiet_sweep_still_writes_a_line() -> None:
    """⚠ This was ``if fired:``, so a cycle where every producer returned
    nothing logged NOTHING — making "the reminder subsystem ran and found
    nothing" indistinguishable from "the reminder subsystem is dead". One line
    per company per six hours is not noise; a silence meaning two different
    things is."""
    from helixis.reminders import sweep

    tree = ast.parse(inspect.getsource(sweep.run_reminder_sweep_cycle))
    logs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "info"
    ]
    assert logs, "the sweep must report its outcome"
    for call in logs:
        parents = [
            p
            for p in ast.walk(tree)
            if isinstance(p, ast.If) and call in list(ast.walk(p.test)) + _body_calls(p)
        ]
        assert not any("fired" in ast.unparse(p.test) for p in parents), (
            "the sweep summary must not be gated on something having fired — "
            "that is what made a dead subsystem invisible"
        )


def _body_calls(node: ast.If) -> list[ast.AST]:
    return [n for stmt in node.body for n in ast.walk(stmt)]


def test_the_sweep_summary_names_the_dark_producers() -> None:
    """The count alone cannot answer "is this thing on?". The operator needs
    it in the line they are already reading, not in the source."""
    from helixis.reminders import sweep

    src = inspect.getsource(sweep.run_reminder_sweep_cycle)
    assert "dark_producer_ids()" in src
    assert "dark=" in src


@pytest.mark.parametrize("name", ["Buildium", "Rent Manager", "Propertyware"])
def test_the_all_zero_note_names_the_company_s_own_system(name: str) -> None:
    """It said "Buildium records no security deposit" to every company, a Rent
    Manager one whose deposits all read 0 included."""
    note = _deposit_note(_rows(0.0, 0.0), name)
    assert note is not None
    assert note.startswith(f"{name} records no security deposit")
    assert f"set up in {name}" in note
    others = {"Buildium", "Rent Manager", "Propertyware"} - {name}
    assert not any(other in note for other in others)


def test_the_expiring_leases_tool_hands_the_note_the_company_s_system() -> None:
    """The note can only name the system it is given: the tool must pass the
    company's (``cov.name``), or every company reads "the property management
    system". Walked as calls (rule 17), never matched as text."""
    tree = ast.parse(inspect.getsource(mirror_tools._expiring_leases_impl))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "_deposit_note"
    ]
    assert len(calls) == 1
    assert [ast.unparse(a) for a in calls[0].args][1:] == ["cov.name"]


def test_the_all_zero_note_names_no_system_it_does_not_know() -> None:
    note = _deposit_note(_rows(0.0, 0.0), None)
    assert note is not None and "Buildium" not in note
    assert "not evidence that each tenant owes nothing" in note
