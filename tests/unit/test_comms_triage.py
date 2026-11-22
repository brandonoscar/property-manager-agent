"""Comms → EnrichedEvent mapper. Pure functions, no Twilio/DB/LLM — these pin
the contract the click-to-call/text feature's *context* half rides on: a known
contact's message becomes the same EnrichedEvent shape a resident request does,
so the existing planner fills signals + can auto-create a task."""

from __future__ import annotations

from helixis.comms.contact_resolver import ResolvedContact
from helixis.webhooks.comms_triage import (
    CALL_EVENT_NAME,
    SMS_EVENT_NAME,
    InboundComms,
    build_comms_action_surface,
    build_comms_enrichment,
    comms_hazards,
    comms_synthetic_task_id,
    is_ambiguous,
    should_surface_comms,
)

_JOSH = ResolvedContact(
    kind="tenant", buildium_id=42, name="Josh Gil", phone="(555) 123-4567", company_id="co-1"
)


def _sms(body: str = "The kitchen faucet won't stop dripping.", **kw: object) -> InboundComms:
    base: dict[str, object] = {
        "channel": "sms",
        "from_number": "+15551234567",
        "to_number": "+15559990000",
        "body": body,
        "occurred_at": "2026-08-13T15:00:00Z",
        "contact": _JOSH,
        "sid": "SM123abc",
    }
    base.update(kw)
    return InboundComms(**base)  # type: ignore[arg-type]


# ── gate ────────────────────────────────────────────────────────────────


def test_known_contact_with_content_surfaces() -> None:
    assert should_surface_comms(_sms()) is True


def test_unknown_number_never_surfaces() -> None:
    # No length floor, but an unknown number (contact=None) is dropped.
    assert should_surface_comms(_sms(contact=None)) is False


def test_short_text_from_known_contact_still_surfaces() -> None:
    # The key divergence from email: no length gate.
    assert should_surface_comms(_sms(body="Fire alarm going off")) is True


def test_empty_body_does_not_surface() -> None:
    assert should_surface_comms(_sms(body="   ")) is False


# ── enrichment (the EnrichedEvent the pipeline consumes) ──────────────────


def test_sms_enrichment_maps_onto_enriched_event() -> None:
    ev = build_comms_enrichment(_sms(), company_id="co-1")
    assert ev["event_name"] == SMS_EVENT_NAME
    assert ev["company_id"] == "co-1"
    task = ev["task"]
    # Title = label + contact + snippet; full body preserved verbatim.
    assert task["title"].startswith("Text from Josh Gil:")
    assert task["description"] == "The kitchen faucet won't stop dripping."
    assert task["category"] == "Text message"
    assert task["requested_by_first_name"] == "Josh"
    assert task["requested_by_last_name"] == "Gil"
    # raw carries the routing + attribution facts.
    raw = task["raw"]
    assert raw["channel"] == "sms"
    assert raw["from_number"] == "+15551234567"
    assert raw["contact"] == {"kind": "tenant", "buildium_id": 42, "name": "Josh Gil"}
    # Same top-level EnrichedEvent envelope as Buildium/email rows.
    assert set(ev) >= {"unit", "active_lease", "history_unit", "history_property", "errors"}


def test_voice_enrichment_uses_call_family_and_transcript() -> None:
    transcript = "Hi, this is Josh in 4B — the heat is out and it's freezing."
    ev = build_comms_enrichment(_sms(channel="voice", body=transcript), company_id="co-1")
    assert ev["event_name"] == CALL_EVENT_NAME
    assert ev["task"]["title"] == "Call with Josh Gil"  # no snippet on calls
    assert ev["task"]["description"] == transcript
    assert ev["task"]["category"] == "Phone call"


def test_full_body_is_never_truncated_in_description() -> None:
    long_body = " ".join(f"word{i}" for i in range(200))
    ev = build_comms_enrichment(_sms(body=long_body), company_id="co-1")
    assert ev["task"]["description"] == long_body  # verbatim, no summary
    # …but the title snippet IS bounded and marked truncated.
    assert ev["task"]["title"].endswith("…")


def test_synthetic_task_id_is_stable_and_positive() -> None:
    a = comms_synthetic_task_id("SM123abc")
    b = comms_synthetic_task_id("SM123abc")
    assert a == b > 0
    assert comms_synthetic_task_id("SM123abc") != comms_synthetic_task_id("SMotherxyz")


