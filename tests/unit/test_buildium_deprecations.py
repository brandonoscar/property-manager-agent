"""A Buildium retirement date that turns into a failing test before it bites.

WHAT WAS WRONG
--------------
Buildium retires the appliance endpoints on 2026-10-19. That was recorded in
three places and **not one of them could fail**:

  * a comment in `buildium/api.py`,
  * a `⏳ tracked` row in `docs/compliance/buildium-api.md`,
  * a numbered follow-up in the same document.

A comment cannot fail (gotcha 48) and a cell reading "tracked" tracks nothing.
The entire mechanism for not being surprised was somebody re-reading a comment
in the right month — and the failure, when it came, would have been
`buildium_list_appliances` returning a 404 to a property manager who asked
what appliances they own.

WHAT THIS DOES
--------------
`buildium/deprecations.py` holds the date once; this file makes it arrive.
Thirty days out the guard goes RED with the action printed in the failure.

⚠ **Deliberately a FAILURE, not a warning.** CI output nobody must act on gets
read as often as the comment this replaces — which is to say, never. The cost
is that once it fires it stays red until somebody acts, and that is the
intended trade: the alternative is a silent 404 in front of a customer.

⚠ **The acknowledgement is EDITING THE REGISTRY, not muting the test.** After
migrating, remove the entry; if Buildium moves the date, change `retires_on`
and its `source`. Both are one-line edits that leave a trace in the diff. That
is the same shape as `test_eval_gate_evidence`'s explicit void marker.

WHAT CHANGED ON 2026-09-22, AND WHY THE MECHANISM TESTS LOOK DIFFERENT
---------------------------------------------------------------------
The appliance entry was closed by RETIRING the capability rather than
migrating it — the other outcome its own `action` named. So `DEPRECATIONS` is
now empty, which is the correct steady state.

That empties every `@pytest.mark.parametrize("entry", DEPRECATIONS)` test in
this file. They are content guards: with no entries there is nothing to check,
pytest reports `empty parameter set`, and that is honest. But it would NOT be
honest to let the same emptiness silently un-test the MECHANISM — a registry
whose window, ordering and reactive lookup are unproven is one that fails the
day somebody registers the next retirement, which is the one day it matters.

So every mechanism test below drives a SYNTHETIC entry instead of
`DEPRECATIONS[0]`. That is strictly stronger than what it replaces: the old
versions proved the window worked for the single shipped entry and would have
gone vacuous (or raised `IndexError`) the moment the registry emptied.
"""

from __future__ import annotations

import ast
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from helixis.buildium import deprecations
from helixis.buildium.deprecations import (
    DEPRECATIONS,
    WARNING_DAYS,
    Deprecation,
    days_remaining,
    due,
    retired_surface,
)
from helixis.buildium.errors import BuildiumApiError

_ROOT = Path(__file__).resolve().parents[2]
_API = _ROOT / "src" / "helixis" / "buildium" / "api.py"
_TOOLS = _ROOT / "src" / "helixis" / "agents" / "buildium_tools.py"
_COMPLIANCE = _ROOT / "docs" / "compliance" / "buildium-api.md"


def _iso(d: date) -> str:
    return d.isoformat()


#: A registry entry that exists only here. Every mechanism test drives THIS,
#: so the window, the ordering and the reactive lookup stay proven whether the
#: shipped registry holds zero entries or five.
#:
#: ⚠ It must never be added to `DEPRECATIONS`: a synthetic surface would make
#: the live alarm fire forever for something nobody can migrate, which is the
#: muted-alarm failure arriving from the opposite side.
_EXAMPLE = Deprecation(
    surface="widgets",
    retires_on=date(2030, 6, 1),
    source="a synthetic entry, defined in tests/unit/test_buildium_deprecations.py",
    action=(
        "Nothing — this entry is a test fixture and names no real Buildium "
        "surface. It exists so the alarm mechanism stays covered."
    ),
    tools=("buildium_list_widgets",),
)


# ---------------------------------------------------------------- the alarm


