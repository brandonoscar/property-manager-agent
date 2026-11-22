"""Merge-gate test 1 of 3 (``design/rentvine-terms-rules.md``, "Before you
merge anything"): **every write path and every outbound message routes through
the confirm gate, and the gate fails closed.**

Terms rules 13, 14 and 16. The gate must cover every write, update and delete
to Rentvine, and every message to a tenant, owner or applicant, "even when the
message goes out through Gmail, Outlook, or Twilio rather than Rentvine". No
approval, no action; no "low-risk" bypass.

The existing guards check a neighbouring property. ``test_write_confirm_gate``
checks that ``run_guarded`` is passed a ``confirm=``, and
``test_dispatch_paths_guarded`` checks that a send sits in an allowlisted
FUNCTION NAME. Neither proves the send is inside the code the gate runs:
a ``client.post`` one line above a perfectly gated ``run_guarded`` passes both.
This walks the AST from each provider call outward and requires an enclosing
closure that is passed as ``execute=`` to a ``run_guarded`` / ``guarded_action``
call carrying a real ``confirm``. That is the only place the write can run
after, and only after, ``confirm_or_abort`` returned an approval.

Behavioural half: each agent-reachable sender (the Rentvine write, the text
message, a Composio email send) is driven with the user DENYING and with the
user NEVER ANSWERING, and must send nothing either way.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from helixis.agents import idempotency as idem
from helixis.agents import tools as agent_tools
from helixis.connectors import tool_gate
from helixis.rentvine.client import RentvineClient

from . import test_composio_tool_gate as composio_h
from . import test_rentvine_write_tool as rentvine_h
from . import test_sms_tool as sms_h

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "helixis"

_GUARDS = {"run_guarded", "guarded_action"}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _has_real_confirm(call: ast.Call) -> bool:
    kw = next((k for k in call.keywords if k.arg == "confirm"), None)
    if kw is None:
        return False
    return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)


def _gated_closures(tree: ast.AST) -> set[ast.AST]:
    """Every nested function passed as ``execute=`` to a guard with a confirm,
    in the scope that defines it."""
    gated: set[ast.AST] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        executes = {
            kw.value.id
            for call in ast.walk(scope)
            if isinstance(call, ast.Call)
            and _called_name(call) in _GUARDS
            and _has_real_confirm(call)
            for kw in call.keywords
            if kw.arg == "execute" and isinstance(kw.value, ast.Name)
        }
        for child in ast.walk(scope):
            if (
                child is not scope
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in executes
            ):
                gated.add(child)
    return gated


def _sites(path: Path, is_target, within: str | None = None) -> list[tuple[int, bool]]:
    """``(lineno, gated)`` for every call ``is_target`` picks out, optionally
    only inside the function named ``within``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    gated = _gated_closures(tree)
    out: list[tuple[int, bool]] = []

    def walk(node: ast.AST, inside: bool, scoped: bool) -> None:
        for child in ast.iter_child_nodes(node):
            now = inside or child in gated
            here = scoped or (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == within
            )
            if isinstance(child, ast.Call) and is_target(child) and here:
                out.append((child.lineno, now))
            walk(child, now, here)

    walk(tree, False, within is None)
    return out


def _is_rentvine_write(call: ast.Call) -> bool:
    # RentvineClient.post takes its body keyword-only, `json_body` or (for the
    # one multipart upload) `files` (pinned below), so this cannot confuse it
    # with `@router.post(...)` or an httpx `post(json=...)`. ⚠ `files` joined
    # 2026-10-03 with the upload: a predicate that knew only `json_body` would
    # not have SEEN that write, and the floor of one was met by the old one.
    return _called_name(call) == "post" and any(
        k.arg in ("json_body", "files") for k in call.keywords
    )


def _is_named(*names: str):
    return lambda call: _called_name(call) in names


