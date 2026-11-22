"""Every column a Rentvine mapper writes must carry a type its column accepts.

⚠ **THE SHIPPED SYNC ABORTED ON ITS FIRST LEASE AND A FULL SUITE WAS GREEN.**
``map_lease`` put ``'2026-03-18'`` — a ``str``, exactly what the spec declares
(``{"type": "string", "format": "date"}``) — into
``helixis_leases.lease_from_date``, a ``timestamptz``. ``sync._upsert`` binds it
with NO cast, so Postgres types the parameter from the INSERT target and asyncpg
encodes it with ``timestamptz_encode``, which takes a ``datetime.date`` or
``None`` and nothing else. ``_sync_leases`` catches only ``RentvineAPIError``, so
the ``DataError`` propagated out of ``sync_company`` — and ``_sync_units`` runs
on the NEXT line, so leases and units both stayed empty with no ``SyncResult``
ever returned to say which part of the mirror was stale.

Measured with a control before anything was changed: properties (same code path,
no temporal column) wrote fine, the sync raised, the units endpoint was never
reached.

**WHY THIS IS A CORPUS GUARD AND NOT TWO ASSERTIONS.** The per-field tests
beside it pin ``lease_from_date`` and ``market_rent``. Neither can see the
EIGHTH field somebody adds to a mapper next year, and the failure mode is
"silently permits more" (gotcha 45) — the sync does not warn, it dies at
runtime against a database no test in this repo can reach.

⚠ **THE SAMPLE VALUES COME FROM THE CAPTURED SPEC, and an unknown key defaults
to a STRING.** A guard that guesses the benign shape passes for free: my first
hand-scan fed ``rent`` where ``map_lease`` reads ``rentAmount``, so that field
was never populated and the scan reported clean. Where the spec is silent, the
pessimistic assumption is the one that would break a typed column.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from helixis.rentvine import mapper, sync

from ._pg_bind_contract import MIRROR_COLUMN_TYPES, reject_untyped_mirror_bind

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs/rentvine/vendor/docs.rentvine.com/openapi.json"

#: Where the mirror's columns are actually declared.
#:
#: ⚠ **BOTH, and that is the whole point.** A scan reading only
#: ``db/schema/helixis_entities.sql`` reports a clean corpus while missing
#: ``market_rent`` outright — that column is created in
#: ``supabase/migrations/0038_entity_mirror_cockpit.sql``. One file read, a
#: green result, and a real defect underneath it.
SCHEMA_SOURCES = (REPO / "src/helixis/db/schema", REPO / "supabase/migrations")


#: A column definition, inside a ``CREATE TABLE`` or added by an ``ALTER``.
#:
#: ⚠ **BOTH FORMS, and the ALTER half was a real miss in the first cut.**
#: ``helixis_leases.security_deposit`` is added by
#: ``0079_leases_security_deposit.sql``, never named in any ``CREATE TABLE``, so
#: a scan reading only table bodies reports it as not a column at all. Same
#: shape as reading one schema FILE and missing ``market_rent`` — the mirror is
#: defined across a corpus, not in one place.
#:
#: ⚠ **``(?!\[)`` is load-bearing.** ``property_ids bigint[]`` is an ARRAY, and
#: without the lookahead ``\b`` matches before the bracket and the column reads
#: as a scalar ``bigint`` — which flagged ``map_owner``'s perfectly correct
#: ``[]`` on this guard's first run. A scan that matches too broadly is as
#: useless as one that matches too little (gotcha 103), and a guard that fails
#: against correct code is the one people switch off (gotchas 111/124).
_COLUMN_DEF = re.compile(
    r"^\s*(?:add\s+column\s+(?:if\s+not\s+exists\s+)?)?(\w+)\s+"
    r"(timestamptz|timestamp|date|numeric|bigint|integer|boolean)\b(?!\s*\[)",
    re.IGNORECASE,
)


def _declared_column_types() -> dict[str, str]:
    """Column name -> declared Postgres type, across every schema source."""
    found: dict[str, str] = {}
    for root in SCHEMA_SOURCES:
        for path in sorted(root.glob("*.sql")):
            for line in path.read_text().splitlines():
                m = _COLUMN_DEF.match(line)
                if m and m.group(1).lower() not in {"primary", "unique", "constraint", "alter"}:
                    found.setdefault(m.group(1), m.group(2).lower())
    return found


def _keys_each_mapper_reads() -> dict[str, set[str]]:
    """The wire keys each ``map_*`` reads, from its OWN ``obj.get(...)`` calls.

    Derived rather than hand-listed, because a hand-listed sample is how the
    first scan came back clean: it fed keys the mappers do not read.
    """
    tree = ast.parse(Path(mapper.__file__).read_text())
    out: dict[str, set[str]] = {}
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("map_")):
            continue
        keys: set[str] = set()
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
        out[fn.name] = keys
    return out


def _spec_types() -> dict[str, str]:
    """Wire key -> the JSON type the captured spec declares for it."""
    schemas = json.loads(SPEC.read_text()).get("components", {}).get("schemas", {})
    types: dict[str, str] = {}
    for schema in schemas.values():
        for key, prop in (schema.get("properties") or {}).items():
            declared = prop.get("type")
            if isinstance(declared, list):  # e.g. ["string", null] — nullable
                declared = next((t for t in declared if isinstance(t, str)), None)
            if isinstance(declared, str):
                types.setdefault(key, declared)
    return types


def _sample(keys: set[str], spec: dict[str, str]) -> dict[str, Any]:
    """A record shaped the way the SPEC says the server sends it."""
    row: dict[str, Any] = {}
    for key in keys:
        declared = spec.get(key, "string")  # unknown ⇒ assume what breaks
        if declared == "integer":
            row[key] = 1
        elif declared == "number":
            row[key] = 1800
        elif declared == "boolean":
            row[key] = True
        elif re.search(r"date", key, re.IGNORECASE):
            row[key] = "2026-03-18"
        else:
            row[key] = "1800.00" if re.search(r"rent|amount|deposit", key, re.I) else "X"
    return row


def _accepts(pg_type: str, value: Any) -> bool:
    """What asyncpg accepts for that column type. See ``_pg_bind_contract``."""
    if value is None:
        return True
    if pg_type in {"timestamptz", "timestamp", "date"}:
        return isinstance(value, date)
    if pg_type == "numeric":
        return isinstance(value, (Decimal, int, float)) and not isinstance(value, bool)
    if pg_type in {"bigint", "integer"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if pg_type == "boolean":
        return isinstance(value, bool)
    return True


# ── the guard ────────────────────────────────────────────────────────────────


def test_the_scan_can_see_the_columns_it_judges() -> None:
    """⚠ The canary, without which every assertion below passes for free.

    A bad glob, a renamed schema directory or a regex that stops matching all
    read as "no mismatches" — a clean result is a claim to check, not a result
    (gotchas 49/100/101/114/128). The two named here are the ones that were
    genuinely wrong, and they live in DIFFERENT files on purpose.
    """
    declared = _declared_column_types()
    assert declared.get("lease_from_date") == "timestamptz", declared.get("lease_from_date")
    assert declared.get("market_rent") == "numeric", declared.get("market_rent")
    # Added by an ALTER, never named in a CREATE TABLE — the form the first cut
    # of this scan could not see at all.
    assert declared.get("security_deposit") == "numeric", declared.get("security_deposit")
    # An ARRAY column, which must NOT read as its scalar element type.
    assert "property_ids" not in declared, "bigint[] read as bigint — the lookahead broke"
    assert len(declared) > 20, f"only {len(declared)} typed columns found — check the glob"
    # Reading a missing spec raises FileNotFoundError, which fails here (or is
    # reported as skipped where the spec is deliberately not shipped).
    assert len(_spec_types()) > 50, "the spec parse found almost nothing"


@pytest.mark.parametrize("fn_name", sorted(_keys_each_mapper_reads()))
def test_every_mapped_column_matches_its_declared_type(fn_name: str) -> None:
    declared = _declared_column_types()
    spec = _spec_types()
    keys = _keys_each_mapper_reads()[fn_name]
    fn = getattr(mapper, fn_name)
    # `map_lease_balance` (2026-10-01) takes the COMPANY's date for aging; a
    # fixed one keeps the sample deterministic.
    extra = (date(2026, 10, 1),) if "today" in inspect.signature(fn).parameters else ()
    out = fn(_sample(keys, spec), *extra)
    # `map_lease_tenant_links` returns one row per tenant (2026-10-01); every
    # row it returns is held to the same column types as a single-row mapper.
    rows = out if isinstance(out, list) else [out]

    wrong = [
        (col, declared[col], type(val).__name__, repr(val))
        for row in rows
        for col, val in row.items()
        if col != "raw" and col in declared and not _accepts(declared[col], val)
    ]
    assert not wrong, f"{fn_name} writes a type its column refuses: {wrong}"


def test_the_typed_columns_are_actually_exercised() -> None:
    """⚠ A mapper whose sample leaves the typed column ``None`` proves nothing.

    ``None`` is acceptable to every column, so the parametrized test above is
    vacuous for any field the sample failed to populate — which is exactly how
    the first hand-scan missed ``rentAmount``. This asserts the sample really
    does reach the columns that were broken.
    """
    spec = _spec_types()
    keys = _keys_each_mapper_reads()
    lease = mapper.map_lease(_sample(keys["map_lease"], spec))
    unit = mapper.map_unit(_sample(keys["map_unit"], spec))
    assert isinstance(lease["lease_from_date"], date), lease["lease_from_date"]
    assert isinstance(lease["lease_to_date"], date), lease["lease_to_date"]
    assert isinstance(unit["market_rent"], Decimal), unit["market_rent"]


def test_every_mirror_column_the_contract_names_is_a_real_column() -> None:
    """The other direction: a stale entry in ``MIRROR_COLUMN_TYPES`` is a
    licence nobody needs, and it makes the fake pool read stricter than it is
    (gotcha 63's stale-allowlist rule)."""
    declared = _declared_column_types()
    for col, expected in MIRROR_COLUMN_TYPES.items():
        assert declared.get(col) == expected, f"{col}: schema says {declared.get(col)!r}"


def test_the_contract_covers_every_typed_column_the_sync_writes() -> None:
    """⚠ The fake pool can only refuse what ``MIRROR_COLUMN_TYPES`` names, so a
    newly-synced typed column that is missing from it is a column the sync tests
    cannot check — the inverted fake returning by the back door."""
    declared = _declared_column_types()
    written = {c for cols in sync.TABLE_COLUMNS.values() for c in cols}
    typed = {
        c for c in written if declared.get(c) in {"timestamptz", "timestamp", "date", "numeric"}
    }
    missing = typed - set(MIRROR_COLUMN_TYPES)
    assert not missing, f"the sync writes these typed columns unguarded: {sorted(missing)}"


# ── the two defects, pinned directly ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-18", date(2026, 3, 18)),
        # A full ISO timestamp is accepted even though the spec says `date` —
        # costs nothing, and is the difference between a working sync and a
        # dead one if any endpoint sends one.
        ("2026-03-18T00:00:00Z", date(2026, 3, 18)),
        ("2026-03-18T14:32:00+00:00", date(2026, 3, 18)),
        (datetime(2026, 3, 18, 14, 32), date(2026, 3, 18)),
        (date(2026, 3, 18), date(2026, 3, 18)),
        # `lease.endDate` is declared `["string", null]` — nullable on the wire.
        (None, None),
        ("", None),
        ("not a date", None),
        (True, None),
    ],
)
def test_a_lease_date_becomes_a_date_or_nothing(raw: Any, expected: date | None) -> None:
    assert mapper._date(raw) == expected


