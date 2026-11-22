"""A flagged gas leak lost its urgency when the card was rebuilt.

`comms_ingest` scans every inbound text and transcript with the deterministic
life-safety lexicon and bumps `priority` to High, so an emergency sorts to the
top without waiting on an LLM. When the enrichment column is missing, the
worker self-heals through `CommsEventEnricher.enrich` — and the queue payload
deliberately does NOT carry the message body (it lives on the enrichment
column so the queue row stays small). So the rebuild re-scanned an EMPTY
string, found nothing, and produced `priority: None` with the "⚠ Possible
life-safety hazard" floor line gone. A gas-leak text sorted beside a lease
question (coverage checklist #235).

⚠ **The signal was already there.** `build_comms_event_payload` puts `Hazards`
on the ENVELOPE, and its own comment says why: *"the deterministic life-safety
scan must survive a lost enrichment column"*. That is precisely this case, and
nothing read it — gotchas 41/50/51's sentence (the field was fetched and thrown
away) landing on the one signal where under-flagging is a life-safety outcome
and over-flagging costs a manager a second look.

⚠ **A FLOOR, not a replacement.** The union of both scans, so a hazard either
source saw survives. Letting the envelope OVERWRITE would be just as wrong in
the other direction: a body that survived and flagged something the envelope
missed must keep its flag too.

⚠ **Not a widening of the lexicon.** The union only ever carries slugs some
scan derived from real text, so this cannot start flagging ordinary messages —
which is what would get the signal switched off (gotcha 52).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.agent.enrichment.comms_event_enricher import CommsEventEnricher
from helixis.webhooks.comms_ingest import build_comms_event_payload
from helixis.webhooks.comms_triage import (
    InboundComms,
    build_comms_enrichment,
    comms_hazards,
)

_ENRICHER = Path("src/helixis/agent/enrichment/comms_event_enricher.py")

_GAS_LEAK = "There is a gas leak in my apartment, it smells really strong"


def _sms(body: str) -> InboundComms:
    from helixis.comms.contact_resolver import ResolvedContact

    return InboundComms(
        channel="sms",
        from_number="+15550001111",
        to_number="+15550002222",
        body=body,
        occurred_at="2026-09-09T12:00:00Z",
        contact=ResolvedContact(
            kind="tenant", buildium_id=1, name="Josh Gil", phone="+15550001111", company_id="co-1"
        ),
        sid="SM1",
    )


# ── the scan itself ──────────────────────────────────────────────────


def test_a_live_body_still_scans_on_its_own() -> None:
    """The ordinary ingest path passes no ``known`` and must behave exactly as
    before — this change is additive or it is a regression."""
    assert comms_hazards(_sms(_GAS_LEAK))
    assert comms_hazards(_sms("Can I get a copy of my lease?")) == []


def test_the_envelope_slugs_survive_an_empty_body() -> None:
    """The whole point. The rebuild has nothing to scan; the flag still holds."""
    assert comms_hazards(_sms(""), known=["gas_leak"]) == ["gas_leak"]


def test_the_floor_is_a_union_not_a_replacement() -> None:
    """⚠ Both directions. A body that survived and flagged something the
    envelope missed keeps its flag; an envelope slug the body no longer shows
    is added rather than substituted."""
    merged = comms_hazards(_sms(_GAS_LEAK), known=["flooding"])
    assert "flooding" in merged
    assert any(slug != "flooding" for slug in merged), (
        "the envelope's slugs replaced the live scan instead of joining it"
    )


def test_the_floor_does_not_duplicate() -> None:
    both = comms_hazards(_sms(_GAS_LEAK), known=comms_hazards(_sms(_GAS_LEAK)))
    assert len(both) == len(set(both))


@pytest.mark.parametrize("known", [None, [], ["", None]])
def test_nothing_known_changes_nothing(known: list | None) -> None:
    """⚠ An empty or junk envelope must not manufacture a hazard. A flag on a
    message nobody scanned is a false alarm, and a signal that fires on
    ordinary texts is one people stop reading (gotcha 52)."""
    assert comms_hazards(_sms("Can I get a copy of my lease?"), known=known) == []


# ── what the card ends up saying ─────────────────────────────────────


def test_a_rebuilt_card_keeps_high_priority() -> None:
    rebuilt = build_comms_enrichment(_sms(""), "co-1", known_hazards=["gas_leak"])
    assert rebuilt["task"]["priority"] == "High", (
        "a flagged emergency rebuilt from the queue envelope came back at "
        "ordinary priority — it sorts beside a lease question"
    )
    assert rebuilt["task"]["raw"]["hazards"] == ["gas_leak"]


def test_an_ordinary_rebuilt_card_is_not_promoted() -> None:
    """The direction that keeps the flag meaningful."""
    rebuilt = build_comms_enrichment(_sms(""), "co-1", known_hazards=[])
    assert rebuilt["task"]["priority"] is None


def test_the_ingest_path_is_byte_for_byte_unchanged() -> None:
    """No caller passes ``known_hazards`` at ingest, so the live path must
    produce exactly what it did before."""
    live = build_comms_enrichment(_sms(_GAS_LEAK), "co-1")
    explicit = build_comms_enrichment(_sms(_GAS_LEAK), "co-1", known_hazards=None)
    assert live["task"]["priority"] == explicit["task"]["priority"] == "High"


# ── the envelope really carries them, and the enricher really reads it ──


def test_the_envelope_carries_the_slugs_it_promises() -> None:
    """⚠ Both sides (gotcha 28). If ingest stopped writing ``Hazards`` the
    enricher below would read an absent key and quietly go back to dropping
    the flag — with every test above still green, because they hand the slugs
    in by hand."""
    payload = build_comms_event_payload(_sms(_GAS_LEAK), company_id="co-1")
    assert payload["Hazards"], "the queue envelope no longer carries the hazard slugs"


@pytest.mark.asyncio
async def test_the_self_heal_rebuild_reads_the_envelope() -> None:
    """The end-to-end shape: the envelope ingest wrote, handed to the enricher
    that rebuilds from it, with no body anywhere in between."""
    payload = build_comms_event_payload(_sms(_GAS_LEAK), company_id="co-1")
    rebuilt = await CommsEventEnricher().enrich(payload, "co-1")
    assert rebuilt.task is not None
    assert rebuilt.task.priority == "High", (
        "the self-heal rebuild dropped the life-safety flag the envelope was written to preserve"
    )


@pytest.mark.asyncio
async def test_a_rebuilt_ordinary_message_stays_ordinary() -> None:
    payload = build_comms_event_payload(_sms("Can I get a copy of my lease?"), company_id="co-1")
    rebuilt = await CommsEventEnricher().enrich(payload, "co-1")
    assert rebuilt.task is not None
    assert rebuilt.task.priority is None


@pytest.mark.asyncio
async def test_a_junk_hazards_field_is_survived() -> None:
    """⚠ The envelope is data read back from a queue row, so it can be
    anything. A malformed value must degrade to "no known slugs", never raise
    inside the worker — a card that fails to build is worse than one that
    lost a flag."""
    payload = build_comms_event_payload(_sms("hello"), company_id="co-1")
    for junk in ("gas_leak", None, 42, {"a": 1}):
        payload["Hazards"] = junk
        rebuilt = await CommsEventEnricher().enrich(payload, "co-1")
        assert rebuilt.task is not None
        assert rebuilt.task.priority is None


def test_the_enricher_actually_passes_the_slugs_on() -> None:
    """⚠ A behavioural test cannot see a caller that stops passing the
    argument — it would simply fall back to the default and read as "no
    hazards", which is the defect (gotchas 62/66/69). This walks the call."""
    tree = ast.parse(_ENRICHER.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_comms_enrichment"
    ]
    assert calls, "the enricher no longer builds the enrichment"
    assert all(any(kw.arg == "known_hazards" for kw in call.keywords) for call in calls), (
        "build_comms_enrichment is called without known_hazards — the rebuild "
        "re-scans an empty body and a flagged emergency silently drops to "
        "ordinary priority"
    )
