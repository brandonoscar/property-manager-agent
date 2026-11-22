"""An uploaded file has to BE what it says it is — and the frontend has to
keep having nowhere to put raw HTML.

TWO GAPS, AND ONLY ONE OF THEM WAS A DEFECT
-------------------------------------------
**`POST /agent/extract-document` trusted a client string.** It gated on
`body.mime_type` and never looked at a byte, while the sibling upload route
340 lines away in `documents.py` had checked `%PDF` since it was written.
Gotcha 25's "fix it at every layer", arriving as a rule that only ever reached
one of two.

⚠ **Be precise about what that is, because the obvious framing overstates it.**
The bytes there are base64 in a JSON body: decoded, handed to a multimodal
model, never written to disk, executed, or served to a browser. So it is NOT
the "executable disguised with a benign extension" it looks like. What it
actually is — the product tells a provider "this is a lease PDF", ships
something else, and reports whatever comes back as a confident extraction. A
plausible wrong answer is this repo's documented worst failure mode, and this
one costs a real multimodal call to produce.

**The XSS surface was already clean and nothing held it there.** Measured
before writing anything: zero `dangerouslySetInnerHTML` in the whole frontend,
and the single `.innerHTML` assignment is a hardcoded constant in `main.tsx`'s
OAuth-callback branch with no interpolation. So there was no defect to fix —
the gap was that the next component to render a tenant-authored maintenance
description could reopen it with nobody noticing. That is a guard, not a fix,
and saying which is which matters (gotcha 58: name the decisions).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from helixis.security.file_types import SNIFF_BYTES, content_matches, has_signature

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "frontend" / "src"

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00"
HTML = b"<!doctype html><script>alert(1)</script>"
PE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00"


# ── the sniffer, both directions ──────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "mime"),
    [
        (PDF, "application/pdf"),
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (JPEG, "image/jpg"),  # unregistered, but browsers send it
        (WEBP, "image/webp"),
    ],
)
def test_a_real_file_of_each_type_is_accepted(content: bytes, mime: str) -> None:
    """The direction that keeps the check switched on. A sniffer that rejects
    genuine files is one somebody removes within a week (gotcha 52)."""
    assert content_matches(content, mime) is True


@pytest.mark.parametrize(
    ("content", "mime"),
    [
        (HTML, "application/pdf"),
        (PE, "application/pdf"),
        (PNG, "application/pdf"),
        (PDF, "image/png"),
        (HTML, "image/png"),
        (PDF, "image/jpeg"),
        (PDF, "image/webp"),
    ],
)
def test_a_file_that_lies_about_itself_is_refused(content: bytes, mime: str) -> None:
    assert content_matches(content, mime) is False


def test_a_riff_container_that_is_not_webp_is_refused() -> None:
    """The one signature that cannot be a `startswith`.

    RIFF holds a 4-byte LENGTH between the two tags, so an AVI and a WebP
    share their first four bytes. Checking only the prefix would accept every
    RIFF file as an image.
    """
    avi = b"RIFF\x24\x00\x00\x00AVI LIST"
    assert avi[:4] == WEBP[:4], "the fixtures must share a prefix or this proves nothing"
    assert content_matches(WEBP, "image/webp") is True
    assert content_matches(avi, "image/webp") is False


def test_the_png_signature_is_checked_in_full() -> None:
    """A mutation battery found this hole, and review would not have.

    Truncating the PNG rule to its first byte left every other test green —
    the negatives are a PDF (`%`) and an HTML file (`<`), and neither starts
    with 0x89, so a one-byte rule still rejected them. RFC 2083 §3.1 makes the
    signature eight bytes ON PURPOSE: the CR/LF pair catches transfers that
    mangle newlines, and the length catches unrelated files that happen to
    share a leading byte. This is the fixture that can tell those apart.
    """
    almost = b"\x89NOT-A-PNG\x00\x00"
    assert almost[:1] == PNG[:1], "the fixtures must share a byte or this proves nothing"
    assert content_matches(PNG, "image/png") is True
    assert content_matches(almost, "image/png") is False

    # The newline half of the same signature: a file mangled in transfer.
    mangled = PNG.replace(b"\r\n", b"\n")
    assert content_matches(mangled, "image/png") is False


def test_a_type_with_no_signature_answers_none_rather_than_guessing() -> None:
    """THE THREE-VALUED PART, and it is deliberate.

    Any byte string is valid `text/plain`, so no signature exists and none
    should be invented. Collapsing this into True waves through every future
    type added to a caller and not to the map; collapsing it into False
    rejects plain text outright. Neither is honest — same reasoning as
    write_verify's WROTE-UNVERIFIED (gotcha 70).
    """
    assert content_matches(b"anything at all", "text/plain") is None
    assert has_signature("text/plain") is False
    assert content_matches(b"...", "application/x-newly-added") is None


def test_it_reads_only_the_head_of_the_file() -> None:
    """A 15 MB upload must not be re-scanned in full to answer a question the
    first dozen bytes settle."""
    assert SNIFF_BYTES <= 64
    assert content_matches(PDF[:SNIFF_BYTES], "application/pdf") is True


def test_a_short_or_empty_file_never_passes() -> None:
    assert content_matches(b"", "application/pdf") is False
    assert content_matches(b"%PD", "application/pdf") is False
    assert content_matches(b"RIFF", "image/webp") is False  # truncated container


# ── both routes read the one definition (gotchas 25 / 62) ─────────────


def _calls_in(path: Path, func_name: str) -> set[str]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
    raise AssertionError(f"{func_name} not found — re-point this guard, do not delete it")


@pytest.mark.parametrize(
    ("module", "route"),
    [
        ("agent.py", "extract_document"),
        ("documents.py", "upload_document"),
    ],
)
def test_every_upload_route_checks_the_bytes(module: str, route: str) -> None:
    """BOTH, because the whole defect was a rule that reached one of two.

    A behavioural test of the sniffer cannot see a route that never calls it —
    gotchas 62/66/69 are three separate instances of exactly that.
    """
    path = _ROOT / "src" / "helixis" / "api" / "routes" / module
    assert "content_matches" in _calls_in(path, route), (
        f"{module}::{route} accepts whatever type the client claims"
    )


def test_the_signature_is_not_restated_beside_the_call() -> None:
    """One definition, or the two routes drift into disagreeing about what a
    PDF is — which is how this started."""
    for module in ("agent.py", "documents.py"):
        src = (_ROOT / "src" / "helixis" / "api" / "routes" / module).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert 'startswith(b"%PDF")' not in code, (
            f"{module} carries its own copy of the PDF signature"
        )


# ── the XSS line the frontend currently holds (item 329) ──────────────
#
# ⚠ NOTHING IS BROKEN TODAY and that is worth stating: measured before writing
# this, the frontend has zero `dangerouslySetInnerHTML` and one `.innerHTML`
# assignment, a hardcoded constant with no interpolation. This is a guard
# against reopening it, not a fix — the next component to render a tenant's
# maintenance description is where it would come back.

_TSX = sorted(_FRONTEND.rglob("*.tsx")) + sorted(_FRONTEND.rglob("*.ts"))


def test_the_scan_reads_the_frontend_it_is_supposed_to_measure() -> None:
    """A wrong root collects zero files and reports green — the self-check
    gotcha 49's guard needed for the same reason."""
    assert len(_TSX) > 100, f"only {len(_TSX)} frontend files found — wrong path?"


