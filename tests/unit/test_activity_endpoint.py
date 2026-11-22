"""GET /api/v1/activity — scope, cursor, actor join, RLS-equivalent
company-boundary tests (PR #70).

The route runs as the asyncpg pool's role (Postgres-side superuser /
service equivalent), so the company-boundary check lives in the SQL
``WHERE company_id = $1`` clause — RLS itself is enforced separately
by Supabase for any REST-side access. These tests fake the asyncpg
pool and assert: company-scope filter, scope=mine filter, cursor
filtering, actor-join shape, empty-state when the table is missing.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

from ._pg_bind_contract import reject_unbindable

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"


def _make_token(sub: str = ME_USER_ID) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _row(
    *,
    user_id: str | None,
    company_id: str,
    description: str,
    minutes_ago: int,
    source: str = "webhook",
    source_label: str = "Buildium · Task.Created",
    action_type: str = "update_buildium_task",
    property: str | None = "1208 Oak St #3A",
    tenant: str | None = "Mei Chen",
) -> dict[str, Any]:
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "id": str(uuid4()),
        "user_id": user_id,
        "company_id": company_id,
        "source": source,
        "source_label": source_label,
        "action_type": action_type,
        "description": description,
        "property": property,
        "tenant": tenant,
        "created_at": now,
    }


def _build_fake_pool(rows: list[dict[str, Any]]) -> Any:
    """Stand in for the asyncpg pool used by activity.list_activity.

    Filters rows by the SQL's ``WHERE company_id = $1`` + optional
    ``user_id = $N`` and ``created_at < $N::timestamptz`` parameters.
    Returns the canonical asyncpg dict shape the route expects.
    """
    actor_directory: dict[str, dict[str, str | None]] = {
        ME_USER_ID: {"display_name": "Brandon Oscar", "email": "brandon@example.com"},
        OTHER_USER_ID: {"display_name": "Casey Lin", "email": "casey@example.com"},
    }

    def _matches_category(row: dict[str, Any], sql: str) -> bool:
        """Mirror the inlined ``_CATEGORY_PREDICATES`` fragments."""
        if "action_type LIKE 'crm" in sql:
            return str(row["action_type"]).startswith("crm_")
        if "action_type LIKE 'member" in sql:
            return str(row["action_type"]).startswith("member_")
        if "source = 'webhook'" in sql:
            return row["source"] == "webhook"
        if "source = 'chat'" in sql:
            return row["source"] == "chat"
        if "source = 'scheduled'" in sql:
            return row["source"] == "scheduled"
        return True

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        # Refuse exactly what the real driver refuses. A fake that is more
        # permissive than asyncpg holds a defect in place while reading as
        # coverage — see tests/unit/_pg_bind_contract.py.
        reject_unbindable(sql, args)
        normalized = " ".join(sql.split())
        # Distinct-actors query for the /actors endpoint.
        if "SELECT DISTINCT user_id" in normalized:
            company_id = args[0]
            seen = sorted(
                {r["user_id"] for r in rows if r["company_id"] == company_id and r["user_id"]}
            )
            return [{"user_id": uid} for uid in seen]
        if "FROM helixis_activity_log" in normalized:
            company_id = args[0]
            params = list(args[1:])
            limit = params[-1]
            params = params[:-1]
            scope_user: str | None = None
            cursor: datetime | None = None
            # Heuristically pull scope_user + cursor based on param count.
            # (Category predicates are inlined, so they carry no param.)
            if "user_id::text =" in normalized:
                scope_user = params.pop(0)
            if "created_at < " in normalized:
                # ⚠ Takes the value AS BOUND. The first cut here did
                # ``datetime.fromisoformat(arg.replace("Z", "+00:00"))``, which
                # REQUIRED the ``str`` asyncpg refuses — so this test exercised
                # the keyset path and stayed green while every "Load more"
                # click in production was a 500 (Sentry PYTHON-FASTAPI-3G).
                # ``reject_unbindable`` above is what stops that recurring.
                cursor = params.pop(0)
            filtered = [r for r in rows if r["company_id"] == company_id]
            if scope_user is not None:
                filtered = [r for r in filtered if r["user_id"] == scope_user]
            filtered = [r for r in filtered if _matches_category(r, normalized)]
            if cursor is not None:
                filtered = [r for r in filtered if r["created_at"] < cursor]
            filtered.sort(key=lambda r: r["created_at"], reverse=True)
            # Project to the SELECT shape.
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "source": r["source"],
                    "source_label": r["source_label"],
                    "action_type": r["action_type"],
                    "description": r["description"],
                    "property": r["property"],
                    "tenant": r["tenant"],
                    "created_at": r["created_at"],
                }
                for r in filtered[:limit]
            ]
        if "FROM auth.users" in normalized:
            requested_ids = args[0]
            return [
                {
                    "id": uid,
                    "email": actor_directory[uid]["email"],
                    "display_name": actor_directory[uid]["display_name"],
                }
                for uid in requested_ids
                if uid in actor_directory
            ]
        raise AssertionError(f"unexpected SQL: {normalized[:100]}")

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)

    @asynccontextmanager
    async def _acquire():
        yield MagicMock()

    pool.acquire = _acquire
    return pool


@pytest.fixture
def fake_rows() -> list[dict[str, Any]]:
    """Three rows for COMPANY_ID + one row for a different company —
    the cross-tenant row must never leak."""
    return [
        _row(user_id=ME_USER_ID, company_id=COMPANY_ID, description="Mine action 1", minutes_ago=2),
        _row(user_id=ME_USER_ID, company_id=COMPANY_ID, description="Mine action 2", minutes_ago=4),
        _row(
            user_id=OTHER_USER_ID, company_id=COMPANY_ID, description="Casey action", minutes_ago=6
        ),
        _row(
            user_id=ME_USER_ID,
            company_id="other-co-uuid",
            description="leak attempt",
            minutes_ago=1,
        ),
    ]


@pytest.fixture
def client(test_settings: Settings, fake_rows: list[dict[str, Any]]):
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()
    pool = _build_fake_pool(fake_rows)

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.routes.activity.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            yield c


def test_default_scope_team_returns_company_rows(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/activity",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 3 rows for COMPANY_ID, none for other-co-uuid.
    assert len(body["rows"]) == 3
    descriptions = [r["description"] for r in body["rows"]]
    assert "leak attempt" not in descriptions
    assert "Mine action 1" in descriptions
    assert "Casey action" in descriptions


def test_scope_mine_filters_to_requester_user_id(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/activity?scope=mine",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # ME_USER_ID has 2 rows in COMPANY_ID.
    assert len(body["rows"]) == 2
    for r in body["rows"]:
        assert r["actor"]["id"] == ME_USER_ID
        assert r["actor"]["is_self"] is True


def test_actor_join_resolves_display_name(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/activity",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    body = resp.json()
    actors_by_id = {r["actor"]["id"]: r["actor"] for r in body["rows"]}
    assert actors_by_id[ME_USER_ID]["name"] == "Brandon Oscar"
    assert actors_by_id[ME_USER_ID]["is_self"] is True
    assert actors_by_id[OTHER_USER_ID]["name"] == "Casey Lin"
    assert actors_by_id[OTHER_USER_ID]["is_self"] is False


def test_cursor_filters_to_rows_older_than_supplied_timestamp(
    client: TestClient, fake_rows: list[dict[str, Any]]
) -> None:
    # Get all rows first to find a real cursor value (the second row's
    # created_at — we expect exactly one row older than it).
    full = client.get(
        "/api/v1/activity",
        headers={"Authorization": f"Bearer {_make_token()}"},
    ).json()
    cursor_iso = full["rows"][1]["created_at"]

    # Use params= so the test client URL-encodes ``+00:00`` properly.
    paged = client.get(
        "/api/v1/activity",
        params={"cursor": cursor_iso},
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert paged.status_code == 200
    body = paged.json()
    assert len(body["rows"]) == 1
    # The remaining row's timestamp must be strictly older than the cursor.
    assert body["rows"][0]["created_at"] < cursor_iso


def test_limit_returns_next_cursor_when_more_rows_available(
    client: TestClient,
) -> None:
    resp = client.get(
        "/api/v1/activity?limit=2",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["next_cursor"] is not None
    # next_cursor equals the oldest row on the page.
    assert body["next_cursor"] == body["rows"][-1]["created_at"]


def test_load_more_walks_the_whole_feed_on_the_cursor_we_handed_back(
    client: TestClient,
) -> None:
    """The live defect, end to end: page 1, then hand ``next_cursor`` back.

    ⚠ This is what "Load more" does, and it was a 500 in production (Sentry
    PYTHON-FASTAPI-3G) — the route typed ``cursor`` as ``str`` and bound it to
    ``created_at < $N::timestamptz``, which asyncpg refuses outright. The
    existing cursor test above passed throughout, because it supplied a
    hand-picked row timestamp to a fake that PARSED the string the driver
    rejects. Walking the real pagination through a contract-enforcing fake is
    what makes the round trip evidence rather than a shape check.
    """
    auth = {"Authorization": f"Bearer {_make_token()}"}
    first = client.get("/api/v1/activity?limit=2", headers=auth).json()
    assert first["next_cursor"] is not None

    # ⚠ PASSED AS A PARAM, never concatenated into the URL. An ISO-8601 offset
    # ends "+00:00", and a raw "+" in a query string decodes to a SPACE — so a
    # hand-built URL turns a valid cursor into "…823911 00:00" and earns a 422
    # about "unexpected extra characters". The first cut of this test did
    # exactly that and failed, which is the useful part: the real client uses
    # URLSearchParams (services/activity.ts) and is safe, and the next client
    # that concatenates will not be.
    second = client.get(
        "/api/v1/activity",
        params={"limit": 2, "cursor": first["next_cursor"]},
        headers=auth,
    )
    assert second.status_code == 200, second.text
    body = second.json()
    # Strictly older, and no row repeats across the two pages.
    assert all(r["created_at"] < first["next_cursor"] for r in body["rows"])
    assert not {r["id"] for r in body["rows"]} & {r["id"] for r in first["rows"]}


def test_an_unparseable_cursor_is_a_422_naming_the_field_not_a_500(
    client: TestClient,
) -> None:
    """Junk in the cursor is the client's problem, and the answer must say so.

    A 500 is the same reply the server gives when IT is broken, so it sends
    the reader looking in the wrong place — the shape this repo keeps
    recording (gotchas 102/106: an answer that points at the wrong cause is
    worse than a blunt one).
    """
    resp = client.get(
        "/api/v1/activity?cursor=not-a-timestamp",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert resp.status_code == 422, resp.text
    assert "cursor" in resp.text

    # The realistic spelling of the same mistake: a client that built the URL
    # by concatenation, so the "+" of the offset arrived as a space. Loud is
    # right here — silently reading it as a naive local time would move the
    # page boundary by the offset instead of failing (gotcha 91's class).
    mangled = client.get(
        "/api/v1/activity",
        params={"cursor": "2026-08-27T07:46:34.797181 00:00"},
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert mangled.status_code == 422, mangled.text

    # ⚠ AND A WELL-FORMED CURSOR WITH NO OFFSET, which is the one that would
    # NOT fail on its own. asyncpg's timestamptz encoder ends in
    # ``obj.astimezone(utc)``, and on a naive value that assumes the SERVER's
    # zone — so this would silently move the page boundary by the container's
    # UTC offset instead of erroring. That is why the annotation is
    # ``AwareDatetime`` and not a bare ``datetime``; a plain ``datetime``
    # accepts this line and every other test in this file still passes.
    naive = client.get(
        "/api/v1/activity",
        params={"cursor": "2026-08-27T07:46:34"},
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    assert naive.status_code == 422, naive.text


def test_short_page_returns_null_next_cursor(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/activity?limit=100",
        headers={"Authorization": f"Bearer {_make_token()}"},
    )
    body = resp.json()
    assert body["next_cursor"] is None  # only 3 rows total, well under limit


def test_missing_table_returns_empty_page_without_500(
    client: TestClient, test_settings: Settings
) -> None:
    """Defensive: a fresh DB without the helixis_activity_log table
    should not 500 the route — it should return an empty page so the
    UI renders an empty state instead of a blank page."""

    async def raise_undefined_table(*_a: Any, **_kw: Any) -> None:
        raise asyncpg.exceptions.UndefinedTableError("relation does not exist")

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=raise_undefined_table)

    @asynccontextmanager
    async def _acquire():
        yield MagicMock()

    pool.acquire = _acquire

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.routes.activity.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/activity",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"rows": [], "next_cursor": None}


def test_requires_authentication(client: TestClient) -> None:
    resp = client.get("/api/v1/activity")
    assert resp.status_code in (401, 403)


# ── category / person filters + actors endpoint (Workstream D) ──────


@pytest.fixture
def category_rows() -> list[dict[str, Any]]:
    """Rows spanning every category bucket, for the filter tests."""
    return [
        _row(
            user_id=ME_USER_ID,
            company_id=COMPANY_ID,
            description="Texted Jordan",
            minutes_ago=1,
            source="manual",
            source_label="CRM",
            action_type="crm_reply_sent",
        ),
        _row(
            user_id=None,
            company_id=COMPANY_ID,
            description="Lease transaction posted",
            minutes_ago=2,
            source="webhook",
            source_label="Buildium · Lease.Transaction",
            action_type="lease_transaction",
        ),
        _row(
            user_id=OTHER_USER_ID,
            company_id=COMPANY_ID,
            description="Searched the web for permit rules",
            minutes_ago=3,
            source="chat",
            source_label="Chat",
            action_type="web_search",
        ),
        _row(
            user_id=ME_USER_ID,
            company_id=COMPANY_ID,
            description="Invited casey@example.com as Manager",
            minutes_ago=4,
            source="manual",
            source_label="Team",
            action_type="member_invited",
        ),
        _row(
            user_id=OTHER_USER_ID,
            company_id=COMPANY_ID,
            description="Called Sam",
            minutes_ago=5,
            source="manual",
            source_label="CRM",
            action_type="crm_call_placed",
        ),
    ]


@pytest.fixture
def filter_client(test_settings: Settings, category_rows: list[dict[str, Any]]):
    from helixis.api.rate_limit import connectors_limiter

    connectors_limiter.counter._log.clear()
    pool = _build_fake_pool(category_rows)

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.routes.activity.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token()}"}


def test_category_crm_returns_only_crm_rows(filter_client: TestClient) -> None:
    body = filter_client.get("/api/v1/activity?category=crm", headers=_auth()).json()
    assert {r["action_type"] for r in body["rows"]} == {"crm_reply_sent", "crm_call_placed"}


def test_category_buildium_returns_only_webhook_rows(filter_client: TestClient) -> None:
    body = filter_client.get("/api/v1/activity?category=buildium", headers=_auth()).json()
    assert [r["action_type"] for r in body["rows"]] == ["lease_transaction"]
    # Webhook rows have no user — actor falls back to the integration name.
    assert body["rows"][0]["actor"]["id"] == "system"


def test_category_team_returns_only_member_rows(filter_client: TestClient) -> None:
    body = filter_client.get("/api/v1/activity?category=team", headers=_auth()).json()
    assert [r["action_type"] for r in body["rows"]] == ["member_invited"]


def test_actor_filter_narrows_to_one_person(filter_client: TestClient) -> None:
    body = filter_client.get(f"/api/v1/activity?actor={OTHER_USER_ID}", headers=_auth()).json()
    # Casey produced the web search + the call — nothing else.
    assert {r["description"] for r in body["rows"]} == {
        "Searched the web for permit rules",
        "Called Sam",
    }
    assert all(r["actor"]["id"] == OTHER_USER_ID for r in body["rows"])


def test_category_and_actor_compose(filter_client: TestClient) -> None:
    body = filter_client.get(
        f"/api/v1/activity?category=crm&actor={OTHER_USER_ID}", headers=_auth()
    ).json()
    assert [r["action_type"] for r in body["rows"]] == ["crm_call_placed"]


def test_invalid_category_is_rejected(filter_client: TestClient) -> None:
    resp = filter_client.get("/api/v1/activity?category=bogus", headers=_auth())
    assert resp.status_code == 422


def test_actors_endpoint_lists_distinct_participants(filter_client: TestClient) -> None:
    body = filter_client.get("/api/v1/activity/actors", headers=_auth()).json()
    # Only ME + OTHER produced rows (the webhook row has no user_id).
    ids = {a["id"] for a in body}
    assert ids == {ME_USER_ID, OTHER_USER_ID}
    by_id = {a["id"]: a for a in body}
    assert by_id[ME_USER_ID]["is_self"] is True
    assert by_id[ME_USER_ID]["name"] == "Brandon Oscar"
    assert by_id[OTHER_USER_ID]["is_self"] is False
    # Sorted by display name: "Brandon Oscar" before "Casey Lin".
    assert [a["name"] for a in body] == ["Brandon Oscar", "Casey Lin"]


def test_actors_endpoint_empty_when_table_missing(test_settings: Settings) -> None:
    async def raise_undefined_table(*_a: Any, **_kw: Any) -> None:
        raise asyncpg.exceptions.UndefinedTableError("relation does not exist")

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=raise_undefined_table)

    @asynccontextmanager
    async def _acquire():
        yield MagicMock()

    pool.acquire = _acquire

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.routes.activity.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/activity/actors", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == []


def test_user_with_no_company_membership_is_403(test_settings: Settings) -> None:
    """Defence-in-depth: a token resolving to no companies must not be
    able to read any activity rows."""

    async def fake_resolve(_user_id: str) -> list[str]:
        return []

    pool = _build_fake_pool([])
    with (
        patch("helixis.api.routes.activity.get_pool", return_value=pool),
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/activity",
                headers={"Authorization": f"Bearer {_make_token()}"},
            )
    assert resp.status_code == 403
