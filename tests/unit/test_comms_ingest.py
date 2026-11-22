"""Guards for the inbound-comms receiver (webhooks/comms_ingest.py).

``comms_triage`` was complete and tested for weeks while nothing called it —
so these guards read the WIRING as well as the behaviour: the Twilio route
must actually invoke the bridge, the event names must reach the Inbox filter,
and both must be registered with an enrichment handler. Testing the bridge
alone would reproduce exactly the gap this closes (gotcha 28's both-sides
rule).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from helixis.comms.contact_resolver import ResolvedContact
from helixis.webhooks import comms_ingest
from helixis.webhooks.comms_ingest import (
    bridge_inbound_comms,
    build_comms_event_payload,
    resolve_company_for_inbound,
)
from helixis.webhooks.comms_triage import CALL_EVENT_NAME, SMS_EVENT_NAME, InboundComms
from helixis.webhooks.inbox_filter import surfaces_in_inbox

BLUE_RIDGE = "5ca1ab1e-0000-4000-8000-000000000002"
OTHER_CO = "5ca1ab1e-0000-4000-8000-000000000004"


def _pool(company_rows: list[dict[str, str]]) -> MagicMock:
    """A pool whose ``fetch`` returns the given consent-ledger company rows."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=company_rows)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _contact() -> ResolvedContact:
    return ResolvedContact(
        kind="tenant",
        buildium_id=115937,
        name="Marcus Whitfield",
        phone="+15055550142",
        company_id=BLUE_RIDGE,
    )


# ── company resolution: the cross-tenant refusal ─────────────────────


@pytest.mark.asyncio
async def test_one_company_on_the_ledger_routes_there() -> None:
    pool = _pool([{"company_id": BLUE_RIDGE}])
    assert await resolve_company_for_inbound(pool, "+15055550142") == BLUE_RIDGE


@pytest.mark.asyncio
async def test_no_company_on_the_ledger_is_not_routed() -> None:
    pool = _pool([])
    assert await resolve_company_for_inbound(pool, "+15055550142") is None


@pytest.mark.asyncio
async def test_two_companies_on_the_ledger_refuses_rather_than_guessing() -> None:
    """The load-bearing one: routing a resident's text into another landlord's
    Inbox is a cross-tenant leak, and there is no signal here that separates
    the two candidates. Declining is the only correct answer."""
    pool = _pool([{"company_id": BLUE_RIDGE}, {"company_id": OTHER_CO}])
    assert await resolve_company_for_inbound(pool, "+15055550142") is None


# ── the bridge ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_known_contact_with_a_body_is_carded(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _pool([{"company_id": BLUE_RIDGE}])
    monkeypatch.setattr(
        comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(return_value=[_contact()])
    )
    event_id = uuid4()
    enqueue = AsyncMock(return_value=MagicMock(id=event_id, duplicate=False))
    store = AsyncMock()
    monkeypatch.setattr(comms_ingest, "enqueue", enqueue)
    monkeypatch.setattr(comms_ingest, "store_enrichment", store)

    got = await bridge_inbound_comms(
        pool,
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="Water is pouring through my ceiling",
        sid="SM123",
    )

    assert got == event_id
    assert enqueue.await_args.kwargs["event_name"] == SMS_EVENT_NAME
    assert enqueue.await_args.kwargs["company_id"] == BLUE_RIDGE
    # The enrichment carries the FULL body — that is the whole point of the
    # card, and the payload envelope deliberately does not.
    enrichment = store.await_args.kwargs["enrichment"]
    assert enrichment["task"]["description"] == "Water is pouring through my ceiling"


