"""The keyless-census migration reached `geocode()` and not its callers.

Gotcha 18 moved `maps.provider` to **census** on 2026-08-17 — public domain,
US-only, and **KEYLESS** — and retired the Maps release gate on exactly that
basis. `geocode()` itself was updated. Three of its four callers were not, and
went on asking `if not settings.maps.api_key`, which on the shipped default is
"no key" for a provider that needs none:

  * `geo/backfill.py`          returned 0 before looking at anything, so the
                               geocode layer was DARK on every keyless install
                               — no `geog`, and therefore no flood zone, no
                               Fair Market Rent, and no answer to which state's
                               landlord-tenant law applies for any property
                               added since that migration
  * `scripts/backfill_geocodes.py`  refused to run and told the operator to set
                               a key nothing reads
  * `agents/mirror_tools._properties_near_impl`  refused a search that works
  * `agents/places_tools`      had a raw-key bail that SHADOWED the honest
                               `places_supported` branch 45 lines below it

The failure mode of the whole class is gotcha 45's — a check that silently
refuses MORE than it should, with nothing failing anywhere — so the load-bearing
test here is the CORPUS scan, not the truth table. `places.places_supported` was
already the correct pattern; geocoding simply never got its counterpart.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from helixis.geo.geocode import geocoding_supported
from helixis.settings import MapsSettings, Settings

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "helixis"
_SCRIPTS = _REPO / "scripts"

#: The ONLY two modules allowed to read the raw key.
#:
#: ⚠ The line is PROVIDER vs CONSUMER, and it is drawn there because a narrower
#: line does not survive contact with the corpus. These two build the actual
#: provider request — `?key=…` in a URL, the argument to `_geocode_google` — so
#: they cannot be written without the key, and their own gates
#: (`places_nearby` returning `[]`, `static_map_url` returning `None`) are
#: never rendered as prose. Everything else is a CONSUMER deciding whether a
#: capability exists, and must ask a predicate instead: on the shipped census
#: default there is no key and geocoding works, so a consumer's raw-key check
#: refuses work that succeeds.
#:
#: ⚠ A wider scan was tried first and flagged all seven legitimate reads in
#: these two files — gotcha 103's "a scan that matches too broadly is as
#: useless as one that matches too little", which ends with the guard being
#: switched off.
_KEY_READER_MODULES = {"geo/geocode.py", "geo/places.py"}


def _rel(path: pathlib.Path) -> str:
    try:
        return path.relative_to(_SRC).as_posix()
    except ValueError:
        return path.relative_to(_REPO).as_posix()


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    """Name of the innermost def containing `target`, or "" at module level."""
    best = ""
    best_depth = -1
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if inner is target:
                depth = getattr(node, "col_offset", 0)
                if depth >= best_depth:
                    best, best_depth = node.name, depth
                break
    return best


def _maps_key_reads(source: str, tree: ast.Module) -> list[tuple[str, int]]:
    """Every `<maps>.api_key` read, as (enclosing function, lineno).

    ⚠ AST, never a substring scan (gotcha 40): the modules now QUOTE the old
    `settings.maps.api_key` form in their comments in order to explain why it
    was wrong, so a text search fires on the sentence documenting the fix. A
    comment is never an `ast.Attribute`.

    ⚠ It has to follow the local rebinding too. Both predicates open with
    `cfg = settings.maps` and then read `cfg.api_key`, which is the shape a
    naive `settings.maps.api_key` matcher misses entirely — and missing it
    would make this guard find zero and report green (gotchas 49/100/101).
    """
    maps_aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Attribute)
            and value.attr == "maps"
        ):
            maps_aliases.add(target.id)

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "api_key":
            continue
        recv = node.value
        is_maps = (isinstance(recv, ast.Attribute) and recv.attr == "maps") or (
            isinstance(recv, ast.Name) and recv.id in maps_aliases
        )
        if is_maps:
            found.append((_enclosing_function(tree, node), node.lineno))
    return found


def _python_files() -> list[pathlib.Path]:
    files = [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]
    files += [p for p in _SCRIPTS.rglob("*.py") if "__pycache__" not in p.parts]
    return files


def _calls_in(path: pathlib.Path, func_name: str) -> set[str]:
    """Names of every function called inside `func_name` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func_name:
            called: set[str] = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    fn = inner.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)
            return called
    raise AssertionError(f"{_rel(path)} has no function named {func_name!r}")


# --------------------------------------------------------------------------
# The corpus guard — the load-bearing one.
# --------------------------------------------------------------------------


