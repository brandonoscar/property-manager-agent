"""PR #94 — route collision: ``GET /api/v1/buildium/events/recent-events``.

Render production log (21:01:02 EDT) showed a 500 on
``GET /api/v1/buildium/events/recent-events?limit=3``:

    asyncpg.exceptions.DataError: invalid input for query argument $1:
    'recent-events' (invalid UUID 'recent-events': length must be
    between 32..36 characters, got 13)

Root cause: the route ``GET /events/{event_id}`` (PR #92) accepted
``event_id`` as a bare ``str`` and passed it directly into an
asyncpg query against a ``uuid`` column. When somebody hit the URL
with the literal ``recent-events`` (a stray request — the inbox
itself correctly calls ``/recent-events`` without the ``/events/``
prefix), asyncpg raised an unhandled ``DataError`` → 500.

PR #94 fix: change every ``/events/{event_id}`` endpoint's path-param
type from ``str`` to ``uuid.UUID``. FastAPI validates the path param
and returns 422 on non-UUID input, so:

  - the entire class of "stray non-UUID hits the events handler"
    bugs returns a clean 422 instead of crashing,
  - the inbox's normal calls (which always pass a real UUID from
    ``row.id``) are unaffected,
  - downstream code in the handler bodies receives a real ``UUID``
    object that asyncpg accepts natively for the uuid column.

This test pins the fix: a deliberately-malformed event_id surfaces
as 422 instead of 500. If a future refactor relaxes the type
constraint, this test fails loudly.
"""

from __future__ import annotations

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
        company_ids=["company-pr94"],
    )
    return app


def test_get_event_returns_422_on_non_uuid_path_param() -> None:
    """The Render-log smoking gun: ``GET /events/recent-events``
    must NOT 500. FastAPI's UUID validator (now applied via the
    path-param type-hint) returns 422 ("unprocessable entity")
    when the path segment isn't a valid UUID."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/v1/buildium/events/recent-events?limit=3")
    # 422 from FastAPI's validation layer — NOT 500 from asyncpg.
    assert resp.status_code == 422


def test_post_event_action_execute_returns_422_on_non_uuid() -> None:
    """The same protection extends to all sibling endpoints that
    take ``{event_id}`` as a path param. Pinned so a future hand-
    written ``str`` regression on any of them fails this test."""
    app = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/buildium/events/not-a-uuid/actions/0/execute",
        json={},
    )
    assert resp.status_code == 422


def test_post_event_freeform_returns_422_on_non_uuid() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/buildium/events/not-a-uuid/freeform",
        json={"note": "hi"},
    )
    assert resp.status_code == 422


def test_get_event_thread_stream_returns_422_on_non_uuid() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/v1/buildium/events/not-a-uuid/thread/stream")
    assert resp.status_code == 422


def test_post_event_followup_returns_422_on_non_uuid() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/buildium/events/not-a-uuid/actions/0/followup",
        json={"chain": []},
    )
    assert resp.status_code == 422


def test_get_recent_events_unaffected_by_path_change() -> None:
    """Sanity: the ``/recent-events`` endpoint (the path the inbox
    actually calls) is a sibling of ``/events/...``, NOT a child,
    so the routing decision must NOT collapse the two. The 500 bug
    came from somebody confusing them — make sure the fix didn't
    accidentally route ``/recent-events`` into the UUID-typed
    handler.

    Uses ``raise_server_exceptions=False`` because we don't wire a
    real DB pool — the handler will 500 from RuntimeError("pool not
    initialized") and that's fine. What we ASSERT is the response is
    NOT 422 (which would prove the routing collapsed into
    ``/events/{event_id}`` instead)."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/buildium/recent-events")
    # A real DB-backed deploy returns 200. Without the pool we get a
    # 500 — fine for THIS test. The discriminating bit is "not 422":
    # 422 would mean FastAPI routed this into the UUID-validated
    # ``/events/{event_id}`` handler and parsed "recent-events" as
    # the path param.
    assert resp.status_code != 422
