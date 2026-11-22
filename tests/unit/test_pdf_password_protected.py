"""A password-protected PDF said "could not be read" and nothing more.

The pipeline handles an encrypted PDF the way it handles every other parse
failure: `_pdf_to_text` catches the exception, falls back to the multimodal
path, the model is handed ciphertext, and the result comes back with every
field null. The specialist's rule 3 then tells the user the document could not
be read — true, and useless. The one thing the reader can act on is the one
thing nobody said: *remove the password and send it again* (coverage checklist
#124).

This is gotcha 46's complaint, one surface over. There, "temporarily
unavailable" sent somebody to retry a spend cap that would not clear for a
week. Here, "could not be read" sends them to re-upload a file that will fail
identically every time.

⚠ **A DIAGNOSIS, NOT A GATE — and that is the whole design.** A PDF can be
encrypted with an EMPTY user password (the permissions-only kind a printer or a
bank emits) and every parser opens those normally. Nothing structural separates
that from a real password without attempting decryption, which needs a crypto
library this backend does not carry. So refusing on the marker would turn a
class of working documents into refusals — gotcha 55's over-block, which is
worse than missing one. Instead the ordinary pipeline runs, and the marker is
consulted only once the read has ALREADY produced nothing: at that point
"encrypted" stops being a guess about the file and becomes the explanation for
a failure we measured.

⚠ **The cause rides ALONGSIDE the generic warning, never instead of it**
(gotcha 46). Every membership check — `company_knowledge._FAILURE_WARNINGS`,
the golden cases' `warnings_must_not_include`, the specialist's own rules — is
therefore untouched, and this only adds a sentence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.agents import document_tools
from helixis.agents.document_tools import (
    PDF_ENCRYPTED_WARNING,
    DocumentExtraction,
    SourceRef,
    _note_encrypted_pdf,
    pdf_carries_encryption_marker,
)

_TOOLS = Path("src/helixis/agents/document_tools.py")
_AGENT = Path("src/helixis/agents/document_agent.py")


def _pdf(body: bytes) -> bytes:
    return b"%PDF-1.7\n" + body + b"\n%%EOF\n"


# ── does this file carry the marker ──────────────────────────────────


@pytest.mark.parametrize(
    "trailer",
    [
        b"trailer\n<< /Size 9 /Root 1 0 R /Encrypt 8 0 R >>",
        b"trailer<</Encrypt 12 0 R/Root 1 0 R>>",
        # An inline dictionary rather than an indirect reference.
        b"trailer\n<< /Encrypt << /Filter /Standard /V 4 /R 4 >> >>",
        # Whitespace is not normalised in the wild.
        b"trailer\n<< /Encrypt   340   0   R >>",
    ],
)
def test_an_encrypted_pdf_is_recognised(trailer: bytes) -> None:
    assert pdf_carries_encryption_marker(_pdf(trailer)) is True


@pytest.mark.parametrize(
    "body",
    [
        # ⚠ The direction that matters. A lease whose TEXT discusses encryption
        # must never be reported to its owner as password-protected — that is a
        # working document refused, and it is why the pattern requires the
        # reference shape rather than the bare word.
        b"BT (All tenant data shall be stored /Encrypt-ed at rest.) Tj ET",
        b"BT (See section 4: Encrypt / Encryption of records.) Tj ET",
        b"trailer\n<< /Size 9 /Root 1 0 R >>",
        b"",
    ],
)
def test_an_ordinary_pdf_is_left_alone(body: bytes) -> None:
    assert pdf_carries_encryption_marker(_pdf(body)) is False


def test_a_non_pdf_is_not_inspected() -> None:
    """The marker means nothing outside a PDF, and a text file quoting it is
    exactly the shape that would false-positive."""
    assert pdf_carries_encryption_marker(b"notes: /Encrypt 1 0 R was in the header") is False
    assert pdf_carries_encryption_marker(b"") is False


def test_the_header_has_to_be_near_the_front() -> None:
    """The spec allows junk before ``%PDF-`` only within the first bytes. A
    file that merely CONTAINS the string much later is not one."""
    assert pdf_carries_encryption_marker(b"x" * 5000 + _pdf(b"/Encrypt 8 0 R")) is False


# ── when the cause is attached ───────────────────────────────────────


def _result(fields: dict, warnings: list[str]) -> DocumentExtraction:
    return DocumentExtraction(
        doc_type="other",
        confidence=0.0,
        source_ref=SourceRef(kind="file", id="lease.pdf"),
        summary="",
        fields=fields,
        warnings=list(warnings),
    )


def test_an_empty_read_of_an_encrypted_pdf_gets_the_cause() -> None:
    out = _note_encrypted_pdf(
        _result({"tenant_name": None, "rent": None}, ["invalid_extraction: boom"]),
        encrypted=True,
    )
    assert PDF_ENCRYPTED_WARNING in out.warnings


def test_the_generic_warning_still_leads() -> None:
    """⚠ Gotcha 46: the cause is ADDED, never substituted. Replacing the
    generic token would pass the prohibition and break every membership check
    that reads it — `_FAILURE_WARNINGS`, the specialist's rules, the golden
    cases."""
    out = _note_encrypted_pdf(_result({"rent": None}, ["invalid_extraction: boom"]), encrypted=True)
    assert out.warnings[0] == "invalid_extraction: boom"
    assert out.warnings[-1] == PDF_ENCRYPTED_WARNING


def test_a_read_that_produced_a_field_is_never_told_it_failed() -> None:
    """⚠ The permissions-only PDF. It is encrypted, it opened fine, and telling
    its owner to strip a password they never set would be a confident false
    claim about their file (gotcha 41's shape, pointed at a document)."""
    out = _note_encrypted_pdf(_result({"rent": "$2,400", "tenant_name": None}, []), encrypted=True)
    assert PDF_ENCRYPTED_WARNING not in out.warnings


def test_an_unencrypted_failure_keeps_todays_wording() -> None:
    """A scan with no text layer fails identically and is NOT password-
    protected. Claiming a cause we cannot support is worse than no cause."""
    scan = _result({"rent": None}, ["invalid_extraction: boom"])
    assert PDF_ENCRYPTED_WARNING not in _note_encrypted_pdf(scan, encrypted=False).warnings


def test_the_cause_is_not_appended_twice() -> None:
    already = _result({"rent": None}, ["invalid_extraction: boom", PDF_ENCRYPTED_WARNING])
    out = _note_encrypted_pdf(already, encrypted=True)
    assert out.warnings.count(PDF_ENCRYPTED_WARNING) == 1


# ── it is actually wired, at BOTH failure exits ──────────────────────
#
# ⚠ A behavioural test of the helper cannot see an exit that stopped calling it
# (gotchas 62/66/69 are three separate instances of exactly that), and this
# function has two failure exits that reach a user: the out-of-budget
# `invalid_extraction` return, and the final all-null result. Fixing one is
# gotcha 25's half-applied rule.


def _extract_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "extract_document_fields_impl":
            return node
    raise AssertionError("extract_document_fields_impl not found")


def test_both_failure_exits_run_the_diagnosis() -> None:
    calls = [
        n
        for n in ast.walk(_extract_fn())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_note_encrypted_pdf"
    ]
    assert len(calls) == 2, (
        f"_note_encrypted_pdf is called {len(calls)} time(s). Both failure exits "
        "need it — the out-of-budget `invalid_extraction` return AND the final "
        "all-null result — or a password-protected PDF says 'could not be read' "
        "on whichever path was missed."
    )


def test_the_marker_is_actually_measured() -> None:
    """⚠ Found by MUTATION, not by review. Hardcoding ``pdf_encrypted = False``
    kills the whole feature — every guard above still passes, because each one
    hands ``encrypted=`` to the helper itself. A positional check ("is there an
    assignment named pdf_encrypted") passes too. What has to be asserted is
    that the VALUE calls the detector."""
    assign = next(
        (
            n
            for n in ast.walk(_extract_fn())
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pdf_encrypted" for t in n.targets)
        ),
        None,
    )
    assert assign is not None, "extract_document_fields_impl never binds pdf_encrypted"
    called = {
        n.func.id
        for n in ast.walk(assign.value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "pdf_carries_encryption_marker" in called, (
        f"pdf_encrypted is bound to {ast.unparse(assign.value)!r} — the detector "
        "is never run, so no PDF is ever diagnosed and the feature is dead code"
    )


def test_the_marker_reads_the_original_bytes() -> None:
    """It must read `raw_bytes`. The docling branch below rebinds `build_bytes`
    to parsed text; measured off that, it would inspect a different buffer."""
    src = _TOOLS.read_text(encoding="utf-8")
    assert src.index("pdf_encrypted = ") < src.index("build_bytes = parsed_text.encode"), (
        "the encryption marker is measured after build_bytes is replaced with "
        "docling's parsed text — it would be inspecting the wrong bytes"
    )


def test_the_specialist_knows_what_to_do_with_the_cause() -> None:
    """⚠ The half that reaches the user. A warning token nobody's prompt reads
    is a fact the product holds and never says (gotcha 83's dead assertion key,
    pointed at a prompt)."""
    prompt = _AGENT.read_text(encoding="utf-8")
    assert PDF_ENCRYPTED_WARNING in prompt, (
        "the document specialist has no branch for the cause, so the reply is "
        "the generic 'could not be read' the cause exists to improve"
    )
    assert "password-protected" in prompt
    assert "unlocked copy" in prompt


def test_the_prompt_forbids_the_retry_advice_that_cannot_work() -> None:
    """Gotcha 46's actual lesson: the failing sentence was not false, it was
    ACTIONABLE-SOUNDING and wrong. Re-sending the same locked PDF fails
    identically."""
    # ⚠ Whitespace-normalised. The prompt is hard-wrapped prose, so an
    # assertion carrying its line breaks fails the next time a word is added
    # ahead of it — the same reflow trap as gotcha 40, arriving in a docstring.
    rule = " ".join(_AGENT.read_text(encoding="utf-8").split(PDF_ENCRYPTED_WARNING, 1)[1].split())
    rule = rule[:700]
    assert 'Do NOT say "temporarily"' in rule
    assert "do NOT suggest trying again" in rule


def test_an_unreadable_pdf_never_gets_a_digest_cached_for_it() -> None:
    """⚠ RE-POINTED. This asserted the token must stay OUT of the digest's
    failure set, on the stated premise that "the generic warning beside it
    already carries that". **That premise is false, and the shape it misses is
    the expensive one.**

    `_note_encrypted_pdf` appends the token whenever `_extraction_is_empty` —
    every field null — which is ALSO what a model produces when it follows
    prompt rules 1 and 4: null each field, name each one in a warning, set
    `doc_type: "other"`. That result carries no generic failure token at all,
    so the old membership check passed it straight through and
    `_compose_digest` cached the model's SUMMARY:

        warnings: ['tenant_name: not visible', 'doc_type_uncertain',
                   'document_password_protected']
        cached:   'Appears to be a residential lease agreement.'

    A sentence about a document nothing could open, stored as the company's
    knowledge of it and reused in later answers. The concern the original test
    named is real — a digest decision must not turn on WHY — and the property
    that actually protects it is stated directly here instead: an unreadable
    document gets no digest, whatever the reason.
    """
    from helixis.agents.document_warnings import nothing_was_read

    unreadable = _result({"tenant_name": None}, ["tenant_name: not visible"])
    noted = _note_encrypted_pdf(unreadable, encrypted=True)

    assert PDF_ENCRYPTED_WARNING in noted.warnings
    assert nothing_was_read(noted.warnings), (
        "an encrypted PDF whose read produced nothing is being treated as a "
        "readable document, so a summary of it gets cached as company knowledge"
    )


def test_the_helper_is_importable_under_its_public_name() -> None:
    """A guard the next reader can find. `pdf_carries_encryption_marker` says
    what it measures — carries a MARKER — rather than `is_encrypted`, which
    would assert the thing this module deliberately declines to claim."""
    assert callable(document_tools.pdf_carries_encryption_marker)