@pytest.mark.asyncio
async def test_a_shared_handset_reaches_the_card_as_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Found by MUTATION, not review, and the mutant is the instructive part.

    Truncating the resolver's answer to one candidate inside the bridge left
    every triage test green — they build ``InboundComms`` themselves, so they
    cannot see what the bridge chose to put in it — and every card would go
    back to naming one person out of a household. The call-site AST walks
    could not see it either: the right function was still called, its result
    was just cut down afterwards.

    Only driving the seam end to end and reading what was STORED catches it.
    """
    pool = _pool([{"company_id": BLUE_RIDGE}])
    josh = _contact()
    dana = ResolvedContact(
        kind="tenant", buildium_id=999, name="Dana Gil", phone="+15055550142", company_id=BLUE_RIDGE
    )
    monkeypatch.setattr(
        comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(return_value=[josh, dana])
    )
    store = AsyncMock()
    monkeypatch.setattr(
        comms_ingest, "enqueue", AsyncMock(return_value=MagicMock(id=uuid4(), duplicate=False))
    )
    monkeypatch.setattr(comms_ingest, "store_enrichment", store)

    await bridge_inbound_comms(
        pool,
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="Can someone look at the heat?",
        sid="SM777",
    )

    task = store.await_args.kwargs["enrichment"]["task"]
    assert "Dana Gil" in task["title"] and josh.name in task["title"]
    assert task["requested_by_first_name"] is None, (
        "the request was filed under one member of the household"
    )
    assert len(task["raw"]["contact_candidates"]) == 2
    surface = store.await_args.kwargs["action_surface"]
    assert any("Confirm who you are speaking to" in sig for sig in surface["signals"])


@pytest.mark.asyncio
async def test_the_number_they_texted_beats_who_we_recognise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ THE ONE THAT WAS SILENTLY DROPPING MESSAGES.

    A resident who appears in TWO companies' consent ledgers — they changed
    landlords, or a vendor serves both — made the sender-side lookup ambiguous,
    so it correctly refused to guess and carded NOTHING. But we always knew
    whose number they dialled: ``helixis_twilio_numbers`` maps it uniquely
    (migration 0082). The ledger read is a fallback, not the answer.
    """
    pool = _pool([{"company_id": BLUE_RIDGE}, {"company_id": OTHER_CO}])
    monkeypatch.setattr(
        comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(return_value=[_contact()])
    )
    enqueue = AsyncMock(return_value=MagicMock(id=uuid4(), duplicate=False))
    monkeypatch.setattr(comms_ingest, "enqueue", enqueue)
    monkeypatch.setattr(comms_ingest, "store_enrichment", AsyncMock())

    got = await bridge_inbound_comms(
        pool,
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="Water is pouring through my ceiling",
        sid="SM123",
        company_id=OTHER_CO,
    )

    assert got is not None, "an ambiguous SENDER is not ambiguous when we know the RECEIVER"
    assert enqueue.await_args.kwargs["company_id"] == OTHER_CO


@pytest.mark.asyncio
async def test_the_receiving_company_wins_over_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions of the same rule. A single ledger row for a DIFFERENT
    company must not redirect the card: whoever owns the number they texted is
    the company that has the relationship, and the ledger row is stale or
    belongs to someone the sender also deals with."""
    pool = _pool([{"company_id": BLUE_RIDGE}])
    contact = AsyncMock(return_value=[_contact()])
    monkeypatch.setattr(comms_ingest, "resolve_all_contacts_by_phone", contact)
    resolve = AsyncMock(return_value=BLUE_RIDGE)
    monkeypatch.setattr(comms_ingest, "resolve_company_for_inbound", resolve)
    enqueue = AsyncMock(return_value=MagicMock(id=uuid4(), duplicate=False))
    monkeypatch.setattr(comms_ingest, "enqueue", enqueue)
    monkeypatch.setattr(comms_ingest, "store_enrichment", AsyncMock())

    await bridge_inbound_comms(
        pool,
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="the boiler is out again",
        sid="SM124",
        company_id=OTHER_CO,
    )

    assert enqueue.await_args.kwargs["company_id"] == OTHER_CO
    assert not resolve.await_count, "the ledger was consulted even though the owner was known"
    # The contact is resolved WITHIN the owning company, not within whichever
    # company the ledger named — looking a tenant up in the wrong books is how
    # a card gets attributed to a stranger.
    assert contact.await_args.kwargs["company_id"] == OTHER_CO


@pytest.mark.asyncio
async def test_without_a_receiving_company_the_ledger_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-0082 path is still live: a deployment with no number mapping
    (single tenant, or a call site that has no ``To``) falls back to the
    ledger. Removing that would silently stop carding on those installs."""
    pool = _pool([{"company_id": BLUE_RIDGE}])
    monkeypatch.setattr(
        comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(return_value=[_contact()])
    )
    enqueue = AsyncMock(return_value=MagicMock(id=uuid4(), duplicate=False))
    monkeypatch.setattr(comms_ingest, "enqueue", enqueue)
    monkeypatch.setattr(comms_ingest, "store_enrichment", AsyncMock())

    await bridge_inbound_comms(
        pool,
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="Water is pouring through my ceiling",
        sid="SM125",
    )

    assert enqueue.await_args.kwargs["company_id"] == BLUE_RIDGE


