"""A risk card describing a problem that was fixed three weeks ago.

`inbox_bridge` stamps `bridged_at` so a finding becomes an Inbox card exactly
once — deliberately, so a resolved→new reactivation cannot re-spam the Inbox.
Meanwhile `detectors/store` auto-resolves any finding the detector stops
emitting. Both halves are right on their own terms and nothing connected them,
so the card outlived the condition (coverage checklist #244).

MEASURED on helixis-test before any of this existed, and it is not an edge case:
**81 risk cards, 35 of them (43%) FULLY healed** — every finding behind them
resolved, card still sitting in the Inbox — and **9 more overstating their
count**, saying "4 findings" where two had since been fixed. 66 healed findings
behind open cards. More than half the risk Inbox was obsolete or wrong about how
much was wrong.

Same family as gotcha 62's stranded reminder cards, and the same failure mode as
gotcha 26: a confident statement about the world that the data no longer
supports. The difference here is that it is a statement about work somebody is
being asked to do.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.detectors.card_freshness import (
    FLAG_IDS_KEY,
    STATE_ALL_RESOLVED,
    STATE_OPEN,
    STATE_PARTLY_RESOLVED,
    STATE_UNKNOWN,
    finding_state,
    finding_states_for_events,
    flag_ids_on,
)

_ROUTE = Path("src/helixis/api/routes/buildium.py")
_MODULE = Path("src/helixis/detectors/card_freshness.py")

A, B, C = "flag-a", "flag-b", "flag-c"


# ── how much of this card is still true ──────────────────────────────


def test_a_card_whose_findings_all_healed_says_so() -> None:
    """The 43% case. Nothing about the card itself changes when the condition
    goes away, so this is the only thing that can tell a reader."""
    state = finding_state([A, B], {A: "resolved", B: "resolved"})
    assert state is not None
    assert state["state"] == STATE_ALL_RESOLVED
    assert (state["total"], state["resolved"], state["open"]) == (2, 2, 0)


def test_a_partly_healed_card_reports_the_remainder_not_a_boolean() -> None:
    """⚠ "3 of 4 have since been resolved" is a different instruction to a
    property manager than "this is done" — and the card's own title still says
    four. Collapsing the two into one flag would leave the count wrong with no
    way to tell."""
    state = finding_state([A, B, C], {A: "resolved", B: "resolved", C: "new"})
    assert state is not None
    assert state["state"] == STATE_PARTLY_RESOLVED
    assert (state["resolved"], state["open"], state["total"]) == (2, 1, 3)


def test_an_untouched_card_is_plain_open() -> None:
    """The twin. Without it every assertion here passes against a function that
    always reports something has healed."""
    state = finding_state([A, B], {A: "new", B: "confirmed"})
    assert state is not None
    assert state["state"] == STATE_OPEN
    assert state["resolved"] == 0


def test_a_dismissal_is_not_a_resolution() -> None:
    """⚠ The distinction that matters most, and the one it would be easiest to
    collapse. RESOLVED means the detector looked again and the condition is
    gone. DISMISSED means a person said "this is fine" — an opinion was
    recorded, the world did not change. Reporting a dismissed card as healed
    tells the PM a problem went away when nobody ever fixed it.
    """
    state = finding_state([A, B], {A: "dismissed", B: "dismissed"})
    assert state is not None
    assert state["state"] == STATE_OPEN, "a dismissal is being reported as a fix"
    assert state["resolved"] == 0
    assert state["dismissed"] == 2, "the dismissals are still counted, just not as fixes"


def test_a_dismissal_beside_a_resolution_does_not_inflate_the_healed_count() -> None:
    state = finding_state([A, B], {A: "resolved", B: "dismissed"})
    assert state is not None
    assert (state["resolved"], state["dismissed"]) == (1, 1)
    assert state["state"] == STATE_ALL_RESOLVED, (
        "no finding is still open, so there is no work left on this card — but "
        "the counts must keep the two apart"
    )


@pytest.mark.parametrize(
    "statuses",
    [
        {A: "resolved"},  # B is missing entirely
        {A: "resolved", B: "some_status_the_check_constraint_does_not_allow"},
    ],
)
def test_an_unreadable_finding_makes_the_whole_card_unknown(statuses: dict[str, str]) -> None:
    """⚠ Gotchas 55/57/70, and note it poisons the WHOLE card rather than one
    finding. "1 of 2 resolved" implies the second was checked; if it was not,
    that sentence claims more than the data supports. A missing row could be a
    purge, a restore, or an id that never existed — none of which is evidence
    the condition healed.
    """
    state = finding_state([A, B], statuses)
    assert state is not None
    assert state["state"] == STATE_UNKNOWN
    assert state["unknown"] == 1


def test_a_repeated_flag_id_is_counted_once() -> None:
    state = finding_state([A, A, B], {A: "resolved", B: "new"})
    assert state is not None
    assert state["total"] == 2


def test_a_card_with_no_findings_is_not_annotated_at_all() -> None:
    """Every other Inbox row — Buildium, email, comms — must come back exactly
    as it did before this existed."""
    assert finding_state([], {A: "resolved"}) is None


# ── reading the ids off the card ─────────────────────────────────────


def test_the_key_this_reads_is_the_key_the_bridge_writes() -> None:
    """⚠ Reads BOTH sides (gotcha 28). A renamed payload key would not fail
    anything — every card would simply report `unknown` forever, which reads
    like caution rather than like a bug, and that is the failure mode that
    survives for months.
    """
    from helixis.detectors.inbox_bridge import build_risk_event_payload

    payload = build_risk_event_payload(
        rule_key="rent_under_charged",
        findings=[{"id": A, "title": "t", "amount": 1.0}, {"id": B, "title": "u", "amount": 2.0}],
        event_time_iso="2026-09-09T00:00:00+00:00",
    )
    assert FLAG_IDS_KEY in payload, (
        f"the bridge no longer writes {FLAG_IDS_KEY!r}; card_freshness would report "
        "every risk card as unknown, silently"
    )
    assert flag_ids_on(payload) == [A, B]


@pytest.mark.parametrize(
    "payload", [None, {}, {"FlagIds": "not-a-list"}, {"FlagIds": []}, {"FlagIds": [None, ""]}, 42]
)
def test_a_card_that_is_not_a_risk_card_yields_nothing(payload: object) -> None:
    """A malformed payload must cost this annotation, never the Inbox page —
    one route carries every card type."""
    assert flag_ids_on(payload) == []


# ── the page-level read ──────────────────────────────────────────────


def _pool(rows: list[dict] | Exception) -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=rows if isinstance(rows, Exception) else None)
    if not isinstance(rows, Exception):
        pool.fetch = AsyncMock(return_value=rows)
    return pool


def test_the_page_read_groups_by_event() -> None:
    rows = [
        {"event_id": "e1", "flag_id": A, "status": "resolved"},
        {"event_id": "e1", "flag_id": B, "status": "resolved"},
        {"event_id": "e2", "flag_id": C, "status": "new"},
    ]
    got = asyncio.run(finding_states_for_events(_pool(rows), "c1", ["e1", "e2"]))
    assert got["e1"]["state"] == STATE_ALL_RESOLVED
    assert got["e2"]["state"] == STATE_OPEN


def test_a_null_status_from_the_left_join_reads_as_unknown() -> None:
    """The LEFT JOIN is what produces this: a flag id in the payload with no
    matching row — including one belonging to ANOTHER company, which the
    join's own company scope turns into a miss rather than a leak."""
    rows = [
        {"event_id": "e1", "flag_id": A, "status": "resolved"},
        {"event_id": "e1", "flag_id": B, "status": None},
    ]
    got = asyncio.run(finding_states_for_events(_pool(rows), "c1", ["e1"]))
    assert got["e1"]["state"] == STATE_UNKNOWN