def test_no_buildium_surface_is_within_its_warning_window() -> None:
    """THE GUARD. Red once a retirement is `WARNING_DAYS` away or past.

    If you are reading this because CI went red: nothing broke. A date you
    already knew about arrived. Do the action printed below, then edit
    `buildium/deprecations.py`.
    """
    today = date.today()
    imminent = due(today)
    if not imminent:
        return

    lines = []
    for entry in imminent:
        left = days_remaining(entry, today)
        when = f"in {left} days" if left >= 0 else f"{-left} days AGO"
        lines.append(
            f"  BuildiumApi.{entry.surface} retires {_iso(entry.retires_on)} ({when})\n"
            f"    tools affected : {', '.join(entry.tools)}\n"
            f"    source         : {entry.source}\n"
            f"    action         : {entry.action}"
        )

    pytest.fail(
        "A Buildium endpoint retirement is due.\n\n" + "\n".join(lines) + "\n\n"
        "This test is the alarm, not the problem. When the work is done, delete the "
        "entry from src/helixis/buildium/deprecations.py; if Buildium moved the date, "
        "change `retires_on` AND `source` together. Do not skip or xfail this — a muted "
        "alarm is the comment it replaced."
    )


# ------------------------------------------------- the registry is trustworthy


@pytest.mark.parametrize("entry", DEPRECATIONS, ids=lambda e: e.surface)
def test_every_entry_names_a_real_api_surface(entry: Deprecation) -> None:
    """A registry pointing at a renamed attribute alarms about nothing.

    Imports `BuildiumApi` lazily — the module pulls the whole endpoint tree,
    and a collection error here would read as a broken test rather than a
    broken registry.
    """
    from helixis.buildium.api import BuildiumApi

    assert entry.surface in BuildiumApi.__init__.__code__.co_names or _api_source_sets(
        entry.surface
    ), (
        f"BuildiumApi has no `{entry.surface}` surface — the deprecation entry is "
        "pointing at something that no longer exists, so its alarm would fire for "
        "a surface nobody can migrate."
    )


def _api_source_sets(surface: str) -> bool:
    """`self.<surface> = ...` appears in api.py.

    Attribute assignment is not reliably visible in `co_names` across Python
    versions, so the source is the honest check — and this file is about a
    registry matching the code, not about bytecode trivia.
    """
    return (
        re.search(rf"self\.{re.escape(surface)}\s*=", _API.read_text(encoding="utf-8")) is not None
    )


@pytest.mark.parametrize("entry", DEPRECATIONS, ids=lambda e: e.surface)
def test_every_named_tool_is_defined_and_registered(entry: Deprecation) -> None:
    """Renaming a tool must not orphan the entry that warns about it.

    Same class as `test_golden_tool_names_are_real` (gotcha 35's family): a
    name recorded in one file and deleted in another goes quiet, and quiet is
    indistinguishable from safe.

    ⚠ Read by AST rather than by `hasattr`, for two reasons. The weaker one is
    that importing `buildium_tools` pulls the OpenAI Agents SDK, which is not
    installable in every environment this suite runs in. The STRONGER one is
    that `hasattr` proves only that a function was DEFINED — a tool deleted
    from `buildium_tools()` while its `def` remains is still gone from the
    agent, and the alarm would go on naming a tool no model can call.
    """
    defined, registered = _tool_names()
    for tool in entry.tools:
        assert tool in defined, (
            f"{entry.surface} names tool {tool!r}, which is not defined in "
            "agents/buildium_tools. Either it was renamed (update the entry) or "
            "already retired (then the deprecation entry can be closed)."
        )
        assert tool in registered, (
            f"{tool!r} is defined but NOT returned by buildium_tools(), so no agent "
            "can call it. The deprecation entry is warning about a tool that is "
            "already unreachable."
        )


def _tool_names() -> tuple[set[str], set[str]]:
    """(defined at module level, returned by `buildium_tools()`).

    Two separate facts, because they fail separately — gotcha 35's registered-
    vs-resolved split, one level down.
    """
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    defined = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "buildium_tools":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                registered.add(inner.id)
    assert defined, "AST scan found no functions — wrong path or a parse failure"
    assert registered, "AST scan found no registry entries — buildium_tools() moved?"
    return defined, registered