@pytest.mark.asyncio
async def test_an_unknown_number_is_never_carded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consent row without a matching Buildium contact is the manager's own
    phone, not a resident — no card, and no enqueue call at all."""
    pool = _pool([{"company_id": BLUE_RIDGE}])
    monkeypatch.setattr(comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(return_value=[]))
    enqueue = AsyncMock()
    monkeypatch.setattr(comms_ingest, "enqueue", enqueue)

    assert (
        await bridge_inbound_comms(
            pool,
            channel="sms",
            from_number="+15055550142",
            to_number="+15055559999",
            body="hello",
            sid="SM123",
        )
        is None
    )
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bridge_failure_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise here would make the Twilio webhook non-2xx, which makes Twilio
    retry the WHOLE delivery — re-running the STOP/START consent mirroring
    that ran before this call. The card is worth less than that."""
    pool = _pool([{"company_id": BLUE_RIDGE}])
    monkeypatch.setattr(
        comms_ingest, "resolve_all_contacts_by_phone", AsyncMock(side_effect=RuntimeError("boom"))
    )
    assert (
        await bridge_inbound_comms(
            pool,
            channel="sms",
            from_number="+15055550142",
            to_number="+15055559999",
            body="hi",
            sid="SM1",
        )
        is None
    )


def test_the_payload_carries_hazards_and_no_body() -> None:
    """Hazard slugs ride the envelope so a lost enrichment column cannot lose a
    life-safety flag; the body does NOT, so the queue row stays small."""
    comms = InboundComms(
        channel="sms",
        from_number="+15055550142",
        to_number="+15055559999",
        body="there is a gas leak in the kitchen",
        occurred_at=datetime.now(UTC).isoformat(),
        contact=_contact(),
        sid="SM9",
    )
    payload = build_comms_event_payload(comms, company_id=BLUE_RIDGE)
    assert payload["Hazards"], "a gas leak must be flagged deterministically at ingest"
    assert payload["MessageId"] == "SM9"
    # No payload VALUE may carry the message text. Checked per-value rather
    # than over ``str(payload)``, because the hazard SLUG legitimately contains
    # the same words and would satisfy a whole-dict substring search either way.
    assert not [v for v in payload.values() if isinstance(v, str) and "kitchen" in v]


def test_a_voice_call_uses_the_call_event_name() -> None:
    comms = InboundComms(
        channel="voice",
        from_number="+15055550142",
        to_number="+15055559999",
        body="transcript",
        occurred_at=datetime.now(UTC).isoformat(),
        contact=_contact(),
        sid="CA1",
    )
    assert build_comms_event_payload(comms, company_id=BLUE_RIDGE)["EventName"] == CALL_EVENT_NAME


# ── wiring: the half that was missing ────────────────────────────────


def test_both_comms_events_reach_the_inbox() -> None:
    assert surfaces_in_inbox(SMS_EVENT_NAME)
    assert surfaces_in_inbox(CALL_EVENT_NAME)