def test_a_failing_read_costs_the_caveat_not_the_inbox() -> None:
    """⚠ Gotcha 62's asymmetry. This is an ANNOTATION on somebody's Inbox;
    letting it raise would 500 the page where the product's work lives, to
    avoid omitting a caveat."""
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("pool is gone"))
    assert asyncio.run(finding_states_for_events(pool, "c1", ["e1"])) == {}


def test_a_row_of_the_wrong_shape_costs_the_caveat_too() -> None:
    """The half the guard above did NOT cover, and it 500'd a live route.

    The read succeeded and the PARSE raised: the `try` wrapped only
    `pool.fetch`, so a row without `event_id` — a renamed column, a caller
    handing back something else — threw a bare `KeyError` out of a function
    whose docstring promises it never raises. Gotcha 48: prose cannot fail.
    Same asymmetry as the test above; the failure just arrives one line later.
    """
    rows = [{"id": "e1", "summary_text": "not a flag row at all"}]
    assert asyncio.run(finding_states_for_events(_pool(rows), "c1", ["e1"])) == {}


def test_an_empty_page_does_not_query_at_all() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    assert asyncio.run(finding_states_for_events(pool, "c1", [])) == {}
    pool.fetch.assert_not_awaited()


# ── it is wired, and scoped ──────────────────────────────────────────


