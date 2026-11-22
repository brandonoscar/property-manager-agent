""" "Today" is a per-company fact, and the product used to answer it three ways.

Only one of the three was about the customer. `agents/clock_prompt` produced a
LOCAL date and said why in its own docstring — *"A property manager's 'today' is
local: a move-out dated 2026-09-30 is a different lease month depending on which
side of midnight you compute it from"* — while `reminders/producers` computed
`date.today()`, which in the deployed container is UTC. No `TZ` is set in
`render.yaml`, the Dockerfile or any workflow; verified, not assumed.

So the rule was written down in one lane and never reached the other. That is
gotcha 25's shape, and the FIFTH instance of the class gotchas 68/69/72/73
record: a deployment-global value that is really a per-company fact, invisible
until a second customer exists somewhere else (coverage checklist #183/#344).

MEASURED, hours per day on which the server's date is not the manager's:
New York 4 · Chicago 5 · Denver 6 · Los Angeles 7 · Anchorage 8 · Honolulu 10 —
17% to 42% of every day. What that bought on the three REGISTERED producers: a
bill due TODAY reported OVERDUE every evening (a false claim about money, which
is gotcha 26's class pointed at the thing a PM acts on first), `days_over` one
day high so `severity="high" if days_over >= 7` flipped early, and the 90/60/30
renewal ladder flipping early. Two more are dark and are hardening rather than
an incident: `deposit_return_clock` (a STATUTORY deadline) and `rent_missing`.

⚠ **The producers' own tests cannot cover this and never could.** They pass
`today=dt.date.today()`, which is what they always effectively used — a test
computing the answer the same way the code does proves the two agree and passes
just as happily against a function that ignores the zone (`clock_prompt`'s own
reasoning about injectable clocks). Everything here pins a FIXED instant.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.common.company_clock import (
    DEFAULT_TIMEZONE,
    company_timezone,
    resolve_zone,
    today_in,
)

_SRC = Path("src/helixis")
_MIGRATIONS = Path("supabase/migrations")

#: 02:00 UTC on the 10th is still the EVENING OF THE 9TH everywhere west of
#: Greenwich — the exact window the defect lived in, and the reason a fixed
#: instant is the only thing that can distinguish the two implementations.
_LATE_UTC = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)


# ── the date itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("America/New_York", dt.date(2026, 9, 9)),
        ("America/Chicago", dt.date(2026, 9, 9)),
        ("America/Denver", dt.date(2026, 9, 9)),
        ("America/Los_Angeles", dt.date(2026, 9, 9)),
        ("Pacific/Honolulu", dt.date(2026, 9, 9)),
        # ...and a zone EAST of UTC, so the test cannot pass by always
        # subtracting a day. Tokyo is already the 10th at 02:00 UTC.
        ("Asia/Tokyo", dt.date(2026, 9, 10)),
    ],
)
def test_the_companys_date_is_not_the_servers(zone: str, expected: dt.date) -> None:
    assert today_in(zone, now=_LATE_UTC) == expected
    if expected != _LATE_UTC.date():
        assert today_in(zone, now=_LATE_UTC) != _LATE_UTC.date(), (
            "the server's UTC date and this company's date are the same value here, "
            "so this row cannot tell a timezone-aware implementation from a naive one"
        )


def test_a_bill_due_today_is_not_overdue_in_the_companys_evening() -> None:
    """⚠ The defect, stated as the claim a manager would have read.

    At 02:00 UTC on the 10th it is 20:00 on the 9th in Denver. A bill due the
    9th is due TODAY for that manager; `date.today()` said the 10th and the
    producer's `due < today` therefore reported it OVERDUE.
    """
    due = dt.date(2026, 9, 9)
    server_says = _LATE_UTC.date()
    company_says = today_in("America/Denver", now=_LATE_UTC)
    assert due < server_says, "the fixture no longer reproduces the defect"
    assert not due < company_says, "a bill due today is being reported overdue"


def test_a_naive_instant_is_read_as_utc_not_as_the_server_clock() -> None:
    """⚠ `astimezone` on a naive datetime silently assumes the SERVER's local
    zone — which is the bug this module exists to remove, re-entering through
    the argument. Reading it as UTC is the only interpretation that does not
    depend on where the container runs."""
    naive = _LATE_UTC.replace(tzinfo=None)
    assert today_in("America/Denver", now=naive) == today_in("America/Denver", now=_LATE_UTC)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ cannot be changed on this platform")
def test_the_naive_reading_does_not_depend_on_the_hosts_own_zone() -> None:
    """⚠ Found by MUTATION, and the mutation was ALIVE for an instructive
    reason: replacing the explicit `tzinfo=UTC` with a bare `astimezone()`
    changed nothing, because THIS container is UTC and so the wrong reading and
    the right one agreed. The property is real and simply was not observable by
    default — which is the shape of a guard that passes everywhere except the
    machine where it matters.

    So the host's zone is moved. Under `TZ=Pacific/Honolulu` a bare
    `astimezone()` would read the naive instant as 02:00 HAWAII, i.e. 12:00 UTC
    the same day, and land on a different date; reading it as UTC does not
    move. A developer running the suite on a laptop in another zone would
    otherwise get different answers from the same input.
    """
    naive = _LATE_UTC.replace(tzinfo=None)
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Pacific/Honolulu"
        time.tzset()
        assert datetime.now().astimezone().utcoffset() != dt.timedelta(0), (
            "the host zone did not actually move, so this test would pass for the "
            "wrong reason — the same false green the mutation exposed"
        )
        assert today_in("America/Denver", now=naive) == dt.date(2026, 9, 9)
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


# ── it never costs the sweep ─────────────────────────────────────────


@pytest.mark.parametrize("junk", ["", None, "Mars/Olympus_Mons", "EST5EDT-nonsense", "  "])
def test_an_unusable_zone_falls_back_instead_of_raising(junk: str | None) -> None:
    """A company that stored a typo must still get cards, on the date the
    product produced before this column existed. Raising would take out every
    OTHER company in the same sweep cycle — a failure far larger than the
    defect."""
    assert today_in(junk, now=_LATE_UTC) == today_in(DEFAULT_TIMEZONE, now=_LATE_UTC)
    assert resolve_zone(junk) is not None


def test_an_unreadable_ledger_returns_the_default_rather_than_raising() -> None:
    """⚠ The OPPOSITE posture to the consent ledger and the role floor
    (gotchas 55/72/89), on purpose. Those refuse because acting without the
    fact is the harm; here refusing IS the harm — the fallback is exactly the
    date the sweep produced before 0098, so a blip costs a customisation and
    raising would cost an Inbox."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("pool is gone"))
    assert asyncio.run(company_timezone(pool, "c1")) == DEFAULT_TIMEZONE


