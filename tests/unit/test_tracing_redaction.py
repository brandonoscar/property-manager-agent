"""Nothing reaches Langfuse unredacted — including the user's own sentence.

Langfuse is third-party telemetry. Tool spans were redacted, and
``codeexec/audit.py`` deliberately sends a code sha256 rather than the source
because "it embeds tenant literals and Langfuse is third-party telemetry"
(gotcha 33). Two paths were not: the root trace's ``input`` — the user's
question, verbatim — and every ``record_event`` payload. We scrubbed the
machine's data and shipped the human's.

⚠ **What this does NOT do.** Redaction is pattern-only: Presidio runs without
spaCy on purpose, because the NLP pipeline was removed from the request path
for RAM (gotcha 9). Emails and phone numbers are removed; PERSON NAMES and
STREET ADDRESSES are not. That is measured below rather than assumed, and it
is asserted explicitly so this file cannot be read as "the query is clean".
Closing that gap needs NER on the telemetry path — a real memory decision,
not a line of code, and deliberately not smuggled in here.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.observability import tracing


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake Langfuse client and hand back what it received."""
    fake = MagicMock()
    monkeypatch.setattr(tracing, "_client", fake)
    return fake


def _sent(call: Any, key: str) -> Any:
    return call.kwargs[key]


# ── the root trace ───────────────────────────────────────────────────


def test_the_user_query_is_redacted_before_it_leaves(client: MagicMock) -> None:
    """THE defect. Every tool span under this trace was redacted; the question
    that produced them was not."""
    tracing.start_trace(
        trace_id="t1",
        name="agent_run",
        company_id="c1",
        session_id="s1",
        user_query="email marcus.w@example.com or call 555-867-5309 about the balance",
    )
    sent = _sent(client.trace.call_args, "input")
    assert "marcus.w@example.com" not in sent, (
        "a tenant's email address reached third-party telemetry"
    )
    assert "555-867-5309" not in sent, "a tenant's phone number reached third-party telemetry"
    assert "[redacted]" in sent


def test_a_query_with_nothing_sensitive_survives_intact(client: MagicMock) -> None:
    """Both directions (gotcha 38). Redaction that ate ordinary questions
    would make the traces useless and get switched off — the observability
    has to still work."""
    tracing.start_trace(
        trace_id="t1",
        name="agent_run",
        company_id="c1",
        session_id="s1",
        user_query="how many work orders are open right now?",
    )
    assert _sent(client.trace.call_args, "input") == "how many work orders are open right now?"


def test_no_query_still_sends_no_input(client: MagicMock) -> None:
    """``None`` must stay ``None`` — not the string "None", and not an empty
    string that reads as "the user asked nothing"."""
    tracing.start_trace(trace_id="t1", name="agent_run", company_id="c1", session_id="s1")
    assert _sent(client.trace.call_args, "input") is None


def test_names_and_addresses_are_known_to_survive(client: MagicMock) -> None:
    """⚠ This asserts a LIMITATION, on purpose.

    Presidio runs pattern-only (gotcha 9 removed spaCy from the request path
    for RAM), so there is no NER and a person's name and a street address pass
    straight through. Pinning it means the gap is a recorded fact rather than
    an assumption someone makes in the other direction — and the day NER is
    added on the telemetry path, this test fails and forces the note to be
    rewritten instead of quietly going stale (gotcha 48).
    """
    tracing.start_trace(
        trace_id="t1",
        name="agent_run",
        company_id="c1",
        session_id="s1",
        user_query="Marcus Whitfield at 1700 W Washington St wants a repair",
    )
    sent = _sent(client.trace.call_args, "input")
    assert "Marcus Whitfield" in sent
    assert "1700 W Washington St" in sent


# ── arbitrary events ─────────────────────────────────────────────────


def test_event_payloads_are_redacted(client: MagicMock) -> None:
    """The other unguarded door. Any caller adding an event could ship
    whatever it held, and the callers are the paths that hold tenant data."""
    tracing.record_event(
        trace_id="t1",
        name="codeexec.run",
        payload={"plan": "notify tenant at jane@example.com", "substrate": "e2b"},
    )
    sent = _sent(client.event.call_args, "input")
    assert "jane@example.com" not in str(sent)
    assert sent["substrate"] == "e2b"


def test_event_redaction_leaves_operational_values_alone(client: MagicMock) -> None:
    """The audit trail has to keep its meaning. A redactor that mangled the
    code hash would destroy exactly the evidence gotcha 33 added it for."""
    digest = hashlib.sha256(b"print(1)").hexdigest()
    tracing.record_event(
        trace_id="t1",
        name="codeexec.run",
        payload={
            "code_sha256": digest,
            "company_id": "5ca1ab1e-0000-4000-8000-000000000002",
            "stop_reason": "wall_clock_deadline",
            "at": "2026-08-29T00:00:00Z",
            "duration_ms": 1234,
        },
    )
    sent = _sent(client.event.call_args, "input")
    assert sent["code_sha256"] == digest
    assert sent["company_id"] == "5ca1ab1e-0000-4000-8000-000000000002"
    assert sent["stop_reason"] == "wall_clock_deadline"
    assert sent["at"] == "2026-08-29T00:00:00Z"
    assert sent["duration_ms"] == 1234


def test_a_nested_payload_is_reached(client: MagicMock) -> None:
    """``_redact_value`` recurses; a one-level scrub would leave the common
    shape — a list of records under a key — completely untouched."""
    tracing.record_event(
        trace_id="t1",
        name="sweep",
        payload={"cards": [{"to": "bob@example.com"}, {"to": "sue@example.com"}]},
    )
    blob = str(_sent(client.event.call_args, "input"))
    assert "bob@example.com" not in blob
    assert "sue@example.com" not in blob


# ── the paths that were already correct must stay correct ────────────


def test_tool_spans_are_still_redacted(client: MagicMock) -> None:
    tracing.record_tool_span(
        trace_id="t1",
        name="contact_lookup",
        input={"q": "sue@example.com"},
        output="phone 555-867-5309",
    )
    call = client.span.call_args
    assert "sue@example.com" not in str(_sent(call, "input"))
    assert "555-867-5309" not in str(_sent(call, "output"))


def test_tracing_off_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client means no call at all — not a call with redacted content."""
    monkeypatch.setattr(tracing, "_client", None)
    tracing.start_trace(
        trace_id="t1", name="n", company_id="c", session_id="s", user_query="x@y.com"
    )
    tracing.record_event(trace_id="t1", name="n", payload={"a": "x@y.com"})