@pytest.mark.parametrize("entry", DEPRECATIONS, ids=lambda e: e.surface)
def test_every_entry_carries_a_source_and_an_action(entry: Deprecation) -> None:
    """A date with no provenance is gotcha 48's expired justification.

    And an alarm with no next action is one people mute.
    """
    assert len(entry.source) > 20, "say where the date came from, specifically"
    assert len(entry.action) > 40, "say what a maintainer must actually do"
    assert entry.tools, "an entry that names no tool cannot say what breaks"


# ------------------------------------------------ the three copies agree


@pytest.mark.parametrize("entry", DEPRECATIONS, ids=lambda e: e.surface)
def test_the_code_comment_names_the_same_date(entry: Deprecation) -> None:
    """`api.py` explains the deprecation in prose beside the surface.

    That prose is what a reader hits first, so a date there that disagrees with
    the registry sets the alarm for one day and the expectation for another —
    gotcha 25's "fix it at every layer" applied to a fact rather than a field.
    """
    source = _API.read_text(encoding="utf-8")
    assert _iso(entry.retires_on) in source, (
        f"api.py does not mention {_iso(entry.retires_on)}. The comment beside "
        f"`self.{entry.surface}` must name the same date as the registry."
    )


@pytest.mark.parametrize("entry", DEPRECATIONS, ids=lambda e: e.surface)
def test_the_compliance_doc_names_the_same_date(entry: Deprecation) -> None:
    """The ToS §18 record is what a reviewer reads; it must not drift either."""
    doc = _COMPLIANCE.read_text(encoding="utf-8")
    assert _iso(entry.retires_on) in doc, (
        f"docs/compliance/buildium-api.md does not mention {_iso(entry.retires_on)}."
    )


# ---------------------------------------------------------- the window itself
#
# ⚠ These drive `_EXAMPLE` through a monkeypatched registry rather than
# `DEPRECATIONS[0]`. `due` and `retired_surface` read the module global at
# call time, so patching the attribute exercises the REAL functions against a
# registry whose contents the test controls — which is what keeps the
# mechanism proven while the shipped registry is empty.


def _with_registry(monkeypatch: pytest.MonkeyPatch, *entries: Deprecation) -> None:
    monkeypatch.setattr(deprecations, "DEPRECATIONS", tuple(entries))


def test_the_window_actually_fires_before_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation in test form: prove `due` is not vacuous.

    Without this, a `due` that always returned `[]` would make the alarm above
    pass forever — gotcha 45's "silently permits more", which is the direction
    that matters here.
    """
    _with_registry(monkeypatch, _EXAMPLE)
    one_day_inside = _EXAMPLE.retires_on - timedelta(days=WARNING_DAYS - 1)
    assert due(one_day_inside), "the window must fire inside WARNING_DAYS"


def test_a_retirement_far_away_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction — an alarm that is always on is one nobody reads."""
    _with_registry(monkeypatch, _EXAMPLE)
    well_before = _EXAMPLE.retires_on - timedelta(days=WARNING_DAYS + 60)
    assert not due(well_before), "the window must be quiet outside WARNING_DAYS"


def test_a_date_already_passed_still_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The most urgent case must not age out of the window.

    A forward-only window goes QUIET on the day the endpoint actually starts
    404ing, which is precisely when the alarm is needed most.
    """
    _with_registry(monkeypatch, _EXAMPLE)
    after = _EXAMPLE.retires_on + timedelta(days=90)
    assert due(after), "a past retirement must stay in the list"
    assert days_remaining(_EXAMPLE, after) < 0


def test_an_empty_registry_is_quiet_rather_than_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The steady state since 2026-09-22, asserted rather than assumed.

    `due` over no entries must return an empty list, not raise — the alarm
    above calls it on every CI run, so an `IndexError` here would red-gate the
    whole repository for having nothing to warn about.
    """
    _with_registry(monkeypatch)
    assert due(date(2030, 1, 1)) == []
    assert retired_surface("widgets", date(2030, 1, 1)) is None