def test_a_missing_row_and_a_null_column_both_read_as_the_default() -> None:
    for row in (None, {"timezone": None}, {"timezone": ""}):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=row)
        assert asyncio.run(company_timezone(pool, "c1")) == DEFAULT_TIMEZONE


def test_a_stored_zone_is_actually_used() -> None:
    """The twin of the fallback tests. Without this they all pass against a
    function that ignores the column entirely and returns the default."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"timezone": "Pacific/Honolulu"})
    assert asyncio.run(company_timezone(pool, "c1")) == "Pacific/Honolulu"


# ── the two answers are one answer ───────────────────────────────────


def test_the_agent_prompt_and_the_producers_share_one_default() -> None:
    """⚠ `clock_prompt` used to hardcode its own "America/New_York". Two
    literals spelling the same zone is how they drift by one character and the
    product quietly holds two calendars again — gotcha 73's `"approved"` typed
    out in three modules."""
    src = (_SRC / "agents/clock_prompt.py").read_text(encoding="utf-8")
    assert "DEFAULT_TIMEZONE" in src
    assert '"America/New_York"' not in src, (
        "clock_prompt has its own copy of the zone again — it must import "
        "DEFAULT_TIMEZONE so the agent's date and the reminder date cannot diverge"
    )


def test_the_migration_default_is_the_same_zone_the_code_defaults_to() -> None:
    """⚠ Reads BOTH sides (gotcha 28). If 0098's column default and
    `DEFAULT_TIMEZONE` disagree, every existing company silently moves to a
    different calendar on the day the migration applies — the one outcome the
    migration's header promises cannot happen."""
    sql = (_MIGRATIONS / "0098_company_timezone.sql").read_text(encoding="utf-8")
    assert f"default '{DEFAULT_TIMEZONE}'" in sql, (
        f"0098 does not default the column to {DEFAULT_TIMEZONE!r}, so applying it "
        "changes what date existing companies see"
    )


# ── nobody reaches for the server clock ──────────────────────────────

