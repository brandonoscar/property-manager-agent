"""A value bound to ``$N::timestamptz`` / ``$N::date`` must be a date, not a string.

⚠ **THIS WAS LIVE.** ``GET /api/v1/activity`` typed its keyset ``cursor`` as
``str``, emitted ``next_cursor`` as ``datetime.isoformat()``, and bound the
value the client handed back to ``created_at < $N::timestamptz``. asyncpg
refuses that — so every "Load more" click on the Activity page returned a 500
(Sentry PYTHON-FASTAPI-3G; a real user, Lititz, Chrome 151). The route catches
``UndefinedTableError`` and ``UndefinedColumnError`` and degrades gracefully;
``DataError`` is neither, so it escaped as an unhandled 500.

The same shape sat in two more places, and what each one's coverage was doing
is the more useful half.

- ``admin._fetch_null_surface_rows``'s ``since`` **had a test, and the test
  asserted the defect**: ``assert pool.fetch.await_args.args[2] ==
  "2026-05-16T00:00:00Z"`` — i.e. that a ``str`` reaches the bind. Its bare
  ``MagicMock`` pool accepted it, so the endpoint's one piece of coverage held
  the bug in place while reading as proof it worked.
- ``compliance_sweep``'s ``started_before`` **was exercised only at its
  default**, ``None``, so the temporal parameter never carried a value in any
  test — the bind was reachable and untested (gotcha 35's shape, one argument
  wide).

⚠ I first wrote here that *neither* had ever been exercised. That was wrong:
``test_pr82_backfill_endpoint.py`` does not carry "admin" or "backfill" in the
name I grepped for, and I read the empty result as an absence instead of as a
claim to check (gotchas 49/100/101/114, recorded four times and re-made here).
The full-suite baseline diff is what surfaced it.

**The fix is declarative, not a parse.** ``AwareDatetime`` / ``date`` on the
route parameter makes FastAPI do the coercion and return a 422 naming the
field. Writing our own ``fromisoformat`` would be a second home for the rule
and a second place to forget it (gotchas 25/50/51 — remove the field, remove
the failure).

⚠ **AWARE, not bare ``datetime``.** asyncpg's ``timestamptz_encode`` ends in
``obj.astimezone(utc)`` (READ from ``pgproto/codecs/datetime.pyx``, not
recalled — gotcha 79), and ``astimezone()`` on a NAIVE value assumes the
*server's* local zone. So an offset-less cursor would not fail; it would
silently move the page boundary by the container's UTC offset, which is
gotcha 91's class arriving in pagination. Every cursor we emit carries an
offset, so demanding one costs nothing live.

Two halves, and each is blind to what the other sees:

- the **behavioural** half drives the real code through a fake pool that
  refuses what asyncpg refuses (``_pg_bind_contract``), because the original
  fake required the string the driver rejects and therefore held the defect in
  place while reading as coverage (gotchas 68/74/93/96/110);
- the **corpus** half reads every temporal bind in ``src/`` and fails on a
  site nobody has classified, because the failure mode here is "silently
  permits more" and a per-function test cannot see a FIFTH site written the
  old way (gotcha 45).
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from helixis.api.routes.admin import _BackfillRequest, _fetch_null_surface_rows
from helixis.api.routes.compliance import SweepIn
from helixis.detectors.compliance_sweep import sweep_leases

from ._pg_bind_contract import reject_unbindable, temporal_bind_positions

SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"


# ─────────────────────────────────────────────────────────────────────
# The contract itself — a guard nobody has pointed at a known input is
# a guard that can pass by matching nothing (gotchas 49/100/101).
# ─────────────────────────────────────────────────────────────────────


def test_the_contract_refuses_the_exact_value_production_refused() -> None:
    emitted = datetime(2026, 8, 27, 7, 46, 34, 797181, tzinfo=UTC).isoformat()
    with pytest.raises(TypeError, match=r"\$2"):
        reject_unbindable("SELECT 1 WHERE created_at < $2::timestamptz", ("co", emitted))


@pytest.mark.parametrize("value", [None, datetime(2026, 1, 1, tzinfo=UTC), date(2026, 1, 1)])
def test_the_contract_spares_what_asyncpg_accepts(value: Any) -> None:
    # BOTH directions. A contract that refuses everything passes the test
    # above and breaks every caller (gotchas 38/51).
    reject_unbindable("SELECT 1 WHERE created_at < $2::timestamptz", ("co", value))
    reject_unbindable("SELECT 1 WHERE d < $2::date", ("co", value))


def test_a_cast_on_a_column_carries_no_argument_to_check() -> None:
    # ``l.lease_to_date::date`` is real, correct SQL in this repo. A scanner
    # that flags it is one people switch off (gotchas 52/103/107), so the
    # pattern is anchored on the ``$N``.
    assert temporal_bind_positions("SELECT l.lease_to_date::date FROM l") == set()
    assert temporal_bind_positions("WHERE last_run_at::date < $2::date") == {2}


def test_a_cast_on_a_column_whose_name_ends_in_a_digit_is_not_a_bind() -> None:
    # ⚠ THE DISTINGUISHING INPUT, and it took two attempts to find.
    #
    # A mutation dropping the ``$`` anchor (``(\d+)::date``) survived the test
    # above, and survived a second cut using a quoted date literal — in both
    # the character before ``::`` is a letter or a quote, so the mutant
    # matched nothing and could not change any output. Per gotcha 58 the first
    # question is whether the mutant differs AT ALL, and the honest answer
    # needed a column name ending in a digit.
    #
    # ``address_line1`` is a real column in this schema, so this is not a
    # contrived shape: under the mutant, ``address_line1::date`` reads as
    # parameter position 1 and the contract then judges an argument that has
    # nothing to do with it — a guard firing on correct code, which is how a
    # guard gets switched off (gotchas 52/103/107). The anchor is what stops
    # that, so it is pinned here directly rather than left to a corpus that
    # happens not to contain the shape today.
    assert temporal_bind_positions("WHERE p.address_line1::date IS NULL") == set()
    assert temporal_bind_positions("WHERE d < '2026-01-01'::date") == set()
    # A real bind sitting beside one is still found.
    assert temporal_bind_positions("WHERE p.address_line1::date IS NULL OR d < $3::date") == {3}


def test_a_uuid_bind_is_deliberately_out_of_scope() -> None:
    # asyncpg's uuid codec DOES accept a str, so widening this would
    # manufacture a finding on correct code.
    reject_unbindable("WHERE company_id = $1::uuid", ("5ca1ab1e-0000-4000-8000-000000000004",))


# ─────────────────────────────────────────────────────────────────────
# The three real call paths.
# ─────────────────────────────────────────────────────────────────────


def _contract_pool(rows: list[dict[str, Any]] | None = None) -> Any:
    """A pool that refuses what asyncpg refuses and otherwise returns rows."""
    seen: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        reject_unbindable(sql, args)
        seen.append((sql, args))
        return list(rows or [])

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    pool.seen = seen
    return pool


@pytest.mark.asyncio
async def test_the_admin_backfill_window_binds_a_datetime() -> None:
    pool = _contract_pool()
    since = datetime(2026, 5, 16, tzinfo=UTC)
    await _fetch_null_surface_rows(pool, company_id="co", since=since)
    sql, args = pool.seen[-1]
    assert "$2::timestamptz" in sql
    assert args[1] is since


@pytest.mark.asyncio
async def test_the_admin_backfill_without_a_window_binds_nothing_temporal() -> None:
    # The no-``since`` branch runs a DIFFERENT statement. Testing only the
    # filtered branch would leave the common one unexercised.
    pool = _contract_pool()
    await _fetch_null_surface_rows(pool, company_id="co", since=None)
    sql, _ = pool.seen[-1]
    assert temporal_bind_positions(sql) == set()


def test_the_admin_backfill_refuses_a_window_it_cannot_place_on_the_clock() -> None:
    assert _BackfillRequest(since=None).since is None
    assert _BackfillRequest(since="2026-05-16T00:00:00Z").since == datetime(  # type: ignore[arg-type]
        2026, 5, 16, tzinfo=UTC
    )
    with pytest.raises(ValidationError):
        _BackfillRequest(since="not-a-timestamp")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        # Naive: asyncpg would read it in the container's zone and shift the
        # window by hours rather than failing.
        _BackfillRequest(since="2026-05-16T00:00:00")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_compliance_sweep_binds_a_date() -> None:
    conn = MagicMock()
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        reject_unbindable(sql, args)
        captured.append((sql, args))
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    started = date(2026, 1, 1)
    await sweep_leases(
        conn, "co", label="late-fee-cap", remediation="drop it", started_before=started
    )
    sql, args = captured[-1]
    assert "$5::date" in sql
    assert args[4] is started


def test_the_sweep_filter_takes_the_format_its_own_description_promises() -> None:
    body = SweepIn(label="late-fee-cap", remediation="x", started_before="2026-01-01")  # type: ignore[arg-type]
    assert body.started_before == date(2026, 1, 1)
    with pytest.raises(ValidationError):
        SweepIn(label="late-fee-cap", remediation="x", started_before="01/01/2026")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_sweep_date_survives_json_dumps_into_the_finding_evidence() -> None:
    """Typing the parameter fixed the bind and would have broken the PERSIST.

    ``persist_findings`` does ``json.dumps(f.evidence)``, and a
    ``datetime.date`` raises there — so the filter the operator asked for
    would have taken down the persisting endpoint the moment it was used. The
    two live in different modules, so the working test selection for this
    change would never have shown it.

    ⚠ Asserted on the OUTPUT, not on the source text. A ``"str(started_before)"
    in src`` check is a substring guard that ``ruff format`` or a rename walks
    straight past (gotcha 40, which this repo has now hit inside four separate
    guards written to stop it).
    """
    import json

    conn = MagicMock()

    async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        reject_unbindable(sql, args)
        return [
            {
                "lease_id": 4001,
                "property_id": 77,
                "unit_id": 12,
                "unit_number": "4B",
                "rent": 1000.0,
                "lease_from_date": date(2025, 1, 1),
                "lease_to_date": date(2026, 12, 31),
            }
        ]

    conn.fetch = AsyncMock(side_effect=fetch)
    findings = await sweep_leases(
        conn, "co", label="late-fee-cap", remediation="x", started_before=date(2026, 1, 1)
    )
    evidence = findings[0].evidence
    json.dumps(evidence)  # the assertion: this is what persist_findings does
    assert evidence["filters"]["started_before"] == "2026-01-01"


# ─────────────────────────────────────────────────────────────────────
# The corpus half.
# ─────────────────────────────────────────────────────────────────────

#: Every temporal bind in ``src/``, keyed by file, with the EXACT casts that
#: file is licensed to contain and what makes each correct. An entry is a
#: LICENCE, so it names the evidence rather than just the file.
#:
#: ⚠ **PINNED TO THE CASTS, NOT JUST THE FILE, and a mutation is why.** The
#: first cut classified per FILE, so adding a SIXTH temporal bind to
#: ``compliance_sweep.py`` — a file already on the list — passed every check.
#: That is gotcha 118's granularity bound exactly: a file-level corpus guard
#: cannot see a new bad instance added to a file that already carries the
#: right strings. Pinning the cast SET makes a new bind loud while leaving an
#: unrelated edit to the same SELECT silent, which is the difference between a
#: guard people keep and one they switch off (gotchas 52/103/107).
_CLASSIFIED: dict[str, tuple[frozenset[str], str]] = {
    "api/routes/activity.py": (
        frozenset({"${}::timestamptz"}),
        "cursor: AwareDatetime | None — the live defect (PYTHON-FASTAPI-3G); "
        "round-tripped end to end in test_activity_endpoint.py through a fake "
        "pool that now enforces this contract",
    ),
    "api/routes/admin.py": (
        frozenset({"$2::timestamptz"}),
        "_BackfillRequest.since: AwareDatetime | None — covered above by "
        "test_the_admin_backfill_window_binds_a_datetime",
    ),
    "detectors/compliance_sweep.py": (
        frozenset({"$5::date"}),
        "started_before: date | None — covered above by test_the_compliance_sweep_binds_a_date",
    ),
    "webhooks/standing_agents.py": (
        frozenset({"$2::date"}),
        "claim_task(..., now: datetime) — already correct before this change, "
        "and the control that proves this scan can tell right from wrong",
    ),
}

#: ⚠ ACCEPTS ``${}`` AS WELL AS ``$5``, and the live defect is the reason.
#: ``activity.py`` writes ``f"created_at < ${len(params) + 1}::timestamptz"``
#: — the position is COMPUTED, so the template renders ``${}::timestamptz``
#: and a ``\$\d+`` pattern reports the one file that was actually broken as
#: clean. My first cut did exactly that, and only the canary above
#: (``test_the_scan_finds_the_sites_it_is_meant_to_judge``) caught it.
#:
#: This is a DIFFERENT job from ``_pg_bind_contract.temporal_bind_positions``,
#: which reads the finished SQL at runtime where the hole is already a number
#: — it must keep requiring ``\$\d+`` because it has to resolve a position.
_TEMPORAL_SQL = re.compile(r"\$(?:\d+|\{\})::(?:timestamptz|timestamp|date)\b")


def _sql_literals(tree: ast.AST) -> list[str]:
    """Every string constant, and every f-string rendered as a template.

    ⚠ An f-string is a ``JoinedStr``, so an ``ast.Constant`` walk sees only the
    fragments BETWEEN the holes — and ``activity.py`` builds its cast inside an
    f-string. Gotcha 63, which cost that scanner two wrong verdicts.
    """
    out: list[str] = []
    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    inner.add(id(piece))
                    parts.append(piece.value)
                else:
                    inner.add(id(piece))
                    parts.append("{}")
                    for sub in ast.walk(piece):
                        inner.add(id(sub))
            out.append("".join(parts))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in inner:
                out.append(node.value)
    return out


def _temporal_binds_by_file() -> dict[str, frozenset[str]]:
    """``src``-relative path → the exact temporal casts that file contains."""
    found: dict[str, frozenset[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the repo does not hold one
            continue
        casts = {m.group(0) for lit in _sql_literals(tree) for m in _TEMPORAL_SQL.finditer(lit)}
        if casts:
            found[str(path.relative_to(SRC)).replace("\\", "/")] = frozenset(casts)
    return found


def test_the_scan_finds_the_sites_it_is_meant_to_judge() -> None:
    # A corpus scan that finds zero is a claim to check, not a result
    # (gotchas 49/100/101 — written down three times and re-made twice).
    sites = _temporal_binds_by_file()
    assert sites.get("api/routes/activity.py") == frozenset({"${}::timestamptz"}), sites
    assert len(sites) >= 3, sites


def test_every_temporal_bind_in_the_product_is_classified() -> None:
    sites = _temporal_binds_by_file()
    undeclared = {
        path: sorted(casts - _CLASSIFIED.get(path, (frozenset(), ""))[0])
        for path, casts in sites.items()
        if casts - _CLASSIFIED.get(path, (frozenset(), ""))[0]
    }
    assert not undeclared, (
        "These bind a value to a $N::timestamptz / $N::date parameter and nothing "
        "here says what type they hand it. asyncpg refuses a str there and the "
        "route sees an unhandled 500, which is what PYTHON-FASTAPI-3G was. Type "
        f"the parameter, cover it, then declare it here: {undeclared}"
    )


def test_no_classification_outlives_the_site_it_licenses() -> None:
    # A stale exemption reads as a licence somebody needs and makes the list
    # look more permissive than the code is (gotcha 63, which caught its own
    # author writing exactly this).
    sites = _temporal_binds_by_file()
    stale = {
        path: sorted(casts - sites.get(path, frozenset()))
        for path, (casts, _reason) in _CLASSIFIED.items()
        if casts - sites.get(path, frozenset())
    }
    assert not stale, f"declared but no longer present — re-point or remove: {stale}"


def test_the_scanner_reads_an_fstring_as_a_template() -> None:
    # The live defect's SQL is assembled in an f-string. A ``Constant``-only
    # walk reports this file as clean.
    tree = ast.parse('n = 1\nq = f"created_at < ${n}::timestamptz"\n')
    assert any(_TEMPORAL_SQL.search(lit) for lit in _sql_literals(tree))


def test_the_scanner_does_not_double_count_an_fstrings_fragments() -> None:
    tree = ast.parse('n = 1\nq = f"a {n} b"\n')
    assert _sql_literals(tree) == ["a {} b"]


# ─────────────────────────────────────────────────────────────────────
# The round trip the user actually performs.
# ─────────────────────────────────────────────────────────────────────


def test_a_cursor_we_emit_is_a_cursor_we_accept() -> None:
    """``next_cursor`` is produced by ``isoformat()`` and consumed by pydantic.

    The two are different libraries agreeing by convention, which is exactly
    the producer/consumer boundary gotcha 28 says to test from BOTH sides.
    """
    from pydantic import TypeAdapter

    from helixis.api.routes.activity import ActivityRow

    emitted = datetime.now(UTC).replace(microsecond=797181).isoformat()
    parsed = TypeAdapter(datetime).validate_python(emitted)
    assert parsed.tzinfo is not None
    assert parsed.isoformat() == emitted
    # And the field the client reads it off really is that string.
    assert ActivityRow.model_fields["created_at"].annotation is str


def test_an_hour_of_drift_would_be_visible_not_silent() -> None:
    """A naive cursor is refused rather than read in the server's zone."""
    from pydantic import AwareDatetime, TypeAdapter

    aware = TypeAdapter(AwareDatetime)
    assert aware.validate_python("2026-08-27T07:46:34+00:00") == datetime(
        2026, 8, 27, 7, 46, 34, tzinfo=UTC
    )
    with pytest.raises(ValidationError):
        aware.validate_python("2026-08-27T07:46:34")
    # Not a formality: naive and aware readings of the same wall clock are a
    # whole offset apart, so the boundary would move by hours.
    assert datetime(2026, 8, 27, 7, 46, 34, tzinfo=UTC) - timedelta(hours=7) != datetime(
        2026, 8, 27, 7, 46, 34, tzinfo=UTC
    )
