"""Every entity the agent can WRITE, it can also READ.

The Buildium tool surface was built write-first, per workflow, and the reads
were never added. The agent could add a vendor note and never read one; change
a task's status without seeing who last changed it or from what; record a
move-out it could not read back.

⚠ **That asymmetry is the engine behind gotchas 41/50/51.** When the row the
agent needs is unreachable it cross-references two other tool results by hand,
or asserts. Whitfield's address — "Marcus Whitfield … at 1700 W Washington St",
a real person placed in a city he does not live in — came from exactly that
manoeuvre. Adding a read is not a nicety; it removes the reason to guess.

⚠ **This is a CENSUS, not twelve hand-written assertions**, because the
finding's own analysis is that a list of known gaps goes stale the moment
someone adds the thirteenth write. It reads the endpoint classes and the tool
registry and pairs them, so a new write with no read fails here on the day it
lands (gotcha 45's rule: measure what the control permits, not what someone
remembered to list).
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"
ENDPOINTS = SRC / "buildium" / "endpoints"
TOOLS = SRC / "agents" / "buildium_tools.py"

#: Method-name prefixes that mutate Buildium state.
_WRITE_PREFIXES = (
    "create",
    "update",
    "post_",
    "add_",
    "upload",
    "share",
    "record_",
    "close_",
    "delete",
    "put_",
    "reassign",
    "renew",
    "undo",
    "set_",
)

#: Endpoint reads the agent deliberately cannot reach, each with the reason.
#: An entry here is a decision someone made; an absence is a gap nobody saw —
#: the whole point of writing them down (gotcha 48: an unexplained absence of a
#: rule is indistinguishable from a forgotten one).
ALLOWED_UNREACHABLE: dict[str, str] = {
    "Files.download_url": (
        "BUILDIUM's file attachments, which are a different corpus from "
        "Occupella's own uploaded library — that one is now readable via "
        "`company_docs_find`, and this exemption originally conflated the two. "
        "Fetching a Buildium attachment still needs a bridge to the document "
        "extraction path plus a ToS §18 decision about anything we cache from "
        "it, so it stays its own change."
    ),
    "Leases.get_transaction": (
        "single-transaction detail; `buildium_list_recent_lease_transactions` "
        "and the mirror's ledger rollups cover every question asked of it so "
        "far, and a per-record ledger read is an N+1 waiting to happen."
    ),
    "Leases.get_move_out_for_tenant": (
        "`buildium_list_lease_departures` returns every move-out on the lease, "
        "which answers the same question in one call."
    ),
    "Tasks.get_todo": (
        "`buildium_list_tasks` carries the same fields with search and "
        "property filters; a single-task read would duplicate it."
    ),
    "Tasks.download_history_file_url": (
        "attachment download — same unbuilt bridge as Files.download_url."
    ),
    "Bills.get_payment": (
        "`buildium_list_bill_payments` returns every payment on the bill, so a "
        "single-payment read adds a round trip and no information."
    ),
}


def _endpoint_methods() -> dict[str, list[str]]:
    """``{ClassName: [public method names]}`` across ``buildium/endpoints/``."""
    out: dict[str, list[str]] = {}
    for path in sorted(ENDPOINTS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ClassDef):
                continue
            names = [
                m.name
                for m in node.body
                if isinstance(m, ast.AsyncFunctionDef | ast.FunctionDef)
                and not m.name.startswith("_")
            ]
            if names:
                out[node.name] = names
    return out


def _called_endpoint_methods() -> set[str]:
    """Every ``api.<group>.<method>(`` the tool module calls."""
    source = TOOLS.read_text(encoding="utf-8")
    return {m for _, m in re.findall(r"\bapi\.(\w+)\.(\w+)\(", source)}


def _is_write(name: str) -> bool:
    return any(name.startswith(p) or name == p.rstrip("_") for p in _WRITE_PREFIXES)


def test_the_census_actually_reads_something() -> None:
    """A walk that finds nothing reports exactly like a clean tree (gotcha 49).
    These floors are far below the real numbers and only catch a broken path."""
    methods = _endpoint_methods()
    assert len(methods) >= 8, "endpoint classes not found — the scan is broken"
    assert sum(len(v) for v in methods.values()) >= 60
    assert len(_called_endpoint_methods()) >= 30, "no api.*.* call sites found"


def test_every_writable_entity_is_also_readable() -> None:
    """THE census. If the agent can change an entity, it must be able to look
    at one — otherwise it reasons about the change from whatever else it has,
    which is how a confident false claim gets made."""
    called = _called_endpoint_methods()
    blind: list[str] = []
    for cls, names in _endpoint_methods().items():
        writes = [n for n in names if _is_write(n) and n in called]
        if not writes:
            continue
        reads = [n for n in names if not _is_write(n) and n in called]
        if not reads:
            blind.append(f"{cls}: writes {writes} and reads NOTHING")
    assert not blind, (
        "the agent can change these entities and cannot read one back, so it "
        "must infer their state from other results — the manoeuvre behind "
        "gotchas 41/50/51:\n  " + "\n  ".join(blind)
    )


def test_every_unreachable_read_is_a_recorded_decision() -> None:
    """A read nobody can call is either a considered omission or an oversight,
    and from the outside those look identical. Naming them makes the difference
    legible — and makes the next unreachable read a deliberate act."""
    called = _called_endpoint_methods()
    undeclared: list[str] = []
    for cls, names in _endpoint_methods().items():
        if not any(_is_write(n) and n in called for n in names):
            continue  # no live write on this entity — nothing to pair
        for name in names:
            if _is_write(name) or name in called:
                continue
            if f"{cls}.{name}" not in ALLOWED_UNREACHABLE:
                undeclared.append(f"{cls}.{name}")
    assert not undeclared, (
        f"unreachable read(s) on an entity the agent WRITES: {sorted(undeclared)}. "
        "Either expose a tool for it or record why not in ALLOWED_UNREACHABLE."
    )


def test_the_exemption_list_has_no_stale_entries() -> None:
    """Both directions (gotcha 38). An exemption for a read that is now
    reachable is a justification nobody is applying, and it would silently
    excuse the read going away again."""
    called = _called_endpoint_methods()
    all_methods = {f"{c}.{n}" for c, names in _endpoint_methods().items() for n in names}
    stale = [
        key for key in ALLOWED_UNREACHABLE if key not in all_methods or key.split(".")[1] in called
    ]
    assert not stale, f"exempted but no longer unreachable (or gone): {sorted(stale)}"


def test_the_new_read_tools_are_capped() -> None:
    """⚠ Every per-record read must be in ``PER_RECORD_CAPS`` or it re-opens
    the N+1 loop that gotchas 25/32/34/37 took four PRs to bound. The cap is
    the only thing that has ever stopped it — prose never has, five times over.
    """
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.call_budget import PER_RECORD_CAPS

    registered = {t.name for t in buildium_tools()}
    for name in (
        "buildium_list_vendor_notes",
        "buildium_get_task_history",
        "buildium_list_lease_departures",
        "buildium_get_unit",
        "buildium_list_bill_payments",
    ):
        assert name in registered, f"{name} is not registered"
        assert name in PER_RECORD_CAPS, f"{name} is a per-record read with no cap"


def test_no_read_tool_carries_a_write_verb() -> None:
    """⚠ ``buildium_list_move_outs`` tripped the stress battery's write-verb
    net, because ``move`` is a write token and the match is whole-token. The
    net was RIGHT — a read one token away from ``buildium_record_move_out`` is
    exactly the shape a safety check should catch, and widening it to admit a
    read would weaken the thing that catches an unauthorized send. The tool was
    renamed to ``buildium_list_lease_departures`` instead.

    Pinned so the convenient name cannot come back and quietly re-flag.
    """
    from helixis.agents.buildium_tools import buildium_tools

    registered = {t.name for t in buildium_tools()}
    assert "buildium_list_move_outs" not in registered
    assert "buildium_list_lease_departures" in registered