def test_an_unreadable_date_costs_the_field_and_not_the_sync() -> None:
    """⚠ Both directions in one assertion (gotchas 38/51).

    Returning ``None`` is only safe because the row SURVIVES: a lease with an
    unreadable term still has a tenant, a unit and a rent worth mirroring, and
    one bad field must never cost the two tables that sync after it.
    """
    row = mapper.map_lease(
        {"leaseID": 7, "propertyID": 1, "unitID": 2, "startDate": "garbage", "rentAmount": 1800}
    )
    assert row["lease_from_date"] is None
    assert row["buildium_id"] == 7, "the row was dropped over one field"
    assert row["rent"] == Decimal("1800")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1800.00", Decimal("1800.00")),
        (1800, Decimal("1800")),
        (1800.5, Decimal("1800.5")),
        (Decimal("1800.00"), Decimal("1800.00")),
        (None, None),
        ("", None),
        ("not money", None),
        (True, None),
    ],
)
def test_money_becomes_a_decimal_or_nothing(raw: Any, expected: Decimal | None) -> None:
    assert mapper._money(raw) == expected


def test_money_does_not_go_through_float() -> None:
    """``numeric`` exists to avoid binary fractions; ``float`` reintroduces them."""
    assert str(mapper._money("1800.10")) == "1800.10"
    assert mapper._money("0.1") + mapper._money("0.2") == Decimal("0.3")


