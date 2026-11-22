"""Identity / PII redaction for logs, telemetry, and agent-visible text.

Single source of truth for the "no identifiers in logs/telemetry" rule. This
replaces a Buildium-identifier regex that was copy-pasted into three tool
modules (web / document / browser), and broadens coverage to universal PII
(email, US SSN, phone, credit card, IBAN).

Backed by **Microsoft Presidio** (MIT) when the optional ``[security]`` extra
is installed — *pattern recognizers only*: no spaCy model, no torch, no
network (the built-in ``EmailRecognizer`` is replaced with a plain pattern so
nothing reaches out to a public-suffix list at redaction time). When Presidio
is absent the module falls back to an equivalent pure-regex pass, so the base
install stays lean and behavior degrades gracefully.

Contract: :func:`redact` never raises and never blocks a log/telemetry path —
on any internal failure it falls back to the regex pass (and that, too, only
ever returns a string).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from functools import lru_cache

logger = logging.getLogger(__name__)

# Buildium-identifier pattern — the one that used to live (identically) in
# web_tools / document_tools / browser_tools as ``_ID_LEAK_PATTERN``.
_HELIXIS_ID_REGEX = r"\b(?:task|tenant|lease|property|owner|vendor|account)\s*#?\s*\d{3,}\b"

# Replacement tokens. The Buildium-ID token is kept verbatim for back-compat
# (activity-log assertions check for it); other PII uses a generic token.
ID_REDACTED = "[id-redacted]"
PII_REDACTED = "[redacted]"

# Regexes for the no-Presidio fallback (and the email recognizer above).
_ID_RE = re.compile(_HELIXIS_ID_REGEX, re.IGNORECASE)
_EMAIL_REGEX = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
_EMAIL_RE = re.compile(_EMAIL_REGEX)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# US-style phone: requires separators/parens so we don't redact bare integers.
# No leading \b — a number may start with "(", which is not a word boundary.
_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s*|\d{3}[-.\s])\d{3}[-.\s]\d{4}\b")
# Candidate card-number runs (13–19 digits, optional space/dash groups); a Luhn
# check gates the actual redaction to keep false positives down.
_CC_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# IBAN (international bank account): 2-letter country + 2 check digits + grouped
# alphanumerics. Used only by the memory pass (the never-store classes); the
# letters mean the CC candidate above never matches it.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _regex_redact(text: str) -> str:
    """Pure-regex redaction — the fallback when Presidio isn't installed."""
    out = _ID_RE.sub(ID_REDACTED, text)
    out = _EMAIL_RE.sub(PII_REDACTED, out)
    out = _SSN_RE.sub(PII_REDACTED, out)
    out = _PHONE_RE.sub(PII_REDACTED, out)

    def _cc(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        return PII_REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group()

    return _CC_CANDIDATE_RE.sub(_cc, out)


@lru_cache(maxsize=1)
def _presidio_redactor() -> Callable[[str], str] | None:
    """Build a pattern-only Presidio redactor, or ``None`` if the optional
    extra isn't installed. Cached — recognizers + engine are built once.

    Pattern-only by design: we call each recognizer with ``nlp_artifacts=None``
    (no spaCy model, no AnalyzerEngine NLP pipeline) and replace via the
    anonymizer. Email uses a plain pattern recognizer rather than the built-in
    one to avoid its ``tldextract`` public-suffix fetch on the hot path.
    """
    try:
        from presidio_analyzer import Pattern, PatternRecognizer
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            IbanRecognizer,
            PhoneRecognizer,
            UsSsnRecognizer,
        )
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        id_recognizer = PatternRecognizer(
            supported_entity="HELIXIS_ID",
            patterns=[Pattern(name="helixis_id", regex=_HELIXIS_ID_REGEX, score=0.9)],
        )
        email_recognizer = PatternRecognizer(
            supported_entity="EMAIL_ADDRESS",
            patterns=[Pattern(name="email", regex=_EMAIL_REGEX, score=0.9)],
        )
        recognizers = [
            id_recognizer,
            email_recognizer,
            UsSsnRecognizer(),
            CreditCardRecognizer(),
            PhoneRecognizer(),
            IbanRecognizer(),
        ]
        anonymizer = AnonymizerEngine()
    except ImportError:
        # Optional [security] extra absent — fall back to the regex pass.
        return None
    except (Exception, SystemExit):  # noqa: BLE001
        # A predefined recognizer / engine tried to bootstrap a spaCy NLP
        # pipeline and the model fetch failed. In a network-restricted deploy
        # (Render egress allowlist) spaCy shells out to pip to download
        # en_core_web_lg and, on failure, calls ``sys.exit(1)`` — which raises
        # SystemExit, NOT Exception, so it slips past every ``except Exception``
        # and kills the uvicorn worker mid-request (the live symptom: /agent/run
        # opens its SSE stream, the worker dies, the copilot spins on
        # "Thinking…" forever). Contain it here: cache the failure (lru_cache)
        # and use the regex fallback for the rest of the process's life.
        logger.warning("presidio init failed; using regex redaction", exc_info=True)
        return None

    operators = {
        "HELIXIS_ID": OperatorConfig("replace", {"new_value": ID_REDACTED}),
        "DEFAULT": OperatorConfig("replace", {"new_value": PII_REDACTED}),
    }

    def _redact(text: str) -> str:
        results: list = []
        for rec in recognizers:
            try:
                # nlp_artifacts=None is valid in pattern-only mode (no NLP
                # pipeline); Presidio's type hint declares it non-optional.
                found = rec.analyze(
                    text=text,
                    entities=rec.supported_entities,
                    nlp_artifacts=None,  # type: ignore[arg-type]
                )
            except (Exception, SystemExit):  # noqa: BLE001 — one bad recognizer must not break redaction
                # SystemExit too: a recognizer that lazily loads a spaCy model
                # can sys.exit() when the model fetch fails (see _presidio_redactor).
                logger.debug("presidio recognizer %s skipped", type(rec).__name__, exc_info=True)
                continue
            if found:
                results.extend(found)
        if not results:
            return text
        return anonymizer.anonymize(text=text, analyzer_results=results, operators=operators).text

    return _redact


