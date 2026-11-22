"""A guarded write reports "done" only when it ran (or a prior run of it did).

Andrew's audit (2026-09-29), confirmed in code: ``run_guarded`` returns
``already_pending`` when THIS call ran nothing. Another request holds the
claim, an earlier attempt failed before sending, or the claim was released
while the approval card waited. Every write tail checked the outcomes it knew
and fell through to its success line for the rest, so the user heard "Task
created" or "Posted a $X charge" for a write that never happened (gotcha 177).

The rule is now an allowlist (``SUCCESS_OUTCOMES``). These tests hold it from
both sides: every caller of ``run_guarded`` checks it (an AST walk, so a new
write tool cannot skip it), and real tails driven through the real guard
answer honestly in both ``already_pending`` shapes.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import FunctionTool

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.agents import buildium_tools as bdt
from helixis.agents import idempotency as idem
from helixis.connectors import tool_gate
from helixis.db.repositories.action_idempotency import (
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    ClaimResult,
    ClaimRow,
)
from helixis.memory.context import memory_context

_SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"


# ── the rule, read from every caller ──────────────────────────────────


def _guarded_callers() -> dict[str, ast.AST]:
    """Every function in src/helixis that calls ``run_guarded``."""
    found: dict[str, ast.AST] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "run_guarded":
                        found[f"{path.relative_to(_SRC)}::{fn.name}"] = fn
                        break
    return found


def _checks_the_allowlist(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "outcome"
            and any(isinstance(op, ast.NotIn) for op in node.ops)
            and any(
                (getattr(c, "id", None) or getattr(c, "attr", None)) == "SUCCESS_OUTCOMES"
                for c in node.comparators
            )
        ):
            return True
    return False


def test_every_guarded_write_checks_the_success_allowlist() -> None:
    callers = _guarded_callers()
    # The scan must actually find the writes (a scan that finds nothing passes
    # everything — CLAUDE.md rule 10).
    for known in (
        "agents/buildium_tools.py::_create_to_do_task_impl",
        "agents/buildium_tools.py::_post_charge_impl",
        "agents/buildium_tools.py::_record_payment_impl",
        "agents/rentvine_tools.py::_update_with_client",
        "agents/browser_tools.py::_submit_form_impl",
        "agents/sms_tools.py::_send_text_message_impl",
        "connectors/tool_gate.py::gated_invoke",
    ):
        assert known in callers, f"scan missed {known}"
    missing = sorted(name for name, fn in callers.items() if not _checks_the_allowlist(fn))
    assert not missing, (
        "these call run_guarded but never check `guard.outcome not in SUCCESS_OUTCOMES`, "
        f"so an outcome they don't name falls through to their success reply: {missing}"
    )


def test_the_allowlist_is_exactly_the_two_outcomes_where_the_action_happened() -> None:
    assert idem.SUCCESS_OUTCOMES == {"executed", "replayed"}


def test_every_already_pending_carries_a_named_detail() -> None:
    """``pending_reply`` words its answer by ``detail``; a producer that wrote a
    new free-text detail would be misread as "still in progress" forever."""
    tree = ast.parse(Path(idem.__file__).read_text())
    named = {n for n in dir(idem) if n.startswith("DETAIL_")}
    seen = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "GuardResult"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        outcome = kw.get("outcome")
        if isinstance(outcome, ast.Constant) and outcome.value == "already_pending":
            seen += 1
            detail = kw.get("detail")
            assert isinstance(detail, ast.Name) and detail.id in named, ast.unparse(node)
    assert seen == len(named)
    assert idem._NOTHING_RUNNING <= {getattr(idem, n) for n in named}


@pytest.mark.parametrize("detail", [idem.DETAIL_WAIT_EXHAUSTED, idem.DETAIL_RECLAIM_LOST])
def test_a_request_still_held_elsewhere_says_check_before_asking_again(detail: str) -> None:
    guard = idem.GuardResult(outcome="already_pending", detail=detail)
    reply = idem.pending_reply(guard, action="post the charge", system="Buildium")
    assert reply.startswith("I didn't post the charge")
    assert "already in progress" in reply
    assert "Check Buildium before asking again" in reply


@pytest.mark.parametrize("detail", [idem.DETAIL_PEER_FAILED, idem.DETAIL_RELEASED_AT_GATE])
def test_nothing_running_says_nothing_changed_and_to_retry(detail: str) -> None:
    guard = idem.GuardResult(outcome="already_pending", detail=detail)
    reply = idem.pending_reply(guard, action="post the charge", system="Buildium")
    assert reply.startswith("I didn't post the charge, and nothing was changed in Buildium")
    assert "ask me again to retry" in reply
    assert "in progress" not in reply


# ── real tails, through the real guard ────────────────────────────────


def _row(status: str) -> ClaimRow:
    return ClaimRow(
        company_id="co-1",
        idem_key="k",
        action_type="t",
        status=status,
        result=None,
        error=None,
        attempts=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class _HeldByAnotherRequest:
    """An earlier attempt failed retryably and another request won the reclaim
    first: ``already_pending`` (reclaim lost), with no 30 s wait."""

    async def claim(self, **_: Any) -> ClaimResult:
        return ClaimResult(won=False, row=_row(STATUS_FAILED_RETRYABLE))

    async def reclaim(self, **_: Any) -> bool:
        return False


class _ReleasedAtTheGate:
    """This request won the claim and parked it at the approval card; the
    claim was released (a deploy, the reaper) before the approval came back:
    ``already_pending`` with NOTHING running."""

    def __init__(self) -> None:
        self.finalized: list[str] = []

    async def claim(self, **_: Any) -> ClaimResult:
        return ClaimResult(won=True, row=_row(STATUS_IN_PROGRESS))

    async def mark_awaiting_approval(self, **_: Any) -> bool:
        return True

    async def resume_from_awaiting(self, **_: Any) -> bool:
        return False


async def _approve(**_: Any) -> bool:
    return True


@pytest.fixture
def approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _approve)


def _buildium(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    create = AsyncMock(return_value={"Id": 1, "Title": "Fix sink"})
    api = MagicMock()
    api.tasks.create = create
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(bdt, "load_buildium_settings_for_company", AsyncMock(return_value=object()))
    monkeypatch.setattr(bdt, "BuildiumClient", lambda *a, **k: client)
    monkeypatch.setattr(bdt, "BuildiumApi", lambda *a, **k: api)
    return create


async def test_buildium_task_held_elsewhere_is_not_reported_created(
    monkeypatch: pytest.MonkeyPatch, approve: None
) -> None:
    create = _buildium(monkeypatch)
    monkeypatch.setattr(idem, "store_from_pool", lambda: _HeldByAnotherRequest())
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        reply = await bdt._create_to_do_task_impl("Fix sink", unit_id=4, assigned_to_user_id=1)
    create.assert_not_awaited()
    assert "Task created" not in reply
    assert reply.startswith("I didn't create the task")
    assert "Check Buildium before asking again" in reply


async def test_buildium_task_released_at_the_gate_says_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, approve: None
) -> None:
    create = _buildium(monkeypatch)
    monkeypatch.setattr(idem, "store_from_pool", lambda: _ReleasedAtTheGate())
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        reply = await bdt._create_to_do_task_impl("Fix sink", unit_id=4, assigned_to_user_id=1)
    create.assert_not_awaited()
    assert "Task created" not in reply
    assert "nothing was changed in Buildium" in reply
    assert "ask me again to retry" in reply


class _Locator:
    def __init__(self) -> None:
        self.clicks = 0

    @property
    def first(self) -> _Locator:
        return self

    async def click(self, timeout: int | None = None) -> None:
        self.clicks += 1


class _Page:
    def __init__(self) -> None:
        self.loc = _Locator()
        self.url = "https://court.example.gov/evict"

    def locator(self, _selector: str) -> _Locator:
        return self.loc


async def test_browser_submit_held_elsewhere_is_not_submitted(
    monkeypatch: pytest.MonkeyPatch, approve: None
) -> None:
    """It used to return ``status="submitted"`` and log a "submitted form" row."""
    from helixis.settings import Settings

    page = _Page()
    session = bs.BrowserSession(
        company_id="co-1", session_id="s-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr("helixis.agents.browser_tools.current_session", lambda: session)
    monkeypatch.setattr(idem, "store_from_pool", lambda: _HeldByAnotherRequest())
    logged = AsyncMock()
    monkeypatch.setattr(bt, "_log_activity", logged)
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        result = await bt._submit_form_impl(
            "button#file", "", settings=Settings(env="test", memory={"enabled": False})
        )
    assert result.status == "not_submitted"
    assert page.loc.clicks == 0
    assert any("didn't submit the form" in w for w in result.warnings)
    assert all(c.kwargs.get("action_type") != "browser_submit_form" for c in logged.await_args_list)


async def test_a_gmail_send_held_elsewhere_returns_an_honest_failure(
    monkeypatch: pytest.MonkeyPatch, approve: None
) -> None:
    """It used to hand the model ``None`` to interpret."""
    sent = {"n": 0}

    async def inner(_ctx: Any, _payload: str) -> str:
        sent["n"] += 1
        return json.dumps({"successful": True, "data": "sent"})

    tool = FunctionTool(
        name="GMAIL_SEND_EMAIL",
        description="stub gmail send",
        params_json_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "additionalProperties": False,
        },
        on_invoke_tool=inner,
        strict_json_schema=False,
    )
    monkeypatch.setattr(idem, "store_from_pool", lambda: _HeldByAnotherRequest())
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        out = await tool_gate.gate_tool(tool).on_invoke_tool(None, json.dumps({"to": "a@b.co"}))
    assert sent["n"] == 0
    body = json.loads(out)
    assert body["successful"] is False
    assert "didn't make this change" in body["data"]
    assert "Check Gmail before asking again" in body["data"]
