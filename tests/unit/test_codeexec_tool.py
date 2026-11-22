"""The run_computation tool: dark-gating, plain-English approval, execution.

These pin the Increment-2 contract: the tool is inert until enabled; it never
runs without an explicit approval; and the thing the user approves is the
plain-English plan, not the Python.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from helixis.agents import codeexec_tools
from helixis.agents.codeexec_tools import _apply_actions_impl, _run_computation_impl
from helixis.settings import Settings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox — prod is Linux")


def _settings(*, enabled: bool) -> Settings:
    """Settings for the TOOL tests.

    ``substrate="e2b"`` is required whenever ``enabled`` is true — the boot
    guard refuses a live capability on the local subprocess, because that path
    can read ``HELIXIS_DATA_KEY`` out of /proc (gotcha 33). These tests are
    about the tool's gating and approval flow, not the substrate, so they
    declare the production-shaped config here and opt into the local executor
    explicitly in ``_patch_settings`` rather than smuggling it through a config
    the guard exists to forbid.
    """
    return Settings(
        env="test",
        memory={"enabled": False},
        codeexec={
            "enabled": enabled,
            "wall_clock_s": 5.0,
            "substrate": "e2b" if enabled else "subprocess",
            "e2b_api_key": "e2b_test_key" if enabled else "",
        },
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, role: str | None = "manager"
) -> None:
    """Enable the capability and give the caller a role.

    Code execution carries a Manager+ fail-closed floor (see
    ``test_codeexec_phase0``). Unit tests have no DB pool, so ``_caller_role``
    returns ``None`` and the floor correctly refuses — which would make every
    test below assert against a permission error instead of the behaviour it
    cares about. Stubbing the role here keeps each test focused on its own
    contract; the floor itself is pinned in its own file.
    """
    import helixis.agents.buildium_tools as bt
    import helixis.codeexec.executor as ex

    monkeypatch.setattr(codeexec_tools, "get_settings", lambda: _settings(enabled=enabled))

    async def _role() -> str | None:
        return role

    monkeypatch.setattr(bt, "_caller_role", _role)

    # Run on the LOCAL executor, explicitly. These tests assert on the tool's
    # behaviour (gating, approval, artifact queuing) and need real execution
    # without an E2B account; the substrate itself is covered by
    # ``test_codeexec_e2b_substrate`` (fake SDK) and ``test_codeexec_containment``.
    monkeypatch.setattr(ex, "_e2b_selected", lambda: False)


@pytest.mark.asyncio
async def test_dark_by_default_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=False)
    called = False

    async def _confirm(**_k: Any) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _confirm)
    out = await _run_computation_impl("Add two numbers.", "result = 1 + 1")
    assert "isn't enabled" in out
    assert called is False  # never even reached the gate


@pytest.mark.asyncio
async def test_requires_plain_english_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _confirm(**_k: Any) -> bool:
        raise AssertionError("must not gate without a plan")

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _confirm)
    out = await _run_computation_impl("   ", "result = 1")
    assert "plain-English" in out


@pytest.mark.asyncio
async def test_approval_gate_gets_english_not_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)
    seen: dict[str, Any] = {}

    async def _confirm(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _confirm)
    plan = "Sum the three invoice amounts and report the total."
    code = "result = 100 + 200 + 300"
    await _run_computation_impl(plan, code)

    # The user approves the English plan; the code is secondary (request_body).
    assert seen["details"] == plan
    assert code not in seen["details"]
    assert seen["request_body"]["code"] == code
    assert seen["action"] == "run_computation"


@pytest.mark.asyncio
async def test_denied_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _deny(**_k: Any) -> bool:
        return False

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _deny)
    # If this ran it would raise; a denial must short-circuit before execution.
    out = await _run_computation_impl("Delete everything.", "raise RuntimeError('should not run')")
    assert "Cancelled" in out


@pytest.mark.asyncio
async def test_approved_runs_in_sandbox_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _approve(**_k: Any) -> bool:
        return True

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _approve)
    out = await _run_computation_impl("Compute 6 times 7.", "result = 6 * 7")
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["result"] == 42


@pytest.mark.asyncio
async def test_sandbox_error_comes_back_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _approve(**_k: Any) -> bool:
        return True

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _approve)
    out = await _run_computation_impl("Divide by zero on purpose.", "result = 1 / 0")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "ZeroDivisionError" in payload["stderr"]


# ── apply_actions (the on-switch, Inc.5a) ────────────────────────────────


@pytest.mark.asyncio
async def test_apply_actions_dark_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=False)
    out = await _apply_actions_impl(
        "Note both tasks.", [{"action": "add_internal_note", "args": {}}]
    )
    assert "isn't enabled" in out


@pytest.mark.asyncio
async def test_apply_actions_requires_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)
    out = await _apply_actions_impl("  ", [{"action": "buildium_add_task_note", "args": {}}])
    assert "plain-English" in out


@pytest.mark.asyncio
async def test_apply_actions_refuses_forbidden_and_applies_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, enabled=True)
    dispatched: list[str] = []

    async def _fake_dispatch(action: str, args: dict) -> Any:
        dispatched.append(action)
        return {"ok": action}

    # Patch the dispatch the impl uses so no real Buildium call happens.
    monkeypatch.setattr(codeexec_tools, "default_dispatch", _fake_dispatch)
    out = await _apply_actions_impl(
        "Note the task; (attempt a payment).",
        [
            {"action": "buildium_add_task_note", "args": {"task_id": 1, "note": "hi"}},
            {"action": "buildium_record_payment", "args": {"amount": 500}},  # forbidden
        ],
    )
    payload = json.loads(out)
    # The money-write never dispatched; the note did.
    assert dispatched == ["buildium_add_task_note"]
    assert any(r["action"] == "buildium_record_payment" for r in payload["rejected"])


# ── document-in (Inc.5b last-mile — feed an uploaded file into the sandbox) ──


@pytest.mark.asyncio
async def test_document_in_feeds_bytes_into_input_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _approve(**_k: Any) -> bool:
        return True

    async def _resolve(_name: str) -> tuple[dict[str, bytes] | None, str | None]:
        return {"invoice.txt": b"AMOUNT=42"}, None

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _approve)
    monkeypatch.setattr(codeexec_tools, "_resolve_input_document", _resolve)

    # The code reads the fed-in file straight from INPUT_DIR.
    code = (
        "import os\nwith open(os.path.join(INPUT_DIR, 'invoice.txt')) as f:\n    result = f.read()"
    )
    out = await _run_computation_impl("Read the invoice amount.", code, "invoice.txt")
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["result"] == "AMOUNT=42"


@pytest.mark.asyncio
async def test_document_in_unresolved_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, enabled=True)

    async def _confirm(**_k: Any) -> bool:
        raise AssertionError("must not gate when the requested document can't be resolved")

    async def _resolve(_name: str) -> tuple[dict[str, bytes] | None, str | None]:
        return None, 'No document matches "ghost.pdf". Your library has: lease.pdf.'

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _confirm)
    monkeypatch.setattr(codeexec_tools, "_resolve_input_document", _resolve)

    out = await _run_computation_impl(
        "Summarise the doc.", "result = open(INPUT_DIR + '/x').read()", "ghost.pdf"
    )
    # The resolver's message is returned verbatim; nothing was gated or run.
    assert out == 'No document matches "ghost.pdf". Your library has: lease.pdf.'


@pytest.mark.asyncio
async def test_produced_artifacts_are_queued_for_download(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    _patch_settings(monkeypatch, enabled=True)

    async def _approve(**_k: Any) -> bool:
        return True

    async def _persist(contents: Any, *, company_id: str, settings: Any) -> list[dict[str, Any]]:
        return [
            {"name": n, "size": len(b), "url": f"https://signed/{n}"} for n, b in contents.items()
        ]

    queued: list[Any] = []

    monkeypatch.setattr(codeexec_tools, "confirm_or_abort", _approve)
    monkeypatch.setattr(codeexec_tools, "persist_artifacts", _persist)
    monkeypatch.setattr(
        codeexec_tools, "current_memory_context", lambda: SimpleNamespace(company_id="co-1")
    )
    monkeypatch.setattr(codeexec_tools, "queue_artifacts", lambda links: queued.append(links))

    # The code writes a file into OUTPUT_DIR; it should come back as a link AND
    # be queued so the frontend renders a download button.
    code = (
        "import os\n"
        "with open(os.path.join(OUTPUT_DIR, 'report.csv'), 'w') as f:\n"
        "    f.write('a,b\\n1,2\\n')\n"
        "result = 'wrote report'"
    )
    out = await _run_computation_impl("Generate a CSV report.", code)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert any(a["name"] == "report.csv" for a in payload["artifact_links"])
    # The download card was queued with the signed link.
    assert queued and any(a["url"].startswith("https://signed/") for a in queued[0])


@pytest.mark.asyncio
async def test_resolve_input_document_is_company_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    seen: dict[str, Any] = {}

    async def _list_documents(_pool: Any, *, company_id: str) -> list[Any]:
        seen["company_id"] = company_id
        return [SimpleNamespace(name="Lease 2026.pdf", storage_path="co-1/doc-9.pdf")]

    async def _load(*, storage_path: str, filename: str, settings: Any) -> tuple[str, bytes]:
        seen["storage_path"] = storage_path
        return filename, b"%PDF-1.7 lease bytes"

    monkeypatch.setattr(
        codeexec_tools, "current_memory_context", lambda: SimpleNamespace(company_id="co-1")
    )
    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: object())
    monkeypatch.setattr("helixis.db.repositories.docs_links.list_documents", _list_documents)
    monkeypatch.setattr(codeexec_tools, "load_input_document", _load)

    # A partial (case-insensitive) name resolves to the one matching library doc.
    files, err = await codeexec_tools._resolve_input_document("lease 2026")
    assert err is None
    assert files is not None
    assert files == {"Lease 2026.pdf": b"%PDF-1.7 lease bytes"}
    # It only ever looked inside the caller's own company.
    assert seen["company_id"] == "co-1"
    assert seen["storage_path"] == "co-1/doc-9.pdf"