def _route_fn(name: str) -> ast.AST:
    tree = ast.parse(_ROUTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_ROUTE}")


def test_the_inbox_route_actually_asks() -> None:
    """⚠ A behavioural test of `finding_states_for_events` cannot see a route
    that stopped calling it — the annotation would simply never appear, which
    is indistinguishable from "no card has healed". Gotchas 62/66/69 are three
    separate instances of a well-tested helper a caller quietly ignored."""
    fn = _route_fn("recent_events")
    src = ast.unparse(fn)
    assert "finding_states_for_events" in src, (
        "recent_events no longer reads the current finding statuses, so every risk "
        "card renders as open work whether or not the problem still exists"
    )
    # ⚠ NOT `"finding_state" in src`. Found by MUTATION: replacing the
    # assignment with `_ = states.get(...)` left that substring satisfied by
    # `finding_states_for_events` itself, so the guard passed against a route
    # that computed the answer and threw it away — the exact "fetched and
    # thrown away" shape gotchas 41/50/51 record, hiding inside the guard
    # meant to catch it. The ASSIGNMENT is what has to be asserted.
    assigned = {
        t.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute)
    }
    assert "finding_state" in assigned, (
        "the finding states are read and then discarded — nothing reaches the "
        "response, so the Inbox renders exactly as it did before"
    )


def test_the_annotation_does_not_reach_the_plain_response_shape() -> None:
    """The non-inbox caller's wire shape must be byte-identical to before —
    `?include=` is what opts a caller into the heavier fields."""
    src = ast.unparse(_route_fn("recent_events"))
    assert "inbox_only and out" in src, (
        "the finding-state query is no longer gated on the inbox path, so a plain "
        "caller pays for a feature it did not ask for"
    )


def test_the_lookup_is_company_scoped_on_both_sides() -> None:
    """⚠ Not redundant scoping. A card's `FlagIds` is a list of ids the product
    did not validate, so without a company predicate on the JOIN a corrupted or
    crafted payload could read another tenant's finding statuses. Scoped, a
    foreign id resolves to no row and the core reports `unknown` — the safe
    answer (gotcha 47).
    """
    sql = " ".join(
        str(n.value)
        for n in ast.walk(ast.parse(_MODULE.read_text(encoding="utf-8")))
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value
    )
    assert sql, "card_freshness no longer issues a SELECT"
    assert sql.count("company_id") >= 2, (
        f"the query is {sql!r} — it scopes on fewer than two company_id predicates, so "
        "an id from another tenant's payload could resolve to that tenant's row"
    )
    assert "f.company_id = e.company_id" in sql, (
        "the JOIN is no longer company-scoped: the WHERE clause alone bounds which "
        "EVENTS are read, not which FLAGS their payloads can reach"
    )
