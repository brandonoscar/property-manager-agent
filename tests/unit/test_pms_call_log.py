"""One log line per PMS API call, and the approval id on every Activity row.

Rentvine terms rule 22 (``design/rentvine-terms-rules.md``): log every call
with the customer, endpoint, action, approval token for writes, result and
time, and never secrets or payloads. The founder chose (2026-09-24) to log
Buildium the same way and to put the approval id on the Activity record of
every change (``observability/pms_calls.py``, ``agents/write_ledger.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from helixis.agents import idempotency as idem
from helixis.agents import tools as agent_tools
from helixis.buildium.client import BuildiumClient
from helixis.memory.context import memory_context
from helixis.observability.pms_calls import template_of
from helixis.rentvine import client as rvclient
from helixis.settings import BuildiumSettings, RentvineSettings

from .test_write_ledger import CONFIRM, _written

KEY = "-".join(("rv", "log", "key"))
SECRET = "-".join(("rv", "log", "secret"))


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("pms_call ")]


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


def _rentvine(handler, company_id: str | None = "co-rv") -> rvclient.RentvineClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=(KEY, SECRET))
    settings = RentvineSettings(account_code="acme", api_key=KEY, api_secret=SECRET)
    return rvclient.RentvineClient(settings, client=http, company_id=company_id)


def _buildium(handler, company_id: str | None = "co-bd") -> BuildiumClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x.test/v1")
    settings = BuildiumSettings(
        client_id="cid-not-logged",
        client_secret=SECRET,
        base_url="https://x.test",
        account_id=695001,
        requests_per_sec=1000.0,
    )
    return BuildiumClient(settings, http_client=http, company_id=company_id)


# ── Rentvine ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rentvine_read_logs_one_line_with_the_template_not_the_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    client = _rentvine(lambda _r: httpx.Response(200, json=[]))
    await client.get("/properties/{propertyID}/units", path_params={"propertyID": 4610})
    (line,) = _lines(caplog)
    f = _fields(line)
    assert f == {
        **f,
        "pms": "rentvine",
        "company": "co-rv",
        "account": "acme",
        "method": "GET",
        "endpoint": "/properties/{propertyID}/units",
        "status": "200",
        "outcome": "ok",
        "attempt": "1",
        "approval": "-",
    }
    assert "4610" not in line and int(f["duration_ms"]) >= 0


@pytest.mark.asyncio
async def test_every_rentvine_attempt_is_its_own_line(caplog: pytest.LogCaptureFixture) -> None:
    """Two failures then a success: three lines, numbered 1, 2, 3."""
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    replies = iter([503, 503, 200])
    client = _rentvine(lambda _r: httpx.Response(next(replies), json=[]))
    await client.get("/properties")
    got = [(_fields(x)["attempt"], _fields(x)["status"]) for x in _lines(caplog)]
    assert got == [("1", "503"), ("2", "503"), ("3", "200")]
    assert [_fields(x)["outcome"] == "ok" for x in _lines(caplog)] == [False, False, True]


@pytest.mark.asyncio
async def test_a_rentvine_write_logs_the_approval_that_authorised_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    client = _rentvine(lambda _r: httpx.Response(200, json={"ok": True}))
    token = agent_tools.current_confirm_id.set("confirm-abc")
    try:
        await client.post(
            "/maintenance/work-orders/{workOrderID}",
            json_body={"x": 1},
            path_params={"workOrderID": 7},
        )
    finally:
        agent_tools.current_confirm_id.reset(token)
    (line,) = _lines(caplog)
    assert _fields(line)["method"] == "POST" and _fields(line)["approval"] == "confirm-abc"


@pytest.mark.asyncio
async def test_a_rentvine_call_line_never_carries_a_credential_or_a_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    client = _rentvine(lambda _r: httpx.Response(401, text=f"bad {KEY}:{SECRET} tenant Jane Roe"))
    with pytest.raises(rvclient.RentvineAPIError):
        await client.get("/properties")
    (line,) = _lines(caplog)
    for leaked in (KEY, SECRET, "Jane Roe"):
        assert leaked not in line


@pytest.mark.asyncio
async def test_the_company_falls_back_to_the_requests_own(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    client = _rentvine(lambda _r: httpx.Response(200, json=[]), company_id=None)
    with memory_context(company_id="co-from-request", user_id="u", session_id="s"):
        await client.get("/properties")
    await client.get("/properties")
    companies = [_fields(x)["company"] for x in _lines(caplog)]
    assert companies == ["co-from-request", "-"]  # unknown is "-", never a guess


# ── Buildium, the same line ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_buildium_call_logs_the_same_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")
    client = _buildium(lambda _r: httpx.Response(200, json=[]))
    await client.request("GET", "/rentals/123/units", query={"name": "Jane Roe"})
    (line,) = _lines(caplog)
    f = _fields(line)
    assert f == {
        **f,
        "pms": "buildium",
        "company": "co-bd",
        "account": "695001",
        "method": "GET",
        "endpoint": "/rentals/{id}/units",
        "status": "200",
        "outcome": "ok",
        "attempt": "1",
    }
    for leaked in ("cid-not-logged", SECRET, "Jane", "123"):
        assert leaked not in line


@pytest.mark.asyncio
async def test_a_buildium_retry_and_a_buildium_write_are_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="helixis.pms_calls")

    async def _no_sleep(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(BuildiumClient, "_sleep_backoff", _no_sleep)
    replies = iter([429, 201])
    client = _buildium(lambda _r: httpx.Response(next(replies), json={"Id": 9}))
    token = agent_tools.current_confirm_id.set("confirm-xyz")
    try:
        await client.request("POST", "/leases/55/transactions/charges", body={"Amount": 10})
    finally:
        agent_tools.current_confirm_id.reset(token)
    got = [_fields(x) for x in _lines(caplog)]
    assert [(g["attempt"], g["status"]) for g in got] == [("1", "429"), ("2", "201")]
    assert {g["approval"] for g in got} == {"confirm-xyz"}
    assert got[0]["endpoint"] == "/leases/{id}/transactions/charges"


def test_template_of_drops_the_query_and_every_id() -> None:
    assert template_of("/v1/rentals/123/units/45?name=Jane") == "/v1/rentals/{id}/units/{id}"
    assert template_of("/properties") == "/properties"


# ── the approval id on the Activity record ─────────────────────────────────


def test_the_activity_row_stores_the_approval_id_and_the_claim() -> None:
    (entry,) = _written(
        company_id="co-1",
        action_type="buildium_post_charge",
        confirm=CONFIRM,
        outcome="executed",
        approval_id="confirm-123",
        idem_key="k-9",
    )
    assert entry.raw["approval_id"] == "confirm-123"
    assert entry.raw["idem_key"] == "k-9"


async def _approve_next() -> str:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            await agent_tools.resolve_confirmation(drained[0]["confirm_id"], True)
            return str(drained[0]["confirm_id"])
    raise AssertionError("no confirm card")


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.asyncio
async def test_a_gated_write_records_the_card_the_user_approved(
    durable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through run_guarded, on both of its paths."""
    if durable:
        from tests.conftest import FakeIdempotencyStore

        store = FakeIdempotencyStore()
        monkeypatch.setattr(idem, "store_from_pool", lambda: store)
    seen: list[dict[str, Any]] = []

    async def _spy(**kw: Any) -> None:
        seen.append(kw)

    await agent_tools.drain_pending_confirmations()

    async def _execute() -> str:
        return "done"

    with patch("helixis.agents.write_ledger.record_write", new=_spy):
        task = asyncio.create_task(
            idem.run_guarded(
                company_id="co-1",
                idem_key="k-1",
                action_type="buildium_post_charge",
                confirm=dict(CONFIRM),
                execute=_execute,
                window=None,
            )
        )
        approved_id = await _approve_next()
        await asyncio.wait_for(task, timeout=2.0)
    (call,) = seen
    assert call["approval_id"] == approved_id
    assert call["idem_key"] == "k-1"


