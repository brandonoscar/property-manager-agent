"""The jurisdiction snapshot names WHERE it decided the property is — and the
local overlay layer can actually match something.

Coverage item 196. Two defects, one root cause, and the second is the larger:

* ``format_compact`` rendered only the STATE. The deposit cap, notice periods
  and late-fee rules below that header come from the state block PLUS whichever
  county/place overlay matched, so a property geocoded a block across a city
  line produced a confidently wrong legal answer that read exactly like a right
  one — with nothing on screen a manager could catch it by. They know their own
  city; they cannot see a FIPS code.
* **Every one of the 17 shipped local overlays was unreachable from
  production.** All 17 are keyed by place NAME and carry no FIPS at all, while
  the one production call site passed FIPS and no name — so
  ``_matches_place``'s FIPS arm had nothing to match and its name arm was never
  handed a name. Los Angeles, San Francisco, Seattle, New York, Chicago,
  Denver, Philadelphia, Boston and five more shipped a rental-licence warning
  that no answer could ever include. Measured before the fix, not assumed:
  ``lookup("CO", county_fips="08031", place_fips="0820000")`` — the exact
  arguments the shipped tool passed for a Denver property — returned
  ``overlay_places == []``.

Both were the same root cause. ``resolve_stack`` has always returned
``county_name``/``place_name`` off the same Census response the FIPS codes come
from, and ``_JURISDICTION_UPDATE_SQL`` persisted three columns and dropped
both — the field-fetched-and-thrown-away shape of gotchas 41/50/51/88/92/93,
and as in every previous one the fix is to keep what was already in hand.

⚠ A FIPS code cannot be turned back into a name without the Census gazetteer,
which this product does not bundle. So the names were not merely unstored, they
were UNRECOVERABLE — which is why migration 0099 ships with no backfill and the
work-list re-resolves instead.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from helixis.geo import intel_backfill, jurisdiction

_RULES = json.loads(
    (
        pathlib.Path(jurisdiction.__file__).resolve().parent / "data" / "jurisdiction_rules.json"
    ).read_text(encoding="utf-8")
)
_MIRROR_SRC = (
    pathlib.Path(jurisdiction.__file__).resolve().parent.parent / "agents" / "mirror_tools.py"
)


def _overlay_entries() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for state, block in (_RULES.get("overlay_jurisdictions") or {}).items():
        for entry in block.get("places") or []:
            if isinstance(entry, dict):
                out.append((state, entry))
    return out


# ── what the snapshot says about WHERE ────────────────────────────────


def test_the_snapshot_names_the_place_and_county_it_resolved_to() -> None:
    answer = jurisdiction.lookup("CO", county_name="Denver County", place_name="Denver")
    out = jurisdiction.format_compact(answer, property_label="200 E Colfax Ave")
    assert "- Resolved to: Denver, Denver County (CO)" in out


def test_an_unresolved_name_says_nothing_rather_than_inventing_one() -> None:
    """The other direction, and it is the one that ships wrong most easily.

    Every row written before migration 0099 carries FIPS codes and NULL names.
    A header reading "None County" is a FRESH false statement where silence is
    merely a missing one (gotcha 26's class, and gotcha 51's FORMER-tenant
    reasoning pointed at a place instead of a person).
    """
    out = jurisdiction.format_compact(
        jurisdiction.lookup("CO", county_fips="08031", place_fips="0820000"),
        property_label="200 E Colfax Ave",
    )
    assert "Resolved to" not in out
    assert "None" not in out


def test_a_county_with_no_place_still_names_the_county() -> None:
    """Unincorporated is an ANSWER, not a failure (resolve_stack's own rule):
    ``place_fips=None`` means county/state law governs, and the county is still
    the thing the overlay would have been selected by."""
    out = jurisdiction.format_compact(
        jurisdiction.lookup("GA", county_name="Fulton County"),
        property_label="1 Garden Way",
    )
    assert "- Resolved to: Fulton County (GA)" in out


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_name_is_stored_as_none_not_as_an_empty_string(blank: str) -> None:
    answer = jurisdiction.lookup("CO", county_name=blank, place_name=blank)
    assert answer.county_name is None and answer.place_name is None
    assert "Resolved to" not in jurisdiction.format_compact(answer, property_label="x")


# ── the overlay layer, which had never matched anything ───────────────


def test_the_shipped_overlays_are_keyed_by_name_and_nothing_else() -> None:
    """The measurement the whole fix rests on, re-run every CI run.

    If a future entry arrives carrying a FIPS, this fails and the next reader
    has to decide deliberately whether the production call site supplies the
    key that entry needs — rather than adding a second silently-dead entry.
    """
    entries = _overlay_entries()
    assert len(entries) == 17, "overlay count changed — re-check reachability"
    keyed_by_name = [e for _, e in entries if str(e.get("name") or "").strip()]
    keyed_by_fips = [
        e for _, e in entries if str(e.get("place_fips") or e.get("fips") or "").strip()
    ]
    assert len(keyed_by_name) == 17
    assert keyed_by_fips == [], (
        "an overlay entry now carries a FIPS key — that arm of _matches_place is "
        "reachable from production (the tool passes place_fips and county_fips), "
        "but confirm which key this entry expects before trusting it"
    )


def test_every_overlay_entry_is_reachable_by_a_key_production_actually_supplies() -> None:
    """A guard on a data file whose failure mode is 'silently matches nothing'
    (gotcha 45's mirror: the loud failure is the one you would have noticed)."""
    unreachable = [
        (state, entry)
        for state, entry in _overlay_entries()
        if not str(entry.get("name") or "").strip()
        and not str(entry.get("place_fips") or entry.get("fips") or "").strip()
    ]
    assert unreachable == []


#: Overlay names that are NOT a bare municipality name, so whether Census
#: returns that exact spelling for a point inside them is **UNPROBED** —
#: geocoding.geo.census.gov is egress-blocked from the build container (the
#: same wall gotcha 75 records for twilio.com), so this cannot be settled here
#: and a guessed spelling encoded as fact is exactly what gotcha 79 warns
#: against. These four stay as dead as they are today; the other thirteen go
#: live. THE STANDING CHECK is one query against our own mirror once the
#: widened backfill has run:
#:
#:     select distinct place_name from public.helixis_properties
#:      where place_name is not null;
#:
#: which returns the real Census spellings, at which point each of these is
#: settled by evidence rather than recall.
_SPELLING_UNPROBED = {
    ("NY", "New York City"): "Census names the incorporated place, not the city government",
    ("MD", "Baltimore City"): "'City' may be part of the COUNTY-equivalent name, not the place",
    ("ME", "Portland (ME)"): "the '(ME)' is OUR disambiguator against Portland OR, not Census's",
    ("DC", "District-wide"): "DC is a state-equivalent; no Census place is named this",
}


def test_the_unprobed_spellings_list_has_no_stale_entries() -> None:
    """An allowlist needs a staleness test or it reads as more permissive than
    the data is — gotcha 63's exemption caught its own author the same way."""
    live = {(state, str(entry.get("name") or "")) for state, entry in _overlay_entries()}
    stale = sorted(key for key in _SPELLING_UNPROBED if key not in live)
    assert stale == [], f"these overlay entries no longer exist: {stale}"


def test_thirteen_overlays_become_live_and_four_stay_unprobed() -> None:
    named = {(state, str(entry.get("name") or "")) for state, entry in _overlay_entries()}
    assert len(named - set(_SPELLING_UNPROBED)) == 13


def test_a_place_name_is_what_makes_an_overlay_match_at_all() -> None:
    """THE REGRESSION DETECTOR. Both directions in one test, because the
    failing one alone is satisfied by an overlay layer that matches nothing.

    The FIPS-only call is not a hypothetical — it is byte-for-byte what the
    shipped ``_jurisdiction_lookup_impl`` passed for a Denver property before
    2026-09-10, and it returned no overlay for a city that ships one.
    """
    fips_only = jurisdiction.lookup("CO", county_fips="08031", place_fips="0820000")
    assert fips_only.overlay_places == []

    with_name = jurisdiction.lookup(
        "CO", county_fips="08031", place_fips="0820000", place_name="Denver"
    )
    assert [e["name"] for e in with_name.overlay_places] == ["Denver"]

    out = jurisdiction.format_compact(with_name, property_label="200 E Colfax Ave")
    assert "⚠ Denver: Residential rental license required" in out


def test_the_overlay_match_is_case_insensitive_and_ignores_surrounding_space() -> None:
    answer = jurisdiction.lookup("CO", place_name="  denver  ")
    assert [e["name"] for e in answer.overlay_places] == ["Denver"]


def test_a_place_name_from_another_state_cannot_pull_in_that_states_overlay() -> None:
    """The overlay block is keyed by state before any name is compared, which
    is what keeps Portland ME and Portland OR apart."""
    assert jurisdiction.lookup("ME", place_name="Portland").overlay_places == []
    assert [e["name"] for e in jurisdiction.lookup("OR", place_name="Portland").overlay_places] == [
        "Portland"
    ]


# ── the call sites (a helper nothing calls is the gotcha-62 failure) ───


def _call_keywords(source: str, func_name: str, callee_tail: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name != func_name:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            tail = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if tail == callee_tail:
                found |= {kw.arg for kw in call.keywords if kw.arg}
    return found


def test_the_tool_hands_both_names_to_lookup() -> None:
    """A behavioural test of ``lookup`` cannot see a caller that ignores it —
    which is precisely how this shipped (gotchas 62/66/69, three separate
    instances of a well-tested helper a route quietly bypassed)."""
    kwargs = _call_keywords(
        _MIRROR_SRC.read_text(encoding="utf-8"), "_jurisdiction_lookup_impl", "lookup"
    )
    assert {"county_name", "place_name"} <= kwargs


def test_the_tool_actually_selects_the_name_columns() -> None:
    """Passing ``row["county_name"]`` is worth nothing if the SELECT never asks
    for it — the two halves are separate boundaries (gotcha 83)."""
    src = _MIRROR_SRC.read_text(encoding="utf-8")
    select = next(
        s for s in _sql_literals(src) if "helixis_properties" in s and "state_fips" in s.lower()
    )
    lowered = select.lower()
    assert "county_name" in lowered and "place_name" in lowered


def _sql_literals(source: str) -> list[str]:
    """Every string literal in the module, f-strings reconstructed as templates.

    ⚠ ``ast.walk`` reaches a ``JoinedStr``'s inner ``Constant``s too, at the
    same lineno, so the fragments must be skipped explicitly or a template is
    judged twice and the fragment verdict silently wins (gotcha 63).
    """
    tree = ast.parse(source)
    inner: set[int] = set()
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                inner.add(id(part))
            out.append(
                "".join(
                    p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "{}"
                    for p in node.values
                )
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inner:
            out.append(node.value)
    return out


# ── the backfill that fills those columns in ──────────────────────────


def test_the_persist_writes_both_names() -> None:
    sql = intel_backfill._JURISDICTION_UPDATE_SQL.lower()
    assert "county_name = $6" in sql and "place_name = $7" in sql


def test_the_worklist_re_resolves_rows_that_have_a_county_and_no_name() -> None:
    """Migration 0099 CANNOT be backfilled — a FIPS code is not a name and this
    product bundles no gazetteer — so the work-list is the only thing that ever
    fills these in. Its sentinel had to widen or the columns stay NULL forever
    on every property already resolved (gotcha 93's once-only sentinel, one
    table over)."""
    sql = " ".join(intel_backfill._JURISDICTION_PENDING_SQL.lower().split())
    assert "p.state_fips is null" in sql
    assert "p.county_fips is not null and p.county_name is null" in sql


def test_never_resolved_properties_sort_ahead_of_name_refreshes() -> None:
    """Both arms share ONE work-list under a LIMIT. Without the leading sort a
    company with many name-less rows spends its whole budget on properties that
    already answer while a brand-new property waits behind them — a staleness
    fix starving the case it was built for (gotcha 93)."""
    sql = " ".join(intel_backfill._JURISDICTION_PENDING_SQL.lower().split())
    order = sql.index("order by")
    assert sql[order:].startswith("order by (p.state_fips is not null)")


def test_resolve_stack_returns_the_names_the_persist_now_keeps() -> None:
    """Reads BOTH sides (gotcha 28): the producer's fields and the consumer's
    parameters. A rename on either side that skipped the other would otherwise
    write NULLs forever with nothing failing."""
    fields = jurisdiction.JurisdictionStack.__dataclass_fields__
    assert {"county_name", "place_name"} <= set(fields)