def test_numbers_are_normalized_in_raw() -> None:
    ev = build_comms_enrichment(_sms(from_number="(555) 123-4567"), company_id="co-1")
    assert ev["task"]["raw"]["from_number"] == "+15551234567"


# ── action surface (the Inbox floor) ──────────────────────────────────────


def test_sms_action_surface_offers_review_and_send() -> None:
    surface = build_comms_action_surface(_sms())
    assert surface["kind"] == "comms"
    assert surface["comms"]["body"] == "The kitchen faucet won't stop dripping."
    action = surface["actions"][0]
    assert action["tool_id"] == "send_reply"
    assert action["parameters"]["to"] == "+15551234567"
    assert "confirm" in action["intent"].lower()  # never auto-sends


def test_voice_action_surface_offers_call_back() -> None:
    surface = build_comms_action_surface(_sms(channel="voice", body="voicemail transcript"))
    action = surface["actions"][0]
    assert action["tool_id"] == "call_back"
    assert action["parameters"]["to"] == "+15551234567"


def test_action_surface_signals_default_to_empty_list() -> None:
    surface = build_comms_action_surface(_sms())
    assert surface["signals"] == []
    assert surface["draft_reply"] is None
    # Enrichment layers on top when present.
    enriched = build_comms_action_surface(
        _sms(), signals=["Rent is 12 days late"], draft_reply="On it"
    )
    assert enriched["signals"] == ["Rent is 12 days late"]
    assert enriched["draft_reply"] == "On it"


# ── deterministic hazard / urgency tagging ────────────────────────────────


def test_hazard_text_bumps_priority_and_records_slugs() -> None:
    ev = build_comms_enrichment(_sms(body="There is a gas leak in my apartment"), "co-1")
    assert ev["task"]["priority"] == "High"
    assert "gas_leak" in ev["task"]["raw"]["hazards"]


def test_non_hazard_text_leaves_priority_unset() -> None:
    ev = build_comms_enrichment(_sms(body="Can I get a copy of my lease?"), "co-1")
    assert ev["task"]["priority"] is None
    assert ev["task"]["raw"]["hazards"] == []


def test_hazard_seeds_a_floor_signal_ahead_of_llm_signals() -> None:
    surface = build_comms_action_surface(
        _sms(body="I smell gas"), signals=["Tenant is on a month-to-month lease"]
    )
    # The deterministic hazard line is first — never lost behind LLM signals.
    assert "hazard" in surface["signals"][0].lower()
    assert surface["signals"][1] == "Tenant is on a month-to-month lease"


def test_comms_hazards_passthrough() -> None:
    assert comms_hazards(_sms(body="burst pipe flooding the unit")) != []
    assert comms_hazards(_sms(body="thanks, see you tuesday")) == []


# ── a shared household handset ───────────────────────────────────────
#
# `resolve_contact_by_phone` picks the highest-precedence match and LOGS the
# rest, so a text from a couple carded as "Text from Josh Gil" — a confident
# false statement about which real person said it — and the auto-task filed
# the request under them. The candidates existed the whole time (gotchas
# 41/51: fetched and thrown away), and the function that returns them had no
# callers at all (gotcha 62).

_DANA = ResolvedContact(
    kind="tenant", buildium_id=43, name="Dana Gil", phone="(555) 123-4567", company_id="co-1"
)
#: The SAME person on two leases. Distinct by (kind, buildium_id), so this is
#: one contact — not an ambiguity.
_JOSH_AGAIN = ResolvedContact(
    kind="tenant", buildium_id=42, name="Josh Gil", phone="+15551234567", company_id="co-1"
)


def test_two_people_behind_one_handset_is_ambiguous() -> None:
    assert is_ambiguous(_sms(candidates=(_JOSH, _DANA))) is True


def test_the_same_person_twice_is_not() -> None:
    """⚠ The other direction, and it is what keeps the caveat meaningful. A
    tenant listed on two leases matches twice; treating that as a shared
    handset would put the warning on a large share of ordinary texts, and a
    signal that fires everywhere is one people stop reading (gotcha 52)."""
    assert is_ambiguous(_sms(candidates=(_JOSH, _JOSH_AGAIN))) is False
    assert is_ambiguous(_sms(candidates=(_JOSH,))) is False
    assert is_ambiguous(_sms()) is False


