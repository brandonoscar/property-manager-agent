"""Site-level idempotency-dedup integration tests.

The primitive's own dedup is covered in ``test_action_idempotency.py``. These
prove dedup at the ACTUAL call sites — by injecting the in-memory fake store via
``store_from_pool`` and binding a real ``memory_context`` (so ``company_id`` is
present and the durable path, not the no-store passthrough, is taken).

REQUIRED: ``tool_gate.gated_invoke`` (all Composio writes). Also covers
``buildium_tools._create_to_do_task_impl`` and ``browser_tools._submit_form_impl``.

These ADD coverage; the seam tests in test_composio_tool_gate / test_pr76 /
test_browser_submit still assert confirm + fail-closed and are untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agents import FunctionTool

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.agents import buildium_tools as bdt
from helixis.agents import idempotency as idem
from helixis.connectors import tool_gate
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_ABORTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
)
from helixis.memory.context import memory_context


class FakeStore:
    """In-memory IdempotencyStore (same shape as the primitive's test fake)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
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
        await self._set(company_id, idem_key, status=STATUS_SUCCEEDED, result=result)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_FAILED_RETRYABLE, error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABANDONED_UNKNOWN, error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABORTED)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        row = self._rows.get((company_id, idem_key))
        if row is None or row.status not in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
            return False
        row.status = STATUS_IN_PROGRESS
        return True

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0


async def _auto_approve(**_kwargs: object) -> bool:
    return True


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Inject the fake store at the run_guarded chokepoint + auto-approve."""
    store = FakeStore()
    monkeypatch.setattr(idem, "store_from_pool", lambda: store)
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    return store


# ── gated_invoke (REQUIRED) ──────────────────────────────────────────────────


def _stub_write_tool() -> tuple[FunctionTool, dict[str, int]]:
    calls = {"n": 0}

    async def inner(_ctx: Any, payload: str) -> str:
        calls["n"] += 1
        return json.dumps({"successful": True, "data": "sent", "n": calls["n"]})

    tool = FunctionTool(
        name="GMAIL_SEND",
        description="stub gmail send",
        params_json_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "additionalProperties": False,
        },
        on_invoke_tool=inner,
        strict_json_schema=False,
    )
    return tool, calls


@pytest.mark.asyncio
async def test_gated_invoke_dedups_identical_call(fake_store: FakeStore) -> None:
    """Two identical approved GMAIL_SEND invocations → original fires ONCE; the
    second returns the replayed result without a second send."""
    tool, calls = _stub_write_tool()
    gated = tool_gate.gate_tool(tool)
    payload = json.dumps({"to": "a@b.com"})

    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        first = await gated.on_invoke_tool(None, payload)
        second = await gated.on_invoke_tool(None, payload)

    assert calls["n"] == 1  # underlying Composio invoke fired exactly once
    # Replay returns the SAME stored result the first call produced.
    assert json.loads(first) == json.loads(second)


@pytest.mark.asyncio
async def test_gated_invoke_distinct_args_both_fire(fake_store: FakeStore) -> None:
    """Different args → different key → both sends fire (no over-dedup)."""
    tool, calls = _stub_write_tool()
    gated = tool_gate.gate_tool(tool)

    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))
        await gated.on_invoke_tool(None, json.dumps({"to": "c@d.com"}))

    assert calls["n"] == 2


# ── buildium _create_to_do_task_impl ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_buildium_create_task_dedups(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical approved task creations → api.tasks.create fires ONCE."""
    from unittest.mock import AsyncMock, MagicMock

    from helixis.buildium.schemas import Task

    create = AsyncMock(return_value={"Id": 1, "Title": "Fix sink"})
    api = MagicMock()
    api.tasks.create = create
    api.tasks.get = AsyncMock(return_value=Task(Id=1, Title="Fix sink"))  # read-back confirms
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(bdt, "load_buildium_settings_for_company", AsyncMock(return_value=object()))
    monkeypatch.setattr(bdt, "BuildiumClient", lambda *a, **k: client)
    monkeypatch.setattr(bdt, "BuildiumApi", lambda *a, **k: api)

    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        first = await bdt._create_to_do_task_impl("Fix sink", unit_id=4, assigned_to_user_id=1)
        second = await bdt._create_to_do_task_impl("Fix sink", unit_id=4, assigned_to_user_id=1)

    create.assert_awaited_once()
    assert "Fix sink" in first
    assert first == second  # replay returns the same user-facing line


# ── browser _submit_form_impl ────────────────────────────────────────────────


class _FakeLocator:
    def __init__(self) -> None:
        self.click_calls = 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls += 1


class _FakeAccessibility:
    async def snapshot(self) -> dict:
        # Post-submit page carries a confirmation phrase so the Cut-2 verifier
        # confirms the filing. URL is left UNCHANGED so the idempotency key
        # (which includes the page url) is stable across both calls and the
        # dedup assertion holds.
        return {"role": "WebArea", "name": "Filing confirmation number 4242", "children": []}


class _FakePage:
    def __init__(self) -> None:
        self._loc = _FakeLocator()
        self.url = "https://court.example.gov/evict"
        self.accessibility = _FakeAccessibility()

    def locator(self, selector: str) -> _FakeLocator:
        return self._loc

    @property
    def loc(self) -> _FakeLocator:
        return self._loc

    async def wait_for_load_state(self, state: str = "load", timeout: int | None = None) -> None:
        # No URL change — confirmation comes from the accessibility tree above.
        return None


@pytest.mark.asyncio
async def test_browser_submit_dedups(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical approved submits in one session → click fires ONCE."""
    page = _FakePage()
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr("helixis.agents.browser_tools.current_session", lambda: sess)

    def _settings() -> Any:
        from helixis.settings import Settings

        return Settings(env="test", memory={"enabled": False})

    with memory_context(company_id="co-1", user_id="u-1", session_id="sess-1"):
        first = await bt._submit_form_impl(
            "button#file", "confirmation number", settings=_settings()
        )
        second = await bt._submit_form_impl(
            "button#file", "confirmation number", settings=_settings()
        )

    assert page.loc.click_calls == 1  # filed exactly once
    assert first.status == "submitted"
    assert second.status == "submitted"  # replay reported as submitted, no 2nd click