def test_a_timestamp_shaped_date_still_parses() -> None:
    """The calendar part is taken from whatever ISO form arrives.

    ⚠ The mutation that proves this is ``text[:10]`` -> ``text``, NOT deleting
    a ``datetime.fromisoformat`` arm — that arm existed, could not change the
    answer for any input, and was removed rather than pinned (gotcha 58). See
    ``mapper._date``'s docstring for the measurement.
    """
    assert mapper._date("2026-03-18T00:00:00Z") == date(2026, 3, 18)
    assert mapper._date("2026-03-18T14:32:00+00:00") == date(2026, 3, 18)


# ── the safety net under the fixtures ────────────────────────────────────────
#
# ⚠ These three guards are worth their own inputs BECAUSE they only fire when
# something else is already broken. With the mapper correct, deleting any of
# them changes no outcome anywhere — so a mutation battery scores them ALIVE
# and reads as "weak guard" when the real answer is "no test supplied the one
# input on which the two versions differ" (gotchas 38/51/58).


def test_the_contract_refuses_what_the_driver_refuses() -> None:
    insert = (
        "INSERT INTO helixis_leases (buildium_id, company_id, lease_from_date, rent) "
        "VALUES ($1, $2, $3, $4)"
    )
    with pytest.raises(TypeError, match="lease_from_date"):
        reject_untyped_mirror_bind(insert, (1, "c", "2026-03-18", Decimal("1800")))
    with pytest.raises(TypeError, match="rent"):
        reject_untyped_mirror_bind(insert, (1, "c", date(2026, 3, 18), "1800.00"))


