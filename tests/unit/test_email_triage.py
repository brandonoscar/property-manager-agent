"""Tests for the email-ingest deterministic core (``email_triage``).

Covers the surface gate (known contact AND long), address normalisation,
and the action_surface projection. All pure — no network/DB/LLM.
"""

from __future__ import annotations

from helixis.webhooks.email_triage import (
    EMAIL_SURFACE_SCHEMA_VERSION,
    LONG_EMAIL_MIN_WORDS,
    InboundEmail,
    build_email_action_surface,
    is_known_contact,
    is_long,
    normalize_contact_set,
    normalize_email_address,
    should_surface,
    word_count,
)
from helixis.webhooks.inbox_filter import surfaces_in_inbox


def _email(**overrides: object) -> InboundEmail:
    base: dict[str, object] = {
        "message_id": "msg-1",
        "from_email": "sarah.chen@example.com",
        "subject": "Leak in unit 4B",
        "body_text": " ".join(["word"] * LONG_EMAIL_MIN_WORDS),
        "received_at": "2026-06-27T12:00:00Z",
        "from_name": "Sarah Chen",
        "thread_id": "thread-9",
    }
    base.update(overrides)
    return InboundEmail(**base)  # type: ignore[arg-type]


# ── address normalisation ────────────────────────────────────────────


def test_normalize_strips_display_name_and_lowercases() -> None:
    assert (
        normalize_email_address("Sarah Chen <Sarah.Chen@Example.COM>") == "sarah.chen@example.com"
    )


def test_normalize_bare_address() -> None:
    assert normalize_email_address("BOB@FOO.COM") == "bob@foo.com"


def test_normalize_returns_empty_on_garbage() -> None:
    for bad in (None, "", "not an address", "   "):
        assert normalize_email_address(bad) == ""  # type: ignore[arg-type]


def test_normalize_contact_set_skips_blanks_and_lowercases() -> None:
    s = normalize_contact_set(["A@B.com", "", None, "x <y@z.io>", 123])
    assert s == frozenset({"a@b.com", "y@z.io"})


# ── importance: known contact ────────────────────────────────────────


def test_is_known_contact_matches_normalised() -> None:
    contacts = normalize_contact_set(["Sarah.Chen@example.com"])
    assert is_known_contact("Sarah Chen <sarah.chen@EXAMPLE.com>", contacts)


def test_is_known_contact_rejects_unknown_and_garbage() -> None:
    contacts = normalize_contact_set(["sarah.chen@example.com"])
    assert not is_known_contact("stranger@spam.com", contacts)
    assert not is_known_contact("", contacts)
    assert not is_known_contact(None, contacts)


# ── length: long ─────────────────────────────────────────────────────


def test_word_count() -> None:
    assert word_count("one two three") == 3
    assert word_count("") == 0
    assert word_count(None) == 0


def test_is_long_boundary() -> None:
    short = " ".join(["w"] * (LONG_EMAIL_MIN_WORDS - 1))
    exact = " ".join(["w"] * LONG_EMAIL_MIN_WORDS)
    assert not is_long(short)
    assert is_long(exact)


def test_is_long_respects_custom_threshold() -> None:
    assert is_long("a b c", min_words=3)
    assert not is_long("a b c", min_words=4)


# ── the gate: should_surface (known contact AND long) ────────────────


def test_should_surface_requires_both_conditions() -> None:
    contacts = normalize_contact_set(["sarah.chen@example.com"])

    # known + long → surface
    assert should_surface(_email(), contacts)

    # known but short → no
    assert not should_surface(_email(body_text="thanks!"), contacts)

    # long but unknown sender → no
    assert not should_surface(_email(from_email="stranger@spam.com"), contacts)

    # neither → no
    assert not should_surface(_email(from_email="stranger@spam.com", body_text="hi"), contacts)


def test_should_surface_honours_custom_min_words() -> None:
    contacts = normalize_contact_set(["sarah.chen@example.com"])
    short_but_ok = _email(body_text="a b c d e")
    assert should_surface(short_but_ok, contacts, min_words=5)
    assert not should_surface(short_but_ok, contacts, min_words=6)


# ── projection: build_email_action_surface ───────────────────────────


