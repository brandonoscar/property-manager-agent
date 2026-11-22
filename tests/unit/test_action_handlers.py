"""Unit tests for Phase A.2 action handlers.

Each handler talks to Buildium via ``BuildiumClient.request``; we
inject a fake client that records calls and returns canned responses,
so the tests cover validation + body shape + result projection
without any network.

Coverage per handler:
  - happy path: valid params → expected Buildium call + result summary
  - validation failure: at least one bad/missing param → HandlerError
  - Buildium upstream rejection (BuildiumApiError) → wrapped HandlerError

``send_tenant_message`` additionally tests the 403/404-to-501
translation that surfaces "this account scope doesn't expose tenant
messaging via API" as a typed 501 (Not Implemented) to the frontend.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.agent.handlers import (
    HANDLERS,
    AddInternalNoteHandler,
    CreateFollowupTaskHandler,
    HandlerError,
    SendTenantMessageHandler,
    UpdateBuildiumTaskHandler,
    get_handler,
)
from helixis.agent.planner.tool_catalog import TOOL_IDS
from helixis.buildium.errors import BuildiumApiError


class _FakeClient:
    def __init__(self, response: Any = None, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Any = None,
        body: Any = None,
        schema: Any = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "body": body, "query": query})
        if self._raise is not None:
            raise self._raise
        return self._response


# ── registry / catalog ────────────────────────────────────────────


def test_registry_matches_planner_catalog_one_for_one() -> None:
    """The planner's tool catalog and the executor's handler registry
    MUST cover the same set of ``tool_id`` strings — otherwise a tool
    the planner proposes can't be executed and a registered handler
    never gets invoked. The integration is the single source of truth
    for what the LLM is allowed to propose."""
    assert set(HANDLERS.keys()) == set(TOOL_IDS)


def test_get_handler_returns_none_for_unknown_tool() -> None:
    assert get_handler("definitely_not_a_real_tool") is None


# ── update_buildium_task ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_task_happy_path_patches_buildium() -> None:
    handler = UpdateBuildiumTaskHandler()
    client = _FakeClient(response={"Id": 11154, "Priority": "High"})

    result = await handler.execute(
        client,  # type: ignore[arg-type]
        {"priority": "High", "category": "Plumbing"},
        context={"event_task_id": 11154},
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "PATCH"
    assert call["path"] == "/tasks/todorequests/11154"
    assert call["body"]["Priority"] == "High"
    assert call["body"]["Category"] == {"Name": "Plumbing"}
    assert result["buildium_id"] == 11154
    assert "Updated task #11154" in result["summary"]


@pytest.mark.asyncio
async def test_update_task_rejects_empty_parameter_set() -> None:
    handler = UpdateBuildiumTaskHandler()
    client = _FakeClient()
    with pytest.raises(HandlerError) as exc_info:
        await handler.execute(client, {}, context={"event_task_id": 11154})  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400
    assert client.calls == []


@pytest.mark.asyncio
async def test_update_task_rejects_bad_priority() -> None:
    handler = UpdateBuildiumTaskHandler()
    client = _FakeClient()
    with pytest.raises(HandlerError) as exc_info:
        await handler.execute(
            client,  # type: ignore[arg-type]
            {"priority": "Critical"},
            context={"event_task_id": 11154},
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_task_wraps_buildium_api_error() -> None:
    handler = UpdateBuildiumTaskHandler()
    client = _FakeClient(
        raise_exc=BuildiumApiError(status=422, message="invalid", path="/tasks"),
    )
    with pytest.raises(HandlerError) as exc_info:
        await handler.execute(
            client,  # type: ignore[arg-type]
            {"priority": "High"},
            context={"event_task_id": 11154},
        )
    assert exc_info.value.status_code == 502
    assert "Buildium rejected" in str(exc_info.value)


# ── add_internal_note ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_note_happy_path() -> None:
    handler = AddInternalNoteHandler()
    client = _FakeClient(response={"Id": 7001})
    result = await handler.execute(
        client,  # type: ignore[arg-type]
        {"note_body": "Vendor on-site at 10am Friday."},
        context={"event_task_id": 11154},
    )
    assert client.calls[0]["path"] == "/tasks/11154/notes"
    assert client.calls[0]["body"] == {"Note": "Vendor on-site at 10am Friday."}
    assert result["buildium_id"] == 11154
    assert result["buildium_note_id"] == 7001


@pytest.mark.asyncio
async def test_add_note_rejects_empty_body() -> None:
    handler = AddInternalNoteHandler()
    client = _FakeClient()
    with pytest.raises(HandlerError):
        await handler.execute(
            client,  # type: ignore[arg-type]
            {"note_body": "   "},
            context={"event_task_id": 11154},
        )


# ── send_tenant_message ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_tenant_message_happy_path() -> None:
    handler = SendTenantMessageHandler()
    client = _FakeClient(response={"Id": 42})
    result = await handler.execute(
        client,  # type: ignore[arg-type]
        {
            "tenant_id": 82336,
            "subject": "Plumber dispatched",
            "body": "Hi Josh, a plumber is on the way.",
        },
    )
    assert client.calls[0]["path"] == "/communications/messages"
    assert client.calls[0]["body"]["TenantIds"] == [82336]
    assert result["buildium_id"] == 42
    assert "Sent message to tenant #82336" in result["summary"]


@pytest.mark.asyncio
async def test_send_tenant_message_translates_403_to_501() -> None:
    """When Buildium's tenant-messaging surface isn't available for
    the company's account scope, return 501 with guidance — not a
    generic 502 — so the frontend can point the PM at the right
    fallback (Buildium web)."""
    handler = SendTenantMessageHandler()
    client = _FakeClient(
        raise_exc=BuildiumApiError(
            status=403,
            message="forbidden",
            path="/communications/messages",
        ),
    )
    with pytest.raises(HandlerError) as exc_info:
        await handler.execute(
            client,  # type: ignore[arg-type]
            {
                "tenant_id": 82336,
                "subject": "Test",
                "body": "Test body.",
            },
        )
    assert exc_info.value.status_code == 501
    assert "Buildium does not expose tenant messaging" in str(exc_info.value)


# ── create_followup_task ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_followup_task_happy_path_with_unit_default() -> None:
    handler = CreateFollowupTaskHandler()
    client = _FakeClient(response={"Id": 11200})
    result = await handler.execute(
        client,  # type: ignore[arg-type]
        {
            "title": "Plumbing inspection — unit 3B",
            "description": "Third plumbing call in 90 days.",
            "due_date": "2026-06-12",
            "category": "Inspection",
            "priority": "Normal",
        },
        context={
            "event_unit_id": 58280,
            "assigned_to_user_id": 77042,
        },
    )
    body = client.calls[0]["body"]
    assert body["Title"] == "Plumbing inspection — unit 3B"
    # Unit_id falls back from context when not in params.
    assert body["UnitId"] == 58280
    assert body["AssignedToUserId"] == 77042
    assert body["Priority"] == "Normal"
    # Buildium's create-task endpoint requires TaskStatus alongside Priority;
    # omitting it 422'd and surfaced as a 502 on webhook execute (gotcha 20).
    assert body["TaskStatus"] == "New"
    assert result["buildium_id"] == 11200
    assert "unit #58280" in result["summary"]


@pytest.mark.asyncio
async def test_create_followup_task_rejects_missing_due_date() -> None:
    handler = CreateFollowupTaskHandler()
    client = _FakeClient()
    with pytest.raises(HandlerError) as exc_info:
        await handler.execute(
            client,  # type: ignore[arg-type]
            {"title": "x", "description": "y"},
            context={"event_unit_id": 1},
        )
    assert exc_info.value.status_code == 400
