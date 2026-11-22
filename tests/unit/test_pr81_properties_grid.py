"""PR #81 — Properties × Roles grid: API + sync + cache + schema.

What this file pins:

1. **Buildium ``User`` schema parses the verified spec.** Brandon
   pulled ``UserMessage.md`` from the LeadSimple/buildium SDK on
   2026-05-17; every field below has to round-trip without raising.
2. **Administration.list_users** issues the right path with the
   right filters (``usertypes=["Staff"]`` + ``status="Active"``).
3. **upsert_staff** is idempotent and soft-deletes missing users.
4. **list_active_staff** filters by ``user_types`` containing
   ``"Staff"`` (Tenant-Center users never leak into dropdowns).
5. **upsert_assignment** writes the row with the v1
   ``assign_to_user`` action_type and ``buildium_user_id``-in-jsonb
   action_config; ``delete_assignment`` clears a cell.
6. **Properties API routes** require auth, render the grid in one
   shot (default row + per-property cells), and the PUT routes call
   the repository helpers with the right arguments.
7. **/properties/sync** surfaces the friendly Buildium permission
   error message instead of leaking raw 403 text.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import properties as properties_routes
from helixis.buildium.endpoints.administration import Administration
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import User, UserRole
from helixis.buildium.sync import sync_buildium_staff
from helixis.db.repositories.buildium_staff import (
    BuildiumStaffRow,
    list_active_staff,
    upsert_staff,
)
from helixis.db.repositories.property_assignments import (
    delete_assignment,
    list_assignments,
    upsert_assignment,
)

# ─────────────────────────────────────────────────────────────────────
# 1. Schema parsing — verified Buildium spec
# ─────────────────────────────────────────────────────────────────────


def test_user_schema_parses_full_spec_payload() -> None:
    """Realistic ``/v1/users`` row (verified against LeadSimple SDK
    on 2026-05-17). Every alias must populate."""
    payload = {
        "Id": 123,
        "UserTypes": ["Staff"],
        "IsActive": True,
        "LastLogin": "2026-05-15T12:34:56Z",
        "FirstName": "Robert",
        "LastName": "Friedman",
        "CompanyName": None,
        "Email": "robert@redbrick.com",
        "AlternateEmail": None,
        "PhoneNumbers": [{"Number": "555-0100", "Type": "Mobile"}],
        "UserRole": {
            "Id": 4,
            "Name": "Property Manager",
            "Description": "…",
            "NumberOfUsers": 7,
        },
        "IsCompany": False,
    }
    u = User.model_validate(payload)
    assert u.id == 123
    assert u.user_types == ["Staff"]
    assert u.is_active is True
    assert u.first_name == "Robert"
    assert u.last_name == "Friedman"
    assert u.email == "robert@redbrick.com"
    assert u.user_role is not None
    assert isinstance(u.user_role, UserRole)
    assert u.user_role.id == 4
    assert u.user_role.name == "Property Manager"
    assert u.is_company is False


def test_user_schema_tolerates_missing_optional_fields() -> None:
    """Only ``Id`` is structurally required — the rest can be None
    in a sparse Buildium response."""
    u = User.model_validate({"Id": 7})
    assert u.id == 7
    assert u.is_active is None
    assert u.user_role is None


def test_user_schema_does_not_invent_fields_off_spec() -> None:
    """PR #78's lesson — typed aliases must match the verified spec.
    The hallucinated ``Title``/``IsAdmin``/``Permissions`` must NOT
    be typed fields. ``extra="allow"`` on the base lets unknown
    keys round-trip but they aren't aliased anywhere."""
    fields = User.model_fields
    typed_aliases = {f.alias for f in fields.values() if f.alias}
    for hallucinated in ("Title", "Department", "IsAdmin", "Permissions", "IsOwner"):
        assert hallucinated not in typed_aliases, (
            f"PR #81: '{hallucinated}' is not in the verified Buildium "
            f"UserMessage spec — must not be a typed alias."
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Administration.list_users — endpoint shape
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_administration_list_users_paginates_users_path() -> None:
    """``list_users`` MUST go to ``/users`` (not ``/users/`` or a
    legacy ``/administration/users``) and use the paginating
    ``list_all`` helper so accounts with >50 staff don't silently
    truncate to the first page."""
    fake_client = MagicMock()
    fake_client.list_all = AsyncMock(return_value=[])

    admin = Administration(fake_client)
    await admin.list_users(usertypes=["Staff"], status="Active")

    fake_client.list_all.assert_awaited_once()
    args, _kwargs = fake_client.list_all.call_args
    assert args[0] == "/users"
    # Filters propagate verbatim — Buildium's spec lists ``usertypes``
    # and ``status`` as the params we care about.
    assert args[1] == {"usertypes": ["Staff"], "status": "Active"}
    assert args[2] is User


# ─────────────────────────────────────────────────────────────────────
# 3. upsert_staff — idempotency + soft-delete
# ─────────────────────────────────────────────────────────────────────


def _fake_pool_with_executes() -> MagicMock:
    """A pool whose ``execute`` calls return predictable strings."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")
    return pool


@pytest.mark.asyncio
async def test_upsert_staff_returns_counts() -> None:
    pool = _fake_pool_with_executes()
    rows = [
        {
            "buildium_user_id": 1,
            "first_name": "Robert",
            "last_name": "Friedman",
            "email": "r@example.com",
            "user_types": ["Staff"],
            "user_role_id": 4,
            "user_role_name": "Property Manager",
            "is_active": True,
        },
        {
            "buildium_user_id": 2,
            "first_name": "Mei",
            "last_name": "Tan",
            "email": "m@example.com",
            "user_types": ["Staff"],
            "user_role_id": 4,
            "user_role_name": "Property Manager",
            "is_active": True,
        },
    ]
    pool.execute = AsyncMock(side_effect=["INSERT 0 1", "INSERT 0 1", "UPDATE 3"])
    summary = await upsert_staff(pool, company_id="c1", rows=rows)
    assert summary.upserted == 2
    assert summary.deactivated == 3


@pytest.mark.asyncio
async def test_upsert_staff_skips_rows_without_buildium_user_id() -> None:
    """A malformed sync row (no buildium_user_id) is silently skipped —
    one bad row must NOT poison the whole upsert."""
    pool = _fake_pool_with_executes()
    rows = [
        {"buildium_user_id": 1, "user_types": ["Staff"]},
        {"user_types": ["Staff"]},  # bad: no id
    ]
    pool.execute = AsyncMock(side_effect=["INSERT 0 1", "UPDATE 0"])
    summary = await upsert_staff(pool, company_id="c1", rows=rows)
    assert summary.upserted == 1


@pytest.mark.asyncio
async def test_upsert_staff_empty_returns_zero_counts_without_query() -> None:
    pool = _fake_pool_with_executes()
    pool.execute = AsyncMock()
    summary = await upsert_staff(pool, company_id="c1", rows=[])
    assert summary.upserted == 0
    assert summary.deactivated == 0
    pool.execute.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────
# 4. list_active_staff — filtering
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_active_staff_filters_to_routing_eligible_by_default() -> None:
    """The default ``only_routing_eligible=True`` path must filter
    server-side to rows where ``'Staff' = ANY(user_types)``. We pin
    this by asserting the SQL contains the filter."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    await list_active_staff(pool, company_id="c1")
    sql_arg = pool.fetch.await_args.args[0]
    assert "'Staff' = ANY (user_types)" in sql_arg
    assert "is_active = true" in sql_arg


def test_staff_row_display_name_prefers_name_over_email() -> None:
    row = BuildiumStaffRow(
        buildium_user_id=1,
        first_name="Robert",
        last_name="Friedman",
        email="r@example.com",
        user_types=["Staff"],
        user_role_id=None,
        user_role_name=None,
        is_active=True,
    )
    assert row.display_name == "Robert Friedman"


def test_staff_row_display_name_falls_back_to_email() -> None:
    row = BuildiumStaffRow(
        buildium_user_id=1,
        first_name=None,
        last_name=None,
        email="r@example.com",
        user_types=["Staff"],
        user_role_id=None,
        user_role_name=None,
        is_active=True,
    )
    assert row.display_name == "r@example.com"


def test_staff_row_display_name_never_falls_back_to_id() -> None:
    """PR #75 HARD PRESENTATION RULES — never expose Buildium ids."""
    row = BuildiumStaffRow(
        buildium_user_id=9999,
        first_name=None,
        last_name=None,
        email=None,
        user_types=["Staff"],
        user_role_id=None,
        user_role_name=None,
        is_active=True,
    )
    label = row.display_name
    assert "9999" not in label
    assert label == "Unnamed user"


# ─────────────────────────────────────────────────────────────────────
# 5. property_assignments writes
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_assignment_writes_assign_to_user_with_buildium_user_id() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "id": "row-1",
            "company_id": "c1",
            "buildium_property_id": 100,
            "category": "maintenance",
            "action_type": "assign_to_user",
            "action_config": '{"buildium_user_id": 42}',
        }
    )
    saved = await upsert_assignment(
        pool,
        company_id="c1",
        buildium_property_id=100,
        category="maintenance",
        action_type="assign_to_user",
        action_config={"buildium_user_id": 42},
    )
    assert saved.action_type == "assign_to_user"
    assert saved.action_config == {"buildium_user_id": 42}
    # The INSERT...ON CONFLICT shape carries jsonb serialization.
    sql = pool.fetchrow.await_args.args[0]
    assert "property_assignments" in sql
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_upsert_assignment_default_row_uses_partial_index_path() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "id": "row-default",
            "company_id": "c1",
            "buildium_property_id": None,
            "category": "maintenance",
            "action_type": "assign_to_user",
            "action_config": {"buildium_user_id": 7},
        }
    )
    saved = await upsert_assignment(
        pool,
        company_id="c1",
        buildium_property_id=None,
        category="maintenance",
        action_type="assign_to_user",
        action_config={"buildium_user_id": 7},
    )
    assert saved.buildium_property_id is None
    sql = pool.fetchrow.await_args.args[0]
    # Default-row branch hits the NULL-property partial index.
    assert "buildium_property_id IS NULL" in sql


