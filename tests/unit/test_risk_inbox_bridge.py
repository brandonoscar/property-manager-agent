"""Detector findings → Inbox bridge (``Risk.FlagRaised``).

Pins the deterministic core (group identity / dedup, grouped titles, the
EnrichedEvent-shape enrichment, the floor surface), the pipeline wiring
(inbox allowlist + enrichment registry), and ``bridge_new_findings``'s
idempotency contract: one event per rule group, flags stamped
``bridged_at`` even on a dedup hit, per-group crash isolation.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agent.enrichment.risk_flag_enricher import RiskFlagEnricher
from helixis.detectors.inbox_bridge import (
    RISK_EVENT_NAME,
    bridge_new_findings,
    build_risk_action_surface,
    build_risk_enrichment,
    build_risk_event_payload,
    group_title,
    risk_group_message_id,
    rule_label,
)
from helixis.webhooks.event_schemas import dedup_key
from helixis.webhooks.inbox_filter import surfaces_in_inbox

COMPANY = "11111111-1111-1111-1111-111111111111"


def _finding(
    flag_id: str,
    rule_key: str = "rent_under_charged",
    *,
    title: str = "Unit 3C rent below the lease amount",
    detail: str = "Charged $1,650 against a $1,850 lease.",
    severity: str = "warning",
    amount: float | None = 200.0,
) -> dict[str, Any]:
    return {
        "id": flag_id,
        "rule_key": rule_key,
        "severity": severity,
        "title": title,
        "detail": detail,
        "amount": amount,
        "property_id": 700,
        "entity_type": "lease",
        "entity_id": 24215,
    }


# ── deterministic core ───────────────────────────────────────────────


def test_group_message_id_is_order_insensitive_and_set_sensitive() -> None:
    same = risk_group_message_id("rent_under_charged", ["b", "a"])
    assert same == risk_group_message_id("rent_under_charged", ["a", "b"])
    # A new flag joining the group is a genuinely new event.
    assert same != risk_group_message_id("rent_under_charged", ["a", "b", "c"])
    # Same set under a different rule is a different event too.
    assert same != risk_group_message_id("duplicate_vendor_bill", ["a", "b"])


def test_same_group_dedups_and_different_groups_do_not() -> None:
    # If the bridged_at stamp fails after enqueue, the retry re-builds the
    # same payload — the queue's dedup key must absorb it.
    f = [_finding("f1"), _finding("f2")]
    a = dedup_key(
        build_risk_event_payload(
            rule_key="rent_under_charged", findings=f, event_time_iso="2026-08-20T12:00:00+00:00"
        )
    )
    b = dedup_key(
        build_risk_event_payload(
            rule_key="rent_under_charged", findings=f, event_time_iso="2026-08-20T12:00:00+00:00"
        )
    )
    assert a == b
    c = dedup_key(
        build_risk_event_payload(
            rule_key="rent_under_charged",
            findings=[_finding("f3")],
            event_time_iso="2026-08-20T12:00:00+00:00",
        )
    )
    assert a != c


def test_single_finding_keeps_its_own_title_and_groups_get_the_label() -> None:
    assert group_title("rent_under_charged", [_finding("f1")]) == (
        "Unit 3C rent below the lease amount"
    )
    assert group_title("rent_under_charged", [_finding("f1"), _finding("f2")]) == (
        "Rent charged below the lease — 2 findings"
    )
    # Unknown rule keys degrade to de-underscored words, never crash.
    assert rule_label("hoa_dues_gap") == "Hoa dues gap"


def test_enrichment_validates_as_enriched_event_with_findings_on_raw() -> None:
    findings = [
        _finding("f1"),
        _finding("f2", title="Unit 2A rent below the lease amount", severity="critical"),
    ]
    enrichment = build_risk_enrichment(
        company_id=COMPANY,
        rule_key="rent_under_charged",
        findings=findings,
        event_time_iso="2026-08-20T12:00:00+00:00",
    )
    event = EnrichedEvent.model_validate(enrichment)  # the worker's reconstruction
    assert event.event_name == RISK_EVENT_NAME
    assert event.task.category == "RiskFlag"
    assert event.task.title == "Rent charged below the lease — 2 findings"
    # Every finding's title+detail reaches the planner via the description.
    assert "Unit 3C" in (event.task.description or "")
    assert "Unit 2A" in (event.task.description or "")
    # One critical finding raises the whole card's priority.
    assert event.task.priority == "High"
    assert [f["flag_id"] for f in event.task.raw["findings"]] == ["f1", "f2"]
    # The whole enrichment must be storable on the jsonb column.
    json.dumps(enrichment)


def test_floor_surface_carries_summary_signals_and_no_actions() -> None:
    surface = build_risk_action_surface(
        rule_key="rent_under_charged",
        findings=[_finding("f1"), _finding("f2")],
    )
    assert surface["kind"] == "risk"
    assert "$400" in surface["summary"]  # 2 × $200 involved
    assert len(surface["signals"]) == 2
    assert surface["actions"] == []  # floor: the planner fills these later


# ── pipeline wiring ──────────────────────────────────────────────────


def test_risk_events_surface_in_the_inbox() -> None:
    assert surfaces_in_inbox(RISK_EVENT_NAME)


def test_enrichment_registry_has_a_handler() -> None:
    from helixis.webhooks.handlers.enrichment_registry import get_enrichment_handler

    assert get_enrichment_handler(RISK_EVENT_NAME) is not None


def test_fallback_enricher_rebuilds_from_the_payload() -> None:
    payload = build_risk_event_payload(
        rule_key="rent_under_charged",
        findings=[_finding("f1"), _finding("f2")],
        event_time_iso="2026-08-20T12:00:00+00:00",
    )
    event = asyncio.run(RiskFlagEnricher().enrich(payload, COMPANY))
    assert event.task.category == "RiskFlag"
    assert event.task.title == "Rent charged below the lease — 2 findings"
    assert "$400" in (event.task.description or "")


# ── bridge_new_findings ──────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _CM:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _CM()


def _run_bridge(
    rows: list[dict[str, Any]],
    *,
    enqueue: AsyncMock | None = None,
    store: AsyncMock | None = None,
) -> tuple[int, _FakeConn, AsyncMock, AsyncMock]:
    conn = _FakeConn(rows)
    pool = _FakePool(conn)
    enq = enqueue or AsyncMock(return_value=MagicMock(duplicate=False, id=42))
    st = store or AsyncMock()
    with (
        patch("helixis.detectors.inbox_bridge.enqueue", new=enq),
        patch("helixis.detectors.inbox_bridge.store_enrichment", new=st),
    ):
        created = asyncio.run(bridge_new_findings(pool, COMPANY))  # type: ignore[arg-type]
    return created, conn, enq, st


def test_bridge_groups_one_event_per_rule() -> None:
    rows = [
        _finding("f1"),
        _finding("f2"),
        _finding(
            "f3",
            rule_key="duplicate_vendor_bill",
            title="Possible duplicate vendor bill",
            amount=Decimal("450.00"),  # numeric column arrives as Decimal
        ),
    ]
    created, conn, enq, st = _run_bridge(rows)
    assert created == 2
    assert enq.await_count == 2
    assert st.await_count == 2
    # Every enrichment written must be jsonb-storable (Decimal normalized).
    for call in st.await_args_list:
        json.dumps(call.kwargs["enrichment"])
        json.dumps(call.kwargs["action_surface"])
    # Both groups' flags were stamped bridged.
    stamped = [args[0] for sql, args in conn.executed if "bridged_at" in sql]
    assert sorted(x for ids in stamped for x in ids) == ["f1", "f2", "f3"]


def test_bridge_dedup_hit_still_stamps_but_stores_nothing() -> None:
    enq = AsyncMock(return_value=MagicMock(duplicate=True, id=42))
    created, conn, _, st = _run_bridge([_finding("f1")], enqueue=enq)
    assert created == 0
    st.assert_not_awaited()
    stamped = [args[0] for sql, args in conn.executed if "bridged_at" in sql]
    assert stamped == [["f1"]]  # re-carding would be the bug; re-selecting forever too


def test_bridge_no_unbridged_rows_is_a_noop() -> None:
    created, conn, enq, st = _run_bridge([])
    assert created == 0
    enq.assert_not_awaited()
    st.assert_not_awaited()
    assert conn.executed == []


def test_bridge_isolates_a_failing_group() -> None:
    # First group's enqueue blows up; the second group must still bridge
    # (and the first group's flags stay unbridged for the next pass).
    enq = AsyncMock(
        side_effect=[RuntimeError("boom"), MagicMock(duplicate=False, id=43)],
    )
    rows = [
        _finding("f1"),  # rent_under_charged — fails
        _finding("f2", rule_key="duplicate_vendor_bill", title="Dup bill"),
    ]
    created, conn, _, st = _run_bridge(rows, enqueue=enq)
    assert created == 1
    st.assert_awaited_once()
    stamped = [args[0] for sql, args in conn.executed if "bridged_at" in sql]
    assert stamped == [["f2"]]