#: (label, files, call predicate, only inside this function, minimum sites).
#: The floor turns "the walk found nothing" (a renamed argument, a moved file)
#: into a failure instead of a pass.
_TARGETS = [
    (
        "Rentvine write (RentvineClient.post)",
        # Not buildium/: its S3 upload is an httpx `post(files=...)`, and
        # Buildium's writes are held by test_write_confirm_gate.
        [
            p
            for p in SRC.rglob("*.py")
            if p != SRC / "rentvine" / "client.py" and (SRC / "buildium") not in p.parents
        ],
        _is_rentvine_write,
        None,
        # One per Rentvine write tool (rentvine_tools()): the status change, the
        # work-order create, the upload, and the charge and payment's shared send.
        4,
    ),
    (
        "SMS send (send_sms)",
        sorted((SRC / "agents").glob("*.py")),
        _is_named("send_sms"),
        None,
        1,
    ),
    # The connectors built on helixis.pms (Rent Manager, Propertyware) write
    # only through `PmsHttpClient.send_once`, and only `pms/gated_write.py`
    # calls it (pinned in test_pms_gated_write). One call site, in a bounded
    # loop: the second send exists only after a 409, under the same approval.
    (
        "PMS toolkit write (PmsHttpClient.send_once)",
        [p for p in SRC.rglob("*.py") if p != SRC / "pms" / "http.py"],
        _is_named("send_once"),
        None,
        1,
    ),
    # Scoped to `gate_tool`: a read slug is returned unwrapped on purpose, and
    # `fence_tool_error` only calls whatever invoke it is handed. Every tool the
    # agent gets passes through `gate_tools` in `get_tools` (pinned below), so
    # a send reaches the provider through the gate in either wrapping order.
    (
        "Composio write invoke (Gmail / Outlook / Calendar / Drive)",
        [SRC / "connectors" / "tool_gate.py"],
        _is_named("original_invoke"),
        "gate_tool",
        1,
    ),
    (
        "any other agent-side provider send",
        sorted((SRC / "agents").glob("*.py")),
        _is_named("create_call", "send_gmail_email", "execute_tool"),
        None,
        0,
    ),
]


@pytest.mark.parametrize(
    "label,files,is_target,within,floor", _TARGETS, ids=[t[0] for t in _TARGETS]
)
def test_every_write_and_send_runs_only_inside_the_confirm_gate(
    label: str, files: list[Path], is_target, within: str | None, floor: int
) -> None:
    found, ungated = 0, []
    for path in files:
        for lineno, gated in _sites(path, is_target, within):
            found += 1
            if not gated:
                ungated.append(f"{path.relative_to(REPO)}:{lineno}")
    assert found >= floor, f"{label}: expected at least {floor} call site(s), found {found}"
    assert ungated == [], (
        f"{label} outside the confirm gate: {ungated}. Move the call into a "
        "closure passed as execute= to run_guarded(confirm={...})."
    )


def test_the_walk_catches_a_write_one_line_outside_the_gate(tmp_path: Path) -> None:
    """The guard must be able to fail, including on the shape the older
    guards pass: a gated run_guarded sitting right next to an ungated write."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "async def tool(client, body):\n"
        "    await client.post('/x', json_body=body)\n"  # line 2: ungated
        "    async def _do():\n"
        "        await client.post('/x', json_body=body)\n"  # line 4: gated
        "    await run_guarded(confirm={'action': 'x'}, execute=_do)\n"
        "    async def _sneaky():\n"
        "        await client.post('/x', json_body=body)\n"  # line 7: confirm=None
        "    await run_guarded(confirm=None, execute=_sneaky)\n",
        encoding="utf-8",
    )
    assert _sites(planted, _is_rentvine_write) == [(2, False), (4, True), (7, False)]


def test_the_rentvine_write_signature_is_what_the_walk_keys_on() -> None:
    """If `json_body` stopped being keyword-only, a positional call would slip
    past `_is_rentvine_write` unseen."""
    param = inspect.signature(RentvineClient.post).parameters["json_body"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_every_composio_tool_the_agent_gets_is_wrapped_by_the_gate() -> None:
    path = SRC / "connectors" / "composio_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (get_tools,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "get_tools"
    ]
    gates = [
        c
        for c in ast.walk(get_tools)
        if isinstance(c, ast.Call) and _called_name(c) == "gate_tools"
    ]
    assert gates, "get_tools no longer wraps its tools with gate_tools"


def _messaging_slugs() -> list[str]:
    """Every Gmail / Outlook / Drive slug in the captured catalogue that sends,
    replies or forwards (reads like GET_AUTO_FORWARDING excluded)."""
    data = json.loads((REPO / "tests/fixtures/composio_tool_slugs.json").read_text())
    slugs = []
    for tools in data["toolkits"].values():
        for tool in tools:
            words = tool["slug"].split("_")[1:]
            if {"SEND", "REPLY", "FORWARD"} & set(words) and words[0] not in {
                "GET",
                "LIST",
                "SETTINGS",
            }:
                slugs.append(tool["slug"])
    return slugs


def test_every_email_send_reply_or_forward_is_classified_a_write() -> None:
    slugs = _messaging_slugs()
    assert {"GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD", "OUTLOOK_SEND_EMAIL"} <= set(slugs)
    assert [s for s in slugs if not tool_gate.is_write_tool(s)] == []


# ── behavioural: denied or unanswered sends nothing (rule 16) ───────────────


async def _never_answered(*_a, **_k):
    """What ``_wait_for_confirmation`` returns when the 300s window runs out."""
    return False, None


@pytest.fixture(params=["no store", "durable store"])
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both of ``run_guarded``'s paths: the durable one production takes, and
    the no-store passthrough. Each makes its own decision about a denial."""
    if request.param == "durable store":
        from tests.conftest import FakeIdempotencyStore

        fake = FakeIdempotencyStore()
        monkeypatch.setattr(idem, "store_from_pool", lambda: fake)
    return request.param


