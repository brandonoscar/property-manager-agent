"""Case 480's two Buildium write mocks say what the real tools say.

A mock that words a success differently from the tool teaches the case a reply
the product never gives: 480's close mock claimed "confirmed the status now
reads Completed" while ``buildium_close_work_order`` answers "Closed <title> in
Buildium (marked Completed)." Both sides are read (rule 15): each tool's
success f-string, rebuilt from its AST (rule 17), against the case's mock for
the work order the case lists.
"""

from __future__ import annotations

import ast
import inspect
import json

from helixis.agents import buildium_tools
from helixis.evals.runner import GOLDEN_DIR

_CASE = GOLDEN_DIR / "480_rentvine_write_not_reached_on_a_buildium_company.json"
_TITLE = "Replace the hallway smoke detector"


def _template(node: ast.JoinedStr) -> str:
    return "".join(
        str(v.value).replace("{", "{{").replace("}", "}}") if isinstance(v, ast.Constant) else "{}"
        for v in node.values
    )


def _success_templates(function: str) -> set[str]:
    """The two success replies ``function`` can give, each interpolated value as
    ``{}``: the last return of the closure it hands ``run_guarded`` as
    ``execute`` (what a person normally reads), and its own last return (the
    replay path). Found by structure, never by their words, so a reworded reply
    cannot drop out of what is compared."""
    tree = ast.parse(inspect.getsource(buildium_tools))
    (fn,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == function
    ]
    executes = {
        kw.value.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "execute" and isinstance(kw.value, ast.Name)
    }
    (name,) = executes
    (closure,) = [n for n in fn.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name]
    replies = [closure.body[-1], fn.body[-1]]
    for reply in replies:
        assert isinstance(reply, ast.Return) and isinstance(reply.value, ast.JoinedStr), function
    return {_template(r.value) for r in replies if isinstance(r.value, ast.JoinedStr)}


def _mock(tool: str) -> str:
    case = json.loads(_CASE.read_text())
    return str(case["input"]["tool_mocks"][tool][0]["return"])


def test_the_case_lists_the_work_order_its_mocks_name() -> None:
    assert _TITLE in _mock("buildium_list_open_work_orders")


def test_the_close_mock_is_the_close_tools_reply() -> None:
    templates = _success_templates("_close_work_order_impl")
    assert {t.format(_TITLE) for t in templates} == {_mock("buildium_close_work_order")}


def test_the_task_status_mock_is_the_task_tools_reply() -> None:
    templates = _success_templates("_update_task_status_impl")
    expected = {_mock("buildium_update_task_status")}
    assert {t.format(_TITLE, "Completed") for t in templates} == expected
