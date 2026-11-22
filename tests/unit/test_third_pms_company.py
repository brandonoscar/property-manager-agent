"""A company on Rent Manager, before the Rent Manager connector exists.

Phase 0a's promise (design/connector-shared-layer.md): a third system's
credentials row alone must not make the product treat the company as
Buildium. Before 0a, anything that was not Rentvine read as Buildium: the
live Buildium tools showed (each answering "connect Buildium"), every money
detector ran over tables nothing fills, and the Connectors page offered a
Buildium connect the trigger must refuse.

The company here is synthetic: only its ``company_pms`` answer says
"rentmanager". Propertyware takes the same paths (the registry has no
per-system branch), so one system is driven end to end.

⚠ The Rent Manager connector now exists (its branch, 2026-10-05). The tests
whose premise is that it does not stand that up themselves with
:func:`no_connector_yet` (rule 19: re-pointed, same assertions).
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from agents import RunContextWrapper

from helixis.agents import run_pms
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.buildium_tools import buildium_tools
from helixis.agents.orchestrator import _display_name, create_orchestrator
from helixis.db.pms_coverage import PMS_FED_TABLES, blind_tables, fed_tables, unlanded_tables
from helixis.detectors import registry as detectors
from helixis.evals import agent_flow
from helixis.reminders import producers, sweep
from helixis.settings import Settings
from tests.unit.test_buildium_routes import (  # noqa: F401 — fixtures
    _make_token,
    client,
    fake_pool,
    test_settings,
)
from tests.unit.test_connectors_rentvine_row import _rows
from tests.unit.test_pms_coverage import COMPANY, _run_detectors

RENT_MANAGER = "rentmanager"
BUILDIUM_LIVE = {t.name for t in buildium_tools()}


async def _visible(pms: str | None) -> set[str]:
    agent = create_buildium_agent(Settings())
    token = run_pms.bind_run_pms(pms)
    try:
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
    finally:
        run_pms.reset_run_pms(token)
    return {t.name for t in tools}


# ── the chat run ──────────────────────────────────────────────────────


async def test_no_live_buildium_tool_is_visible() -> None:
    visible = await _visible(RENT_MANAGER)
    assert visible & BUILDIUM_LIVE == set()


async def test_the_mirror_tools_are_visible_and_rentvines_creates_are_not() -> None:
    """``rentvine_update_work_order_status`` stays visible, as it is on a
    Buildium run: the negative golden cases need the model to be able to reach
    for it, and it refuses at call time for a company with no Rentvine row
    (``buildium_agent``'s note on the Rentvine write surface)."""
    visible = await _visible(RENT_MANAGER)
    assert {"open_work_orders", "contact_lookup", "property_snapshot", "rent_roll"} <= visible
    creates = {
        "rentvine_create_work_order",
        "rentvine_upload_file",
        "rentvine_post_charge",
        "rentvine_record_payment",
    }
    assert not visible & creates
    assert creates <= await _visible("rentvine")


async def test_buildium_and_an_unresolved_run_see_what_they_saw_before() -> None:
    for pms in ("buildium", None):
        assert BUILDIUM_LIVE <= await _visible(pms)


def test_the_specialist_is_named_for_the_system() -> None:
    token = run_pms.bind_run_pms(RENT_MANAGER)
    try:
        assert _display_name("buildium_agent") == "Rent Manager Agent"
        orchestrator = create_orchestrator(Settings(), loop_mode=False)
    finally:
        run_pms.reset_run_pms(token)
    assert isinstance(orchestrator.instructions, str)
    assert "Buildium" not in orchestrator.instructions
    specialists = [h for h in orchestrator.handoffs if hasattr(h, "instructions")]
    pms_agent = next(h for h in specialists if "Rent Manager" in str(h.instructions))
    assert "Buildium" not in str(pms_agent.instructions)


def test_the_run_is_told_nothing_is_synced_rather_than_zero() -> None:
    token = run_pms.bind_run_pms(RENT_MANAGER)
    try:
        note = run_pms.run_note()
    finally:
        run_pms.reset_run_pms(token)
    assert "Rent Manager" in note and "never say it is zero" in note


def test_evals_can_run_as_every_system() -> None:
    assert {"buildium", "rentvine", "rentmanager", "propertyware"} <= agent_flow._RUN_PMS_VALUES


# ── coverage: every PMS-fed table is blind until the connector exists ──


@pytest.fixture
def no_connector_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rent Manager as it was before its connector: its registry entry,
    pointed at a package that does not exist, read through the REAL lookup
    (``registry.connector_attr`` then answers None, "fills nothing")."""
    import dataclasses

    from helixis.pms import registry

    missing = dataclasses.replace(registry.PMS[RENT_MANAGER], package="helixis.no_such_connector")
    monkeypatch.setattr(registry, "PMS", {**registry.PMS, RENT_MANAGER: missing})


@pytest.mark.usefixtures("no_connector_yet")
def test_every_pms_fed_table_is_blind() -> None:
    assert fed_tables(RENT_MANAGER) == frozenset()
    assert blind_tables(RENT_MANAGER, PMS_FED_TABLES) == PMS_FED_TABLES
    # Tables Occupella writes itself are never blind.
    assert blind_tables(RENT_MANAGER, {"helixis_reminders"}) == frozenset()


@pytest.mark.usefixtures("no_connector_yet")
def test_landing_applies_to_every_system_but_buildium() -> None:
    assert unlanded_tables("buildium", {"helixis_leases"}, ()) == frozenset()
    # No stage map yet, so nothing is held back by landing: blindness does it.
    assert unlanded_tables(RENT_MANAGER, {"helixis_leases"}, ()) == frozenset()


@pytest.mark.usefixtures("no_connector_yet")
async def test_the_detectors_skip_with_blind_set_not_a_clean_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=RENT_MANAGER))
    assert not ran & blind
    reads_pms_data = {
        d.detector_id
        for d in detectors.state_rule_detectors()
        if set(d.reads_tables or ()) & PMS_FED_TABLES
    }
    assert reads_pms_data, "no detector reads PMS data; this test proves nothing"
    assert reads_pms_data <= blind
    assert {"rent_not_collected", "rent_under_charged", "duplicate_vendor_bill"} <= blind


async def test_the_reminder_sweep_skips_every_producer_that_reads_pms_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    def _fake(name: str) -> Any:
        async def produce(pool: Any, company_id: str, **kw: Any) -> list[Any]:
            ran.append(name)
            return []

        return produce

    monkeypatch.setattr(sweep, "PRODUCERS", {n: _fake(n) for n in producers.PRODUCERS})
    monkeypatch.setattr(sweep, "company_pms_strict", AsyncMock(return_value=RENT_MANAGER))
    monkeypatch.setattr(sweep, "company_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(sweep, "get_freshness", AsyncMock(return_value={}))
    monkeypatch.setattr(sweep.reminders_repo, "sync_producer", AsyncMock())
    monkeypatch.setattr(sweep.reminders_repo, "clear_unknown_producers", AsyncMock(return_value=0))
    blind: set[str] = set()
    await sweep.run_reminder_sweep(object(), COMPANY, blind=blind)
    assert not set(ran) & blind
    assert {"bill_due", "renewal_window", "task_overdue"} <= blind


# ── Settings → Connectors ─────────────────────────────────────────────


async def test_the_connectors_page_offers_no_buildium_or_rentvine_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One PMS per workspace: either card could only offer a connect the
    trigger refuses. The Rent Manager card ships with the connector."""
    monkeypatch.setattr("helixis.db.company_pms.company_pms", AsyncMock(return_value=RENT_MANAGER))
    rows = await _rows(monkeypatch, enabled=True)
    assert "buildium" not in rows and "rentvine" not in rows
    assert "gmail" in rows


async def test_a_buildium_workspace_costs_no_extra_query(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = AsyncMock(return_value="buildium")
    monkeypatch.setattr("helixis.db.company_pms.company_pms", lookup)
    rows = await _rows(
        monkeypatch, enabled=True, buildium={"company_id": COMPANY, "last_tested_at": None}
    )
    assert "buildium" in rows
    lookup.assert_not_awaited()


async def test_a_failed_lookup_shows_the_cards_every_workspace_saw_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "helixis.db.company_pms.company_pms", AsyncMock(side_effect=RuntimeError("pool down"))
    )
    rows = await _rows(monkeypatch, enabled=True)
    assert {"buildium", "rentvine"} <= set(rows)


# ── connecting another system is refused with a readable 409 ──────────


def _refused_by_the_trigger(fake_pool: Any, name: str) -> None:  # noqa: F811
    message = (
        f"company company-A is already connected to {name}; one PMS per company "
        "(ADR 0010 a1). Disconnect the existing PMS first."
    )

    async def execute(sql: str, *args: Any) -> None:
        if " ".join(sql.split()).startswith("INSERT INTO public.company_buildium_credentials"):
            raise asyncpg.exceptions.UniqueViolationError(message)

    fake_pool.execute = AsyncMock(side_effect=execute)


def test_a_buildium_connect_is_a_409_naming_rent_manager(client, fake_pool) -> None:  # noqa: F811
    """It was an unhandled 500, which skips CORS: the browser said "failed to
    fetch" and the admin never learned why."""
    _refused_by_the_trigger(fake_pool, "Rent Manager")
    resp = client.put(
        "/api/v1/buildium/credentials",
        headers={"Authorization": f"Bearer {_make_token()}"},
        json={"client_id": "abcdef-1234", "client_secret": "supersecretvalue"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already connected to Rent Manager" in detail
    assert "disconnect Rent Manager first, then connect Buildium" in detail
    assert fake_pool._state["rows"] == {}


def _refusal_handler_key(path: str) -> str:
    """The registry key each connect route passes to ``one_pms_refusal``."""
    from pathlib import Path

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and "UniqueViolationError" in ast.unparse(node.type or ast.Constant(None))
        and "one_pms_refusal" in ast.unparse(node)
    )
    call = next(
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "one_pms_refusal"
    )
    return ast.unparse(call.args[0])


def test_each_connect_route_names_itself_in_the_refusal() -> None:
    """The Rentvine text is pinned by its own route test; this pins that each
    route says which system it was connecting, so neither 409 reads "then
    connect Buildium" from the Rentvine page."""
    assert _refusal_handler_key("src/helixis/api/routes/buildium.py") == "BUILDIUM"
    assert _refusal_handler_key("src/helixis/api/routes/rentvine.py") == "RENTVINE"


def test_the_buildium_handler_catches_only_the_trigger() -> None:
    """A broader ``except`` would turn an outage into "already connected"."""
    from helixis.api.routes import buildium as route

    source = inspect.getsource(route.upsert_credentials)
    assert "except asyncpg.exceptions.UniqueViolationError as exc:" in source
    assert "except Exception" not in source


# ── once the connector exists, landing applies to it as it does to Rentvine ──


#: A stand-in for ``helixis.rentmanager.sync``: one stage per PMS-fed table.
_FAKE_STAGES = {table: f"rm_{table}" for table in PMS_FED_TABLES}


@pytest.fixture
def rent_manager_connector(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Pretend the connector's package exists, exposing a stage map."""
    from helixis.pms import registry

    def _attr(key: str | None, module: str, attr: str) -> Any:
        if (key, module, attr) == (RENT_MANAGER, "sync", "SYNC_STATE_STAGE"):
            return _FAKE_STAGES
        if (key, module, attr) == (RENT_MANAGER, "sync", "DERIVED_TABLES"):
            return frozenset()
        return None

    monkeypatch.setattr(registry, "connector_attr", _attr)
    return _FAKE_STAGES


def test_a_connector_fills_what_its_stage_map_names(rent_manager_connector: Any) -> None:
    assert fed_tables(RENT_MANAGER) == frozenset(_FAKE_STAGES)
    assert blind_tables(RENT_MANAGER, PMS_FED_TABLES) == frozenset()
    assert unlanded_tables(RENT_MANAGER, {"helixis_leases"}, ()) == {"helixis_leases"}
    assert unlanded_tables(RENT_MANAGER, {"helixis_leases"}, {"rm_helixis_leases"}) == set()


async def test_coverage_waits_for_a_table_to_land(
    rent_manager_connector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helixis.db import pms_coverage

    monkeypatch.setattr(pms_coverage, "company_pms_strict", AsyncMock(return_value=RENT_MANAGER))
    monkeypatch.setattr(
        pms_coverage, "get_freshness", AsyncMock(return_value={"rm_helixis_leases": "x"})
    )
    cov = await pms_coverage.coverage_for(object(), COMPANY)
    assert cov.sees("helixis_leases")
    assert not cov.sees("helixis_lease_transactions"), "an unlanded table read as seen"


async def test_detectors_run_once_their_tables_have_landed(
    rent_manager_connector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    every_stage = {stage: "2026-10-04T00:00:00Z" for stage in _FAKE_STAGES.values()}
    ran, blind = await _run_detectors(
        monkeypatch, AsyncMock(return_value=RENT_MANAGER), every_stage
    )
    assert blind == set()
    assert {"rent_not_collected", "duplicate_vendor_bill"} <= ran

    ran, blind = await _run_detectors(monkeypatch, AsyncMock(return_value=RENT_MANAGER), {})
    assert {"rent_not_collected", "duplicate_vendor_bill"} <= blind


async def test_the_sweep_runs_producers_once_their_tables_have_landed(
    rent_manager_connector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def _fake(name: str) -> Any:
        async def produce(pool: Any, company_id: str, **kw: Any) -> list[Any]:
            ran.append(name)
            return []

        return produce

    every_stage = {stage: "2026-10-04T00:00:00Z" for stage in _FAKE_STAGES.values()}
    monkeypatch.setattr(sweep, "PRODUCERS", {n: _fake(n) for n in producers.PRODUCERS})
    monkeypatch.setattr(sweep, "company_pms_strict", AsyncMock(return_value=RENT_MANAGER))
    monkeypatch.setattr(sweep, "company_timezone", AsyncMock(return_value=None))
    monkeypatch.setattr(sweep, "get_freshness", AsyncMock(return_value=every_stage))
    monkeypatch.setattr(sweep.reminders_repo, "sync_producer", AsyncMock())
    monkeypatch.setattr(sweep.reminders_repo, "clear_unknown_producers", AsyncMock(return_value=0))
    blind: set[str] = set()
    await sweep.run_reminder_sweep(object(), COMPANY, blind=blind)
    assert {"bill_due", "task_overdue"} <= set(ran)
    assert blind == set()


# ── the run note reaches the model, in production and in evals ────────


def test_the_orchestrator_adds_the_note_on_every_mirror_only_run() -> None:
    """Rentvine keeps its own branch (pinned by `test_rentvine_chat_routing`);
    every other mirror-only system takes the `elif` right after it."""
    from tests.unit.test_rentvine_chat_routing import _called, _run_agent_streaming_tree

    tree = _run_agent_streaming_tree()
    rentvine_if = next(
        n for n in ast.walk(tree) if isinstance(n, ast.If) and "run_on_rentvine" in _called(n.test)
    )
    (elif_,) = rentvine_if.orelse
    assert isinstance(elif_, ast.If) and "run_on_mirror_pms" in _called(elif_.test)
    body = ast.Module(body=elif_.body, type_ignores=[])
    assert "run_note" in _called(body)
    assert any(isinstance(n, ast.Name) and n.id == "context_block" for n in ast.walk(body))


async def test_an_eval_case_runs_as_rent_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_agent_flow_rentvine_runs import _case, _probe

    seen: dict[str, Any] = {}
    monkeypatch.setattr(agent_flow.Runner, "run", _probe(seen))
    result = await agent_flow.run_agent_flow_case(_case(pms=RENT_MANAGER))
    assert result.error == "_StopError: probe"
    assert not seen["tools"] & BUILDIUM_LIVE
    assert "Rent Manager Agent" in seen["instructions"]
    assert "Buildium" not in seen["instructions"]
    assert seen["input"] == "Show me the rent roll." + run_pms.run_note_for(RENT_MANAGER)


# ── the empty-mirror message and the contact lookup ─────────────────────


@pytest.mark.parametrize("how", ["run binding", "coverage lookup"])
@pytest.mark.parametrize("synced_empty", [False, True])
async def test_an_empty_mirror_is_explained_in_the_systems_own_name(
    how: str, synced_empty: bool
) -> None:
    """Never the Buildium wording ("no Buildium connection on file"), which
    tells a Rent Manager customer to connect a system they do not use; and
    never the live Buildium contact search, which needs Buildium keys."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from helixis.agents import mirror_tools
    from tests.unit.test_mirror_tools import _ctx, _pool

    live = AsyncMock(return_value="People matching 'Zed' (live Buildium)")
    coverage = AsyncMock(
        return_value=SimpleNamespace(pms=RENT_MANAGER if how == "coverage lookup" else None)
    )
    token = run_pms.bind_run_pms(RENT_MANAGER if how == "run binding" else None)
    try:
        with (
            patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
            patch.object(mirror_tools, "get_pool", return_value=_pool(fetch_rows=[])),
            patch.object(
                mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[])
            ),
            patch.object(mirror_tools, "_live_contact_fallback", live),
            patch.object(mirror_tools, "coverage_for", coverage),
            patch.object(
                mirror_tools,
                "_empty_after_a_successful_sync",
                AsyncMock(return_value=synced_empty),
            ),
        ):
            out = await mirror_tools._contact_lookup_impl("Zed")
    finally:
        run_pms.reset_run_pms(token)
    live.assert_not_awaited()
    expected = (
        mirror_tools._mirror_empty_portfolio("Rent Manager")
        if synced_empty
        else mirror_tools._mirror_not_synced("Rent Manager")
    )
    assert out == expected
