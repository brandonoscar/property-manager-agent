"""#226 — Composio honor successful:false (no false-success on failed writes).

A Composio write tool that returns ``{"successful": false, ...}`` WITHOUT
raising must NOT be recorded / replayed / reported as success. This reuses the
cut-1 verify hook (verify=_composio_successful, tag
verify_failed:provider_unsuccessful).

CI-safe: stub FunctionTool, no Composio/creds. The confirm gate is auto-approved
via the real module channel; the idempotency store is injected (fake) for the
durable-path tests and forced absent for the passthrough test.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from agents import FunctionTool

from helixis.agents import idempotency as idem
from helixis.connectors import tool_gate
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
)
from helixis.memory.context import memory_context


class FakeStore:
    """In-memory IdempotencyStore for the durable-path tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        k = (company_id, idem_key)
        if k in self._rows:
            return ClaimResult(won=False, row=self._rows[k])
        row = ClaimRow(
            company_id=company_id,
            idem_key=idem_key,
            action_type=action_type,
            status=STATUS_IN_PROGRESS,
            result=None,
            error=None,
            attempts=1,
            expires_at=None,
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
        await self._set(company_id, idem_key, status="failed_retryable", error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABANDONED_UNKNOWN, error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        await self._set(company_id, idem_key, status="aborted")

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        return False

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0


async def _auto_approve(**_kwargs: object) -> bool:
    return True


def _stub(
    slug: str, *, successful: Any = True, include_flag: bool = True
) -> tuple[FunctionTool, dict[str, int]]:
    """A provider-shaped write tool; its result's ``successful`` flag is
    configurable, and the flag can be omitted entirely (include_flag=False)."""
    calls = {"n": 0}

    async def inner(_ctx: Any, payload: str) -> str:
        calls["n"] += 1
        body: dict[str, Any] = {"data": "provider-data", "error": None, "secret_to": "victim@x.com"}
        if include_flag:
            body["successful"] = successful
        return json.dumps(body)

    tool = FunctionTool(
        name=slug,
        description="stub",
        params_json_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "additionalProperties": False,
        },
        on_invoke_tool=inner,
        strict_json_schema=False,
    )
    return tool, calls


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(idem, "store_from_pool", lambda: s)
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    return s


# ── successful:true → success (unchanged) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_true_records_success(store: FakeStore) -> None:
    tool, calls = _stub("GMAIL_SEND", successful=True)
    gated = tool_gate.gate_tool(tool)
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        result = await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))

    assert calls["n"] == 1
    assert json.loads(result)["successful"] is True
    key = next(iter(store._rows))
    assert store._rows[key].status == STATUS_SUCCEEDED


# ── successful:false → NOT success ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_false_is_needs_review_not_success(store: FakeStore) -> None:
    tool, calls = _stub("GMAIL_SEND", successful=False)
    gated = tool_gate.gate_tool(tool)
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        result = await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))

    assert calls["n"] == 1  # executed exactly once
    # Row is abandoned (not SUCCEEDED) with the distinct provider tag.
    key = next(iter(store._rows))
    assert store._rows[key].status == STATUS_ABANDONED_UNKNOWN
    assert store._rows[key].error == "verify_failed:provider_unsuccessful"

    # User-facing result is the honest non-success message — NOT the raw payload,
    # and no arg/PII content echoed.
    parsed = json.loads(result)
    assert parsed["successful"] is False
    assert "flagged for review" in parsed["data"]
    assert "provider-data" not in result  # not the raw provider payload
    assert "victim@x.com" not in result  # no PII echoed
    assert "a@b.com" not in result


@pytest.mark.asyncio
async def test_successful_false_no_auto_refire(store: FakeStore) -> None:
    """A SECOND identical call hits the abandoned record → needs-review,
    original_invoke NOT called again."""
    tool, calls = _stub("GMAIL_SEND", successful=False)
    gated = tool_gate.gate_tool(tool)
    payload = json.dumps({"to": "a@b.com"})
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        first = await gated.on_invoke_tool(None, payload)
        second = await gated.on_invoke_tool(None, payload)

    assert calls["n"] == 1  # NOT re-invoked on the second call
    assert json.loads(first)["successful"] is False
    assert json.loads(second)["successful"] is False
    assert "flagged for review" in json.loads(second)["data"]


# ── passthrough (no store): fix is live without the idempotency flag ──────────


@pytest.mark.asyncio
async def test_successful_false_passthrough_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: False)
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    tool, calls = _stub("GMAIL_SEND", successful=False)
    gated = tool_gate.gate_tool(tool)
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        result = await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))

    assert calls["n"] == 1
    parsed = json.loads(result)
    assert parsed["successful"] is False
    assert "flagged for review" in parsed["data"]


# ── absent field → preserve success (no regression) ───────────────────────────


@pytest.mark.asyncio
async def test_absent_flag_preserves_success(store: FakeStore) -> None:
    """A tool that omits ``successful`` must still succeed (don't regress)."""
    tool, calls = _stub("GMAIL_SEND", include_flag=False)
    gated = tool_gate.gate_tool(tool)
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        result = await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))

    assert calls["n"] == 1
    assert "successful" not in json.loads(result)  # passed through unchanged
    key = next(iter(store._rows))
    assert store._rows[key].status == STATUS_SUCCEEDED


# ── #227: store-unavailable fail-closed → nothing-changed, not success ────────


@pytest.mark.asyncio
async def test_store_unavailable_does_not_report_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """required=True + no store → fail closed before invoking; the Composio
    caller must return the nothing-changed shape, NOT success and NOT the raw
    result. Guards the new blocked_store_unavailable outcome (#227) — without
    the caller arm it would fall through to ``return guard.result`` (success)."""
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: True)
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    tool, calls = _stub("GMAIL_SEND", successful=True)
    gated = tool_gate.gate_tool(tool)
    with memory_context(company_id="co-1", user_id="u", session_id="s"):
        result = await gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"}))

    assert calls["n"] == 0  # never invoked (fail-closed before execute)
    parsed = json.loads(result)
    assert parsed["successful"] is False
    assert "nothing was changed" in parsed["data"].lower()


# ── verifier unit: explicit-false-only + raise-on-unparseable ─────────────────


@pytest.mark.asyncio
async def test_composio_successful_verifier_unit() -> None:
    assert await tool_gate._composio_successful(json.dumps({"successful": True})) is True
    assert await tool_gate._composio_successful(json.dumps({"successful": False})) is False
    assert await tool_gate._composio_successful(json.dumps({"data": "x"})) is True  # absent → True
    assert await tool_gate._composio_successful({"successful": False}) is False  # dict tolerated
    with pytest.raises(Exception):
        await tool_gate._composio_successful("not json{{")  # → verify_error → needs-review
