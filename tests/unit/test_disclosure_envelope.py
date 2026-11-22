"""Progressive-disclosure envelope (W2 slice 6, F9).

The envelope is DERIVED server-side from the finished run's artifacts (prose +
SYNTHESIZE footer + specialist trail + run bookkeeping) — never model-emitted —
and published as an additive ``disclosure`` SSE event in loop mode only. These
tests pin the derivation (footer split, degraded no-footer case, trail
semantics), the wire shape, the event builder, and the loop-only wiring.
"""

from __future__ import annotations

from helixis.agents.disclosure import (
    DisclosureEnvelope,
    build_disclosure_envelope,
    strip_synthesis_footer,
)
from helixis.common.events import EventType, disclosure_event

_ANSWER = (
    "Bob Kim owes $2,400 and promised to pay by July 25. The promise is in his "
    "Jul 14 email; no payment has posted since."
)
_FOOTERED = (
    _ANSWER + "\n\n"
    "Sources: buildium: $2,400 past due; gmail: Bob's Jul 14 promise\n"
    "Confidence: likely — promise stated, no payment posted\n"
    "Unverified: none\n"
)


# ── layer 1: footer stripping ────────────────────────────────────


def test_footer_is_stripped_from_answer() -> None:
    assert strip_synthesis_footer(_FOOTERED) == _ANSWER


def test_no_footer_leaves_answer_unchanged() -> None:
    assert strip_synthesis_footer(_ANSWER) == _ANSWER


def test_quoted_footer_mid_answer_survives() -> None:
    # A footer quoted in the middle of prose is content, not presentation —
    # only the TRAILING block is stripped.
    text = "Reports end like this:\nSources: example\n\nAnyway, Bob paid."
    assert strip_synthesis_footer(text) == text


def test_footer_only_text_is_kept_never_emptied() -> None:
    footer_only = "Sources: buildium\nConfidence: likely — x\nUnverified: none"
    assert strip_synthesis_footer(footer_only) == footer_only


# ── the derived envelope ─────────────────────────────────────────


def test_envelope_splits_answer_from_reasoning() -> None:
    env = build_disclosure_envelope(
        _FOOTERED,
        specialist_trail=["check_known_state", "buildium_agent", "gmail_agent"],
        run_id="run-1",
        trace_id="trace-1",
    )
    assert env.answer == _ANSWER
    assert env.sources == [
        "buildium: $2,400 past due",
        "gmail: Bob's Jul 14 promise",
    ]
    assert env.confidence == "likely"
    assert env.confidence_reason == "promise stated, no payment posted"
    assert env.unverified == []
    assert env.footer_well_formed is True
    assert env.specialist_trail == ["check_known_state", "buildium_agent", "gmail_agent"]


def test_envelope_degrades_without_footer() -> None:
    # Disclosure never gates the answer — a footerless reply still yields a
    # usable envelope with the reasoning layer honestly empty.
    env = build_disclosure_envelope(_ANSWER, specialist_trail=[])
    assert env.answer == _ANSWER
    assert env.footer_well_formed is False
    assert env.confidence is None
    assert env.sources == [] and env.unverified == []
    assert env.specialist_trail == []


def test_trail_keeps_order_and_duplicates() -> None:
    # A repeat IS information: the absence-recheck rule re-querying a source.
    trail = ["gmail_agent", "gmail_agent", "buildium_agent"]
    env = build_disclosure_envelope(_FOOTERED, specialist_trail=trail)
    assert env.specialist_trail == trail


def test_trail_is_capped_defensively() -> None:
    env = build_disclosure_envelope(_FOOTERED, specialist_trail=["a"] * 100)
    assert len(env.specialist_trail) == 30


# ── the wire shape ───────────────────────────────────────────────


def test_payload_shape_is_three_layers() -> None:
    payload = build_disclosure_envelope(
        _FOOTERED,
        specialist_trail=["buildium_agent"],
        run_id="run-1",
        trace_id="trace-1",
    ).to_payload()
    assert set(payload) == {"answer", "reasoning", "audit"}
    assert payload["answer"] == _ANSWER
    assert set(payload["reasoning"]) == {
        "sources",
        "confidence",
        "confidence_reason",
        "unverified",
        "footer_well_formed",
        "specialist_trail",
    }
    assert payload["audit"] == {"run_id": "run-1", "trace_id": "trace-1"}


def test_payload_tolerates_missing_audit_ids() -> None:
    payload = DisclosureEnvelope(answer="x").to_payload()
    assert payload["audit"] == {"run_id": None, "trace_id": None}


# ── the SSE event ────────────────────────────────────────────────


def test_disclosure_event_type_and_passthrough() -> None:
    data = {"answer": "x", "reasoning": {}, "audit": {}}
    event = disclosure_event(data)
    assert event.type == EventType.DISCLOSURE
    assert event.data == data
    assert '"disclosure"' in event.to_sse()


# ── loop-only wiring ─────────────────────────────────────────────


def test_emission_is_gated_on_reasoning_loop() -> None:
    # Source-level pin (the emission sits deep in run_agent_streaming, which a
    # unit test can't drive end-to-end — the fence-chokepoint precedent). The
    # envelope must be built + published only when the loop actually ran, and
    # the specialist trail must be accumulated under the same gate.
    #
    # The gate reads ``loop_mode``, NOT ``settings.agent.reasoning_loop``: the
    # loop is a per-question decision now (``loop_mode_for``), so a recognised
    # one-fact lookup runs the plain router even where the deployment flag is
    # on. Gating the envelope on the raw flag would promise a reasoning trail
    # for a turn that did no reasoning — the disclosure would describe work
    # that never happened.
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator.run_agent_streaming)
    build_at = src.index("build_disclosure_envelope(")
    gate_at = src.rindex("if loop_mode and final_output:", 0, build_at)
    assert gate_at < build_at
    assert "disclosure_event(envelope.to_payload())" in src
    assert "loop_specialist_trail.append(name)" in src