def test_only_the_two_predicates_read_the_raw_maps_key():
    """Nobody else may ask "is there a key" — they must ask "can we do it".

    Gotcha 45: the failure mode is "silently refuses more than it should", and
    a per-function test cannot see a FIFTH caller written the old way. This
    reads the whole corpus so a new one has to be a decision.
    """
    offenders: list[str] = []
    seen_allowed: set[str] = set()
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        if "api_key" not in source:
            continue
        reads = _maps_key_reads(source, ast.parse(source))
        if not reads:
            continue
        rel = _rel(path)
        if rel in _KEY_READER_MODULES:
            seen_allowed.add(rel)
        else:
            offenders += [f"{rel}:{line} in {func or '<module>'}()" for func, line in reads]

    # ⚠ A scan that finds nothing is a claim to check, not a result. If the
    # walk breaks (a rename, a changed alias shape), it would collect zero
    # reads and report a clean green over a corpus it never inspected
    # (gotchas 49/100/101). It also doubles as the stale-allowlist test:
    # an exemption matching nothing reads as more permissive than the code
    # actually is (gotcha 63, which caught its own author).
    assert seen_allowed == _KEY_READER_MODULES, (
        f"the scan found raw-key reads in {sorted(seen_allowed)}, expected "
        f"{sorted(_KEY_READER_MODULES)}. Either the walk is broken or an "
        "allowlist entry is stale — neither is a clean result."
    )
    assert not offenders, (
        "these read settings.maps.api_key directly instead of asking a "
        "predicate whether the deployment can do the thing:\n  "
        + "\n  ".join(offenders)
        + "\n\nOn the shipped census default there is no key and geocoding "
        "works, so a raw-key check refuses work that succeeds."
    )


# --------------------------------------------------------------------------
# Both sides of the predicate: what it answers, and that geocode() obeys it.
# --------------------------------------------------------------------------


def _settings(provider: str, api_key: str = "") -> Settings:
    s = Settings.model_construct()
    object.__setattr__(s, "maps", MapsSettings(provider=provider, api_key=api_key))
    return s


@pytest.mark.parametrize(
    ("provider", "key", "expected"),
    [
        # The shipped default: keyless and fully supported. This row is the
        # entire finding — the old raw-key check answered False here.
        ("census", "", True),
        ("census", "abc", True),
        ("google", "", False),
        ("google", "abc", True),
        ("mapbox", "", False),
        ("mapbox", "abc", True),
    ],
)
def test_geocoding_supported_asks_the_provider_not_the_key(provider, key, expected):
    assert geocoding_supported(_settings(provider, key)) is expected


def test_geocode_itself_consults_the_predicate():
    """The predicate and the behaviour cannot drift into disagreeing.

    Gotcha 28's both-sides rule: a caller that gates on `geocoding_supported`
    and a `geocode()` that gates on something else would refuse and attempt on
    different conditions, and only the disagreement is visible.
    """
    assert "geocoding_supported" in _calls_in(_SRC / "geo" / "geocode.py", "geocode")


@pytest.mark.parametrize(
    ("module", "func"),
    [
        ("geo/backfill.py", "geocode_company_properties"),
        ("agents/mirror_tools.py", "_properties_near_impl"),
    ],
)
def test_every_geocoding_caller_asks_the_predicate(module, func):
    """A behavioural test of the predicate cannot see a caller that ignores it.

    Gotchas 62/66/69/103/105 are five separate instances of exactly this: a
    well-tested helper whose one production call site quietly did its own thing.
    """
    assert "geocoding_supported" in _calls_in(_SRC / module, func)


def test_the_operator_script_asks_the_predicate_too():
    assert "geocoding_supported" in _calls_in(_SCRIPTS / "backfill_geocodes.py", "main")


# --------------------------------------------------------------------------
# places_tools: one gate for one question.
# --------------------------------------------------------------------------


def test_places_tools_gates_on_places_supported_and_nothing_else():
    """The raw-key bail SHADOWED the honest branch, so both had to go.

    `places_supported` is deliberately STRICTER than `geocoding_supported` —
    census geocodes and has no POI data at all — so the two are not
    interchangeable and a caller must pick the one matching its question.
    """
    source = (_SRC / "agents" / "places_tools.py").read_text(encoding="utf-8")
    calls = _calls_in(_SRC / "agents" / "places_tools.py", "_find_places_near_property_impl")
    assert "places_supported" in calls
    assert "geocoding_supported" not in calls, (
        "places search needs POI data, which census does not have at any price"
    )
    # The constant carried advice that cannot help (gotcha 74's deleted
    # accessor): its absence is the fix, so pin the absence.
    assert "_MAPS_UNCONFIGURED" not in ast.dump(ast.parse(source))
