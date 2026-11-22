"""Release-safety gate 300 — no agent write ships without a confirm gate.

Risk #5: ``confirm_gate`` is the right design, but a refactor can silently
regress it (drop the ``confirm=`` payload from a write's ``run_guarded`` call,
or add a new write that never goes through ``run_guarded`` at all). The live
confirm gate lives in the streaming wrapper, which the eval harness bypasses —
so a model-driven golden case cannot actually test it. This deterministic AST
check is the real, non-flaky gate: every ``run_guarded`` write in the scanned
tool modules must pass a non-None ``confirm``.

Scanned modules: ``buildium_tools.py`` (the Buildium write surface) and, since
SMS Stage 4, ``sms_tools.py`` (``send_text_message`` — the agent-facing text
send is a write and must surface the rendered message for approval like every
other write).

A connector built on ``helixis.pms`` (Rent Manager, Propertyware) keeps its
tools in ``agents/<key>_tools.py`` and writes only through
``pms.gated_write.gated_write``, which shows the card before its one send
(``test_pms_gated_write`` pins that order). Its module joins the scan by
itself, the day it exists (:func:`_connector_tool_files`).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_AGENTS_DIR = Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents"

# (file, minimum expected run_guarded call sites) — the floor guards against a
# false pass if the call-site shape ever changes and the AST walk finds nothing.
_WRITE_TOOL_FILES: tuple[tuple[Path, int], ...] = (
    (_AGENTS_DIR / "buildium_tools.py", 4),
    (_AGENTS_DIR / "sms_tools.py", 1),  # SMS Stage 4: send_text_message
    # Phase 6 — the first write into a SECOND property-management system.
    (_AGENTS_DIR / "rentvine_tools.py", 1),
)


def _connector_tool_files(agents_dir: Path = _AGENTS_DIR) -> tuple[Path, ...]:
    """Each toolkit connector's tools module that exists: the module
    ``tool_registries.tools_module_name`` names, the one the agent loads, so
    the scan and the agent cannot look in two places."""
    from helixis.agents.tool_registries import tools_module_name
    from helixis.pms import sync_cycle

    names = (tools_module_name(key).rsplit(".", 1)[1] for key in sync_cycle.systems())
    return tuple(path for name in names if (path := agents_dir / f"{name}.py").exists())


def _calls_named(tree: ast.AST, wanted: str) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == wanted:
                calls.append(node)
    return calls


def _run_guarded_calls(tree: ast.AST) -> list[ast.Call]:
    return _calls_named(tree, "run_guarded")


def _gated_write_calls(tree: ast.AST) -> list[ast.Call]:
    return _calls_named(tree, "gated_write")


def test_every_write_passes_a_confirm_gate() -> None:
    for path, min_sites in _WRITE_TOOL_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = _run_guarded_calls(tree)

        assert len(calls) >= min_sites, (
            f"{path.name}: expected >={min_sites} run_guarded write site(s), "
            f"found {len(calls)} — a write may have bypassed run_guarded entirely"
        )

        ungated: list[int] = []
        for call in calls:
            confirm_kw = next((k for k in call.keywords if k.arg == "confirm"), None)
            is_none = confirm_kw is None or (
                isinstance(confirm_kw.value, ast.Constant) and confirm_kw.value.value is None
            )
            if is_none:
                ungated.append(getattr(call, "lineno", -1))

        assert ungated == [], (
            f"{path.name}: run_guarded write(s) missing a confirm gate at line(s) {ungated} "
            "— every agent write must surface a confirmation before executing"
        )


# ── the name-based write classifier must not miss a real write ────────


def _impl_of(tool_name: str, tree: ast.AST) -> ast.AsyncFunctionDef | None:
    """The ``async def`` behind a registered tool name.

    Tools are declared as ``@function_tool`` wrappers that delegate to an
    ``_..._impl`` coroutine, so a write's ``run_guarded`` call lives in the
    impl rather than in the function that carries the tool's name. Match
    either spelling.
    """
    from helixis.pms import registry

    # Every system's prefix, so `rentmanager_create_work_order` finds
    # `_create_work_order_impl` the way the Buildium and Rentvine tools do.
    wanted = {
        tool_name,
        f"_{tool_name}_impl",
        *(f"_{tool_name.removeprefix(f'{key}_')}_impl" for key in registry.PMS),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted:
            return node
    return None


def test_no_gated_write_escapes_the_name_based_write_classifier() -> None:
    """``test_negative_should_not_act`` decides what the 32x "should not act"
    cases must FORBID by matching ten substrings against a tool's name —
    create / update / add / upload / share / close / post / record / send /
    text. That is a naming convention doing a safety job.

    ⚠ It is correct today (14 writes, all caught) and it will fail silently
    for the next write whose name is unlucky. Two are already in view:
    ``buildium_renew_lease`` is WRITTEN and dark, waiting to be registered —
    "renew" is not in the list, and a lease renewal is the most irreversible
    write in the product (gotcha 22: no clean undo). ``undo_move_out`` is the
    same shape. Either would join the live surface with no 32x case forbidding
    it, and nothing would say so.

    So the naming convention is cross-checked against a STRUCTURAL signal: a
    tool that goes through ``run_guarded(confirm=...)``, or through the
    toolkit's ``gated_write`` (which shows its card the same way), is a write,
    whatever it is called. The two must agree.
    """
    from helixis.agents.tool_registries import write_registry_tools
    from tests.unit.test_negative_should_not_act import WRITE_TOOLS

    tools = write_registry_tools()
    files = [path for path, _ in _WRITE_TOOL_FILES] + list(_connector_tool_files())
    gated: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for tool in tools:
            impl = _impl_of(tool.name, tree)
            if impl is not None and (_run_guarded_calls(impl) or _gated_write_calls(impl)):
                gated.add(tool.name)

    assert gated, "no confirm-gated tool found — the structural scan is broken, not the code"
    missed = gated - WRITE_TOOLS
    assert not missed, (
        f"these tools take a confirm gate — they WRITE — but their names do not "
        f"match any verb in test_negative_should_not_act._WRITE_VERBS, so no 32x "
        f"should-not-act case forbids them: {sorted(missed)}. Add the verb there."
    )


# ── a connector built on helixis.pms writes only through gated_write ──


def test_every_connector_tools_module_writes_through_gated_write() -> None:
    """A toolkit connector's tools module calls ``gated_write`` and never
    ``run_guarded`` itself. ``gated_write`` runs the whole gate in one order
    (switch, company, role floor, money refusal, card, claim, re-read, one
    send, read-back); a ``run_guarded`` of the connector's own would be a gate
    outside that order, which none of the toolkit's tests hold.

    Nothing to scan until a connector's tools land; the next test proves the
    scan finds a module and holds it to the rule.
    """
    for path in _connector_tool_files():
        _assert_writes_through_gated_write(path)


def _assert_writes_through_gated_write(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _gated_write_calls(tree), (
        f"{path.name}: no pms.gated_write call; a connector's writes go through it"
    )
    assert not _run_guarded_calls(tree), (
        f"{path.name}: calls run_guarded itself; a connector writes only through "
        "pms.gated_write, which claims the write in its own order"
    )


def test_the_connector_scan_finds_a_tools_module_and_holds_it_to_the_rule(
    tmp_path: Path,
) -> None:
    from helixis.pms import sync_cycle

    assert sync_cycle.systems(), "no toolkit system to scan for: check the registry"
    key = sync_cycle.systems()[0]
    assert _connector_tool_files(tmp_path) == ()
    module = tmp_path / f"{key}_tools.py"
    module.write_text(
        "async def _create_work_order_impl():\n    return await gated_write(client=None)\n",
        encoding="utf-8",
    )
    assert _connector_tool_files(tmp_path) == (module,)
    _assert_writes_through_gated_write(module)

    module.write_text(
        "async def _create_work_order_impl():\n    return await run_guarded(confirm=None)\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="no pms.gated_write call"):
        _assert_writes_through_gated_write(module)

    module.write_text(
        "async def _a():\n    await gated_write(client=None)\n"
        "async def _b():\n    await run_guarded(confirm=1)\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="calls run_guarded itself"):
        _assert_writes_through_gated_write(module)


def test_a_connector_tools_impl_is_found_by_its_systems_prefix() -> None:
    tree = ast.parse(
        "async def _create_work_order_impl():\n    return await gated_write(client=None)\n"
    )
    for key in ("rentmanager", "propertyware"):
        impl = _impl_of(f"{key}_create_work_order", tree)
        assert impl is not None and _gated_write_calls(impl), key