def test_surface_floor_shape_is_valid_inbox_surface() -> None:
    surface = build_email_action_surface(_email())
    # The Inbox reader / coalesce treats a surface as valid only with
    # these three keys (queue.coalesce_action_surface._is_valid_surface).
    assert "summary" in surface
    assert isinstance(surface["actions"], list)
    assert "freeform_placeholder" in surface
    assert surface["schema_version"] == EMAIL_SURFACE_SCHEMA_VERSION
    assert surface["kind"] == "email"


def test_surface_summary_is_one_liner_but_body_is_full() -> None:
    long_body = " ".join(["sentence"] * 300)
    surface = build_email_action_surface(_email(body_text=long_body))
    # Summary is a short list-row label…
    assert surface["summary"] == "Email from Sarah Chen: Leak in unit 4B"
    # …but the full body is preserved verbatim (the user's ask: NOT
    # summarized).
    assert surface["email"]["body_text"] == long_body


def test_surface_floor_has_empty_signals_and_no_draft() -> None:
    surface = build_email_action_surface(_email())
    assert surface["signals"] == []
    assert surface["draft_reply"] is None


def test_surface_carries_enrichment_when_provided() -> None:
    surface = build_email_action_surface(
        _email(),
        signals=["Sarah mentions a move-out date you haven't confirmed."],
        draft_reply="Hi Sarah, thanks for flagging — I'll send a plumber Tuesday.",
    )
    assert surface["signals"] == ["Sarah mentions a move-out date you haven't confirmed."]
    assert surface["draft_reply"].startswith("Hi Sarah")


def test_surface_reply_action_targets_sender_and_thread() -> None:
    surface = build_email_action_surface(_email())
    reply = next(a for a in surface["actions"] if a["tool_id"] == "send_reply")
    assert reply["parameters"]["to"] == "sarah.chen@example.com"
    assert reply["parameters"]["thread_id"] == "thread-9"


def test_surface_handles_missing_subject_and_name() -> None:
    surface = build_email_action_surface(_email(subject="", from_name="", from_email="x@y.com"))
    assert surface["summary"] == "Email from x@y.com: (no subject)"
    assert surface["email"]["subject"] == "(no subject)"


# ── inbox allowlist wiring ───────────────────────────────────────────


def test_email_events_surface_in_inbox() -> None:
    assert surfaces_in_inbox("Email.Received")
    # Guard the trailing-dot discipline: a different family must not leak.
    assert not surfaces_in_inbox("EmailDigest.Created")


# ── email → EnrichedEvent mapping (reuse the Buildium pipeline) ───────


def test_synthetic_task_id_is_stable_and_positive() -> None:
    from helixis.webhooks.email_triage import email_synthetic_task_id

    a = email_synthetic_task_id("abc123")
    b = email_synthetic_task_id("abc123")
    c = email_synthetic_task_id("def456")
    assert a == b  # deterministic
    assert a != c  # distinct messages → distinct ids (overwhelmingly)
    assert a > 0  # fits EnrichedTask.id (signed positive int)


def test_email_enrichment_maps_onto_task_shape() -> None:
    from helixis.webhooks.email_triage import build_email_enrichment

    enr = build_email_enrichment(_email(), company_id="co-1")
    task = enr["task"]
    assert enr["event_name"] == "Email.Received"
    assert enr["company_id"] == "co-1"
    assert task["title"] == "Leak in unit 4B"
    # FULL body is preserved verbatim on the task description (the
    # frontend renders the real email from here, not a summary).
    assert task["description"] == _email().body_text
    assert task["category"] == "Email"
    assert task["requested_by_first_name"] == "Sarah"
    assert task["requested_by_last_name"] == "Chen"
    assert task["raw"]["message_id"] == "msg-1"
    assert task["raw"]["thread_id"] == "thread-9"
    assert task["raw"]["from_email"] == "sarah.chen@example.com"


def test_email_enrichment_reconstructs_as_valid_enriched_event() -> None:
    """The whole point of the mapping: the existing worker reconstructs
    an EnrichedEvent from buildium_events.enrichment. If this validates,
    an email row flows through the planner/summarizer exactly like a
    Buildium row."""
    from helixis.agent.enrichment.models import EnrichedEvent
    from helixis.webhooks.email_triage import build_email_enrichment

    enr = build_email_enrichment(_email(), company_id="co-1")
    event = EnrichedEvent.model_validate(enr)
    assert event.task.id > 0
    assert event.task.description == _email().body_text
    assert event.event_name == "Email.Received"