#: Every remaining server-clock read, with the reason it is allowed. An entry
#: is a claim about a specific line, so it goes stale when the line moves —
#: which is the stale-entry test below doing its job (gotcha 63/68).
_SERVER_CLOCK_ALLOWED = {
    "geo/jurisdiction.py": (
        "`today or date.today()` — a FALLBACK on an injectable parameter. Both "
        "callers that hold a company inject it (reminders/producers and "
        "agents/mirror_tools); the fallback covers the pure-lookup path used by "
        "tests and by callers with no company in scope."
    ),
    "webhooks/mirror_context.py": (
        "`today or dt.date.today()` — the same injectable-fallback shape. The "
        "facts it dates are descriptive context for the planner rather than a "
        "deadline or an overdue claim."
    ),
    "webhooks/operational_signals.py": (
        "`snapshot.date() if ... else date.today()` — a LABEL for an aging "
        "snapshot whose own timestamp is missing. It dates a display line, not "
        "a claim the product acts on."
    ),
    "buildium/deprecations.py": (
        "`today or date.today()` in `retired_surface` — the same injectable-"
        "fallback shape, against a VENDOR's announced retirement date rather "
        "than any customer's calendar. It is REACTIVE by contract: a caller "
        "reaches it only after a Buildium call has already failed, so a few "
        "hours of zone slack changes the WORDING of that failure and can never "
        "change an answer. It lives here rather than at the call site because "
        "this allowlist is FILE-scoped and the caller is a 7,000-line tool "
        "module — exempting that would licence every future clock read in it."
    ),
}

#: Attribute chains that read the process's own clock with no zone attached.
#: `datetime.now(UTC)` is fine and must stay fine — the defect is the ABSENCE
#: of an argument, so the check is on the call's arity, not on the name.
_CLOCK_ATTRS = {"today", "utcnow", "now"}


