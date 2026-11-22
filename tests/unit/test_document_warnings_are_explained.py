"""Every machine warning the extractor can emit has a sentence a person can read.

A property manager attached a scan, waited out the upload, and was shown:

    ### Notes
    - attachment_too_large

The reason was known precisely — their file was bigger than our reader opens —
and the product had simply never turned it into words. This file is the guard
that keeps that from recurring, and it has to read BOTH sides (gotcha 28): the
Python call sites that EMIT tokens, and the TypeScript map that EXPLAINS them.
A token added to one and not the other fails here rather than on a screen.

⚠ **The pass-through arm is why completeness matters.** `explainWarning` returns
an unrecognised warning unchanged, because the extraction prompt asks the MODEL
to write free-text warnings naming any field it had to null out — the most
useful line in the result. So "drop what I don't recognise" is not available as
a safety net, and an un-enumerated machine token would sail straight through to
the reader exactly as before.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from helixis.agents import document_warnings as warn
from helixis.settings import DocumentSettings

_REPO = Path(__file__).resolve().parents[2]
_TOOLS = _REPO / "src/helixis/agents/document_tools.py"
_CLIENT = _REPO / "frontend/src/lib/documentWarnings.ts"

#: The two functions that return ``(value, warning_token)`` tuples. Scoped by
#: NAME rather than scanning every 2-tuple return in the module, because plenty
#: of those carry ordinary strings; a staleness test below fails if either is
#: renamed, so the narrowing cannot go quietly out of date (gotcha 63).
_TUPLE_RETURN_FUNCTIONS = {"_fetch_bytes", "_call_llm"}


# ---------------------------------------------------------------------------
# The Python side: which tokens can actually reach `warnings`.
# ---------------------------------------------------------------------------


def _emitted_tokens() -> set[str]:
    """Every string literal `document_tools` can put into a warning list.

    Three shapes, all of which have produced a live token:
      * ``_empty_extraction(source_ref, "unsupported_mime_type")``
      * ``result.warnings.append("retried_once_after_malformed_json")``
      * ``return None, "attachment_too_large"`` inside the two fetch/call
        helpers.

    AST, not a substring scan: this module's comments and its 200-line prompt
    both QUOTE these tokens in order to explain them, so a text scan fires on
    the prose documenting the rule (gotchas 40/59).
    """
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_empty_extraction":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    value = node.args[1].value
                    if isinstance(value, str):
                        found.add(value)
            if isinstance(func, ast.Attribute) and func.attr == "append":
                if node.args and isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value
                    if isinstance(value, str):
                        found.add(value)

    seen_functions: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in _TUPLE_RETURN_FUNCTIONS:
            continue
        seen_functions.add(node.name)
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or not isinstance(inner.value, ast.Tuple):
                continue
            for element in inner.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    found.add(element.value)

    assert seen_functions == _TUPLE_RETURN_FUNCTIONS, (
        f"the tuple-return allowlist is stale: {_TUPLE_RETURN_FUNCTIONS - seen_functions} "
        "no longer exist in document_tools.py — re-point it, do not delete it"
    )
    return found


def test_every_token_the_extractor_emits_is_in_the_vocabulary() -> None:
    """A new failure token must be declared, not just returned.

    This is the direction that fails silently: `explainWarning` passes an
    unknown string through untouched, so an undeclared token reads as the
    model's own free text and lands on the screen looking like a note somebody
    wrote.
    """
    emitted = _emitted_tokens()

    # Gotchas 49/100/101: a corpus scan that finds nothing is a claim to check,
    # not a result. Name one token that must be there, so a broken walk or a
    # renamed helper cannot collect zero and report green.
    assert warn.ATTACHMENT_TOO_LARGE in emitted, (
        "the scan found no emitted tokens at all — the AST walk is broken, "
        "not the code it is checking"
    )

    undeclared = emitted - warn.MACHINE_TOKENS
    assert not undeclared, (
        f"document_tools emits {sorted(undeclared)}, which document_warnings does "
        "not declare — so no sentence exists for them and they reach the reader raw"
    )


def test_the_prompt_instructed_tokens_are_declared_too() -> None:
    """Three tokens the MODEL writes on instruction, not the code.

    They live in prompt prose the model reads, so they cannot be interpolated
    from the vocabulary — which means the only thing tying the two together is
    this test. Reading both sides is the whole point (gotcha 28).
    """
    prompt = _TOOLS.read_text(encoding="utf-8")
    for token in (warn.LOW_CONFIDENCE, warn.DOC_TYPE_UNCERTAIN, warn.EMPTY_DOCUMENT):
        assert f'"{token}"' in prompt, (
            f"{token!r} is declared in the vocabulary but the extraction prompt "
            "no longer asks for it — one of the two has moved"
        )
        assert token in warn.MACHINE_TOKENS


# ---------------------------------------------------------------------------
# The TypeScript side: what the reader is actually shown.
# ---------------------------------------------------------------------------


def _ts_sentence_keys() -> set[str]:
    """The keys of the client's `SENTENCES` map.

    Three key shapes appear and all three matter: a bare identifier
    (`attachment_unavailable:`), a quoted string (`'pdf parsed to text via
    docling':` — that token has spaces), and one COMPUTED key
    (`[INVALID_EXTRACTION_PREFIX]:`) which is resolved from its own
    declaration. A regex that handled only the first would silently report the
    other two as missing and this guard would fail against correct code.
    """
    src = _CLIENT.read_text(encoding="utf-8")

    block = re.search(r"const SENTENCES[^=]*=\s*\{(.*?)\n\};", src, re.DOTALL)
    assert block, "SENTENCES map not found in documentWarnings.ts — renamed, or reshaped"
    body = block.group(1)

    keys: set[str] = set()
    for match in re.finditer(r"^\s{2}(?:'([^']+)'|\[([A-Za-z_]+)\]|([A-Za-z_][\w]*)):", body, re.M):
        quoted, computed, bare = match.groups()
        if quoted:
            keys.add(quoted)
        elif bare:
            keys.add(bare)
        else:
            constant = re.search(rf"const {computed} = '([^']+)'", src)
            assert constant, f"computed key {computed} has no declaration in the same file"
            keys.add(constant.group(1))
    return keys


def test_every_machine_token_has_a_human_sentence() -> None:
    explained = _ts_sentence_keys()

    assert warn.ATTACHMENT_TOO_LARGE in explained, (
        "the SENTENCES parse found nothing recognisable — the regex is broken, "
        "not the map it is reading"
    )

    missing = warn.MACHINE_TOKENS - explained
    assert not missing, (
        f"{sorted(missing)} can reach the chat client with no sentence, so the raw "
        "token renders under '### Notes' exactly as `attachment_too_large` did"
    )


def test_the_two_languages_agree_on_which_warnings_mean_nothing_was_read() -> None:
    """`FAILURE_TOKENS` decides whether the client renders an answer or a reason.

    Drift in EITHER direction is user-visible: a token missing from the client
    renders the full "here is what I found" scaffolding — a confidence
    percentage and an offer to act on the fields — around an extraction that
    never happened; a token present only on the client suppresses a real result.
    """
    src = _CLIENT.read_text(encoding="utf-8")
    block = re.search(r"const FAILURE_TOKENS:[^=]*=\s*new Set\(\[(.*?)\]\);", src, re.DOTALL)
    assert block, "FAILURE_TOKENS set not found in documentWarnings.ts"

    client_tokens: set[str] = set(re.findall(r"'([^']+)'", block.group(1)))
    # The computed member is the invalid_extraction PREFIX, declared above it.
    if "INVALID_EXTRACTION_PREFIX" in block.group(1):
        client_tokens.add(warn.INVALID_EXTRACTION_PREFIX)

    assert client_tokens == set(warn.FAILURE_TOKENS), (
        f"client-only: {sorted(client_tokens - set(warn.FAILURE_TOKENS))}, "
        f"server-only: {sorted(set(warn.FAILURE_TOKENS) - client_tokens)}"
    )


def test_the_client_ceiling_matches_the_reader_it_is_guarding() -> None:
    """The browser refuses what the reader cannot open — the same number.

    ⚠ This is the defect the whole change exists for. The client's ceiling was
    20 MB against a reader that opens 8, so every file in between was accepted,
    base64-encoded to ~1.33x, uploaded, and refused server-side. The person paid
    the upload to learn something the product already knew.
    """
    src = _CLIENT.read_text(encoding="utf-8")
    match = re.search(r"export const DOCUMENT_MAX_BYTES = ([\d\s*]+);", src)
    assert match, "DOCUMENT_MAX_BYTES not found in documentWarnings.ts"

    client_bytes = eval(match.group(1), {"__builtins__": {}}, {})  # noqa: S307 — digits and '*'
    assert client_bytes == DocumentSettings.model_fields["max_bytes"].default, (
        "the browser's ceiling and the reader's ceiling disagree — whichever is "
        "higher is the one that wastes somebody's upload"
    )


# ---------------------------------------------------------------------------
# The one production caller, which a test of the predicate cannot see.
# ---------------------------------------------------------------------------


def test_the_digest_path_still_asks_the_shared_predicate() -> None:
    """A behavioural test of `nothing_was_read` cannot see a caller ignoring it.

    Gotchas 62/66/69/103 are four separate instances of exactly that: a
    thoroughly-tested helper whose one production call site quietly went back
    to doing its own thing. Here the regression is silent and it CACHES — a
    summary of a document nobody could open, stored as the company's knowledge
    of it and reused in later answers.
    """
    source = (_REPO / "src/helixis/company_knowledge/documents.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "nothing_was_read" in called, (
        "company_knowledge no longer consults the shared failure predicate — a "
        "hand-typed copy of the token list is how this gap opened the first time"
    )


# ---------------------------------------------------------------------------
# The predicate itself.
# ---------------------------------------------------------------------------


def test_invalid_extraction_is_matched_as_a_prefix() -> None:
    """The impl appends the exception text, so equality never fires.

    The hand-typed set this replaces did not contain the token at all, so the
    hardest failure in the vocabulary was classified as an ordinary note —
    which on the client means rendering "0% confidence" and an invitation to
    act on fields that do not exist.
    """
    assert warn.is_failure("invalid_extraction: Expecting value: line 1 column 1")
    assert warn.is_failure(warn.INVALID_EXTRACTION_PREFIX)


def test_a_caveat_is_not_a_failure() -> None:
    """Both directions, or a one-sided test passes against a predicate that
    calls everything a failure (gotchas 38/51) — which would suppress every
    low-confidence extraction the product ever produces."""
    for caveat in (
        warn.LOW_CONFIDENCE,
        warn.DOC_TYPE_UNCERTAIN,
        warn.RETRIED_ONCE_AFTER_MALFORMED_JSON,
        warn.RECOVERED_NOISY_JSON,
        warn.DOCLING_NOTE,
    ):
        assert not warn.is_failure(caveat), f"{caveat} is a note about a real result"
    assert not warn.nothing_was_read([warn.LOW_CONFIDENCE, "monthly_rent: smudged"])
    assert warn.nothing_was_read([warn.LOW_CONFIDENCE, warn.ATTACHMENT_TOO_LARGE])


def test_free_text_from_the_model_is_never_a_failure() -> None:
    # Rule 1 of the extraction prompt asks for exactly this shape.
    assert not warn.is_failure("monthly_rent: figure smudged in the rent clause")
    assert not warn.nothing_was_read(["signed_date: bottom of page 2 illegible"])


def test_a_non_list_is_not_a_claim_either_way() -> None:
    assert not warn.nothing_was_read(None)
    assert not warn.nothing_was_read("attachment_too_large")
