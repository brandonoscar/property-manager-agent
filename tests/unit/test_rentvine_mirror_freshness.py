"""A Rentvine mirror must be able to report itself stale — and the sentence
that says so must not name the wrong property-management system.

THE MEASURED DEFECT, in two halves that mask each other:

1. ``rentvine/sync.py`` wrote no ``helixis_mirror_sync_state`` rows. That
   table is the ONLY thing that can produce a staleness verdict:
   ``mirror_sync_state.assess`` reads it, ``STALE_AFTER_HOURS`` is the
   threshold, and the chat footer, ``GET /mirror/freshness`` and the
   connectors card's ``lastSyncAt`` all hang off the verdict. So a nightly
   Rentvine sync that broke a week ago answered every question with week-old
   figures and nothing anywhere said so.

2. ``mirror_tools._freshness_line``'s stale branch asked
   ``_buildium_is_connected``, which for a Rentvine company is ``False`` BY
   CONSTRUCTION — migration 0100's ``assert_one_pms_per_company`` trigger
   guarantees they hold no Buildium row. So the footer would have told a
   customer whose connection was working perfectly to go and reconnect a
   system they have never used.

⚠ **THE SECOND IS LATENT ONLY BECAUSE OF THE FIRST.** With no stamped rows
``get_freshness`` returns empty and ``_freshness_line`` exits on the legacy
branch before the stale arm is ever reached — so fixing (1) alone would have
SHIPPED (2) to every Rentvine customer. They are one change.

The third assertion here is a third instance of the same theme:
``_as_of_line`` attributed every mirror read to "synced Buildium data", on the
footer ``test_mirror_freshness_contract`` forces onto all of them.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents import mirror_tools
from helixis.db.repositories import mirror_sync_state
from helixis.rentvine import sync
from helixis.rentvine.mapper import MAPPERS
from helixis.settings import Settings

_SYNC = pathlib.Path(sync.__file__)
_MIRROR = pathlib.Path(mirror_tools.__file__)


# --------------------------------------------------------------------------
# The stage map — read BOTH sides (gotcha 28)
# --------------------------------------------------------------------------


def test_every_table_the_mapper_writes_has_a_stage_name() -> None:
    """A mirror table with no entry is a table that silently never stamps.

    Re-pointed 2026-10-01: ``helixis_lease_tenants`` is written too, from the
    lease rows rather than by a ``MAPPERS`` entry of its own, so it is the ONE
    table in the stage map with no mapper — named exactly, not tolerated."""
    assert set(MAPPERS) <= set(sync.SYNC_STATE_STAGE), (
        "a Rentvine mirror table is missing from SYNC_STATE_STAGE, so its "
        "freshness is never recorded and it can never report itself stale"
    )
    # Re-pointed 2026-10-01: lease balances are written from the lease rows
    # too (sync._replace_balances), with the company's date for aging.
    assert set(sync.SYNC_STATE_STAGE) - set(MAPPERS) == {
        "helixis_lease_tenants",
        "helixis_lease_balances",
        # Bill lines ride on the payables rows (sync._write_bill_lines).
        "helixis_bill_lines",
        # Lease history rides on the rent read (sync._write_lease_transactions).
        "helixis_lease_transactions",
    }


def test_every_stage_name_is_one_the_freshness_verdict_considers() -> None:
    """⚠ The half that would fail SILENTLY.

    ``assess`` filters on ``CORE_TABLES``, so a stage name outside that tuple
    stores a row nothing ever reads — the mirror stays unable to report itself
    stale while a table full of rows says otherwise. Stamping the full
    ``helixis_*`` name instead of the bare stage is exactly that mistake.
    """
    # Re-pointed 2026-10-02: reference stages (the chart of accounts) are
    # stamped outside CORE_TABLES on purpose, exactly as the Buildium backfill
    # stamps them; they are named, not tolerated.
    unknown = (
        set(sync.SYNC_STATE_STAGE.values())
        - set(mirror_sync_state.CORE_TABLES)
        - sync.REFERENCE_STAGES
    )
    assert sync.REFERENCE_STAGES.isdisjoint(mirror_sync_state.CORE_TABLES)
    assert not unknown, (
        f"{sorted(unknown)} are not in mirror_sync_state.CORE_TABLES, so "
        "assess() drops them and the stamp buys nothing"
    )


def test_the_stage_names_are_the_ones_buildium_already_writes() -> None:
    """One vocabulary, not two. ``helixis_mirror_sync_state`` is keyed on the
    bare stage name because the Buildium backfill has written it that way
    since 0057; a second spelling would give one company two rows for one
    table and make the stalest-table verdict read whichever it liked."""
    tree = ast.parse(
        pathlib.Path(__import__("helixis.buildium.backfill", fromlist=["x"]).__file__).read_text(
            encoding="utf-8"
        )
    )
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and "freshness" in node.name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    literals.add(sub.value)
    assert literals, "the backfill's freshness stage list could not be read"
    shared = set(sync.SYNC_STATE_STAGE.values()) & literals
    assert len(shared) >= 5, (
        f"only {sorted(shared)} of Rentvine's stage names appear in the "
        "Buildium backfill's own list — the two connectors are keying the "
        "same table on different vocabularies"
    )


# --------------------------------------------------------------------------
# The sync actually stamps, and refuses to stamp what it must not
# --------------------------------------------------------------------------


def _enabled() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


def _pool() -> Any:
    pool = MagicMock()
    conn = AsyncMock()
    # asyncpg's `transaction()` is a plain call returning an async context
    # manager (the lease prune runs inside one), and `fetch` returns rows.
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetch = AsyncMock(return_value=[])
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    # The owner-to-property join writes inside a transaction; an AsyncMock's
    # `transaction()` would return a coroutine, not a context manager.
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetch = AsyncMock(return_value=[])
    pool._conn = conn
    return pool


def _stamped(pool: Any) -> dict[str, int | None]:
    """table_name → row_count for every sync-state upsert the pass issued."""
    out: dict[str, int | None] = {}
    for call in pool._conn.execute.await_args_list:
        if "helixis_mirror_sync_state" in call.args[0]:
            out[call.args[2]] = call.args[3]
    return out


def _client(handler: Any) -> Any:
    from helixis.rentvine.client import RentvineClient
    from helixis.settings import RentvineSettings

    client = RentvineClient(RentvineSettings(account_code="acct", api_key="k", api_secret="s"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


_PROPERTY = {
    "propertyID": "7",
    "name": "Garden Row",
    "address": {"address1": "1 Main", "city": "Denver", "state": "CO", "zip": "80202"},
}


@pytest.mark.asyncio
async def test_a_successful_sync_stamps_every_table_it_refreshed() -> None:
    """The headline: without this the staleness threshold is unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/properties?" in url:
            return httpx.Response(
                200,
                json={"properties": [{"property": _PROPERTY}]},
                headers={"pagination-total-pages": "1", "pagination-total-items": "1"},
            )
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _pool()
    result = await sync.sync_company(pool, "c", settings=_enabled(), client=_client(handler))
    stamped = _stamped(pool)
    assert "properties" in stamped, (
        "a completed properties fetch left no mirror_sync_state row, so this "
        "company's mirror can never be judged stale"
    )
    assert stamped["properties"] == result.rows_by_table["helixis_properties"]


