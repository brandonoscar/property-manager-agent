"""The shared layer sees the Propertyware connector, and reads it right.

The connector never edits shared code: ``pms.registry.connector_attr`` finds
what it exposes, ``pms_coverage`` derives what Propertyware feeds from its
stage map, ``agents.run_pms`` reads its run note, and the shared sync cycle
calls its pass. Each of those is checked from the shared side (rule 15).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.agents import run_pms
from helixis.analysis import sql_guard
from helixis.db.pms_coverage import PMS_FED_TABLES, blind_tables, fed_tables, unlanded_tables
from helixis.pms import registry, sync_cycle
from helixis.pms.mirror import SyncResult
from helixis.propertyware import client, sync
from helixis.rentvine import sync as rentvine_sync
from tests.unit._propertyware_fake import (
    COMPANY,
    TODAY,
    FakePool,
    FakePropertyware,
    creds_row,
    fixture_rows,
    settings,
)

KEY = registry.PROPERTYWARE


def test_the_registry_finds_everything_the_shared_layer_reads() -> None:
    assert registry.connector_attr(KEY, "sync", "SYNC_STATE_STAGE") is sync.SYNC_STATE_STAGE
    assert registry.connector_attr(KEY, "sync", "DERIVED_TABLES") is sync.DERIVED_TABLES
    assert registry.connector_attr(KEY, "sync", "RUN_NOTE") is sync.RUN_NOTE
    assert registry.connector_attr(KEY, "sync", "sync_company") is sync.sync_company


def test_the_stage_map_is_a_plain_dict() -> None:
    """``pms_coverage._stage_map`` reads anything else as "fills nothing"."""
    assert type(sync.SYNC_STATE_STAGE) is dict


def test_coverage_is_exactly_what_the_pass_fills() -> None:
    reads = {
        path: sync.ResourceRead(rows=fixture_rows(path), complete=True) for path in client.READS
    }
    rows, _report = sync.mirror_rows(reads, today=TODAY, now=1.0)
    filled = set(rows) | {"helixis_turnovers"}
    assert fed_tables(KEY) == frozenset(filled)
    assert fed_tables(KEY) == frozenset(sync.SYNC_STATE_STAGE) | sync.DERIVED_TABLES
    assert sync.DERIVED_TABLES == frozenset({"helixis_turnovers"})
    assert not sync.DERIVED_TABLES & set(sync.SYNC_STATE_STAGE)
    assert fed_tables(KEY) <= PMS_FED_TABLES


def test_what_propertyware_does_not_fill_is_blind_so_its_readers_decline() -> None:
    blind = blind_tables(KEY, PMS_FED_TABLES)
    # The payment-history table above all: charges without their payments
    # would read as "no payments" (design, "built correctly").
    assert "helixis_lease_transactions" in blind
    assert {"helixis_tasks", "helixis_bank_accounts", "helixis_applicants"} <= blind
    assert not blind & fed_tables(KEY)


def test_a_fed_table_waits_for_its_stage_to_land() -> None:
    assert unlanded_tables(KEY, {"helixis_leases"}, landed=set()) == {"helixis_leases"}
    assert unlanded_tables(KEY, {"helixis_leases"}, landed={"leases"}) == frozenset()
    # Turnovers are derived: never stamped, so never held back by a stage.
    assert unlanded_tables(KEY, {"helixis_turnovers"}, landed=set()) == frozenset()


def test_the_stage_names_are_rentvines_for_the_same_tables() -> None:
    """One vocabulary in ``helixis_mirror_sync_state``, both sides read."""
    for table, stage in sync.SYNC_STATE_STAGE.items():
        assert rentvine_sync.SYNC_STATE_STAGE[table] == stage, table


def _spend_report_ledger_types() -> set[int]:
    """The ledger types the spend report counts as belonging to no single
    property, read from its own SQL string constants (AST, never comments)."""
    import ast
    import inspect
    import re

    from helixis.agents import mirror_tools

    tree = ast.parse(inspect.getsource(mirror_tools._spending_report_impl))
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for group in re.findall(r"ledger_type IN \(([\d,\s]+)\)", node.value):
                found |= {int(n) for n in re.findall(r"\d+", group)}
    return found


def test_the_spend_report_counts_a_portfolio_wide_line_as_no_propertys() -> None:
    """Both sides (rule 15): the ledger value the mapper writes for a split
    charged to a portfolio is one the spend report states on its own."""
    from helixis.propertyware import mapper

    counted = _spend_report_ledger_types()
    assert counted, "the spend report's SQL was not found: the scan reads nothing"
    assert mapper.PORTFOLIO_LEDGER in counted


def _filled_columns() -> dict[str, set[str]]:
    """Every column a Propertyware pass fills with a value, by table, from
    the mapped fixtures."""
    reads = {
        path: sync.ResourceRead(rows=fixture_rows(path), complete=True) for path in client.READS
    }
    rows, _report = sync.mirror_rows(reads, today=TODAY, now=1.0)
    return {
        table: {k for row in read.rows for k, v in row.items() if v is not None}
        for table, read in rows.items()
    }


def test_the_analyst_sees_every_column_a_propertyware_pass_fills() -> None:
    """Both sides (rule 15): what the mapper fills against what the analyst
    may read. The ledger columns above all: hidden, the analyst would sum a
    portfolio's spend into no property and not know it (ADR 0010 (b))."""
    filled = _filled_columns()
    assert {"ledger_id", "ledger_type"} <= filled["helixis_bill_lines"], "the case is not vacuous"
    hidden = sql_guard.hidden_columns(KEY)
    for table, columns in filled.items():
        assert table in sql_guard.ALLOWED_TABLES, table
        visible = set(sql_guard.visible_columns(sorted(columns), KEY))
        assert visible == columns, (table, sorted(columns - visible))
    # The discriminator stays withheld, as for every system.
    assert "pms" in hidden


