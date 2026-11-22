"""PR #76 — task creation tool + uniform Buildium error sanitizer.

These tests pin three behaviors that survived PR #75:

1. **Outstanding balances fallback shape.** PR #75 sent
   ``entitytype="Rental"`` on the first call, which Buildium 400s
   because ``entitytype`` requires a matching ``entityid``. PR #76
   drops the param. (The full first-call/fallback shape is covered
   by ``test_pr75_bug_ab.py``; the test here is a focused regression
   pin that ``entitytype`` doesn't sneak back in.)

2. **``_sanitize_buildium_error`` strips raw Buildium English.**
   Pre-PR-#76 every tool returned ``f"Buildium API error … : {exc}"``,
   leaking field names, "EntityId when EntityType is provided",
   "Settings → Developer Tools → API Keys" — straight into the chat
   the property manager sees. The new helper logs the raw error
   server-side and returns a short class-based phrase ("Buildium
   rejected that request…" / "Buildium is having trouble…").

3. **``buildium_create_to_do_task`` actually creates a task.**
   The agent used to lie ("Buildium doesn't support task management").
   It does — ``api.tasks.create(...)`` POSTs to
   ``/v1/tasks/todorequests``. This tool wraps that, and the
   user-facing string echoes ONLY the task title (no Buildium task
   id per the HARD PRESENTATION RULES from PR #75).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.errors import BuildiumApiError
from helixis.memory.context import memory_context

# ── Test helpers (lightweight; mirror existing test files) ─────────


def _ctx() -> Any:
    return memory_context(company_id="company-1", user_id="u1", session_id="s1")


def _mock_task_get(api: Any, task_id: int, title: str = "") -> None:
    """Wire api.tasks.get to confirm the read-back verifier (cut 1): returns a
    Task object whose .id matches the created id, so the create is verified."""
    from helixis.buildium.schemas import Task

    api.tasks.get = AsyncMock(return_value=Task(Id=task_id, Title=title))


def _client(api: Any) -> Any:
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


def _settings() -> Any:
    return MagicMock()


def _patch(client: Any, api: Any, *, approve_writes: bool = True) -> Any:
    """Patch the Buildium API surface for a tool call.

    ``approve_writes`` auto-approves the in-code confirm gate
    (``confirm_or_abort``) so these tests can exercise post-approval write
    behavior (payload shape, sanitized errors) without driving the async
    confirm round-trip. The gate's own approve/deny/timeout/skip enforcement
    is covered by ``test_buildium_write_confirmation.py``. Read-only tools
    ignore this (they never call the gate).

    The confirm gate is now reached via ``run_guarded`` (the idempotency
    primitive), which calls ``confirm_or_abort`` imported into
    ``helixis.agents.idempotency`` — so we patch the gate THERE, not on
    ``buildium_tools``."""
    from contextlib import ExitStack

    from helixis.agents import idempotency as idem

    stack = ExitStack()

    async def _auto_approve(**_kwargs: Any) -> bool:
        return True

    class _Combined:
        def __enter__(self) -> Any:
            stack.enter_context(_ctx())
            stack.enter_context(
                patch.object(
                    bt,
                    "load_buildium_settings_for_company",
                    new=AsyncMock(return_value=_settings()),
                )
            )
            stack.enter_context(patch.object(bt, "BuildiumClient", return_value=client))
            stack.enter_context(patch.object(bt, "BuildiumApi", return_value=api))
            if approve_writes:
                stack.enter_context(patch.object(idem, "confirm_or_abort", new=_auto_approve))
            return None

        def __exit__(self, *exc: Any) -> None:
            stack.__exit__(*exc)

    return _Combined()


# ── Bug 1 — outstandingbalances must not send entitytype ────────────


@pytest.mark.asyncio
async def test_outstandingbalances_no_entitytype() -> None:
    """Regression pin: ``entitytype`` must NOT appear in either of the
    two outstanding-balances calls. Buildium 400s when entitytype is
    sent without a matching entityid; PR #76 drops the param.

    Both call sites (``_list_overdue_balances_impl`` and
    ``_get_rent_roll_impl``) have to obey this rule, so we exercise
    both here as one focused regression."""
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list = AsyncMock(return_value=[])
    api.leases.list_outstanding_balances = AsyncMock(return_value=[])
    api.leases.get = AsyncMock()

    with _patch(_client(api), api):
        await bt._list_overdue_balances_impl()
        await bt._get_rent_roll_impl()

    # ``await_args_list`` for the union of both impls — every single
    # outstanding-balances call must be entitytype-free.
    for call in api.leases.list_outstanding_balances.await_args_list:
        kwargs = call.kwargs
        assert "entitytype" not in kwargs, (
            f"PR #76: entitytype must NOT be sent on outstandingbalances calls"
            f" — Buildium 400s without a matching entityid. Got kwargs={kwargs!r}"
        )
        assert "entityid" not in kwargs, (
            f"PR #76: entityid is paired with entitytype and shouldn't appear"
            f" on the list-filter calls. Got kwargs={kwargs!r}"
        )


# ── Bug 3 — _sanitize_buildium_error ─────────────────────────────────


def _bup_error(status: int, message: str) -> BuildiumApiError:
    # The constructor takes (status, message, *, code, request_id, path).
    return BuildiumApiError(status, message, path="/leases/outstandingbalances")


def test_buildium_error_sanitized_400() -> None:
    """A 400 from Buildium carrying technical jargon ("EntityId required
    when EntityType is provided") must be redacted to a one-line
    "Buildium rejected that request" phrase — no field names, no
    settings-page paths, no API-key references."""
    exc = _bup_error(400, "EntityId required when EntityType is provided")
    msg = bt._sanitize_buildium_error(exc, "list overdue balances")
    assert "Buildium rejected" in msg
    assert "list overdue balances" in msg
    # The raw jargon must be gone.
    assert "EntityId" not in msg
    assert "EntityType" not in msg
    # Defense-in-depth: no settings-page / API-key wording either.
    assert "API key" not in msg
    assert "Developer Tools" not in msg


def test_buildium_error_sanitized_500() -> None:
    """5xx errors get a "trouble responding" message; the raw exception
    text (which could include internal stack traces or system paths)
    is NOT included."""
    exc = _bup_error(503, "Service Unavailable: upstream pool exhausted")
    msg = bt._sanitize_buildium_error(exc, "list overdue balances")
    assert "trouble responding" in msg.lower()
    assert "list overdue balances" in msg
    # The raw exception body must NOT leak.
    assert "Service Unavailable" not in msg
    assert "upstream pool exhausted" not in msg


def test_buildium_error_sanitized_401() -> None:
    """A 401 mentions the Connectors page in a friendly way — but NEVER
    pastes the raw Buildium phrasing ("API key", "Developer Tools") that
    PR #75 banned from chat.

    ⚠ This said "401/403" while only ever exercising 401, and the two have
    since been split — a 403 means the credentials were ACCEPTED and the
    request refused, so it deliberately DOES name the API key's permission
    path (that is where a permission is actually set, and it is the one
    remedy a 403 user can act on). The PR #75 ban is about echoing
    Buildium's raw English at somebody whose credential is dead; naming a
    settings path in the one message where it is the answer is a different
    thing. Both are pinned in test_buildium_error_is_not_an_absence.py."""
    exc = _bup_error(401, "Unauthorized: invalid API key in Developer Tools")
    msg = bt._sanitize_buildium_error(exc, "list overdue balances")
    assert "Connectors" in msg
    assert "list overdue balances" in msg
    assert "API key" not in msg
    assert "Developer Tools" not in msg
    assert "Unauthorized" not in msg
    # Degraded-honesty contract (2026-07-22, the live sandbox-401 dogfood):
    # the message must read as a PERSISTENT auth failure with an explicit
    # no-retry / no-"you have no records" instruction to the agent — a dead
    # credential must never be summarized as an empty account.
    assert "failed authentication" in msg
    assert "reconnected" in msg
    assert "Do NOT retry" in msg
    assert "no records" in msg


def test_buildium_error_sanitized_404() -> None:
    """A 404 is a "couldn't find that" message — no leaked path."""
    exc = _bup_error(404, "Lease 99999 not found")
    msg = bt._sanitize_buildium_error(exc, "fetch that lease")
    assert "couldn't find" in msg.lower()
    assert "fetch that lease" in msg
    # The raw error string (with internal ids like "99999") must NOT leak.
    assert "99999" not in msg
    assert "Lease 99999" not in msg


def test_buildium_error_sanitized_unknown_status() -> None:
    """An error with an unrecognised status (e.g. None or 418) still
    gets a sanitized fallback string — never the raw exception text."""
    exc = _bup_error(418, "I'm a teapot")
    msg = bt._sanitize_buildium_error(exc, "list overdue balances")
    assert "list overdue balances" in msg
    assert "logged for review" in msg
    assert "teapot" not in msg.lower()


def test_sanitizer_logs_raw_for_debugging(caplog: pytest.LogCaptureFixture) -> None:
    """The raw error MUST be preserved in server-side logs so a
    debugger can see what Buildium actually said, even though the
    chat surface only sees the sanitized phrase."""
    import logging

    exc = _bup_error(400, "EntityId required when EntityType is provided")
    with caplog.at_level(logging.WARNING, logger="helixis.agents.buildium_tools"):
        bt._sanitize_buildium_error(exc, "list overdue balances")
    raw = " ".join(r.getMessage() for r in caplog.records)
    # The full raw Buildium phrasing is on a log line, NOT the return.
    assert "EntityId required when EntityType is provided" in raw
    assert "list overdue balances" in raw
    assert "status=400" in raw


# ── Bug 2 — buildium_create_to_do_task ─────────────────────────────


@pytest.mark.asyncio
async def test_create_to_do_task_calls_buildium_with_correct_payload() -> None:
    """The tool must shape Buildium's expected payload — Title
    (required) plus only the optional fields the caller passed."""
    api = MagicMock()
    api.tasks = MagicMock()
    # Buildium's create returns the full task dict including its Id —
    # we mock with one to confirm the tool does NOT surface it.
    api.tasks.create = AsyncMock(return_value={"Id": 99887, "Title": "Send lease to Ryan"})
    _mock_task_get(api, 99887, "Send lease to Ryan")  # read-back confirms it landed

    with _patch(_client(api), api):
        result = await bt._create_to_do_task_impl(
            title="Send lease to Ryan",
            assigned_to_user_id=42,
            due_date="2026-06-15",
        )

    # Per HARD PRESENTATION RULES — the response echoes only the title.
    assert result == 'Task created in Buildium: "Send lease to Ryan".'
    # And explicitly NOT the Buildium task id.
    assert "99887" not in result

    # Payload shape: title is required, the optional fields the caller
    # passed are included, the ones they didn't are NOT.
    api.tasks.create.assert_awaited_once()
    fields = api.tasks.create.await_args.kwargs.get("fields") or (
        api.tasks.create.await_args.args[0] if api.tasks.create.await_args.args else {}
    )
    assert fields["Title"] == "Send lease to Ryan"
    assert fields["AssignedToUserId"] == 42
    assert fields["DueDate"] == "2026-06-15"
    # Caller didn't pass these, so they must NOT be in the payload.
    assert "Description" not in fields
    assert "PropertyId" not in fields
    assert "UnitId" not in fields


@pytest.mark.asyncio
async def test_create_to_do_task_returns_only_title_no_id() -> None:
    """Independent of the payload shape, the user-facing reply must
    contain the title and NOTHING that looks like a Buildium id."""
    api = MagicMock()
    api.tasks = MagicMock()
    api.tasks.create = AsyncMock(return_value={"Id": 24188, "Title": "Anything"})
    _mock_task_get(api, 24188, "Anything")  # read-back confirms it landed

    with _patch(_client(api), api):
        result = await bt._create_to_do_task_impl(title="Anything", assigned_to_user_id=1)

    assert result == 'Task created in Buildium: "Anything".'
    assert "24188" not in result
    assert "Id" not in result  # No accidental dict-dump.
    assert "#" not in result


@pytest.mark.asyncio
async def test_create_to_do_task_handles_400_with_sanitized_message() -> None:
    """If Buildium 400s on the create (e.g. missing required field),
    the user must see the sanitized "Buildium rejected" message —
    never the raw exception text."""
    api = MagicMock()
    api.tasks = MagicMock()
    api.tasks.create = AsyncMock(
        side_effect=BuildiumApiError(400, "Title is required", path="/tasks/todorequests")
    )

    with _patch(_client(api), api):
        result = await bt._create_to_do_task_impl(title="", assigned_to_user_id=1)

    assert "Buildium rejected" in result
    assert "create the task" in result
    # Raw exception jargon must NOT leak.
    assert "Title is required" not in result
    assert "todorequests" not in result


@pytest.mark.asyncio
async def test_create_to_do_task_not_connected() -> None:
    """Smoke test for the no-credentials path — the tool returns the
    standard "Buildium is not connected" guidance without trying to
    POST."""
    # No memory context bound → ``_open_api`` returns ``_NOT_CONNECTED``.
    result = await bt._create_to_do_task_impl(title="Send lease to Ryan", assigned_to_user_id=1)
    assert result.startswith("Buildium is not connected")


@pytest.mark.asyncio
async def test_create_to_do_task_is_registered_in_tool_factory() -> None:
    """The new tool must be wired into ``buildium_tools()`` or the
    orchestrator won't have access to it."""
    tools = bt.buildium_tools()
    names = {t.name for t in tools}
    assert "buildium_create_to_do_task" in names


# ── End-to-end smoke: a tool returning a sanitized error reads cleanly ─


@pytest.mark.asyncio
async def test_real_tool_returns_sanitized_error_on_buildium_400() -> None:
    """Integration-flavored sanity check: the FULL tool path
    (``_list_overdue_balances_impl``) returns a sanitized message
    when Buildium 400s, NOT the raw exception text the agent would
    have parroted pre-PR-#76."""
    api = MagicMock()
    api.leases = MagicMock()
    api.leases.list_outstanding_balances = AsyncMock(
        side_effect=BuildiumApiError(
            400,
            "EntityId required when EntityType is provided",
            path="/leases/outstandingbalances",
        )
    )

    with _patch(_client(api), api):
        result = await bt._list_overdue_balances_impl()

    # Sanitized form is what the agent (and therefore the user) sees.
    assert "Buildium rejected" in result
    assert "list overdue balances" in result
    # NONE of the technical jargon leaks.
    assert "EntityId" not in result
    assert "EntityType" not in result
    assert "outstandingbalances" not in result
