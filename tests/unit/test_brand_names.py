"""No user- or model-visible string may say "Helixis".

The product is **Occupella**. "Helixis" is the old internal name, and it is
still the PACKAGE name (``src/helixis/``), the env prefix (``HELIXIS_*``), the
table prefix (``helixis_*``) and the event prefix (``Helixis.TaskRequest``) —
none of which a user ever sees, and all of which are load-bearing.

⚠ A live chat answer said **"Helixis's synced Buildium data"** to a property
manager. The rename pass had renamed runtime strings across the codebase and
missed two things:

* ``agents/mirror_tools.py`` entirely — including ``_as_of_line``, the
  freshness footer appended to nine mirror rollup results, and the module
  docstring that makes CITING it a contract.
* every ``@function_tool`` docstring, anywhere. The Agents SDK lifts a tool
  docstring straight into ``FunctionTool.description``, so those are model
  prompt text, not comments. CLAUDE.md gotcha 70 already records this class
  ("a tool description reaching the model is a contract change, not a comment
  tidy-up") — the rename simply did not apply it.

⚠ **Reads the CORPUS, not a helper** (gotcha 45). The bug was not a broken
function; it was a rename that missed files. Only walking the tree can see
that. The two pre-existing tests over ``_as_of_line`` asserted
``"synced Buildium data" in out`` — a substring that survives the rename in
BOTH directions, so they passed against the bug and against the fix and could
not tell them apart.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"

#: Strings that legitimately contain the old name, each for a stated reason.
#: ⚠ Keep this SHORT and justified. A long allowlist is how a guard becomes a
#: formality — and note the entries are wire contracts and operator text, not
#: "we did not get round to it".
ALLOWED = {
    # The real repository URL, sent as the NWS User-Agent. Renaming it would
    # falsify the contact header a public API identifies us by.
    "detectors/weather_rules.py": {"github.com/brandonoscar/property-manager-agent"},
    # Wire-protocol event-name prefixes, alongside "Buildium." / "Comms." /
    # "Risk.". Stored on queued rows; renaming breaks routing and history.
    "webhooks/inbox_filter.py": {"Helixis."},
    "webhooks/team_tasks.py": {"Helixis.TaskRequest"},
}

#: Operator-facing surfaces: boot logs, eval descriptions, config errors and
#: OpenAPI field docs. A property manager never reads these; a developer does,
#: and the package really is called helixis.
OPERATOR_ONLY = {
    "main.py",
    "llm/router.py",
    "settings.py",
    "smoke/__main__.py",
    "smoke/output.py",
    "evals/runner.py",
    "evals/langfuse_report.py",
    "evals/live/langfuse_live.py",
    "agent/enrichment/models.py",
    "db/repositories/activity.py",
    "db/repositories/leads.py",
}


def _is_tool(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        node = dec.func if isinstance(dec, ast.Call) else dec
        if (getattr(node, "attr", None) or getattr(node, "id", "")) == "function_tool":
            return True
    return False


def _scan() -> tuple[list[str], list[str]]:
    """(runtime string literals, @function_tool docstrings) naming Helixis."""
    runtime: list[str] = []
    tool_docs: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse error is its own test
            continue
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module
            ):
                continue
            body = getattr(node, "body", [])
            if not (body and isinstance(body[0], ast.Expr)):
                continue
            const = body[0].value
            if not (isinstance(const, ast.Constant) and isinstance(const.value, str)):
                continue
            # A module/class/function docstring is a COMMENT — except on a
            # tool, where the SDK makes it the model-visible description.
            docstrings.add(id(const))
            if _is_tool(node) and "Helixis" in const.value:
                tool_docs.append(f"{rel}:{node.lineno} ({getattr(node, 'name', '?')})")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings or "Helixis" not in node.value:
                continue
            if rel in OPERATOR_ONLY:
                continue
            if any(a in node.value for a in ALLOWED.get(rel, ())):
                continue
            runtime.append(f"{rel}:{node.lineno} {node.value.strip()[:70]!r}")
    return runtime, tool_docs


def test_no_tool_description_says_helixis() -> None:
    """A tool docstring IS the model's prompt, not a comment."""
    _, tool_docs = _scan()
    assert not tool_docs, "tool descriptions still name the old product:\n  " + "\n  ".join(
        tool_docs
    )


def test_no_user_visible_string_says_helixis() -> None:
    runtime, _ = _scan()
    assert not runtime, "user-visible strings still name the old product:\n  " + "\n  ".join(
        runtime
    )


def test_the_scan_actually_finds_things() -> None:
    """⚠ A guard that collects zero cases reports green forever.

    The scanner has a wrong-CWD / bad-glob failure mode that looks exactly
    like a pass, so assert it is reading a real corpus before trusting an
    empty result (gotcha 49's rule).
    """
    files = list(SRC.rglob("*.py"))
    assert len(files) > 100, f"only found {len(files)} source files — wrong root?"
    tools = sum(
        1
        for p in files
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and _is_tool(n)
    )
    assert tools > 40, f"only found {tools} @function_tool defs — the scan is not seeing tools"


def test_the_freshness_footer_names_occupella() -> None:
    """The exact sentence from the screenshot.

    ⚠ The two pre-existing tests over this helper assert
    ``"synced Buildium data" in out`` — true before AND after the rename, so
    neither could tell the bug from the fix.

    ⚠ **RE-POINTED, not relaxed.** The footer stopped naming Buildium once the
    mirror had two possible writers (ADR 0010 Decision (a)) — see
    ``_as_of_line``'s own note. The property this test guards is unchanged and
    is still an EXACT equality, because a substring over the brand is exactly
    what failed to catch the rename it was written for.
    """
    from helixis.agents.mirror_tools import _as_of_line

    assert _as_of_line(None) == "(from Occupella's synced property data)"


def test_a_buildium_note_is_stamped_occupella() -> None:
    """⚠ This one is written into the CUSTOMER's permanent Buildium record —
    a work order's WorkDetails, which a property manager reads in Buildium and
    which gotcha 20 says cannot be deleted. It said "[Helixis note …]" while
    the other note writer said "[Occupella]"."""
    src = (SRC / "buildium" / "endpoints" / "work_orders.py").read_text(encoding="utf-8")
    assert "[Helixis note" not in src
    assert "[Occupella note" in src