# ── the run note ──────────────────────────────────────────────────────────


def test_a_propertyware_run_reads_this_connectors_note() -> None:
    assert run_pms.run_note_for(KEY) == sync.RUN_NOTE


def test_the_run_note_names_propertyware_and_only_propertyware() -> None:
    note = sync.RUN_NOTE
    assert note.startswith("\n\n## This workspace's property-management system\n")
    assert "runs on Propertyware" in note and "Propertyware Agent" in note
    lowered = note.lower()
    for other in ("buildium", "rentvine", "rent manager", "rentmanager"):
        assert other not in lowered, other


#: The run note's guard against a made-up Propertyware number. Occupella's
#: record ids are not the display numbers (``number`` / ``idNumber``) that
#: Propertyware shows; those wait on the shared mirror-tool PR (design).
RECORD_NUMBER_SENTENCE = (
    "The record numbers Occupella shows are not the lease or work order numbers "
    "Propertyware shows, so never give one as a Propertyware number."
)


def test_the_run_note_never_lets_a_record_id_pass_as_a_propertyware_number() -> None:
    assert RECORD_NUMBER_SENTENCE in sync.RUN_NOTE


def test_the_run_note_says_a_missing_item_is_not_available_never_zero() -> None:
    note = sync.RUN_NOTE
    assert "not available in Occupella yet" in note
    assert "can be checked in Propertyware" in note
    assert "never say it is zero, empty or none" in note
    # The design's "Chat" list: each named as not available, never as zero.
    for missing in (
        "owner statements",
        "security deposits held",
        "rent payment history",
        "custom fields",
    ):
        assert missing in note, missing


def test_the_run_note_lists_every_table_it_says_is_synced() -> None:
    """Rule 12 in reverse: a table in coverage is one the note can promise."""
    words = {
        "helixis_properties": "properties",
        "helixis_units": "units",
        "helixis_leases": "leases",
        "helixis_tenants": "tenants",
        "helixis_owners": "owners",
        "helixis_vendors": "vendors",
        "helixis_work_orders": "work orders",
        "helixis_bills": "vendor bills",
        "helixis_lease_balances": "rent balances",
        "helixis_turnovers": "turnovers",
    }
    for table, word in words.items():
        assert table in fed_tables(KEY), table
        assert word in sync.RUN_NOTE, word


# ── the shared sync cycle calls this pass ─────────────────────────────────


async def test_the_shared_cycle_runs_this_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    current = settings(enabled=True)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    monkeypatch.setattr(sync, "today_in", lambda _zone: TODAY)
    server = FakePropertyware()
    real_build = client.build_client

    def build(creds: Any, *, http: Any = None, budget: Any = None) -> Any:
        return real_build(creds, http=server.http(), budget=budget)

    monkeypatch.setattr(client, "build_client", build)
    result = await sync_cycle.sync_one(FakePool(creds=creds_row()), KEY, COMPANY)
    assert isinstance(result, SyncResult) and result.pms == KEY
    assert result.failed == {}
    assert server.requests, "the pass reached Propertyware through the shared cycle"


async def test_the_shared_cycle_never_runs_a_switched_off_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(enabled=False)
    monkeypatch.setattr("helixis.settings.get_settings", lambda: current)
    assert KEY not in sync_cycle.switched_on()
    assert await sync_cycle.sync_one(MagicMock(), KEY, COMPANY) is None
