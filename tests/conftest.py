"""Shared test fixtures."""

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Tests must NEVER emit to the real Sentry project. A developer with
# HELIXIS_SENTRY__DSN exported in their shell would otherwise have the app
# lifespan (TestClient startup) initialise Sentry with env=test and capture
# every *intentional* test failure — the ~60-issue "test burst" that polluted
# the prod dashboard on 2026-07-08 (BadDsn, "boom", SystemExit, MagicMock
# TypeErrors, etc.). Forcing the subsystem off for the whole session keeps the
# suite's deliberate errors out of production error tracking. configure_sentry
# checks `enabled` before `dsn`, so disabling is sufficient even when a DSN is
# present; we clear the DSN too as defence in depth. The dedicated
# test_sentry_setup unit tests are unaffected — they construct their own
# SentrySettings with an explicit DSN and a fake SDK, bypassing env entirely.
os.environ["HELIXIS_SENTRY__ENABLED"] = "false"
os.environ.pop("HELIXIS_SENTRY__DSN", None)

from helixis.settings import Settings  # noqa: E402
from helixis.streaming.bus import EventBus  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Test settings with safe defaults."""
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )


class _NoRateLimit:
    async def acquire(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _rentvine_rate_limit_off_unless_asked(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Rentvine client takes a token per request at 4/s by default, so a
    test making dozens of calls would sleep for real. A test ABOUT the limiter
    marks itself ``@pytest.mark.rentvine_rate_limit`` and gets the real one."""
    from helixis.rentvine import client as rvclient

    if request.node.get_closest_marker("rentvine_rate_limit") is None:
        monkeypatch.setattr(rvclient, "_rate_bucket_for", lambda _settings: _NoRateLimit())


@pytest.fixture(autouse=True)
def _no_rentvine_hold_leaks_between_tests() -> None:
    """``rentvine.client._holds`` is per-process state (a Retry-After hold on an
    account). One test's 429 must not hold the next test's account."""
    from helixis.rentvine import client as rvclient

    rvclient._holds.clear()
    # The circuit breaker is per-process state too (terms rule 25).
    rvclient._breakers.clear()


