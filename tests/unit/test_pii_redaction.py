"""Identity/PII redaction (helixis.security.pii) — the consolidated redactor
that replaced the regex duplicated across web/document/browser tools (#3 OSS).

Both engines are exercised: the Presidio path (the [security]/[dev] extra,
installed in CI) and the pure-regex fallback (forced by stubbing the engine
out), so behavior is pinned regardless of whether the extra is present.
"""

from __future__ import annotations

import pytest

from helixis.security import pii


def _assert_redacts_everything(redact) -> None:
    text = (
        "Email sarah@oak.com about Task #12345 and Tenant# 9981; "
        "SSN 078-05-1120; call (415) 555-0199; card 4111 1111 1111 1111."
    )
    out = redact(text)
    # Buildium identifiers keep the back-compat token (activity-log assertions).
    assert "[id-redacted]" in out
    assert "Task #12345" not in out
    assert "Tenant# 9981" not in out
    # Universal PII is gone.
    assert "sarah@oak.com" not in out
    assert "078-05-1120" not in out
    assert "4111 1111 1111 1111" not in out
    assert "555-0199" not in out


def test_presidio_path_redacts() -> None:
    """Default path — Presidio is installed via the [dev] extra in CI."""
    assert pii._presidio_redactor() is not None, "expected Presidio (the [dev] extra) installed"
    _assert_redacts_everything(pii.redact)


def test_fallback_path_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Presidio stubbed out, the regex fallback covers the same classes."""
    monkeypatch.setattr(pii, "_presidio_redactor", lambda: None)
    _assert_redacts_everything(pii.redact)


def test_clean_text_untouched() -> None:
    clean = "Roof leak in unit 4 fixed yesterday; lease renews next month."
    assert pii.redact(clean) == clean


def test_clean_text_untouched_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii, "_presidio_redactor", lambda: None)
    clean = "Roof leak in unit 4 fixed yesterday; lease renews next month."
    assert pii.redact(clean) == clean


def test_empty_and_none() -> None:
    assert pii.redact("") == ""
    assert pii.redact(None) == ""


def test_fallback_luhn_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 16-digit run that fails Luhn is NOT treated as a card (false-positive
    guard); a Luhn-valid one is redacted."""
    monkeypatch.setattr(pii, "_presidio_redactor", lambda: None)
    assert "1234567812345678" in pii.redact("ref 1234567812345678 here")  # not Luhn-valid
    assert "4111 1111 1111 1111" not in pii.redact("card 4111 1111 1111 1111")  # Luhn-valid


def test_redact_never_raises_on_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Presidio engine itself raises, redact falls back, never bubbles."""

    def _boom() -> object:
        def _raise(_text: str) -> str:
            raise RuntimeError("engine exploded")

        return _raise

    monkeypatch.setattr(pii, "_presidio_redactor", _boom)
    out = pii.redact("Task #12345 leak")
    assert "[id-redacted]" in out  # fell through to regex


def test_redact_contains_systemexit_from_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spaCy/presidio model fetch that fails calls ``sys.exit(1)`` — which
    raises SystemExit, NOT Exception. redact() must contain it (regex fallback),
    never let it propagate and kill the uvicorn worker. Regression for the
    deploy crash that hung the copilot on "Thinking…"."""

    def _engine() -> object:
        def _raise(_text: str) -> str:
            raise SystemExit(1)  # what spaCy.util.run_command does on failure

        return _raise

    monkeypatch.setattr(pii, "_presidio_redactor", _engine)
    out = pii.redact("Email sarah@oak.com about Task #12345")
    assert "[id-redacted]" in out
    assert "sarah@oak.com" not in out


def test_redact_contains_systemexit_from_redactor_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """If building the redactor itself sys.exit()s (a recognizer/engine
    bootstrapping a missing spaCy model), redact() still degrades to regex."""

    def _boom_build() -> object:
        raise SystemExit(1)

    monkeypatch.setattr(pii, "_presidio_redactor", _boom_build)
    out = pii.redact("call (415) 555-0199 re Tenant# 9981")
    assert "[id-redacted]" in out
    assert "555-0199" not in out


# ── redact_for_memory — the narrow, "never-store-only" pass ──────────────


def test_memory_redaction_keeps_operational_pii() -> None:
    """Memory must retain names, email, phone, unit/Buildium IDs — the agent
    needs to recall "Sarah's number is X" across a company's employees."""
    text = (
        "Unit 4B tenant is Sarah Chen, sarah@oak.com, (415) 555-0199. "
        "Her open Task #12345 is about a leak."
    )
    out = pii.redact_for_memory(text)
    assert "Sarah Chen" in out
    assert "sarah@oak.com" in out  # email KEPT
    assert "555-0199" in out  # phone KEPT
    assert "Task #12345" in out  # Buildium ID KEPT
    assert out == text  # nothing sensitive present → untouched


def test_memory_redaction_strips_never_store_classes() -> None:
    """SSN, Luhn-valid credit card, and IBAN must never enter the vector store
    even when the surrounding operational facts are kept."""
    text = (
        "Sarah Chen (sarah@oak.com) SSN 078-05-1120, card 4111 1111 1111 1111, "
        "IBAN GB82 WEST 1234 5698 7654 32 — call 415-555-0199."
    )
    out = pii.redact_for_memory(text)
    # never-store classes gone
    assert "078-05-1120" not in out
    assert "4111 1111 1111 1111" not in out
    assert "GB82 WEST 1234 5698 7654 32" not in out
    # operational facts preserved
    assert "Sarah Chen" in out
    assert "sarah@oak.com" in out
    assert "415-555-0199" in out


def test_memory_redaction_luhn_gates_card() -> None:
    """A non-Luhn 16-digit run is left alone (could be an order/ref number);
    a Luhn-valid card is stripped."""
    assert "1234567812345678" in pii.redact_for_memory("ref 1234567812345678")
    assert "4111 1111 1111 1111" not in pii.redact_for_memory("card 4111 1111 1111 1111")


def test_memory_redaction_empty_and_none() -> None:
    assert pii.redact_for_memory("") == ""
    assert pii.redact_for_memory(None) == ""