@pytest.mark.asyncio
async def test_the_stamp_carries_the_row_count_and_this_connectors_source() -> None:
    """``row_count`` is what separates "we never asked" from "your account has
    none" (``get_stage_sync``'s own reason for existing), and ``source`` is the
    one place a row says which connector wrote it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _pool()
    await sync.sync_company(pool, "c", settings=_enabled(), client=_client(handler))
    sources = {
        call.args[4]
        for call in pool._conn.execute.await_args_list
        if "helixis_mirror_sync_state" in call.args[0]
    }
    assert sources == {sync.SYNC_SOURCE}
    assert _stamped(pool)["properties"] == 0


@pytest.mark.asyncio
async def test_a_failed_table_is_not_stamped_fresh() -> None:
    """⚠ A stamp outranks the truth. ``failed`` says the table is STALE rather
    than empty (gotcha 106); stamping ``synced_at = now()`` beside it would
    make the footer report a current mirror for a table the same result
    describes as not synced."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/vendors" in url:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _pool()
    result = await sync.sync_company(pool, "c", settings=_enabled(), client=_client(handler))
    assert "helixis_vendors" in result.failed
    stamped = _stamped(pool)
    assert "vendors" not in stamped
    assert "properties" in stamped, "the other tables must still stamp"


@pytest.mark.asyncio
async def test_units_are_not_stamped_when_the_property_fetch_failed() -> None:
    """Gotcha 104's derived-stage rule, held BEHAVIOURALLY rather than by a
    second copy of the backfill's ``_STAGE_WORK_LIST_FROM`` map.

    The units walk never ran — its work-list came from a fetch that failed —
    so a stamped ``units`` row reading zero would mean "we asked Rentvine and
    this customer has no units", which is a fabricated absence.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "/properties?" in str(request.url):
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool = _pool()
    result = await sync.sync_company(pool, "c", settings=_enabled(), client=_client(handler))
    assert "helixis_units" in result.failed
    assert "units" not in _stamped(pool)
    assert "properties" not in _stamped(pool)


@pytest.mark.asyncio
async def test_a_partially_failed_units_walk_is_not_stamped_either() -> None:
    """⚠ THE CASE A ``rows_by_table`` CHECK ALONE WOULD MISS. ``_sync_units``
    reports its count even on a partial walk (gotcha 160), so units can carry
    a non-zero count AND a failure at once — the one table where "has rows"
    and "is current" genuinely come apart."""
    result = sync.SyncResult(company_id="c")
    result.rows_by_table["helixis_units"] = 41
    result.failed["helixis_units"] = "2 of 66 properties' units failed"
    pool = _pool()
    await sync._record_freshness(pool, result)
    assert "units" not in _stamped(pool)


@pytest.mark.asyncio
async def test_a_table_that_never_ran_is_not_stamped() -> None:
    """No count means the stage was never reached. A stamp would claim we
    asked."""
    result = sync.SyncResult(company_id="c")
    result.rows_by_table["helixis_properties"] = 3
    pool = _pool()
    await sync._record_freshness(pool, result)
    assert set(_stamped(pool)) == {"properties"}


def test_the_sync_actually_calls_the_recorder() -> None:
    """⚠ A behavioural test of ``_record_freshness`` cannot see that nothing
    calls it — the ``comms_triage`` shape this repo has recorded thirteen
    times. Walk the call site."""
    tree = ast.parse(_SYNC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "sync_company":
            called = {ast.unparse(sub.func) for sub in ast.walk(node) if isinstance(sub, ast.Call)}
            assert "_record_freshness" in called, (
                "sync_company no longer records freshness, so a Rentvine "
                "mirror is back to being unable to report itself stale"
            )
            return
    raise AssertionError("sync_company not found")


# --------------------------------------------------------------------------
# The footer stops naming the wrong system
# --------------------------------------------------------------------------


def test_the_as_of_footer_names_no_vendor() -> None:
    """It is appended to EVERY mirror read and the model paraphrases it, so on
    a Rentvine company it was a false statement about a named third party on
    every question they asked."""
    for line in (mirror_tools._as_of_line(None), mirror_tools._as_of_line(datetime.now(UTC))):
        assert "Buildium" not in line
        assert "Rentvine" not in line
        assert "Occupella" in line, "the brand pin (gotcha: the Helixis rename) still holds"


@pytest.mark.asyncio
async def test_a_rentvine_company_is_not_told_to_reconnect_buildium() -> None:
    """⚠ THE SENTENCE THE FIRST HALF OF THIS CHANGE WOULD HAVE UNMASKED.

    Migration 0100's trigger guarantees a Rentvine company holds no Buildium
    row, so the old ``_buildium_is_connected(...) is False`` check was True by
    construction for every one of them.
    """
    stale = {"properties": datetime.now(UTC) - timedelta(hours=100)}
    pool = _pool()
    with (
        patch.object(mirror_tools.sync_state, "get_freshness", AsyncMock(return_value=stale)),
        patch.object(mirror_tools, "_recent_write_note", AsyncMock(return_value="")),
        patch.object(mirror_tools.credentials, "fetch_buildium_row", AsyncMock(return_value=None)),
        patch.object(
            mirror_tools, "fetch_rentvine_row", AsyncMock(return_value={"company_id": "c"})
        ),
    ):
        line = await mirror_tools._freshness_line(pool, "c")
    assert "100h ago" in line, "the staleness warning must still fire"
    assert "Buildium" not in line
    assert "NOT" not in line, (
        "a connected Rentvine company was told its mirror will not catch up "
        "on its own — it will, on the next sync cycle"
    )


@pytest.mark.asyncio
async def test_a_company_with_no_connection_at_all_is_still_warned() -> None:
    """The other direction (gotchas 38/51). A helper that answered "connected"
    for everybody would pass the test above and delete the warning that made
    the branch worth having."""
    stale = {"properties": datetime.now(UTC) - timedelta(hours=100)}
    pool = _pool()
    with (
        patch.object(mirror_tools.sync_state, "get_freshness", AsyncMock(return_value=stale)),
        patch.object(mirror_tools, "_recent_write_note", AsyncMock(return_value="")),
        patch.object(mirror_tools.credentials, "fetch_buildium_row", AsyncMock(return_value=None)),
        patch.object(mirror_tools, "fetch_rentvine_row", AsyncMock(return_value=None)),
        # "No connection at all" covers Rent Manager's and Propertyware's
        # tables too (re-pointed, rule 19: this stated two systems of four).
        patch.object(
            mirror_tools, "_holds_pms_connector_credentials", AsyncMock(return_value=False)
        ),
    ):
        line = await mirror_tools._freshness_line(pool, "c")
    assert "will NOT" in line
    assert "Settings → Connectors" in line
    # ⚠ Names no system: the credentials row was the only record of which one
    # they used, and it is gone. An instruction that cannot be wrong beats a
    # specific one that can be (gotchas 55/102).
    assert "Buildium" not in line
    assert "Rentvine" not in line


@pytest.mark.asyncio
async def test_an_unreadable_connection_state_claims_nothing() -> None:
    """``None`` is UNKNOWN and never collapses into "disconnected" — telling a
    customer their mirror is frozen on the strength of a transient read error
    is a fresh false claim (gotcha 55)."""
    stale = {"properties": datetime.now(UTC) - timedelta(hours=100)}
    pool = _pool()
    with (
        patch.object(mirror_tools.sync_state, "get_freshness", AsyncMock(return_value=stale)),
        patch.object(mirror_tools, "_recent_write_note", AsyncMock(return_value="")),
        patch.object(
            mirror_tools.credentials,
            "fetch_buildium_row",
            AsyncMock(side_effect=RuntimeError("db")),
        ),
    ):
        line = await mirror_tools._freshness_line(pool, "c")
    assert "100h ago" in line
    assert "will NOT" not in line


@pytest.mark.asyncio
async def test_a_buildium_company_still_gets_the_warning_it_always_had() -> None:
    """No regression for the connector that has customers today."""
    stale = {"properties": datetime.now(UTC) - timedelta(hours=100)}
    pool = _pool()
    with (
        patch.object(mirror_tools.sync_state, "get_freshness", AsyncMock(return_value=stale)),
        patch.object(mirror_tools, "_recent_write_note", AsyncMock(return_value="")),
        patch.object(mirror_tools.credentials, "fetch_buildium_row", AsyncMock(return_value=None)),
        patch.object(mirror_tools, "fetch_rentvine_row", AsyncMock(return_value=None)),
        # "No connection at all" covers Rent Manager's and Propertyware's
        # tables too (re-pointed, rule 19: this stated two systems of four).
        patch.object(
            mirror_tools, "_holds_pms_connector_credentials", AsyncMock(return_value=False)
        ),
    ):
        gone = await mirror_tools._freshness_line(pool, "c")
    with (
        patch.object(mirror_tools.sync_state, "get_freshness", AsyncMock(return_value=stale)),
        patch.object(mirror_tools, "_recent_write_note", AsyncMock(return_value="")),
        patch.object(
            mirror_tools.credentials,
            "fetch_buildium_row",
            AsyncMock(return_value={"company_id": "c"}),
        ),
    ):
        live = await mirror_tools._freshness_line(pool, "c")
    assert "will NOT" in gone
    assert "will NOT" not in live


def test_the_freshness_footer_does_not_ask_the_buildium_only_question() -> None:
    """⚠ Pinned at the CALL SITE. ``_buildium_is_connected`` legitimately
    survives for ``_not_synced``, whose question really IS "can the live
    Buildium tools work" — so a behavioural test cannot see the footer
    quietly going back to it, and both helpers return a plausible answer."""
    tree = ast.parse(_MIRROR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_freshness_line":
            called = {ast.unparse(sub.func) for sub in ast.walk(node) if isinstance(sub, ast.Call)}
            assert "_mirror_will_catch_up" in called
            assert "_buildium_is_connected" not in called, (
                "the staleness footer is asking whether BUILDIUM is connected "
                "again — for a Rentvine company that is False by construction "
                "(migration 0100's one-PMS trigger)"
            )
            return
    raise AssertionError("_freshness_line not found")


def test_the_catch_up_check_reads_every_system_s_credential_table() -> None:
    """Reading one of them is the original defect. ⚠ Source-level because a
    helper that consulted only Buildium and happened to be handed a Buildium
    fixture passes every behavioural case above.

    ⚠ **CALLS, not a substring scan — the first cut failed against the correct
    code.** This function's own docstring names ``db.company_pms`` in order to
    explain why it is NOT reused, so a substring check fires on the sentence
    written to stop somebody reusing it (gotcha 40, for at least the
    seventeenth time). A docstring is an ``ast.Constant`` and never a call.
    """
    tree = ast.parse(_MIRROR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_mirror_will_catch_up":
            called = {ast.unparse(sub.func) for sub in ast.walk(node) if isinstance(sub, ast.Call)}
            names = {name.rsplit(".", 1)[-1] for name in called}
            assert "fetch_buildium_row" in names
            assert "fetch_rentvine_row" in names
            # Re-pointed (rule 19): "both" was every system there was. Rent
            # Manager and Propertyware keep their keys in their own tables.
            assert "_holds_pms_connector_credentials" in names
            assert "company_pms" not in names, (
                "db.company_pms fails OPEN to 'buildium', which for this "
                "caller produces the exact wrong sentence"
            )
            return
    raise AssertionError("_mirror_will_catch_up not found")


# --------------------------------------------------------------------------
# Two tables share the "leases" stage (2026-10-01)
# --------------------------------------------------------------------------


def _clean_result(**over: Any) -> sync.SyncResult:
    result = sync.SyncResult(company_id="c")
    result.rows_by_table = {table: 3 for table in sync.SYNC_STATE_STAGE}
    result.rows_by_table["helixis_lease_tenants"] = 5
    for key, value in over.items():
        setattr(result, key, value)
    return result


async def _stamps(result: sync.SyncResult) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    async def record(
        _pool: Any, *, company_id: str, table_name: str, row_count: int, source: str
    ) -> None:
        calls.append((table_name, row_count))

    with patch.object(sync, "record_sync", record):
        await sync._record_freshness(MagicMock(), result)
    return calls


@pytest.mark.asyncio
async def test_a_clean_pass_stamps_every_stage_once_each() -> None:
    """The stage set a Rentvine pass stamps is unchanged by the links joining
    "leases", and "leases" is stamped ONCE, with the LEASE count."""
    calls = await _stamps(_clean_result())
    assert sorted(name for name, _ in calls) == sorted(
        # Re-pointed 2026-10-01: Rentvine bills and balances sync under
        # Buildium's "bills" and "balances" stages.
        [
            "properties",
            "units",
            "leases",
            "tenants",
            "owners",
            "vendors",
            "work_orders",
            "bills",
            "balances",
            "gl_accounts",
            "applicants",
            # Re-pointed 2026-10-02: lease history, under Buildium's name.
            "transactions",
        ]
    )
    assert dict(calls)["leases"] == 3  # helixis_leases' count, not the links' 5


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["helixis_leases", "helixis_lease_tenants"])
async def test_leases_are_not_stamped_when_either_shared_table_is_stale(stale: str) -> None:
    """Capped leases with clean links (or the reverse) must not call the lease
    mirror fresh: one stage, two tables, both must be clean."""
    calls = await _stamps(_clean_result(failed={stale: "rent read for 250 of 400"}))
    names = [name for name, _ in calls]
    assert "leases" not in names
    assert "properties" in names  # the rest still stamp


@pytest.mark.asyncio
async def test_a_buildium_backfill_stamps_exactly_the_stages_it_did_before() -> None:
    """Founder request 2026-10-01: the Rentvine freshness change must not alter
    Buildium. Buildium stamps through ``backfill._record_stage_freshness``,
    never ``sync._record_freshness``; this pins its stage list as it stood on
    main before the change, so any drift fails here."""
    from helixis.buildium import backfill

    stamped: list[str] = []

    async def record(_pool: Any, *, company_id: str, table_name: str, **_kw: Any) -> None:
        stamped.append(table_name)

    with patch("helixis.db.repositories.mirror_sync_state.record_sync", record):
        await backfill._record_stage_freshness(
            MagicMock(), company_id="c", result=backfill.BackfillResult()
        )
    assert stamped == [
        "properties",
        "units",
        "tenants",
        "leases",
        "balances",
        "transactions",
        "work_orders",
        "applicants",
        "vendors",
        "owners",
        "tasks",
        "gl_accounts",
        "bills",
        "bank_accounts",
    ]
    assert "lease_tenants" not in stamped
