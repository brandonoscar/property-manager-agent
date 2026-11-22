"""Every Buildium *connection* route calls the admin gate — read or write.

⚠ WHY THIS IS AN AST WALK AND NOT A BEHAVIOURAL TEST. On 2026-09-04 the
question "who can touch the Buildium connection?" was answered wrong twice in
a row from `grep`, in both directions:

  1. ``grep require_role`` found 2 hits and missed every ``_require_manager``
     call site, so the mutations looked unguarded. They were not.
  2. The helper's own docstring claimed "every route that mutates that row
     requires Manager+", which was true of the mutations and silently said
     nothing about the two GETs — which really were open.

A docstring cannot fail (gotcha 48) and a substring scan cannot survive a
wrapper (gotcha 40). Walking the decorators and the call graph is the only
form that answers the question asked. This is also gotcha 62's rule — pair a
behavioural test of a gate with a guard on its CALL SITES, because a gate
nothing calls is the `comms_triage` failure.

The split below is the product decision, recorded so a future reader can tell
a considered exemption from a forgotten one:

* CONNECTION routes (credentials, webhook secret, test, backfill) are ADMIN.
  They share one per-company row; changing it breaks everyone at once.
* INBOX routes (followup / freeform / replan / resummarize) are deliberately
  UNGATED. They re-run the planner or summarizer — LLM spend a member can
  burn — but write nothing to Buildium: ``execute`` is separately guarded and
  the agent's own confirm gates and role floors still apply. Working the Inbox
  is what a member is for.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes/buildium.py"

#: Paths whose every method must reach the admin gate. Substring match against
#: the decorator's route path, so ``/credentials`` covers GET/PUT/DELETE.
_CONNECTION_PATHS = ("/credentials", "/webhook-secret", "/test", "/backfill")

#: Deliberately ungated — see the module docstring. Listed by (method, path) so
#: adding a FIFTH inbox write has to land here on purpose.
_ACCEPTED_UNGATED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/events/{event_id}/actions/{action_index}/followup"),
        ("POST", "/events/{event_id}/freeform"),
        ("POST", "/events/{event_id}/replan"),
        ("POST", "/events/{event_id}/resummarize"),
    }
)

_GATES = {"_require_admin", "require_role"}


def _routes() -> list[tuple[str, str, set[str]]]:
    """(method, path, gates-called) for every ``@router.<verb>`` function."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    out: list[tuple[str, str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "router"
            ):
                continue
            path = (
                dec.args[0].value
                if dec.args and isinstance(dec.args[0], ast.Constant)
                else next(
                    (k.value.value for k in dec.keywords if k.arg == "path"),
                    "",
                )
            )
            gates = {
                (c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
                for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name | ast.Attribute)
                and (c.func.id if isinstance(c.func, ast.Name) else c.func.attr) in _GATES
            }
            out.append((dec.func.attr.upper(), path, gates))
    return out


def test_the_scan_actually_found_routes() -> None:
    """A bad path or a refactor that collects ZERO routes would make every
    assertion below vacuously true — the shape of failure gotcha 49's guard
    exists to stop. Assert the scan bit before trusting what it says."""
    routes = _routes()
    assert len(routes) >= 15, f"only found {len(routes)} routes — scan is broken"


def test_every_connection_route_reaches_the_admin_gate() -> None:
    """Reads included. The two GETs were the actual hole."""
    missing = [
        f"{m} {p}"
        for m, p, gates in _routes()
        if any(c in p for c in _CONNECTION_PATHS) and not gates
    ]
    assert not missing, (
        "Buildium connection routes with NO admin gate: "
        + ", ".join(missing)
        + " — every route touching the shared credentials row calls "
        "_require_admin, reads included (2026-09-04)."
    )


def test_the_gate_is_admin_not_manager() -> None:
    """Pins the LEVEL, not just that a gate exists.

    Raising manager → admin is the founder call; a silent revert would leave
    every call site intact and every other test in this file green. Same shape
    as gotcha 59: a guard on a list must also pin what consumes it.
    """
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_require_admin"
    )
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "ADMIN" in names, "_require_admin no longer passes ADMIN to require_role"
    assert "MANAGER" not in names, "gate fell back to MANAGER — it is ADMIN by decision"


def test_ungated_routes_are_exactly_the_accepted_inbox_set() -> None:
    """Both directions.

    A NEW ungated write fails (the list is a closed set, not a floor), and an
    accepted entry that quietly gained a gate also fails — because then the
    exemption is stale and reads as more permissive than the code, which is
    gotcha 63's stale-allowlist finding.
    """
    ungated = {(m, p) for m, p, gates in _routes() if not gates and m != "GET"}
    unexpected = ungated - _ACCEPTED_UNGATED
    assert not unexpected, (
        f"new ungated non-GET route(s): {sorted(unexpected)} — add the gate, or "
        "add it to _ACCEPTED_UNGATED with a reason."
    )
    stale = _ACCEPTED_UNGATED - ungated
    assert not stale, (
        f"stale exemption(s): {sorted(stale)} — these are gated now, so remove "
        "them from _ACCEPTED_UNGATED rather than leaving a licence nobody needs."
    )
