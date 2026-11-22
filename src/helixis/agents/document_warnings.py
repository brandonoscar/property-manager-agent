"""What a document-extraction warning MEANS — one vocabulary, one home.

``DocumentExtraction.warnings`` carries two completely different kinds of
string and nothing in the product told them apart:

* **machine tokens** the code appends — ``attachment_too_large``,
  ``llm_unauthorized``, ``doc_type_uncertain``. Finite, enumerable, and
  meaningless to a property manager.
* **free text the MODEL writes**, which the extraction prompt explicitly asks
  for: rule 1 demands a warning naming any field it had to null out
  (``"monthly_rent: figure smudged in the rent clause"``) and rule 7 asks it to
  cite the region. Unbounded, and already the most useful line in the result —
  it tells somebody exactly which number to re-check.

The chat client rendered the whole list verbatim under "### Notes", so a
manager who attached a 12 MB scan waited out the upload and was shown
``- attachment_too_large``. That is the failure shape this product exists to
avoid: it looks like nothing was caught, when in fact the reason was known
precisely and simply never said in words.

⚠ **THE PASS-THROUGH ARM IS LOAD-BEARING, not caution.** The obvious fix —
drop anything not recognised — would delete exactly the model's per-field
explanations, which are the half worth reading. So the rule is: translate the
enumerable tokens, pass everything else through untouched. Which makes this
module's completeness the thing that matters, and why
``tests/unit/test_document_warnings_are_explained.py`` reads BOTH the call
sites that emit tokens AND the client's sentence map (gotcha 28) — a token
with no sentence must fail CI, never reach a screen.
"""

from __future__ import annotations

from helixis.llm.provider_errors import (
    WARN_QUOTA_EXHAUSTED,
    WARN_UNAUTHORIZED,
    WARN_UNAVAILABLE,
)

__all__ = [
    "ATTACHMENT_TOO_LARGE",
    "ATTACHMENT_UNAVAILABLE",
    "DOCLING_NOTE",
    "DOCUMENT_PASSWORD_PROTECTED",
    "DOC_TYPE_UNCERTAIN",
    "EMPTY_DOCUMENT",
    "FAILURE_TOKENS",
    "INVALID_EXTRACTION_PREFIX",
    "LOW_CONFIDENCE",
    "MACHINE_TOKENS",
    "RECOVERED_NOISY_JSON",
    "RETRIED_ONCE_AFTER_MALFORMED_JSON",
    "UNSUPPORTED_MIME_TYPE",
    "is_failure",
    "nothing_was_read",
]

# ── the fetch stage ──────────────────────────────────────────────
#: The bytes could not be obtained at all — missing file, dead URL, undecodable
#: base64, or no input given.
ATTACHMENT_UNAVAILABLE = "attachment_unavailable"

#: The input was bigger than ``settings.document.max_bytes``. ⚠ This is OUR
#: reader's ceiling, not the file being invalid — the message a person sees has
#: to say which, or they re-upload the same file expecting a different answer.
ATTACHMENT_TOO_LARGE = "attachment_too_large"

#: MIME outside the supported set.
UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"

#: A PDF we could open but not decrypt.
DOCUMENT_PASSWORD_PROTECTED = "document_password_protected"

# ── the model stage ──────────────────────────────────────────────
#: Three tokens the PROMPT instructs the model to append (rules 3/4/5). They
#: are prose the model reads, so they cannot be interpolated from here — the
#: guard asserts each appears in the prompt AND in this vocabulary instead.
LOW_CONFIDENCE = "low_confidence"
DOC_TYPE_UNCERTAIN = "doc_type_uncertain"
EMPTY_DOCUMENT = "empty_document"

#: Prefix only: the impl appends the exception text after it
#: (``invalid_extraction: <exc>``). ⚠ A match on this has to be a PREFIX test —
#: an equality check silently never fires, which is how a hard failure ends up
#: rendered as an unrecognised free-text note.
INVALID_EXTRACTION_PREFIX = "invalid_extraction"

# ── diagnostics: the extraction WORKED, these describe how ───────
#: The first response failed to parse and a single retry recovered.
RETRIED_ONCE_AFTER_MALFORMED_JSON = "retried_once_after_malformed_json"

#: The first response parsed only after stripping noise around the JSON.
RECOVERED_NOISY_JSON = "recovered_noisy_json_attempt_0"

#: Emitted when the PDF went through the text-layer path rather than being sent
#: as an image. ⚠ Not an identifier-shaped token — it is already a sentence,
#: and it NAMES AN INTERNAL LIBRARY. "docling" means nothing to a property
#: manager, so it needs translating exactly like the others do.
DOCLING_NOTE = "pdf parsed to text via docling"

#: Every enumerable token, i.e. everything a person must never be shown raw.
#: Anything NOT in here is assumed to be the model's own free text and is
#: passed through untouched — see the module note.
MACHINE_TOKENS: frozenset[str] = frozenset(
    {
        ATTACHMENT_UNAVAILABLE,
        ATTACHMENT_TOO_LARGE,
        UNSUPPORTED_MIME_TYPE,
        DOCUMENT_PASSWORD_PROTECTED,
        LOW_CONFIDENCE,
        DOC_TYPE_UNCERTAIN,
        EMPTY_DOCUMENT,
        INVALID_EXTRACTION_PREFIX,
        RETRIED_ONCE_AFTER_MALFORMED_JSON,
        RECOVERED_NOISY_JSON,
        DOCLING_NOTE,
        WARN_UNAVAILABLE,
        WARN_UNAUTHORIZED,
        WARN_QUOTA_EXHAUSTED,
    }
)

#: The subset meaning NOTHING USABLE CAME BACK. A result carrying one of these
#: has no fields, no summary and 0.0 confidence, so a client that renders the
#: usual "here is what I found" scaffolding around it is describing an
#: extraction that did not happen.
#:
#: ⚠ ``low_confidence`` / ``doc_type_uncertain`` / the two retry notes are
#: deliberately NOT here: those results carry real extracted fields and the
#: warning is a caveat on them, not a failure.
FAILURE_TOKENS: frozenset[str] = frozenset(
    {
        ATTACHMENT_UNAVAILABLE,
        ATTACHMENT_TOO_LARGE,
        UNSUPPORTED_MIME_TYPE,
        DOCUMENT_PASSWORD_PROTECTED,
        EMPTY_DOCUMENT,
        INVALID_EXTRACTION_PREFIX,
        WARN_UNAVAILABLE,
        WARN_UNAUTHORIZED,
    }
)


def is_failure(warning: str) -> bool:
    """Does this one warning mean the extraction produced nothing usable?

    ⚠ **Prefix, not equality, for ``invalid_extraction``.** The impl appends the
    exception text after it, so a membership test against the raw string never
    matches and the hardest failure in the set reads as an ordinary note. The
    call sites this replaces were plain ``in`` checks against a hand-typed
    frozenset and had exactly that hole.
    """
    token = (warning or "").strip()
    if not token:
        return False
    if token.startswith(INVALID_EXTRACTION_PREFIX):
        return True
    return token in FAILURE_TOKENS


def nothing_was_read(warnings: object) -> bool:
    """True when a result's warning list says the extraction did not happen.

    Takes the whole list because that is the question callers actually ask, and
    because a per-item check is the thing a caller gets subtly wrong.
    """
    if not isinstance(warnings, (list, tuple)):
        return False
    return any(isinstance(w, str) and is_failure(w) for w in warnings)