@pytest.fixture(autouse=True)
def _pms_toolkit_state_is_per_test(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``helixis.pms`` keeps per-process state like Rentvine's client: rate
    holds, circuit breakers, session tokens, and the sync cycle's clocks and
    claims. One test's 429, outage, login or sync must not reach the next.
    The rate limiter is off unless a test about it marks itself
    ``@pytest.mark.pms_rate_limit``."""
    from helixis.pms import auth as pms_auth
    from helixis.pms import breaker as pms_breaker
    from helixis.pms import rate as pms_rate
    from helixis.pms import sync_cycle as pms_sync_cycle

    pms_rate._holds.clear()
    pms_breaker._breakers.clear()
    pms_auth._tokens.clear()
    pms_auth._broken.clear()
    pms_auth._locks.clear()
    pms_sync_cycle._last_synced.clear()
    pms_sync_cycle._running.clear()
    pms_sync_cycle._last_finished.clear()
    if request.node.get_closest_marker("pms_rate_limit") is None:
        monkeypatch.setattr(pms_rate, "bucket", lambda _account, _rps: _NoRateLimit())


@pytest.fixture(autouse=True)
def _pms_coverage_is_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every mirror reader asks ``coverage_for`` which PMS the company is on.

    Most tests hand those readers a fake pool scripted for the reader's own
    queries, which can't answer the PMS lookup too. This states Buildium (the
    product's behaviour before Rentvine) for every test. A test about another
    PMS, or about the lookup itself, patches the same name again or calls
    ``Coverage`` directly.
    """

    async def _buildium(_pool: Any, _company_id: str) -> str:
        return "buildium"

    monkeypatch.setattr("helixis.db.pms_coverage.company_pms_strict", _buildium)

    # And, for a test that switches to Rentvine, every sync stage has landed:
    # those tests are about what Rentvine FEEDS. A test about a stage that has
    # not landed yet patches this name again (``pms_coverage.unlanded_tables``).
    from helixis.rentvine.sync import SYNC_STATE_STAGE

    landed = {stage: "2026-10-02T00:00:00Z" for stage in SYNC_STATE_STAGE.values()}

    async def _all_landed(_pool: Any, _company_id: str) -> dict[str, Any]:
        return landed

    monkeypatch.setattr("helixis.db.pms_coverage.get_freshness", _all_landed)


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


class FakeIdempotencyStore:
    """In-memory :class:`IdempotencyStore` for tests that drive a write path
    through the durable action guard without a real asyncpg pool.

    Same shape as the per-test fakes in ``test_action_idempotency.py`` /
    ``test_idempotency_site_dedup.py``; lives here so the execute-endpoint
    end-to-end tests can share one via the ``execute_idempotency_store``
    fixture (issue #224)."""

    def __init__(self) -> None:
        from helixis.db.repositories.action_idempotency import ClaimRow

        self._rows: dict[tuple[str, str], ClaimRow] = {}

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> Any:
        from helixis.db.repositories.action_idempotency import (
            STATUS_IN_PROGRESS,
            ClaimResult,
            ClaimRow,
        )

        k = (company_id, idem_key)
        existing = self._rows.get(k)
        if existing is not None:
            return ClaimResult(won=False, row=existing)
        row = ClaimRow(
            company_id=company_id,
            idem_key=idem_key,
            action_type=action_type,
            status=STATUS_IN_PROGRESS,
            result=None,
            error=None,
            attempts=1,
            expires_at=datetime.now(UTC) + window if window else None,
        )
        self._rows[k] = row
        return ClaimResult(won=True, row=row)

    async def _set(self, company_id: str, idem_key: str, **f: Any) -> None:
        row = self._rows[(company_id, idem_key)]
        for key, val in f.items():
            setattr(row, key, val)

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        from helixis.db.repositories.action_idempotency import STATUS_SUCCEEDED

        await self._set(company_id, idem_key, status=STATUS_SUCCEEDED, result=result)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        from helixis.db.repositories.action_idempotency import STATUS_FAILED_RETRYABLE

        await self._set(company_id, idem_key, status=STATUS_FAILED_RETRYABLE, error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        from helixis.db.repositories.action_idempotency import STATUS_ABANDONED_UNKNOWN

        await self._set(company_id, idem_key, status=STATUS_ABANDONED_UNKNOWN, error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        from helixis.db.repositories.action_idempotency import STATUS_ABORTED

        await self._set(company_id, idem_key, status=STATUS_ABORTED)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        from helixis.db.repositories.action_idempotency import (
            STATUS_ABORTED,
            STATUS_FAILED_RETRYABLE,
            STATUS_IN_PROGRESS,
        )

        row = self._rows.get((company_id, idem_key))
        if row is None or row.status not in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
            return False
        row.status = STATUS_IN_PROGRESS
        return True

    async def get(self, *, company_id: str, idem_key: str) -> Any:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0


@pytest.fixture
def execute_idempotency_store(monkeypatch: pytest.MonkeyPatch) -> FakeIdempotencyStore:
    """Inject an in-memory store into the Buildium execute endpoint so its
    durable idempotency guard runs without asyncpg."""
    store = FakeIdempotencyStore()
    monkeypatch.setattr(
        "helixis.api.routes.buildium._idempotency_store",
        lambda pool: store,
    )
    return store


# ── Files the public repository leaves out ─────────────────────────────────
# The development repository also holds vendor API specs captured for the
# connectors' contract tests, design notes with vendor correspondence,
# operator runbooks, and workflows that run against the original deployment.
# None of them are published here. A test that reads one of them is reported
# as SKIPPED with the path it needed, never as passed. Only these exact
# prefixes count: a missing file anywhere else still fails the test.
NOT_PUBLISHED = (
    "docs/doorloop/",
    "docs/propertyware/",
    "docs/rentmanager/",
    "docs/rentvine/",
    "docs/runbooks/",
    "docs/gotchas/",
    "docs/NOW.md",
    "docs/decisions.md",
    "design/",
    "CLAUDE.md",
    ".github/workflows/eval-flake.yml",
    ".github/workflows/field-shapes.yml",
    ".github/workflows/memory-reembed.yml",
    ".github/workflows/smoke.yml",
    ".github/workflows/release-check.yml",
    "scripts/rentvine_e2e.ps1",
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _unpublished_path(exc: BaseException | None) -> str | None:
    if not isinstance(exc, FileNotFoundError) or not exc.filename:
        return None
    path = os.path.relpath(os.path.abspath(str(exc.filename)), _REPO_ROOT).replace(os.sep, "/")
    unpublished = path.startswith(NOT_PUBLISHED) or path.endswith("/CLAUDE.md")
    return path if unpublished else None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.excinfo is not None:
        missing = _unpublished_path(call.excinfo.value)
        if missing:
            report.outcome = "skipped"
            report.longrepr = (
                str(item.path),
                item.location[1] or 0,
                f"Skipped: needs {missing}, which the public repository does not include",
            )
