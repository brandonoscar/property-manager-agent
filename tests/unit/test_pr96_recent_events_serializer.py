"""PR #96 — recent-events serializer truth.

Production bug evidence (Render logs vs. Supabase, 2026-05-19 ~21:00 EDT):
the DB had ``summary_text='Jimmy created a new task for 160'`` and
``summary_attempts=1`` on two rows, but the
``/api/v1/buildium/recent-events`` JSON response carried
``"text": null, "attempts": 0`` for those same rows — a logically
impossible combination if the serializer had returned what the row
actually held.

This file pins the fix:

  - When ``pool.fetch`` returns a row with all ``summary_*`` columns
    populated, the API response surfaces those exact values
    (not the Pydantic defaults).
  - When the catchall ``except Exception`` path fires, the API
    NO LONGER silently returns ``[]`` — it logs at ``error`` level
    and re-raises so FastAPI returns 500 and monitoring catches it.
  - When ``UndefinedColumnError`` fires (legitimate pre-migration
    case), the skinny-shape fallback still works but is now logged
    at ``error`` with ``exc_info=True`` instead of ``warning``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import buildium as buildium_routes


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(buildium_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-pr96"],
    )
    return app


def _seed_row(*, summary_text: str, summary_attempts: int) -> dict[str, Any]:
    """A row matching the asyncpg.Record shape the production query
    returns. The ``keys()`` membership-check pattern in
    ``_row_to_summary`` works against a plain dict in tests."""
    return {
        "id": uuid4(),
        "event_name": "Task.Created",
        "event_time": datetime(2026, 5, 19, 0, 23, 11, tzinfo=UTC),
        "status": "done",
        "last_error": None,
        "planner_status": "succeeded",
        "planner_error": None,
        "summary_status": "succeeded",
        "summary_text": summary_text,
        "summary_error": None,
        "summary_attempts": summary_attempts,
        "summary_last_attempted_at": datetime(2026, 5, 19, 0, 23, 11, tzinfo=UTC),
        "action_surface": None,
        "enrichment": None,
        "executed_action_index": None,
    }


def _pool_answering_each_query_separately(event_rows: list[dict[str, Any]]) -> MagicMock:
    """A fake pool that answers the recent-events query and the risk-flag
    query DIFFERENTLY — because the route asks two.

    ⚠ One `AsyncMock(return_value=[row])` answers EVERY query with the same
    rows, so `finding_states_for_events` was handed event rows and looked for
    `event_id` on them. That is gotcha 96's fixture lesson: a fake that
    answers everything is not modelling the database, it is asserting that
    every query returns the same thing — and here it asserted something
    impossible. The flag query returns `[]`, which is the truthful answer for
    a `Task.Created` row that carries no `FlagIds`.
    """
    pool = MagicMock()

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "helixis_risk_flags" in sql:
            return []
        return event_rows

    pool.fetch = AsyncMock(side_effect=_fetch)
    return pool


def test_recent_events_returns_real_summary_text_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoking-gun production scenario: the DB has the real
    ``summary_text`` value and the API response MUST carry that
    value, not the Pydantic-default ``None``. Pre-PR-#96 a silent
    fallback path could return the default while the DB had real
    data; this test pins that the happy round-trip works."""
    app = _make_app()

    row = _seed_row(
        summary_text="Jimmy created a new task for 160 East End Ave #120",
        summary_attempts=1,
    )
    pool = _pool_answering_each_query_separately([row])

    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: pool)

    client = TestClient(app)
    resp = client.get(
        "/api/v1/buildium/recent-events"
        "?include=action_surface,enrichment,executed_action_index&limit=3"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    summary = body[0]["summary"]

    # The real production check that caught the bug:
    assert summary["text"] == "Jimmy created a new task for 160 East End Ave #120"
    assert summary["attempts"] == 1
    assert summary["status"] == "succeeded"
    # last_attempted_at was the field that DID pass through correctly
    # in the original bug — pin it too so a future regression that
    # breaks IT doesn't slip through silently.
    assert summary["last_attempted_at"].startswith("2026-05-19T00:23:11")


def test_recent_events_re_raises_on_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PR #96 — the catchall ``except Exception`` no longer silently
    returns ``[]``. It logs at ``error`` with the traceback and
    re-raises so the response is a 500 and Render's monitoring picks
    it up.

    Pre-PR-#96, a DataError / network blip / JSON failure inside the
    fetch would return ``[]`` and the inbox would render as
    "no events" while the API was actively broken — that's how the
    summary-truth bug hid for 30 minutes."""
    app = _make_app()
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("simulated DB outage"))

    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: pool)

    # The handler re-raises so FastAPI returns 500. Without
    # ``raise_server_exceptions=False`` TestClient would propagate
    # the exception to the caller instead.
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="helixis.api.routes.buildium"):
        resp = client.get(
            "/api/v1/buildium/recent-events?include=action_surface,enrichment&limit=3"
        )

    assert resp.status_code == 500
    # The log carried the full exception class + message at error
    # level (was warning pre-PR-#96).
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("recent-events query failed" in record.getMessage() for record in error_records), (
        f"expected an error log; got: {[r.getMessage() for r in caplog.records]}"
    )


def test_recent_events_undefined_column_falls_back_with_error_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``UndefinedColumnError`` is a legitimate pre-migration case
    (rolling deploy where code references a column that hasn't been
    added yet). We keep the skinny-shape fallback so the inbox
    keeps rendering, but PR #96 escalates the log from ``warning``
    → ``error`` with ``exc_info=True`` so the specific missing
    column shows up in Render's error stream immediately."""
    app = _make_app()
    skinny_row = {
        "id": uuid4(),
        "event_name": "Task.Created",
        "event_time": datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
        "status": "done",
        "last_error": None,
    }
    pool = MagicMock()
    pool.fetch = AsyncMock(
        side_effect=[
            asyncpg.exceptions.UndefinedColumnError('column "summary_text" does not exist'),
            [skinny_row],
        ]
    )

    monkeypatch.setattr(
        buildium_routes.connectors_limiter,
        "check_by_user",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(buildium_routes, "get_pool", lambda: pool)

    client = TestClient(app)
    with caplog.at_level(logging.ERROR, logger="helixis.api.routes.buildium"):
        resp = client.get("/api/v1/buildium/recent-events?limit=3")

    assert resp.status_code == 200
    # Fallback shape: Pydantic default summary (status='succeeded',
    # text=None) — pre-PR-#96 behavior preserved for legitimate
    # pre-migration deploys. The OBSERVABILITY is what changed.
    body = resp.json()
    assert len(body) == 1
    assert body[0]["summary"]["text"] is None
    assert body[0]["summary"]["status"] == "succeeded"

    # The escalated log level + exc_info is the actual fix here.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("extended column missing" in record.getMessage() for record in error_records), (
        "UndefinedColumnError should be logged at error level (was warning pre-PR-#96)"
    )


def test_create_pool_disables_statement_cache() -> None:
    """PR #96 — asyncpg's per-connection prepared-statement cache is
    incompatible with Supabase's PgBouncer transaction-pooling mode
    and was the most plausible cause of the production summary-truth
    bug. The pool factory MUST keep ``statement_cache_size=0`` so the
    cache stays off across rolling migrations + connection rebinds.

    ⚠ RE-POINTED, NOT DELETED (gotchas 89/93/95). This asserted the right
    property against the wrong mechanism: it patched ``asyncpg.create_pool``
    and read the kwargs we handed it. ``engine.create_pool`` no longer routes
    through that function — it constructs ``_TimeoutDefaultingPool`` directly,
    because asyncpg exposes no ``pool_class`` hook and every ``acquire()`` in
    the process needs a default timeout (gotcha 105). The property is
    unchanged and is now checked one step closer to the truth: on the REAL
    pool object's own ``_connect_kwargs``, rather than on what a mock was
    told. A spy ``__await__`` stops it opening a socket.
    """
    from helixis.db import engine

    made: dict[str, Any] = {}
    real = engine._TimeoutDefaultingPool

    class _NoConnect(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made["pool"] = self

        def __await__(self):  # noqa: ANN204 — hand back the pool unconnected
            async def _self() -> Any:
                return self

            return _self().__await__()

    with patch.object(engine, "_TimeoutDefaultingPool", _NoConnect):
        asyncio.run(engine.create_pool("postgresql://stub"))

    assert made["pool"]._connect_kwargs["statement_cache_size"] == 0