@pytest.mark.asyncio
async def test_delete_assignment_returns_true_when_row_deleted() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 1")
    ok = await delete_assignment(
        pool,
        company_id="c1",
        buildium_property_id=100,
        category="maintenance",
    )
    assert ok is True


@pytest.mark.asyncio
async def test_delete_assignment_returns_false_when_no_row() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 0")
    ok = await delete_assignment(
        pool,
        company_id="c1",
        buildium_property_id=999,
        category="maintenance",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_list_assignments_returns_empty_when_table_missing() -> None:
    """During the migration window 0014 might not be applied yet —
    the loader has to silently return [] rather than crashing the
    Properties page."""
    import asyncpg

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("missing"))
    rows = await list_assignments(pool, company_id="c1")
    assert rows == []


# ─────────────────────────────────────────────────────────────────────
# 6. /properties API routes — TestClient end-to-end
# ─────────────────────────────────────────────────────────────────────


def _make_app() -> tuple[FastAPI, AuthenticatedUser]:
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    test_user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-1"],
    )

    def _stub() -> AuthenticatedUser:
        return test_user

    app.dependency_overrides[get_current_user] = _stub
    return app, test_user


def _pool_with_role(role: str = "admin") -> MagicMock:
    """Pool stub whose ``company_users`` lookup returns ``role`` — so the
    routing-grid role gate (``require_role``, #70) passes for manager/admin
    and denies for member, deterministically."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"role": role})
    return pool


def test_grid_requires_auth() -> None:
    """Without a valid bearer the route must NOT return data."""
    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    client = TestClient(app)
    resp = client.get("/api/v1/properties")
    assert resp.status_code in (401, 403)


def test_grid_renders_default_row_and_property_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """One Buildium property → grid response has one PropertyRow +
    the default row, both with all five category keys present
    (even if every cell is null)."""
    app, _user = _make_app()

    async def _fake_props(*_a: Any, **_kw: Any) -> tuple[list[dict[str, Any]], int]:
        return (
            [{"buildium_property_id": 100, "name": "150 Main Ave", "address": "Cleveland, OH"}],
            1,
        )

    fake_assignment = MagicMock()
    fake_assignment.id = "a-1"
    fake_assignment.company_id = "company-1"
    fake_assignment.buildium_property_id = 100
    fake_assignment.category = "maintenance"
    fake_assignment.action_type = "assign_to_user"
    fake_assignment.action_config = {"buildium_user_id": 42}

    monkeypatch.setattr(properties_routes, "_fetch_properties_from_buildium", _fake_props)
    monkeypatch.setattr(
        properties_routes,
        "list_assignments",
        AsyncMock(return_value=[fake_assignment]),
    )
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.get("/api/v1/properties")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert len(body["properties"]) == 1
    prop = body["properties"][0]
    assert prop["buildium_property_id"] == 100
    # Five category keys, one populated.
    assert set(prop["assignments"].keys()) == {
        "maintenance",
        "leasing",
        "accounting",
        "tenant_relations",
        "other",
    }
    assert prop["assignments"]["maintenance"]["buildium_user_id"] == 42
    assert prop["assignments"]["leasing"] is None
    # Default row exists + has the same five keys.
    assert set(body["default_row"]["assignments"].keys()) == {
        "maintenance",
        "leasing",
        "accounting",
        "tenant_relations",
        "other",
    }


def test_staff_endpoint_returns_active_routing_eligible_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _user = _make_app()

    staff_row = BuildiumStaffRow(
        buildium_user_id=1,
        first_name="Robert",
        last_name="Friedman",
        email="r@example.com",
        user_types=["Staff"],
        user_role_id=4,
        user_role_name="Property Manager",
        is_active=True,
    )
    monkeypatch.setattr(properties_routes, "list_active_staff", AsyncMock(return_value=[staff_row]))
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())

    client = TestClient(app)
    resp = client.get("/api/v1/properties/staff")
    assert resp.status_code == 200
    options = resp.json()["options"]
    assert len(options) == 1
    assert options[0]["buildium_user_id"] == 1
    assert options[0]["display_name"] == "Robert Friedman"
    assert options[0]["role"] == "Property Manager"


def test_put_assignment_calls_upsert_with_assign_to_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _user = _make_app()

    saved = MagicMock()
    saved.action_type = "assign_to_user"
    saved.action_config = {"buildium_user_id": 42}
    upsert_mock = AsyncMock(return_value=saved)
    monkeypatch.setattr(properties_routes, "upsert_assignment", upsert_mock)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: _pool_with_role("admin"))

    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/100/assignment/maintenance",
        json={"buildium_user_id": 42},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cleared"] is False
    upsert_mock.assert_awaited_once()
    kwargs = upsert_mock.await_args.kwargs
    assert kwargs["buildium_property_id"] == 100
    assert kwargs["category"] == "maintenance"
    assert kwargs["action_type"] == "assign_to_user"
    assert kwargs["action_config"] == {"buildium_user_id": 42}


def test_put_assignment_with_null_user_calls_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _user = _make_app()

    delete_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(properties_routes, "delete_assignment", delete_mock)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: _pool_with_role("manager"))

    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/100/assignment/maintenance",
        json={"buildium_user_id": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cleared"] is True
    delete_mock.assert_awaited_once()


def test_put_assignment_forbidden_for_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """#70: a plain member must NOT be able to reroute the team's work.
    The role gate denies with 403 BEFORE any assignment write happens."""
    app, _user = _make_app()

    upsert_mock = AsyncMock()
    delete_mock = AsyncMock()
    monkeypatch.setattr(properties_routes, "upsert_assignment", upsert_mock)
    monkeypatch.setattr(properties_routes, "delete_assignment", delete_mock)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: _pool_with_role("member"))

    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/100/assignment/maintenance",
        json={"buildium_user_id": 42},
    )
    assert resp.status_code == 403
    # The write never happened — gated before the repository call.
    upsert_mock.assert_not_awaited()
    delete_mock.assert_not_awaited()


def test_put_default_assignment_forbidden_for_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """#70: the default-row route shares the same choke point, so a
    member is denied there too."""
    app, _user = _make_app()

    upsert_mock = AsyncMock()
    monkeypatch.setattr(properties_routes, "upsert_assignment", upsert_mock)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: _pool_with_role("member"))

    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/default-assignment/maintenance",
        json={"buildium_user_id": 42},
    )
    assert resp.status_code == 403
    upsert_mock.assert_not_awaited()


def test_put_assignment_rejects_unknown_category() -> None:
    app, _user = _make_app()
    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/100/assignment/not_a_category",
        json={"buildium_user_id": 42},
    )
    assert resp.status_code == 400
    assert "not_a_category" in resp.json()["detail"]


def test_put_default_assignment_writes_with_null_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _user = _make_app()
    saved = MagicMock()
    saved.action_type = "assign_to_user"
    saved.action_config = {"buildium_user_id": 11}
    upsert_mock = AsyncMock(return_value=saved)
    monkeypatch.setattr(properties_routes, "upsert_assignment", upsert_mock)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: _pool_with_role("admin"))

    client = TestClient(app)
    resp = client.put(
        "/api/v1/properties/default-assignment/other",
        json={"buildium_user_id": 11},
    )
    assert resp.status_code == 200
    kwargs = upsert_mock.await_args.kwargs
    assert kwargs["buildium_property_id"] is None
    assert kwargs["category"] == "other"


# ─────────────────────────────────────────────────────────────────────
# 7. /properties/sync — friendly errors
# ─────────────────────────────────────────────────────────────────────


def test_sync_endpoint_surfaces_friendly_403_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Buildium denies the staff list (missing Administration > Users
    — View permission), the toast must NOT show raw Buildium English —
    just the friendly permission-pointer message."""
    app, _user = _make_app()

    async def _fake_settings(_: str) -> object:
        return MagicMock()  # truthy so we don't bail at "not connected"

    monkeypatch.setattr(
        "helixis.buildium.sync.load_buildium_settings_for_company",
        _fake_settings,
    )

    class _Ctx:
        async def __aenter__(self) -> Any:
            api = MagicMock()
            api.administration = MagicMock()
            api.administration.list_users = AsyncMock(
                side_effect=BuildiumApiError(403, "forbidden", path="/users")
            )
            return api

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr("helixis.buildium.sync.BuildiumClient", lambda settings, **_kw: _Ctx())
    # BuildiumApi(client) wraps client; we replace with our mock context.
    monkeypatch.setattr("helixis.buildium.sync.BuildiumApi", lambda client: client)

    client = TestClient(app)
    resp = client.post("/api/v1/properties/sync", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Administration > Users — View" in (body["error"] or "")
    # Raw Buildium phrasing must NOT leak.
    assert "forbidden" not in (body["error"] or "").lower()


@pytest.mark.asyncio
async def test_sync_buildium_staff_no_credentials_returns_friendly_error() -> None:
    """``Buildium is not connected`` path."""
    with patch(
        "helixis.buildium.sync.load_buildium_settings_for_company",
        new=AsyncMock(return_value=None),
    ):
        result = await sync_buildium_staff("c1")
    assert result.ok is False
    assert "not connected" in (result.error or "").lower()