def redact(text: str | None) -> str:
    """Redact identifiers + PII from ``text``. Never raises.

    Uses Presidio when the ``[security]`` extra is installed, else an
    equivalent regex pass. Returns ``""`` for empty/None input.
    """
    if not text:
        return ""
    try:
        engine = _presidio_redactor()
        if engine is not None:
            return engine(text)
    except (Exception, SystemExit):  # noqa: BLE001 — redaction must never break a log/telemetry path
        # SystemExit is deliberately included: spaCy/presidio can sys.exit() when
        # a model download fails in a network-restricted deploy, and that must
        # degrade to regex, never kill the request/worker (the module contract:
        # "redact never raises and never blocks a log/telemetry path").
        logger.debug("presidio redaction failed; using regex fallback", exc_info=True)
    return _regex_redact(text)


def redact_for_memory(text: str | None) -> str:
    """Redact only the **never-store** high-sensitivity classes (US SSN, credit
    card, IBAN) before text enters long-term memory — while PRESERVING the
    operational facts memory exists to recall: names, **emails**, **phone
    numbers**, unit/property references, Buildium IDs.

    This is deliberately narrower than :func:`redact` (the logs/telemetry pass,
    which also strips email/phone/IDs). Memory's whole purpose is for the agent
    to recall things like "Sarah Chen's number is 415-555-0199" across a
    company's employees, so those must survive; only the classes that must
    never sit in a vector store are cut. Pure-regex (no Presidio dependency),
    never raises, returns ``""`` for empty/None.
    """
    if not text:
        return ""
    out = _SSN_RE.sub(PII_REDACTED, text)
    out = _IBAN_RE.sub(PII_REDACTED, out)

    def _cc(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        return PII_REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group()

    return _CC_CANDIDATE_RE.sub(_cc, out)


__all__ = ["ID_REDACTED", "PII_REDACTED", "redact", "redact_for_memory"]