def test_no_component_renders_raw_html() -> None:
    offenders = [
        f.relative_to(_ROOT).as_posix()
        for f in _TSX
        if "dangerouslySetInnerHTML" in f.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "dangerouslySetInnerHTML appears in: "
        + ", ".join(offenders)
        + ".\nEvery string this app renders can carry tenant-authored text — a "
        "maintenance description, a lead's reply, a vendor note. React escapes "
        "by default and that default is the whole defence. If raw markup is "
        "genuinely required, sanitize at the boundary and add the file here "
        "with the reason."
    )


#: `path:line` → why this assignment cannot carry user data.
#:
#: ⚠ AN ALLOWLIST, NOT A CLEVER REGEX, and the first draft is why. A pattern
#: like `\.innerHTML\s*=\s*(?!['"])` reads correctly and does not work: `\s*`
#: backtracks to zero width, so the lookahead lands on the SPACE after `=` and
#: every safe literal assignment matches. Its own self-test caught that.
#:
#: The deeper problem is that no line-based pattern survives formatting. The
#: real site in `main.tsx` puts the `=` at the end of one line and the string
#: on the next, exactly as prettier left it — so a line scan sees an
#: assignment with no right-hand side and any rule about "what follows the
#: equals sign" is reading the wrong line (gotchas 40/49).
#:
#: Every assignment is therefore flagged, and the safe ones are named here
#: with a reason. That cannot go blind on a reflow, and a new site has to be
#: looked at by a person rather than argued past by a regex.
ALLOWED_INNER_HTML: dict[str, str] = {
    "frontend/src/main.tsx:24": (
        "the OAuth-callback branch, before React mounts — a hardcoded constant "
        "with no interpolation, on a page that renders no user data at all"
    ),
}

_INNER_HTML_ASSIGN = re.compile(r"\.innerHTML\s*=")


def _inner_html_sites() -> set[str]:
    return {
        f"{f.relative_to(_ROOT).as_posix()}:{i}"
        for f in _TSX
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if _INNER_HTML_ASSIGN.search(line)
    }


def test_every_inner_html_assignment_is_declared_safe() -> None:
    """The one that would catch a tenant's maintenance description reaching
    the DOM unescaped."""
    undeclared = sorted(_inner_html_sites() - set(ALLOWED_INNER_HTML))
    assert not undeclared, (
        "innerHTML assigned at: "
        + ", ".join(undeclared)
        + ".\nEvery string this app renders can carry tenant-authored text — a "
        "maintenance description, a lead's reply, a vendor note. If the value "
        "is genuinely a constant, add the line to ALLOWED_INNER_HTML with the "
        "reason; if it is not, render it as text and let React escape it."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """An entry for an assignment that no longer exists is a licence nobody
    needs, and it goes stale silently the moment a line moves — the same
    failure `test_tenant_scoping_corpus` records for its own allowlist."""
    stale = sorted(set(ALLOWED_INNER_HTML) - _inner_html_sites())
    assert not stale, (
        f"ALLOWED_INNER_HTML entries matching nothing: {stale} — the assignment "
        "was removed or moved. Re-point the entry, or delete it."
    )


def test_the_scan_would_catch_a_new_assignment() -> None:
    """Mutation in test form: a pattern matching nothing passes both tests
    above forever (gotcha 45's "silently permits more" — the failure mode you
    would never otherwise notice)."""
    assert _INNER_HTML_ASSIGN.search("el.innerHTML = userText")
    assert _INNER_HTML_ASSIGN.search("node.innerHTML=`<b>${name}</b>`")
    assert _INNER_HTML_ASSIGN.search("  container.innerHTML =")  # reflowed onto two lines
    assert not _INNER_HTML_ASSIGN.search("const html = el.innerHTML")  # a READ is not a write
