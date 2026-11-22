"""The nightly Rentvine mirror refresh, and the wiring that makes it reachable.

⚠ **The AST guards at the bottom are the load-bearing half.** Every behavioural
test here drives ``run_rentvine_sync_cycle`` directly, and a test that drives a
helper directly structurally cannot see that nothing CALLS it — which is the
exact defect this module fixes (``sync_company`` had no production caller at
all). Gotchas 62/66/69/103/121/136/138/146/147 are nine prior instances of the
same blind spot, so the call-site checks are written first-class rather than as
an afterthought.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.rentvine import sync_cycle
from helixis.rentvine.sync import PAGED_TABLES, RentvineSyncDisabled, SyncResult
from helixis.settings import Settings

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "src" / "helixis" / "webhooks" / "worker.py"


@pytest.fixture(autouse=True)
def _fresh_clock():
    """The due-check dict is module-level on purpose (it survives ticks within
    one worker process), so a test that leaves entries behind silently makes
    the next one skip its company."""
    sync_cycle._last_synced.clear()
    yield
    sync_cycle._last_synced.clear()


def _settings(*, enabled: bool = True, interval_hours: float = 24.0) -> Settings:
    s = Settings()
    return s.model_copy(
        update={
            "rentvine": s.rentvine.model_copy(
                update={"enabled": enabled, "sync_interval_hours": interval_hours}
            )
        }
    )


def _result(company_id: str, **kw) -> SyncResult:
    return SyncResult(company_id=company_id, **kw)


# ── the deployment gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dark_deployment_does_not_even_ask_which_companies_exist(monkeypatch):
    """The flag is checked BEFORE the population query, so today's deployment —
    where the flag is false on web and worker — spends nothing per tick."""
    population = AsyncMock(return_value=["c1"])
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", population)
    sync = AsyncMock()
    monkeypatch.setattr(sync_cycle, "sync_company", sync)

    out = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings(enabled=False))

    assert out == {}
    population.assert_not_awaited()
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_enabled_deployment_syncs_every_connected_company(monkeypatch):
    monkeypatch.setattr(
        sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["c1", "c2"])
    )
    sync = AsyncMock(side_effect=lambda _pool, cid, **_kw: _result(cid))
    monkeypatch.setattr(sync_cycle, "sync_company", sync)

    out = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    assert sorted(out) == ["c1", "c2"]
    assert sync.await_count == 2


# ── the interval guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_tick_inside_the_interval_syncs_nobody(monkeypatch):
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["c1"]))
    sync = AsyncMock(side_effect=lambda _pool, cid, **_kw: _result(cid))
    monkeypatch.setattr(sync_cycle, "sync_company", sync)

    first = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())
    second = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    assert first and second == {}
    assert sync.await_count == 1


@pytest.mark.asyncio
async def test_a_company_that_raises_is_not_retried_on_the_very_next_tick(monkeypatch):
    """Attempted is marked BEFORE the call, so a company failing every time
    cannot turn each hourly tick into a full rate-limited crawl."""
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["c1"]))
    sync = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(sync_cycle, "sync_company", sync)

    await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())
    await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    assert sync.await_count == 1


# ── failure posture: never raises, never abandons the rest ─────────────


@pytest.mark.asyncio
async def test_one_company_raising_does_not_abandon_the_others(monkeypatch):
    monkeypatch.setattr(
        sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["bad", "good"])
    )

    async def _sync(_pool, company_id, **_kw):
        if company_id == "bad":
            raise RuntimeError("boom")
        return _result(company_id)

    monkeypatch.setattr(sync_cycle, "sync_company", AsyncMock(side_effect=_sync))

    out = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    assert list(out) == ["good"]


@pytest.mark.asyncio
async def test_a_company_whose_credentials_vanished_is_skipped_quietly(monkeypatch, caplog):
    """The reachable case is a race — the row was deleted between the
    population query and the call. Logging it as a fault is how a canary
    nobody reads gets born (gotcha 84)."""
    monkeypatch.setattr(
        sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["gone", "good"])
    )

    async def _sync(_pool, company_id, **_kw):
        if company_id == "gone":
            raise RentvineSyncDisabled("company gone has no Rentvine connection")
        return _result(company_id)

    monkeypatch.setattr(sync_cycle, "sync_company", AsyncMock(side_effect=_sync))

    with caplog.at_level("INFO"):
        out = await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    assert list(out) == ["good"]
    skipped = [r for r in caplog.records if "skipped company=gone" in r.getMessage()]
    assert skipped, "the skip must be visible"
    assert all(r.levelname == "INFO" for r in skipped), (
        "a company simply not being on Rentvine is the ordinary state — "
        "reporting it at ERROR trains the reader to ignore the log"
    )


@pytest.mark.asyncio
async def test_an_unreadable_population_returns_empty_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        sync_cycle, "with_rentvine_credentials", AsyncMock(side_effect=RuntimeError("no db"))
    )
    sync = AsyncMock()
    monkeypatch.setattr(sync_cycle, "sync_company", sync)

    assert await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings()) == {}
    sync.assert_not_awaited()


# ── the honesty fields reach the log ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_table_is_reported_as_stale_and_not_as_empty(monkeypatch, caplog):
    """A table whose fetch failed is STALE, not empty (gotcha 106). If the log
    only carried the row count, an operator reading "3 rows" would have no way
    to know a whole collection is missing from that number."""
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["c1"]))
    result = _result(
        "c1",
        rows_by_table={"helixis_properties": 3},
        failed={"helixis_vendors": "rentvine 500"},
        confirmed_complete=set(PAGED_TABLES) - {"helixis_vendors"},
    )
    monkeypatch.setattr(sync_cycle, "sync_company", AsyncMock(return_value=result))

    with caplog.at_level("WARNING", logger="helixis.rentvine.sync_cycle"):
        await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "STALE, not empty" in text
    assert "helixis_vendors" in text
    assert "rentvine 500" in text, "the reason is what an operator acts on"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ({}, True),  # nothing answered
        ({"helixis_properties": 0}, False),  # a table answered, with nothing
        ({"helixis_properties": 3}, False),  # a table answered, with rows
    ],
    ids=["nothing-answered", "answered-empty", "answered-rows"],
)
async def test_a_pass_where_nothing_answered_is_an_error_naming_its_tables(
    monkeypatch, caplog, rows, error
):
    """The STALE line is a WARNING, which Sentry keeps only as a breadcrumb; a
    pass failing every table every day must reach Sentry as an ERROR."""
    monkeypatch.setattr(sync_cycle, "with_rentvine_credentials", AsyncMock(return_value=["c1"]))
    monkeypatch.setattr(sync_cycle, "enrich_places", AsyncMock())
    result = _result(
        "c1",
        rows_by_table=rows,
        failed={"helixis_vendors": "401 for tenant Jane Roe", "helixis_owners": "401"},
    )
    monkeypatch.setattr(sync_cycle, "sync_company", AsyncMock(return_value=result))

    with caplog.at_level("WARNING", logger="helixis.rentvine.sync_cycle"):
        await sync_cycle.run_rentvine_sync_cycle(MagicMock(), _settings())

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    if not error:
        assert errors == []
        return
    assert len(errors) == 1
    assert "c1" in errors[0] and "helixis_owners, helixis_vendors" in errors[0]
    # The vendor's own text stays in the WARNING, off Sentry.
    assert "Jane" not in errors[0] and "401" not in errors[0]


def test_a_collection_rentvine_cannot_page_is_never_called_unconfirmed():
    """``helixis_leases``/``helixis_units`` can NEVER be confirmed — Rentvine
    cannot page them. Naming them nightly is a caveat that is always true and
    therefore worthless (gotchas 57/93)."""
    every_paged_source_confirmed = _result("c1", confirmed_complete=set(PAGED_TABLES))

    assert sync_cycle._unconfirmed_paged_sources(every_paged_source_confirmed) == []


def test_a_failed_table_is_not_also_reported_as_unconfirmed():
    """It is in ``failed``; reporting the same fact twice under a second name
    makes one problem read as two."""
    out = sync_cycle._unconfirmed_paged_sources(
        _result(
            "c1",
            failed={"helixis_vendors": "rentvine 500"},
            confirmed_complete=set(PAGED_TABLES) - {"helixis_vendors"},
        )
    )

    assert out == []


def test_a_paged_source_that_stopped_on_a_short_page_is_reported():
    """The distinguishing input: a collection that SHOULD have carried
    ``pagination-total-pages`` and did not. Without this the two subtractions
    above would be indistinguishable from a function that reports nothing."""
    out = sync_cycle._unconfirmed_paged_sources(
        _result(
            "c1",
            rows_by_table={"helixis_vendors": 7},
            confirmed_complete=set(PAGED_TABLES) - {"helixis_vendors"},
        )
    )

    assert out == ["helixis_vendors"]


# ── the wiring (gotcha 62: a behavioural test cannot see an absent caller) ──


def _worker_tree() -> ast.Module:
    return ast.parse(WORKER.read_text(encoding="utf-8"))


def _awaited_calls(tree: ast.AST, name: str) -> list[ast.Await]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name:
            out.append(node)
    return out


def test_the_worker_actually_awaits_the_cycle():
    """⚠ An ``Await`` of a ``Call``, not a mention. A presence check is
    satisfied by ``_ = run_rentvine_sync_cycle`` sitting beside a loop that
    never runs it — gotchas 94/128/141/143, four prior instances."""
    assert _awaited_calls(_worker_tree(), "run_rentvine_sync_cycle"), (
        "webhooks/worker.py must AWAIT run_rentvine_sync_cycle — without a call "
        "site the cycle is exactly the dead code it was written to replace"
    )


def test_the_import_is_gated_on_the_deployment_flag():
    """A Buildium-only install must not import the Rentvine stack at all, and
    the flag must be the thing that decides."""
    tree = _worker_tree()
    gated = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        flag = any(
            isinstance(sub, ast.Attribute)
            and sub.attr == "enabled"
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "rentvine"
            for sub in ast.walk(node.test)
        )
        if not flag:
            continue
        imports = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.ImportFrom) and sub.module == "helixis.rentvine.sync_cycle"
        ]
        if imports:
            gated = True
    assert gated, (
        "the `from helixis.rentvine.sync_cycle import ...` in worker.py must sit "
        "under `if settings.rentvine.enabled:`"
    )


def test_the_cycle_reads_the_population_from_the_one_home():
    """``db/company_population.py`` exists because five cycles each derived
    their own company list and nobody could see that they disagreed
    (gotcha 62). A sixth private query here would restart that."""
    src = (REPO / "src" / "helixis" / "rentvine" / "sync_cycle.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "helixis.db.company_population"
        and any(a.name == "with_rentvine_credentials" for a in n.names)
        for n in ast.walk(tree)
    )
    assert "company_rentvine_credentials" not in src, (
        "the cycle must not carry its own population SQL — that is the drift "
        "company_population.py was created to end"
    )
