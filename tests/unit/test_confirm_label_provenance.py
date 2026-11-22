"""A confirm card may not render a model-supplied label. Structural.

Every confirm-gated Buildium write takes BOTH an id (what executes) and,
historically, an optional model-supplied ``*_label`` (what the human reads) —
and nothing checked they agreed::

    tenant_disp = tenant_label or f"tenant {tenant_id}"

So a model that believed "Marcus Whitfield, Phoenix" could pass that string
beside Elena Vasquez's ``tenant_id``: the card said Marcus, the write hit
Elena. The gate is the human's last line of defence and it rendered the
model's BELIEF.

**Why a test and not a prompt or a golden case.** The Whitfield investigation
ruled out the data (75 leases, 0 disagreements) and the tool (``_resolve_unit``
returned all 50 candidates); the model held both and collapsed them anyway.
Gotchas 25, 32 and 34 each end with *only a structural stop works* after prose
failed four times over. A golden case is model-driven — it can show the model
behaving once, it cannot make the card incapable of lying. This scan can.

**What it pins.** No write ``*_impl`` may READ a ``*_label`` parameter. Not
"should prefer the record" — may not read it at all. That is deliberately
stricter than "the rendered text must match the record", because matching
needs a heuristic and a heuristic is one more thing that can be wrong.

The tool signatures keep their ``*_label`` parameters for backward
compatibility (the model may still pass one; the orchestrator prompt and the
golden fixtures still do). They are simply never read on the render path.

Precedent for the shape: ``test_write_confirm_gate.py`` (AST-scans for the
gate itself) and ``test_card_event_types.py`` (AST-scans queued card strings
against the enum). Both exist because a new tool added with the wrong shape
would otherwise ship unnoticed — which is exactly the risk here, since a new
write tool copying an existing one inherits the vulnerable pattern for free.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC_PATH = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/agents/buildium_tools.py"
_TREE = ast.parse(_SRC_PATH.read_text(encoding="utf-8"))

#: Functions that BUILD a confirm payload. ``*_impl`` is the live render path;
#: ``_confirm_details_*`` are the helpers they delegate card text to.
_RENDER_FN = lambda n: n.name.endswith("_impl") or n.name.startswith("_confirm_details")  # noqa: E731


def _label_params(fn: ast.AST) -> set[str]:
    """Parameter names that carry model-supplied display text."""
    args = fn.args.args + fn.args.kwonlyargs  # type: ignore[attr-defined]
    return {a.arg for a in args if a.arg.endswith("_label") or a.arg == "label"}


def _render_functions() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    return [
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and _RENDER_FN(n)
    ]


def _label_reads() -> list[tuple[str, int, str]]:
    """(function, line, param) for every READ of a label param on a render path."""
    out: list[tuple[str, int, str]] = []
    for fn in _render_functions():
        params = _label_params(fn)
        if not params:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in params and isinstance(node.ctx, ast.Load):
                out.append((fn.name, node.lineno, node.id))
    return out


# ── the structural pin ──────────────────────────────────────────────────────


def test_no_render_path_reads_a_model_supplied_label() -> None:
    """THE guard. A new write tool copying the old shape fails HERE."""
    leaks = _label_reads()
    assert not leaks, (
        "a confirm-render path reads a model-supplied label — the card would show "
        "the model's belief rather than the record it is about to write:\n"
        + "\n".join(f"  {fn}() line {line}: reads {param!r}" for fn, line, param in leaks)
        + "\n\nDerive the display text from the id via _gate_label instead. If no "
        "getter exists for that entity kind, render the ID form: an unfamiliar "
        "'tenant 4410' is checkable against Buildium, a wrong name is deceptive."
    )


def test_the_deriving_helper_exists_and_is_used() -> None:
    """The positive half. An empty `_label_reads()` would also be satisfied by
    a file that renders nothing at all, so pin that the replacement is wired."""
    names = {
        n.name for n in ast.walk(_TREE) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    assert "_gate_label" in names, "the record-deriving helper is gone"
    calls = sum(
        1
        for n in ast.walk(_TREE)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_gate_label"
    )
    assert calls >= 8, f"only {calls} _gate_label call sites — expected the write set to use it"


def test_gate_label_never_falls_back_to_a_supplied_string() -> None:
    """The fallback is where this bug would quietly come back.

    A future 'be friendlier when the fetch fails' edit that reaches for the
    model's label on the error path restores the whole vulnerability while
    every other test here still passes — the read would live in _gate_label,
    which takes no label parameter, so `_label_reads()` would not see it.
    """
    fn = next(
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_gate_label"
    )
    assert not _label_params(fn), "_gate_label must not accept a model-supplied label at all"

    # ⚠ Substring-matching "_label" here is WRONG and the first cut did it:
    # it flags `_lease_unit_label`, an internal helper that DERIVES from a
    # record — a guard that manufactures findings gets ignored as fast as one
    # that misses them. Only module-level *_label helpers are legitimate; any
    # OTHER name ending in _label inside this function would be a value from
    # somewhere else, which is the thing that must not exist.
    derive_helpers = {
        n.name
        for n in _TREE.body
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name.endswith("_label")
    }
    foreign = sorted(
        {
            n.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Name)
            and isinstance(n.ctx, ast.Load)
            and n.id.endswith("_label")
            and n.id not in derive_helpers
        }
    )
    assert not foreign, (
        f"_gate_label reads {foreign} — its whole contract is that the model's "
        "string never reaches the card, including on the failure path"
    )


# ── guard self-test ─────────────────────────────────────────────────────────


def test_the_scan_detects_the_original_vulnerable_shape() -> None:
    """Feeds the checker the PRE-FIX code and requires a positive.

    Three guards shipped this month passed against the very defect they
    targeted (a dead E2B sandbox, an AST scan run on unfixed source, a
    fixed-width window reading 0 as absent). A guard nobody has watched fail
    is not evidence — so this one is watched failing, here, every run.
    """
    vulnerable = ast.parse(
        "async def _record_move_out_impl(lease_id, tenant_id, *, tenant_label=None):\n"
        '    tenant_disp = tenant_label or f"tenant {tenant_id}"\n'
        "    return tenant_disp\n"
    )
    fn = vulnerable.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    params = _label_params(fn)
    reads = [
        n.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and n.id in params and isinstance(n.ctx, ast.Load)
    ]
    assert reads == ["tenant_label"], (
        "the scan cannot see the exact line that caused the Whitfield "
        "misattribution — it is not measuring what it claims to measure"
    )


@pytest.mark.parametrize(
    "kind", ["lease", "tenant", "task", "work_order", "vendor", "gl_account", "applicant"]
)
def test_every_kind_has_a_defined_outcome(kind: str) -> None:
    """_gate_label must handle each kind explicitly OR fall through to the ID
    form — never raise, and never return an empty string, on a write path."""
    src = _SRC_PATH.read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_gate_label"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert 'fallback = f"{kind.replace' in body, (
        "the ID-form fallback is what makes an unhandled kind safe rather than "
        "an exception on a live write path"
    )
