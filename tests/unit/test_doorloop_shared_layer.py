"""The shared layer sees the DoorLoop sync, and reads it right.

The connector never edits shared code: ``pms.registry.connector_attr`` finds
what it exposes, ``pms_coverage`` derives what DoorLoop feeds from its stage
map, ``agents.run_pms`` reads its run note, and the shared sync cycle calls
its pass. Each of those is checked from the shared side (rule 15).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents import run_pms
from helixis.analysis import sql_guard
from helixis.db.pms_coverage import PMS_FED_TABLES, blind_tables, fed_tables, unlanded_tables
from helixis.doorloop import client, mapper, sync
from helixis.pms import http as pms_http
from helixis.pms import registry, sync_cycle
from helixis.pms.mirror import SyncResult
from helixis.rentvine import sync as rentvine_sync
from tests.unit._doorloop_fake import (
    COMPANY,
    TODAY,
    FakeDoorLoop,
    FakePool,
    account,
    creds_row,
    settings,
)

KEY = registry.DOORLOOP


def _mapped() -> dict[str, Any]:
    reads = {
        name: sync.ListRead(rows=account()[path], complete=True)
        for name, path, _filters in sync.READS
    }
    return dict(sync.mirror_rows(reads, today=TODAY, now=1.0))


def test_the_registry_finds_everything_the_shared_layer_reads() -> None:
    assert registry.connector_attr(KEY, "sync", "SYNC_STATE_STAGE") is sync.SYNC_STATE_STAGE
    assert registry.connector_attr(KEY, "sync", "DERIVED_TABLES") is sync.DERIVED_TABLES
    assert registry.connector_attr(KEY, "sync", "RUN_NOTE") is sync.RUN_NOTE
    assert registry.connector_attr(KEY, "sync", "sync_company") is sync.sync_company


def test_the_stage_map_is_a_plain_dict() -> None:
    """``pms_coverage._stage_map`` reads anything else as "fills nothing"."""
    assert type(sync.SYNC_STATE_STAGE) is dict


def test_coverage_is_exactly_what_the_pass_fills() -> None:
    """Re-pointed 2026-10-07: the pass now also opens turnovers, a table no
    single DoorLoop list mirrors (``DERIVED_TABLES``, was empty)."""
    filled = set(_mapped()) | {"helixis_turnovers"}
    assert fed_tables(KEY) == frozenset(filled)
    assert fed_tables(KEY) == frozenset(sync.SYNC_STATE_STAGE) | sync.DERIVED_TABLES
    assert sync.DERIVED_TABLES == frozenset({"helixis_turnovers"})
    assert not sync.DERIVED_TABLES & set(sync.SYNC_STATE_STAGE)
    assert fed_tables(KEY) <= PMS_FED_TABLES


def test_what_doorloop_does_not_fill_is_blind_so_its_readers_decline() -> None:
    """Rule 14: a reader of a table DoorLoop does not fill says "not synced",
    never zero or none."""
    blind = blind_tables(KEY, PMS_FED_TABLES)
    # Re-pointed 2026-10-07: bills, their lines, the chart of accounts, tasks,
    # applicants and turnovers are filled now. What DoorLoop's API cannot give
    # stays blind: bank balances, a work order's status history, and the two
    # Buildium-only sets.
    assert blind == {
        "helixis_bank_accounts",
        "helixis_work_order_status_history",
        "buildium_staff",
        "buildium_events",
    }
    assert blind == PMS_FED_TABLES - fed_tables(KEY)


def test_a_fed_table_waits_for_its_stage_to_land() -> None:
    assert unlanded_tables(KEY, {"helixis_leases"}, landed=set()) == {"helixis_leases"}
    assert unlanded_tables(KEY, {"helixis_leases"}, landed={"leases"}) == frozenset()
    # The ledger lands only from four complete reads: until then it is unknown.
    assert unlanded_tables(KEY, {"helixis_lease_transactions"}, landed={"leases"}) == {
        "helixis_lease_transactions"
    }


def test_the_stage_names_are_rentvines_for_the_same_tables() -> None:
    """One vocabulary in ``helixis_mirror_sync_state``, both sides read.

    Re-pointed 2026-10-07: Rentvine writes no tasks, so the tasks stage is
    held to the stage name the freshness footer already reads (Buildium's)."""
    from helixis.db.repositories.mirror_sync_state import CORE_TABLES

    for table, stage in sync.SYNC_STATE_STAGE.items():
        if table in rentvine_sync.SYNC_STATE_STAGE:
            assert rentvine_sync.SYNC_STATE_STAGE[table] == stage, table
    assert set(sync.SYNC_STATE_STAGE) - set(rentvine_sync.SYNC_STATE_STAGE) == {"helixis_tasks"}
    assert sync.SYNC_STATE_STAGE["helixis_tasks"] == "tasks" and "tasks" in CORE_TABLES


def test_the_analyst_sees_every_column_a_doorloop_pass_fills() -> None:
    """Both sides (rule 15): what the mapper fills against what the analyst
    may read."""
    filled = {
        table: {k for row in read.rows for k, v in row.items() if v is not None}
        for table, read in _mapped().items()
    }
    assert {"total_amount", "transaction_type"} <= filled["helixis_lease_transactions"]
    hidden = sql_guard.hidden_columns(KEY)
    for table, columns in filled.items():
        assert table in sql_guard.ALLOWED_TABLES, table
        visible = set(sql_guard.visible_columns(sorted(columns), KEY))
        assert visible == columns, (table, sorted(columns - visible))
    # The discriminator stays withheld, as for every system.
    assert "pms" in hidden


# ── one vocabulary with the readers (rule 15) ─────────────────────────────


def test_a_finished_task_reads_closed_and_every_other_task_open() -> None:
    """``open_status_sql`` counts every word but the closed ones as open, NULL
    included, so a finished DoorLoop task must land on a closed word."""
    from helixis.buildium.maintenance_status import CLOSED_STATUSES

    finished = {"COMPLETED", "ARCHIVED"}
    for word, mapped in mapper.TASK_STATUS.items():
        assert (mapped in CLOSED_STATUSES) == (word in finished), (word, mapped)


def test_applicant_and_account_words_are_the_ones_the_other_systems_write() -> None:
    from helixis.propertyware.mapper import GL_TYPES
    from helixis.rentvine.mapper import APPLICATION_STATUS

    # Rentvine's map plus the word it writes for an application with a lease:
    # Buildium's own enum (``buildium.schemas.Applicant``).
    words = set(APPLICATION_STATUS.values()) | {"AddedToLease"}
    assert set(mapper.APPLICANT_STATUS.values()) <= words
    assert set(mapper.GL_TYPE.values()) == set(GL_TYPES)


def test_a_company_line_is_in_the_spend_reports_unattributed_total() -> None:
    """The spend report states lines on a manager or portfolio ledger on their
    own; a DoorLoop line on no property carries the manager ledger, so it is
    counted there rather than lost from every property's total."""
    import ast
    import re
    from pathlib import Path

    from helixis.agents import mirror_tools

    tree = ast.parse(Path(mirror_tools.__file__).read_text(encoding="utf-8"))
    found = [
        re.search(r"ledger_type IN \(([\d, ]+)\)", node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    ledgers = [{int(n) for n in m.group(1).split(",")} for m in found if m]
    assert ledgers and all(mapper.MANAGER_LEDGER in found for found in ledgers)
    company = [row for row in _mapped()["helixis_bill_lines"].rows if row["property_id"] is None]
    assert company and {row["ledger_type"] for row in company} == {mapper.MANAGER_LEDGER}


def test_the_no_work_order_rule_leaves_doorloop_bills_out() -> None:
    """Both sides: a pass whose tasks read stopped short writes a new bill with
    no ``work_order_id`` at all, so the rule that reads a NULL there as "no
    work order" must not read DoorLoop's bills (rule 14)."""
    from helixis.detectors.mirror_state_rules import bill_link_systems

    reads = {
        name: sync.ListRead(rows=account()[path], complete=True)
        for name, path, _filters in sync.READS
    }
    reads["tasks"] = sync.ListRead(rows=account()["/tasks"][:1], complete=False)
    bills = sync.mirror_rows(reads, today=TODAY, now=1.0)["helixis_bills"].rows
    assert bills and all("work_order_id" not in row for row in bills)
    assert mapper.BILL_WORK_ORDER_LINK is False
    assert KEY not in bill_link_systems()


def test_the_analyst_is_told_how_to_tell_paid_bills_from_owed_ones() -> None:
    """DoorLoop's pass writes paid bills, so the note the analyst reads must
    not say the table holds unpaid bills only: a SUM over it would count paid
    bills as owed."""
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    assert {row["is_paid"] for row in _mapped()["helixis_bills"].rows} == {True, False}
    notes = [n for n in _SCHEMA_NOTES if "helixis_bills" in n and "is_paid" in n]
    assert len(notes) == 1, notes
    assert "is_paid IS NOT TRUE" in notes[0]
    assert "UNPAID bills only" not in notes[0]


# ── the run note ──────────────────────────────────────────────────────────


def test_a_doorloop_run_reads_this_connectors_note() -> None:
    assert run_pms.run_note_for(KEY) == sync.RUN_NOTE


def test_the_run_note_names_doorloop_and_only_doorloop() -> None:
    note = sync.RUN_NOTE
    assert note.startswith("\n\n## This workspace's property-management system\n")
    assert "runs on DoorLoop" in note and "DoorLoop Agent" in note
    lowered = note.lower()
    for other in ("buildium", "rentvine", "rent manager", "rentmanager", "propertyware"):
        assert other not in lowered, other


def test_the_run_note_never_lets_a_record_id_pass_as_a_doorloop_number() -> None:
    """Occupella's record ids are hashes of DoorLoop's (``mapper.mirror_id``),
    never a number DoorLoop shows."""
    assert "never give one as a DoorLoop number" in sync.RUN_NOTE


def test_the_run_note_says_a_missing_item_is_not_available_never_zero() -> None:
    note = sync.RUN_NOTE
    assert "not available in Occupella yet" in note
    assert "can be checked in DoorLoop" in note
    assert "never say it is zero, empty or none" in note
    # Re-pointed 2026-10-07: vendor bills and the other tasks are synced now.
    for missing in ("expenses", "vendor credits", "owner statements", "notes"):
        assert missing in note, missing


def test_the_run_note_lists_every_table_it_says_is_synced() -> None:
    """Rule 12 in reverse: a table in coverage is one the note can promise."""
    words = {
        "helixis_properties": "properties",
        "helixis_units": "units",
        "helixis_leases": "leases",
        "helixis_lease_tenants": "which tenant is on which lease",
        "helixis_tenants": "tenants",
        "helixis_owners": "owners",
        "helixis_vendors": "vendors",
        "helixis_work_orders": "work orders",
        "helixis_lease_balances": "rent balances",
        "helixis_lease_transactions": "charges, payments and credits",
        "helixis_tasks": "tasks (to-dos and tenant and owner requests)",
        "helixis_applicants": "applicants",
        "helixis_bills": "vendor bills",
        "helixis_bill_lines": "what each was spent on",
        "helixis_gl_accounts": "the chart of accounts",
        "helixis_turnovers": "turnovers",
    }
    assert set(words) == set(fed_tables(KEY))
    for table, word in words.items():
        assert word in sync.RUN_NOTE, (table, word)


def test_the_run_note_promises_no_write() -> None:
    """Slice A has no write: the note must not let the model offer one."""
    assert "cannot change anything in DoorLoop yet" in sync.RUN_NOTE


# ── the shared sync cycle calls this pass ─────────────────────────────────


async def test_the_shared_cycle_runs_this_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    current = settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(sync, "today_in", lambda _zone: TODAY)
    monkeypatch.setattr(pms_http, "_sleep", AsyncMock())
    server = FakeDoorLoop()
    real_build = client.build_client

    def build(creds: Any, *, http: Any = None, budget: Any = None) -> Any:
        return real_build(creds, http=server.http(), budget=budget)

    monkeypatch.setattr(client, "build_client", build)
    result = await sync_cycle.sync_one(FakePool(creds=creds_row()), KEY, COMPANY)
    assert isinstance(result, SyncResult) and result.pms == KEY
    assert result.failed == {}
    assert server.requests, "the pass reached DoorLoop through the shared cycle"


async def test_the_shared_cycle_never_runs_a_switched_off_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(enabled=False)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    assert KEY not in sync_cycle.switched_on()
    assert await sync_cycle.sync_one(MagicMock(), KEY, COMPANY) is None