@pytest.mark.asyncio
async def test_an_ungated_write_never_inherits_an_earlier_approval() -> None:
    """A UI-approved or scheduled write (confirm=None) in the same task as an
    earlier chat approval must not claim that approval."""
    seen: list[dict[str, Any]] = []

    async def _spy(**kw: Any) -> None:
        seen.append(kw)

    async def _execute() -> str:
        return "done"

    token = agent_tools.current_confirm_id.set("stale-approval")
    try:
        with patch("helixis.agents.write_ledger.record_write", new=_spy):
            await idem.run_guarded(
                company_id="co-1",
                idem_key="k-2",
                action_type="buildium_post_charge",
                confirm=None,
                execute=_execute,
                window=None,
            )
    finally:
        agent_tools.current_confirm_id.reset(token)
    (call,) = seen
    assert call["approval_id"] is None


@pytest.mark.asyncio
async def test_a_denied_gate_clears_the_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _deny(*_a: Any, **_k: Any) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(agent_tools, "_wait_for_confirmation", _deny)
    token = agent_tools.current_confirm_id.set("earlier")
    try:
        assert not await agent_tools.confirm_or_abort(action="a", title="t", details="d")
        assert agent_tools.current_confirm_id.get() is None
    finally:
        agent_tools.current_confirm_id.reset(token)