def test_the_shipped_registry_does_not_carry_the_test_fixture() -> None:
    """`_EXAMPLE` names no real surface, so a copy-paste into the live
    registry would alarm forever about something nobody can migrate — and
    `test_every_entry_names_a_real_api_surface` would then fail with a
    confusing message about `BuildiumApi.widgets`."""
    assert _EXAMPLE.surface not in {e.surface for e in DEPRECATIONS}


# ------------------------------------------------- the retirement is not an absence


def test_retired_surface_is_quiet_until_the_date_and_then_names_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retired_surface`` is the one thing here a request path may act on.

    Both directions, because a one-sided check passes against a predicate that
    answers the same thing for every input (gotchas 38/51): before the date it
    must stay silent, or a caller retires the endpoint on our side ahead of the
    vendor — which this module's header forbids, and which would break a
    working feature early if Buildium extended the date.
    """
    _with_registry(monkeypatch, _EXAMPLE)

    assert retired_surface(_EXAMPLE.surface, _EXAMPLE.retires_on - timedelta(days=1)) is None
    assert retired_surface(_EXAMPLE.surface, _EXAMPLE.retires_on) is _EXAMPLE
    assert retired_surface(_EXAMPLE.surface, _EXAMPLE.retires_on + timedelta(days=365)) is _EXAMPLE
    assert retired_surface("not-a-surface", _EXAMPLE.retires_on + timedelta(days=1)) is None


def test_the_404_branch_still_omits_the_not_an_absence_guard() -> None:
    """THE PREMISE THE WHOLE REACTIVE PATTERN RESTS ON.

    This file used to assert it through `_list_appliances_impl`, the one tool
    that consulted the registry. That tool was removed with the appliance
    surface on 2026-09-22, so the assertion is re-pointed at the property it
    was really about rather than deleted (gotchas 89/93/95).

    `_sanitize_buildium_error`'s 404 branch deliberately carries no
    `_NOT_AN_ABSENCE` guard, because for a RECORD lookup absence is the
    correct reading — "that task isn't in Buildium" is a fact. That omission
    is exactly why `retired_surface` exists: for a retired SURFACE nothing was
    named, so "I couldn't find that" is one step from "you have none of those"
    (gotcha 26).

    If this ever fails because the 404 branch STARTED appending the guard,
    the reactive pattern may have become unnecessary — read
    `deprecations.retired_surface` before deleting either side.
    """
    from helixis.agents import buildium_tools as bt

    unguarded = bt._sanitize_buildium_error(BuildiumApiError(404, "gone"), "list widgets")
    assert bt._NOT_AN_ABSENCE not in unguarded


def test_a_caller_composing_the_pattern_does_not_report_a_retirement_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reactive pattern, exercised end to end with no production caller.

    ⚠ `retired_surface` has had none since the appliance tool went, so
    without this the helper would be present-but-unproven — the
    `comms_triage` shape (gotcha 62) this repo has recorded nine times. Here
    the test IS the caller: it runs the exact sequence
    `deprecations.retired_surface` documents, so the day a real tool needs it
    the helper is known to work rather than merely known to exist.
    """
    from helixis.agents import buildium_tools as bt

    _with_registry(monkeypatch, _EXAMPLE)

    def reply_for(today: date) -> str:
        retired = retired_surface(_EXAMPLE.surface, today)
        if retired is not None:
            return (
                f"Buildium retired that surface on {_iso(retired.retires_on)}, so this "
                f"lookup can no longer run." + bt._NOT_AN_ABSENCE
            )
        return bt._sanitize_buildium_error(BuildiumApiError(404, "gone"), "list widgets")

    after = reply_for(_EXAMPLE.retires_on + timedelta(days=1))
    assert _iso(_EXAMPLE.retires_on) in after
    assert bt._NOT_AN_ABSENCE in after, (
        "a retired surface must never be reported as an empty portfolio"
    )

    # And the other direction: before the date the caller falls through to the
    # ordinary error path, which is the branch that correctly omits the guard.
    before = reply_for(_EXAMPLE.retires_on - timedelta(days=1))
    assert bt._NOT_AN_ABSENCE not in before