def _server_clock_hits() -> dict[str, list[str]]:
    """Every zone-less clock read in `src/helixis`, by module.

    ⚠ **AST, not a line scan, and the reason is that the first cut failed.**
    A regex over stripped lines flagged this file's own docstring and
    `producers.py`'s header, because both QUOTE `date.today()` in order to
    explain why it is banned — gotcha 40/59's trap, arriving in exactly the
    shape those entries describe. The tempting repair is to strip docstrings
    too, and that is the step that eventually makes the guard blind. A
    docstring is an `ast.Constant` and never an `ast.Call`, so walking the tree
    needs no comment handling at all and cannot be fooled by `ruff format`
    wrapping a call across two lines either.
    """
    hits: dict[str, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _CLOCK_ATTRS:
                continue
            # `now(UTC)` / `now(tz=...)` are the CORRECT forms — an argument is
            # what makes the instant mean something. Only the bare call is a
            # server-clock read. `utcnow()` is always wrong (it returns a naive
            # value), so it fails regardless of how it is called.
            if node.args or node.keywords:
                if node.func.attr != "utcnow":
                    continue
            key = str(path.relative_to(_SRC)).replace("\\", "/")
            hits.setdefault(key, []).append(f"{node.lineno}: {ast.unparse(node)}")
    return hits


def test_no_new_module_computes_today_on_the_server_clock() -> None:
    """⚠ Reads the CORPUS, not the helper (gotcha 45). A test feeding inputs to
    `today_in` would pass forever while the next module quietly reopened the
    hole — the failure mode here is "silently permits more", which is the one
    you would never otherwise notice.

    ⚠ It scans STRIPPED CODE, so the prose above and in `producers.py` can
    quote `date.today()` in order to explain it without tripping the guard —
    gotcha 40/59's trap, which fires in exactly this shape.
    """
    unexplained = {
        f: lines for f, lines in _server_clock_hits().items() if f not in _SERVER_CLOCK_ALLOWED
    }
    assert not unexplained, (
        "these read the SERVER's clock, which in the deployed container is UTC and is "
        "not any customer's calendar:\n"
        + "\n".join(f"  {f}\n    " + "\n    ".join(v) for f, v in sorted(unexplained.items()))
        + "\n\nUse common/company_clock.today_in(await company_timezone(pool, company_id)), "
        "or add an entry to _SERVER_CLOCK_ALLOWED saying why this one is safe."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """An exemption that matches nothing is a licence nobody needs, and it
    makes the list read as more permissive than the code is (gotcha 63)."""
    hits = _server_clock_hits()
    stale = sorted(set(_SERVER_CLOCK_ALLOWED) - set(hits))
    assert not stale, f"these no longer read the server clock — drop their exemptions: {stale}"


def test_the_reminder_producers_carry_no_exemption() -> None:
    """⚠ The one module the guard exists for. Pinned separately so that
    exempting it later is a deliberate, visible edit rather than one more line
    in a growing allowlist."""
    assert not any(f.startswith("reminders/") for f in _SERVER_CLOCK_ALLOWED)
    assert not any(f.startswith("reminders/") for f in _server_clock_hits())


# ── every producer is handed the date ────────────────────────────────


def _producers_module() -> ast.Module:
    return ast.parse((_SRC / "reminders/producers.py").read_text(encoding="utf-8"))


def _registered_producer_names() -> set[str]:
    from helixis.reminders.producers import PRODUCERS

    return {fn.__name__ for fn in PRODUCERS.values()}


def test_every_producer_requires_the_date_as_a_keyword_argument() -> None:
    """⚠ This is the forcing function, and it is why two producers accept
    `today` without using it. A set of "date-aware" producers to opt into is
    the thing somebody forgets — gotcha 88's reasoning about the aging ladder,
    which the sweep now applies to the clock. A signature cannot be forgotten.
    """
    missing = []
    for node in ast.walk(_producers_module()):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
            continue
        if node.name not in _registered_producer_names() and node.name != "rent_missing":
            continue
        kwonly = {a.arg for a in node.args.kwonlyargs}
        if "today" not in kwonly:
            missing.append(node.name)
        else:
            idx = [a.arg for a in node.args.kwonlyargs].index("today")
            assert node.args.kw_defaults[idx] is None, (
                f"{node.name} gives `today` a DEFAULT — a default is how the server "
                "clock gets back in without anyone editing the call site"
            )
    assert not missing, f"these producers do not take the company's date: {missing}"


def test_the_sweep_hands_the_date_to_every_producer() -> None:
    """⚠ A behavioural test of `today_in` cannot see a sweep that stopped
    passing it — the producers would then fail loudly, but only because the
    argument is required, and a future default would make that silent instead.
    Gotchas 62/66/69 are three separate instances of a well-tested helper a
    caller quietly ignored."""
    src = (_SRC / "reminders/sweep.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "produce"
    ]
    assert calls, "the sweep no longer calls the producers through `produce`"
    for call in calls:
        assert "today" in {k.arg for k in call.keywords}, (
            f"a producer is invoked without the company's date: {ast.unparse(call)}"
        )
    assert "company_timezone" in src and "today_in" in src, (
        "the sweep no longer resolves the company's zone — it would be passing "
        "whatever `today` it found lying around"
    )


def test_the_sweep_resolves_the_zone_once_not_per_producer() -> None:
    """Not a style point: per-producer resolution is one row read per producer
    per company per cycle, and it is the shape that lets a new producer skip
    it. One binding, before the loop."""
    tree = ast.parse((_SRC / "reminders/sweep.py").read_text(encoding="utf-8"))
    resolutions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "today_in"
    ]
    assert len(resolutions) == 1, (
        f"the sweep resolves the company's date {len(resolutions)} times; it is one "
        "row per company per cycle and belongs before the producer loop"
    )


# ── every timestamp is timezone-aware (checklist #183) ───────────────

_COLUMN = re.compile(
    r'^\s*"?(?P<col>[a-z_]+)"?\s+'
    r"(?P<type>timestamptz|timestamp with time zone|timestamp without time zone|timestamp)\b",
    re.IGNORECASE | re.MULTILINE,
)


def test_every_timestamp_column_in_the_corpus_is_timezone_aware() -> None:
    """⚠ Checklist #183 said "timestamptz throughout; a per-column audit has not
    been done". This IS the audit, and it runs every time rather than once:
    measured 118 of 118 aware and 0 naive across 97 migrations on 2026-09-09.

    A naive `timestamp` column silently records the container's wall clock. In
    a product doing statutory deadline math that is the same defect this whole
    module is about, moved down a layer and made permanent — a stored value
    cannot be reinterpreted later, because nothing records which zone it meant.
    """
    naive: list[str] = []
    aware = 0
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        for m in _COLUMN.finditer(path.read_text(encoding="utf-8")):
            if m.group("type").lower() in {"timestamptz", "timestamp with time zone"}:
                aware += 1
            else:
                naive.append(f"{path.name}:{m.group('col')} -> {m.group('type')}")
    assert aware > 100, (
        f"the scan found only {aware} timestamp columns across "
        f"{len(list(_MIGRATIONS.glob('*.sql')))} migrations — the pattern or the glob "
        "has broken, and a guard that collects nothing reports green"
    )
    assert not naive, (
        "these columns store a wall clock with no zone, so what they mean depends on "
        "where the container ran:\n  " + "\n  ".join(naive)
    )