def test_the_contract_is_silent_on_what_the_driver_accepts() -> None:
    """⚠ The other direction, or a contract that refused everything would pass
    the test above perfectly while failing every correct sync."""
    insert = (
        "INSERT INTO helixis_leases (buildium_id, company_id, lease_from_date, rent) "
        "VALUES ($1, $2, $3, $4)"
    )
    reject_untyped_mirror_bind(insert, (1, "c", date(2026, 3, 18), Decimal("1800")))
    reject_untyped_mirror_bind(insert, (1, "c", None, None))  # NULL is always fine
    reject_untyped_mirror_bind("SELECT 1 FROM helixis_leases WHERE x = $1", ("anything",))


def test_the_sync_fake_pool_actually_calls_the_contract() -> None:
    """⚠ A behavioural test cannot see a fixture that stops enforcing.

    ``_fake_pool`` was a bare ``AsyncMock`` accepting everything, which is what
    let the date defect pass a full suite. Removing the call again restores the
    inverted fake and no assertion in this repo would notice
    (gotchas 62/66/69/103/121/136/138).
    """
    tree = ast.parse((Path(__file__).parent / "test_rentvine_sync.py").read_text())
    fake = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fake_pool"),
        None,
    )
    assert fake is not None, "_fake_pool was renamed — re-point this guard"
    called = {
        n.func.id
        for n in ast.walk(fake)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "reject_untyped_mirror_bind" in called
    assert "reject_unbindable" in called


def test_an_unknown_wire_key_defaults_to_the_shape_that_breaks() -> None:
    """⚠ Supplied directly, because no mapper currently reads a key the spec
    omits — so the default is real, correct, and unobservable through the
    corpus. A benign default would make every check above pass for free on the
    first field somebody adds ahead of the spec.
    """
    assert _sample({"someFieldTheSpecDoesNotDeclare"}, {}) == {
        "someFieldTheSpecDoesNotDeclare": "X"
    }
    assert _sample({"weirdRentThing"}, {}) == {"weirdRentThing": "1800.00"}
