"""Every assertion a golden case declares must be one its OWN harness reads.

`test_golden_tool_names_are_real` holds the line one level down: the tool
NAMES an assertion mentions must ship. This is the level above — the KEY
carrying them must be implemented by the harness that will run the case.
Neither implies the other, and an unknown key is silently ignored, so the
failure is completely quiet.

Measured 2026-09-02, which is why this file exists. Three cases written the
day before declared assertions against `orchestrator_routing`:

* **460** put its five note/task/text prohibitions under
  ``tool_call_trail_must_not_contain`` — implemented in `agent_flow` only.
  The routing harness reads ``handoff_trail_must_not_contain``, and that
  trail holds AGENT names, so there was no spelling of a tool prohibition
  that could have fired there at all. The load-bearing half of a SAFETY case
  was decoration; the case was converted to `agent_flow` (419's precedent).
* **470 and 471** declared ``reply_must_not_be_empty``, also `agent_flow`
  only. It has since been wired into the routing harness — an empty reply
  satisfies every ``reply_must_not_*`` needle vacuously, which is precisely
  the shape both cases exist to catch.

419's own note records the MIRROR IMAGE of this mistake and the same fix:
"⚠ Wiring that flag onto an agent_flow case meant WIRING IT:
judge_safety_gate was implemented only in the routing harness, so setting it
here would otherwise have been a safety claim nothing reads (gotcha 48)."
That author noticed by hand. Nothing made it checkable until now.

⚠ TWO false positives cost a diagnostic step each while this was being
written, and both are guarded against below:

* ``judge_safety_gate`` is reached as
  ``(case.get("expected") or {}).get("judge_safety_gate")`` — a CHAINED
  access with no bare ``expected`` receiver. A scanner keyed on that receiver
  reported ten correct fair-housing cases as dead. So the scan collects every
  string used as a ``.get()`` argument or subscript anywhere in the module.
* `agent_flow` DELEGATES to `orchestrator_routing` for the judge gate, so a
  strictly per-module reading flagged 419. Import edges are followed, and
  they run one way only (routing does not import agent_flow) — which is what
  keeps the delegation from laundering a genuinely dead key.

Both corrections make the guard MORE permissive, so the residual risk is a
missed dead key rather than a false alarm — the right direction for a guard
somebody would otherwise switch off (gotcha 45's reasoning, and the same
reason `connector_tool_names()` is parsed rather than hardcoded).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "src" / "helixis" / "evals"
GOLDEN = EVALS / "golden"

#: Harness module per `case_type`. A case type absent here is not checked —
#: `planner_summarizer` and `tool_invocation` are driven from `runner.py`,
#: whose keys are in the shared set below.
_HARNESS = {
    "agent_flow": "agent_flow.py",
    "orchestrator_routing": "orchestrator_routing.py",
}

#: Read by every harness regardless of case type.
_SHARED = "runner.py"

#: Case-level keys that are configuration rather than assertions. They live
#: outside `expected`, so they never reach this scan — listed only so a reader
#: does not go looking for them.
_NOT_ASSERTIONS = frozenset({"id", "case_type", "requires_env", "single_run_gate", "input"})


def _keys_read_by(module: str) -> set[str]:
    """Every string used as a ``.get()`` argument or a subscript in `module`.

    Deliberately over-collects — see this file's docstring. Docstrings and
    comments are NOT counted: prose describing a key is not code reading one,
    and counting it would let a note about a removed assertion keep that
    assertion looking alive (gotcha 48's shape).
    """
    tree = ast.parse((EVALS / module).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            found.add(node.slice.value)
    return found


def _eval_modules_imported_by(module: str) -> set[str]:
    """Sibling `helixis.evals.*` modules `module` imports from, at any depth.

    `agent_flow` imports `judge_safety_verdict` from `orchestrator_routing`
    inside a function, so the edge is real at runtime and invisible to a
    top-level import scan.
    """
    tree = ast.parse((EVALS / module).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("helixis.evals."):
            name = (node.module or "").rsplit(".", 1)[-1] + ".py"
            if (EVALS / name).is_file():
                out.add(name)
    return out


def _readable_keys(case_type: str) -> set[str]:
    module = _HARNESS[case_type]
    modules = {module, _SHARED} | _eval_modules_imported_by(module)
    keys: set[str] = set()
    for m in modules:
        keys |= _keys_read_by(m)
    return keys


def _declared() -> list[tuple[str, str, str]]:
    """(case file, case_type, assertion key) for every checkable case."""
    out: list[tuple[str, str, str]] = []
    for path in sorted(GOLDEN.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case_type = case.get("case_type")
        if case_type not in _HARNESS:
            continue
        for key in case.get("expected") or {}:
            out.append((path.name, case_type, key))
    return out


def test_the_scan_reads_both_sides() -> None:
    """A guard that silently reads zero of either side passes forever."""
    declared = _declared()
    assert len(declared) > 200, f"only {len(declared)} assertions found — corpus scan broken"
    assert {t for _f, t, _k in declared} == set(_HARNESS), (
        "one of the two harnesses contributed no cases — check `case_type` spelling"
    )
    # ⚠ THE MODULE ITSELF, not `_readable_keys` — those answer DIFFERENT
    # questions and the union was the wrong one here (found 2026-09-16, as a
    # false positive rather than by review). `_readable_keys` unions in
    # `runner.py` because shared VERDICT keys live there; this canary asks
    # whether the ROUTING HARNESS can act on a tool prohibition. The per-PR
    # cost gate reads `tool_call_trail_must_not_contain` in `runner.py` to
    # decide which cases to SPEND a model call on — selection, never a verdict,
    # and gotcha 117 records those two readers of one key as deliberately
    # separate. Widening this back makes every future selection-side read of
    # any key look like the harness gaining an assertion.
    routing_module = _keys_read_by(_HARNESS["orchestrator_routing"])
    assert "must_not_route_to" in routing_module, "known routing key missing — module scan broken"
    assert "tool_call_trail_must_not_contain" not in routing_module, (
        "the routing harness now reads tool_call_trail_must_not_contain. If that is "
        "deliberate, this guard's premise changed and 460 (converted to agent_flow "
        "BECAUSE of it) should be re-read — its handoff_trail holds AGENT names, so "
        "a tool prohibition there could never have fired"
    )
    assert "judge_safety_gate" in _readable_keys("agent_flow"), (
        "the import-edge walk stopped working — agent_flow reaches judge_safety_gate "
        "through orchestrator_routing, and without that edge 419 reads as dead"
    )


def test_every_declared_assertion_is_one_its_harness_reads() -> None:
    """The whole point. An unknown key is ignored in silence."""
    readable = {t: _readable_keys(t) for t in _HARNESS}
    dead = sorted(
        {
            f"{case}: {case_type} does not read {key!r}"
            for case, case_type, key in _declared()
            if key not in readable[case_type]
        }
    )
    assert not dead, (
        "golden cases declare assertions their harness never reads:\n  "
        + "\n  ".join(dead)
        + "\n\nAn unrecognised key is ignored without comment, so the assertion "
        "cannot fail — in the 4xx family that is a SAFETY claim that passes "
        "forever. Either move the case to the harness that implements the key "
        "(460 → agent_flow, following 419), or WIRE the key into this harness "
        "(reply_must_not_be_empty → orchestrator_routing). Do not simply delete "
        "the assertion: it was written because something needed bounding."
    )


def test_every_routing_case_declares_where_it_expects_to_route() -> None:
    """The OTHER direction, and it cost a live battery run to find.

    The test above checks that a DECLARED key is read. It says nothing about a
    REQUIRED key being absent, and those are different failures: an unknown key
    is ignored in silence, while a missing routing declaration makes the
    harness fail the case on a schema assertion before the model is ever
    called.

    Measured 2026-09-03, first live run of the 48-case battery: 470 and 471
    were the only two of 48 that did not execute. Both declared
    ``case_type: orchestrator_routing`` with no ``must_route_to`` and no
    ``must_route_to_one_of``, so both had been failing structurally since the
    day they were written — they would have red-gated every release with a
    permanent false failure, and reported it as ``kind=safety``, which is the
    loudest label the harness has.

    ⚠ Reading the failure correctly matters as much as fixing it: the
    fail-closed classifier was RIGHT to call it safety (an unrecognised
    failure must never be downgraded), but a malformed case is a schema error,
    not unsafe model behaviour. Anyone triaging a red battery needs that
    distinction, which is why this guard exists rather than a looser one.
    """
    offenders = []
    for path in sorted(GOLDEN.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if case.get("case_type") != "orchestrator_routing":
            continue
        expected = case.get("expected") or {}
        declared = [
            k for k in ("must_route_to", "must_route_to_one_of") if expected.get(k) is not None
        ]
        if len(declared) != 1:
            offenders.append(f"{path.name}: declares {declared or 'neither'}, needs exactly one")
    assert not offenders, (
        "routing cases with a malformed route declaration:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe harness fails these on a schema assertion BEFORE calling the "
        "model, so the case never runs and reports kind=safety forever. Either "
        'declare where it routes ("Occupella" means the router answers without '
        "a handoff — see 415/416/441/442), or move it to agent_flow, where no "
        "routing declaration is required."
    )


def test_the_two_repaired_cases_keep_a_key_that_fires() -> None:
    """Pins the specific repair, so a revert is loud rather than silent.

    The generic test above catches a key no harness reads. This catches the
    other direction — the bound quietly disappearing altogether, which is how
    the coverage would go away while every test stayed green.
    """
    for name in ("470_billing_no_plan_change_claimed", "471_billing_usage_not_invented"):
        expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))["expected"]
        assert expected.get("reply_must_not_be_empty"), (
            f"{name} stopped requiring a non-empty reply. Every one of its bounds is a "
            "PROHIBITION, and an empty reply satisfies all of them vacuously — a run "
            "that returned nothing would score as a clean pass on the case written to "
            "catch a fabricated answer."
        )

    case_460 = json.loads(
        (GOLDEN / "460_fair_housing_source_of_income_screening.json").read_text(encoding="utf-8")
    )
    assert case_460["case_type"] == "agent_flow", (
        "460 is back on orchestrator_routing, whose handoff_trail holds AGENT names — "
        "its five tool prohibitions cannot fire there in any spelling"
    )
    forbidden = set(case_460["expected"]["tool_call_trail_must_not_contain"])
    assert {
        "buildium_add_task_note",
        "buildium_add_work_order_note",
        "buildium_add_vendor_note",
    } <= forbidden, (
        "460 stopped forbidding the note writes. Persisting the rule into a "
        "landlord's records is the point at which a sentence becomes a practice a "
        "regulator can read — it is the load-bearing half of the case."
    )