def test_both_comms_events_have_an_enrichment_handler() -> None:
    """Without a registered handler the worker marks the row
    ``planner_status='failed'`` and the card never gets its signals — the
    exact reason EmailEventEnricher exists."""
    from helixis.webhooks.handlers.enrichment_registry import ENRICHMENT_HANDLERS

    for name in (SMS_EVENT_NAME, CALL_EVENT_NAME):
        assert name in ENRICHMENT_HANDLERS, f"{name} has no enrichment handler"


def test_the_twilio_route_actually_calls_the_bridge() -> None:
    """The finding this whole change closes was "built, tested, called by
    nothing". A behavioural test of the bridge cannot see that; only reading
    the CALLER can.

    AST, not a substring scan: ``ruff format`` wraps this call across lines, so
    a text search for ``bridge_inbound_comms(`` plus its arguments goes blind
    on a reflow (gotcha 40).
    """
    from helixis.api.routes import twilio as twilio_route

    tree = ast.parse(inspect.getsource(twilio_route))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "bridge_inbound_comms" in called, (
        "the Twilio inbound route no longer calls the comms bridge — "
        "comms_triage is unreachable again"
    )


def test_the_twilio_route_tells_the_bridge_who_received_it() -> None:
    """The bridge's ``company_id`` defaults to ``""`` so the pre-0082 fallback
    keeps working — which means a caller that stops passing it degrades
    SILENTLY back to the ambiguous sender-side lookup, carding nothing for
    anyone in two ledgers. No behavioural test of the bridge can see that
    (gotcha 62): only reading the call site can.
    """
    from helixis.api.routes import twilio as twilio_route

    tree = ast.parse(inspect.getsource(twilio_route))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bridge_inbound_comms"
    ]
    assert calls, "the Twilio inbound route no longer calls the comms bridge"
    for call in calls:
        assert "company_id" in {kw.arg for kw in call.keywords}, (
            "the inbound route stopped telling the bridge which company owns "
            "the number that was texted — it will fall back to the ambiguous "
            "consent-ledger lookup and silently drop cards"
        )


def test_the_bridge_asks_for_every_contact_not_the_best_one() -> None:
    """⚠ Reverting to ``resolve_contact_by_phone`` FAILS NOTHING behaviourally.

    That function still exists, still works, and returns the highest-precedence
    match — so the bridge would go on carding every text, just naming one
    person out of a household again. The card would look right. Only the call
    site says which question was asked (gotchas 62/66/69, three separate
    instances of a route quietly ignoring a correct helper).
    """
    from helixis.webhooks import comms_ingest as mod

    tree = ast.parse(inspect.getsource(mod))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_all_contacts_by_phone" in called, (
        "the bridge is back to picking one contact by precedence, so a shared "
        "household number is carded under whichever name sorts first"
    )
    assert "resolve_contact_by_phone" not in called


def test_the_bridge_hands_the_candidates_to_the_projection() -> None:
    """The list has to REACH `InboundComms`, or it is computed and dropped —
    which is the very shape (fetched and thrown away) this change fixes."""
    from helixis.webhooks import comms_ingest as mod

    tree = ast.parse(inspect.getsource(mod))
    builds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "InboundComms"
    ]
    assert builds, "the bridge no longer builds an InboundComms"
    for call in builds:
        assert "candidates" in {kw.arg for kw in call.keywords}, (
            "the resolved candidates never reach the card, so the title and "
            "the attribution are back to naming one person out of several"
        )


def test_the_company_discovery_query_says_why_it_is_unscoped() -> None:
    """Gotcha 47: every ``helixis_*`` query carries ``company_id`` or a comment
    saying why it is deliberately global. This one cannot be scoped — its job
    is to DISCOVER the company — so the exemption must be stated in the source
    where the next reader will see it, not just in a commit message."""
    src = Path(comms_ingest.__file__).read_text(encoding="utf-8")
    marker = src.index("_COMPANIES_FOR_PHONE_SQL")
    preamble = src[max(0, marker - 700) : marker]
    assert "NOT company-scoped" in preamble or "not company-scoped" in preamble
    assert "gotcha 47" in preamble