def test_the_title_names_everybody_rather_than_picking_one() -> None:
    """Gotcha 41's rule pointed at a person: the line must be self-contained,
    so no reader can quote a name that a precedence tiebreak chose."""
    task = build_comms_enrichment(_sms(candidates=(_JOSH, _DANA)), "co-1")["task"]
    assert "Josh Gil or Dana Gil" in task["title"]


def test_an_unambiguous_text_still_names_the_person() -> None:
    """The caveat must not cost the ordinary case its name — that would make
    every card read as uncertain and the distinction worthless."""
    task = build_comms_enrichment(_sms(candidates=(_JOSH,)), "co-1")["task"]
    assert "Josh Gil" in task["title"]
    assert " or " not in task["title"]
    assert task["requested_by_first_name"] == "Josh"


def test_attribution_is_left_empty_when_the_number_is_shared() -> None:
    """⚠ These two fields are what the auto-task path files the request under.
    Filling them from a precedence tiebreak files one resident's request under
    their partner's name — declining to name anybody is recoverable, naming
    the wrong resident is not (gotcha 55, pointed at a person)."""
    task = build_comms_enrichment(_sms(candidates=(_JOSH, _DANA)), "co-1")["task"]
    assert task["requested_by_first_name"] is None
    assert task["requested_by_last_name"] is None


def test_the_raw_payload_carries_every_candidate_only_when_shared() -> None:
    """Its PRESENCE is the signal, so a list of one would read as an ambiguity
    on every ordinary text."""
    shared = build_comms_enrichment(_sms(candidates=(_JOSH, _DANA)), "co-1")["task"]["raw"]
    assert [c["buildium_id"] for c in shared["contact_candidates"]] == [42, 43]
    plain = build_comms_enrichment(_sms(candidates=(_JOSH,)), "co-1")["task"]["raw"]
    assert plain["contact_candidates"] is None


def test_the_shared_number_warning_is_a_deterministic_floor_signal() -> None:
    """Not a hope that the model notices the "or" in the title. Acting on a
    shared handset as one named resident — a rent promise, entry consent, a
    lease answer to the wrong person — is the harm, and it is the one thing
    here a manager fixes by asking."""
    surface = build_comms_action_surface(_sms(candidates=(_JOSH, _DANA)))
    joined = " ".join(surface["signals"])
    assert "Confirm who you are speaking to" in joined
    assert "2 people" in joined


def test_an_ordinary_text_gets_no_such_signal() -> None:
    surface = build_comms_action_surface(_sms(candidates=(_JOSH,)))
    assert not any("Confirm who you are speaking to" in s for s in surface["signals"])


# ── a voice transcript is a machine's guess ──────────────────────────


def _voice(body: str, **kw: object) -> InboundComms:
    return _sms(body, channel="voice", **kw)


def test_a_voice_transcript_with_figures_says_to_check_the_recording() -> None:
    """The transcript is what the planner reads and drafts a reply from, and a
    rent amount is the token ASR most reliably gets wrong. Twilio sends no
    confidence value, so naming what to check is the available control."""
    surface = build_comms_action_surface(_voice("This is unit 4B, I can pay $1,500 Friday."))
    joined = " ".join(surface["signals"])
    assert "automatic transcription" in joined
    assert "an amount" in joined and "a unit number" in joined


def test_a_text_message_with_the_same_figures_gets_no_such_caveat() -> None:
    """⚠ VOICE only, and this is the assertion that makes the rule correct
    rather than merely cautious. An SMS body is what the sender TYPED — there
    is no transcription in between — so caveating it would tell a manager to
    doubt something nothing has garbled."""
    surface = build_comms_action_surface(_sms("This is unit 4B, I can pay $1,500 Friday."))
    assert not any("automatic transcription" in s for s in surface["signals"])


def test_an_ordinary_voicemail_gets_no_caveat() -> None:
    """The false-positive direction. Most voicemails carry no figure at all,
    and a line on every card is one nobody reads (gotcha 52)."""
    surface = build_comms_action_surface(_voice("Hi, it's Dana, please call me back."))
    assert not any("automatic transcription" in s for s in surface["signals"])


def test_a_hazard_still_leads() -> None:
    """Ordering is not cosmetic: a gas leak outranks "check the figures", and
    the hazard line is appended first so it stays the first thing read."""
    surface = build_comms_action_surface(_voice("There is a gas leak in unit 4B, I owe $500."))
    assert "life-safety" in surface["signals"][0]
