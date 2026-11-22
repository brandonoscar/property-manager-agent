"""Unit tests for the automatic mirror re-sync cycle (webhooks/mirror_sync.py).

Pins the cycle contract: syncs every connected company, skips companies
inside their interval, marks-before-run (a failing company can't crawl on
every tick), isolates per-company failures, and never raises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium.backfill import BackfillResult, fetched_nothing, keys_refused
from helixis.buildium.errors import BuildiumApiError, BuildiumRetryableError
from helixis.webhooks import mirror_sync


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool_with_rows(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    return pool


def _row(company_id: str = "co-1", account_id: int = 7) -> dict:
    return {
        "company_id": company_id,
        "encrypted_client_id": "enc-id",
        "encrypted_client_secret": "enc-secret",
        "base_url": "https://apisandbox.buildium.com",
        "account_id": account_id,
    }


@pytest.fixture(autouse=True)
def _reset_guard():
    mirror_sync._last_synced.clear()
    yield
    mirror_sync._last_synced.clear()


def _settings(interval_hours: float = 24.0) -> MagicMock:
    s = MagicMock()
    s.buildium.mirror_sync_interval_hours = interval_hours
    return s


@pytest.mark.asyncio
async def test_syncs_every_connected_company() -> None:
    pool = _pool_with_rows([_row("co-1", 7), _row("co-2", 8)])
    backfill = AsyncMock(return_value=MagicMock(errors=[]))
    with (
        patch.object(mirror_sync, "get_settings", return_value=_settings()),
        patch.object(mirror_sync, "decrypt_buildium_settings", return_value=MagicMock()),
        patch.object(mirror_sync, "backfill_company", backfill),
    ):
        results = await mirror_sync.run_mirror_sync_cycle(pool)
    assert set(results) == {"co-1", "co-2"}
    assert backfill.await_count == 2
    # Keyword wiring: company_id + account_id reach the backfill.
    kwargs = backfill.await_args_list[0].kwargs
    assert kwargs["company_id"] == "co-1"
    assert kwargs["account_id"] == 7


@pytest.mark.asyncio
async def test_skips_company_inside_interval() -> None:
    pool = _pool_with_rows([_row("co-1")])
    backfill = AsyncMock(return_value=MagicMock(errors=[]))
    with (
        patch.object(mirror_sync, "get_settings", return_value=_settings()),
        patch.object(mirror_sync, "decrypt_buildium_settings", return_value=MagicMock()),
        patch.object(mirror_sync, "backfill_company", backfill),
    ):
        await mirror_sync.run_mirror_sync_cycle(pool)
        # Second tick within the interval: nothing due.
        results = await mirror_sync.run_mirror_sync_cycle(pool)
    assert backfill.await_count == 1
    assert results == {}


@pytest.mark.asyncio
async def test_failing_company_is_isolated_and_not_retried_same_interval() -> None:
    pool = _pool_with_rows([_row("co-bad", 7), _row("co-good", 8)])
    backfill = AsyncMock(side_effect=[RuntimeError("buildium 500"), MagicMock(errors=[])])
    with (
        patch.object(mirror_sync, "get_settings", return_value=_settings()),
        patch.object(mirror_sync, "decrypt_buildium_settings", return_value=MagicMock()),
        patch.object(mirror_sync, "backfill_company", backfill),
    ):
        results = await mirror_sync.run_mirror_sync_cycle(pool)
        # Next tick: the failure was marked-before-run, so no immediate retry.
        second = await mirror_sync.run_mirror_sync_cycle(pool)
    assert "co-good" in results and "co-bad" not in results
    assert second == {}
    assert backfill.await_count == 2


@pytest.mark.asyncio
async def test_listing_failure_never_raises() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    with patch.object(mirror_sync, "get_settings", return_value=_settings()):
        results = await mirror_sync.run_mirror_sync_cycle(pool)
    assert results == {}


@pytest.mark.asyncio
async def test_decrypt_failure_is_isolated() -> None:
    pool = _pool_with_rows([_row("co-1")])
    with (
        patch.object(mirror_sync, "get_settings", return_value=_settings()),
        patch.object(
            mirror_sync,
            "decrypt_buildium_settings",
            side_effect=RuntimeError("HELIXIS_DATA_KEY rotated"),
        ),
        patch.object(mirror_sync, "backfill_company", AsyncMock()) as backfill,
    ):
        results = await mirror_sync.run_mirror_sync_cycle(pool)
    assert results == {}
    backfill.assert_not_awaited()


# ── a pull that fetched nothing is an ERROR (Sentry's threshold) ─────────────


def _failed(*stages: str, **counts: int) -> BackfillResult:
    result = BackfillResult(**counts)
    for stage in stages:
        result.record_failure(stage, RuntimeError("401 Unauthorized for tenant Jane Roe"))
    return result


def test_fetched_nothing_needs_failures_and_no_rows() -> None:
    assert fetched_nothing(_failed("properties", "units", "tenants"))
    # An empty account that answered is not a dead connection.
    assert not fetched_nothing(BackfillResult())
    # One flaky stage beside rows that came back is the stages' WARNING, as before.
    assert not fetched_nothing(_failed("properties", units=3))
    # Every count field counts, the last one declared included.
    assert not fetched_nothing(_failed("properties", bank_accounts=1))


async def _cycle_with(result: BackfillResult) -> MagicMock:
    pool = _pool_with_rows([_row("co-dead")])
    with (
        patch.object(mirror_sync, "get_settings", return_value=_settings()),
        patch.object(mirror_sync, "decrypt_buildium_settings", return_value=MagicMock()),
        patch.object(mirror_sync, "backfill_company", AsyncMock(return_value=result)),
        patch.object(mirror_sync, "geocode_company_properties", AsyncMock()),
        patch.object(mirror_sync, "enrich_company_properties", AsyncMock()),
        patch.object(mirror_sync, "logger") as log,
    ):
        await mirror_sync.run_mirror_sync_cycle(pool)
    return log


@pytest.mark.asyncio
async def test_a_pull_that_fetched_nothing_is_an_error_naming_its_stages() -> None:
    """The stages log WARNINGs, which Sentry keeps only as breadcrumbs, so a
    pull that failed every day left no event anywhere."""
    log = await _cycle_with(_failed("properties", "units"))
    log.error.assert_called_once()
    message = log.error.call_args.args[0] % log.error.call_args.args[1:]
    assert "co-dead" in message and "properties, units" in message
    # The vendor's own text stays in the stage WARNINGs, off Sentry.
    assert "Jane" not in message and "401" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [BackfillResult(), _failed("units", properties=2)],
    ids=["clean", "one-stage-failed"],
)
async def test_a_pull_that_brought_rows_back_is_not_an_error(result: BackfillResult) -> None:
    log = await _cycle_with(result)
    log.error.assert_not_called()


# ── a pull Buildium refused says so (the cause on helixis-test, 2026-10-05) ──


def _refused(*stages: str, status: int = 401, **counts: int) -> BackfillResult:
    result = BackfillResult(**counts)
    for stage in stages:
        result.record_failure(
            stage, BuildiumApiError(status, "Authentication failed.", path=f"/{stage}")
        )
    return result


def test_a_failure_carries_the_status_buildium_answered_with() -> None:
    result = BackfillResult()
    result.record_failure("properties", BuildiumApiError(401, "no", path="/rentals"))
    result.record_failure("bills", BuildiumRetryableError(503, "busy"), detail="paid")
    result.record_failure("units", RuntimeError("an exception of ours"))
    assert result.failure_statuses == [401, 503, None]
    # Beside the messages, which every existing reader still sees.
    assert len(result.errors) == 3


def test_keys_refused_needs_a_401_on_every_failure() -> None:
    assert keys_refused(_refused("properties", "units", "tenants"))
    # A 500 among the 401s is not the keys.
    mixed = _refused("properties", "units")
    mixed.record_failure("tenants", BuildiumApiError(500, "boom", path="/leases/tenants"))
    assert not keys_refused(mixed)
    # Nor is a failure Buildium never answered (a timeout, an exception of ours).
    assert not keys_refused(_failed("properties", "units"))
    # Rows came back: one stage refused is not a pull that fetched nothing.
    assert not keys_refused(_refused("properties", units=3))
    # A 403 is a permission, not the keys (gotcha 102).
    assert not keys_refused(_refused("properties", "units", status=403))
    # An error recorded past record_failure has no status, so all() of the
    # shorter list cannot pass for it.
    short = _refused("properties")
    short.errors.append("transactions: written without record_failure")
    assert not keys_refused(short)
    assert not keys_refused(BackfillResult())


@pytest.mark.asyncio
async def test_a_pull_buildium_refused_says_the_keys_were_refused() -> None:
    """Its own sentence, so Sentry files it apart from an outage: new keys are
    the customer's to enter."""
    log = await _cycle_with(_refused("properties", "units"))
    log.error.assert_called_once()
    message = log.error.call_args.args[0] % log.error.call_args.args[1:]
    assert "co-dead" in message and "Buildium refused the saved keys (401)" in message
    # Still none of the vendor's own words.
    assert "Authentication failed" not in message


@pytest.mark.asyncio
async def test_a_pull_that_failed_some_other_way_keeps_the_stage_names() -> None:
    mixed = _refused("properties")
    mixed.record_failure("units", BuildiumApiError(500, "boom", path="/rentals/units"))
    log = await _cycle_with(mixed)
    log.error.assert_called_once()
    message = log.error.call_args.args[0] % log.error.call_args.args[1:]
    assert "every stage failed (properties, units)" in message
    assert "refused" not in message