@pytest.fixture(params=["denied", "unanswered"])
def answer(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, store: str) -> str:
    if request.param == "unanswered":
        monkeypatch.setattr(agent_tools, "_wait_for_confirmation", _never_answered)
    return request.param


async def _deny_if_asked(answer: str) -> None:
    if answer != "denied":
        return
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            await agent_tools.resolve_confirmation(drained[0]["confirm_id"], False)
            return
    raise AssertionError("no confirm card was raised")


@pytest.mark.asyncio
async def test_the_rentvine_write_sends_nothing_without_approval(answer: str) -> None:
    await agent_tools.drain_pending_confirmations()
    client = rentvine_h._FakeClient()
    with ExitStack() as stack:
        rentvine_h._patches(stack, client)
        task = asyncio.create_task(
            rentvine_h.rt._update_work_order_status_impl(
                rentvine_h._WO_ID, "Completed", work_order_label=rentvine_h._LABEL
            )
        )
        await _deny_if_asked(answer)
        await asyncio.wait_for(task, timeout=2.0)
    assert client.posts == []


@pytest.mark.asyncio
async def test_a_text_message_sends_nothing_without_approval(answer: str) -> None:
    await agent_tools.drain_pending_confirmations()
    with ExitStack() as stack:
        send = sms_h._wired(stack, pool=sms_h.FakePool([sms_h._row()]))
        task = asyncio.create_task(
            sms_h.st._send_text_message_impl("Sarah Chen", "account_notice", sms_h.NOTICE_PARAMS)
        )
        await _deny_if_asked(answer)
        await asyncio.wait_for(task, timeout=2.0)
    send.assert_not_called()


@pytest.mark.parametrize(
    "slug", ["GMAIL_SEND_EMAIL", "OUTLOOK_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD"]
)
@pytest.mark.asyncio
async def test_an_email_sends_nothing_without_approval(answer: str, slug: str) -> None:
    """The route a message built on Rentvine data would take to a tenant."""
    await agent_tools.drain_pending_confirmations()
    tool, calls = composio_h._stub_tool(slug)
    gated = tool_gate.gate_tool(tool)
    task = asyncio.create_task(gated.on_invoke_tool(None, json.dumps({"to": "t@example.com"})))
    await _deny_if_asked(answer)
    await asyncio.wait_for(task, timeout=2.0)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_the_behavioural_harness_can_see_a_send(
    monkeypatch: pytest.MonkeyPatch, store: str
) -> None:
    """The control: approve, and the same Composio harness DOES record the send,
    so a zero above means the gate held rather than that nothing was wired."""
    await agent_tools.drain_pending_confirmations()
    monkeypatch.setattr(agent_tools, "_wait_for_confirmation", AsyncMock(return_value=(True, None)))
    tool, calls = composio_h._stub_tool("GMAIL_SEND_EMAIL")
    await tool_gate.gate_tool(tool).on_invoke_tool(None, json.dumps({"to": "t@example.com"}))
    assert calls["n"] == 1
