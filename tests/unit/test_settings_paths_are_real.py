"""Every "Settings → X" a customer reads names a section that exists.

Found 2026-09-16 by measuring the on-screen destinations against the nav.
Three strings sent people to sections this product does not have: the demo's
confirm card said *"copy is editable in Settings → Templates"* and *"You can
edit any draft before approving in Settings → Inbox"* — there is no Templates
section and drafts are edited in the Inbox itself, not under Settings — and an
offboarding log line told an operator to *"check Settings → SMS"* when the
consent panel lives at Settings → Connectors → Twilio SMS.

Same class as the contact-support fix that shipped hours earlier: an
instruction the reader cannot carry out is worse than no instruction
(gotchas 102/106/118). Here it is sharper than usual, because the two demo
strings are what a PROSPECT reads while deciding whether this product is
competent.

⚠ THE SCAN MUST NOT FLAG A VENDOR'S OWN SETTINGS, and that is HALF the corpus:
MEASURED at 26 vendor-qualified against 26 of ours. "Buildium → Settings →
Developer Tools → Create API Key" is the path inside BUILDIUM's UI and is
exactly right; so is "Supabase … (Settings → API → JWT Settings)", which names
a dashboard we do not own either. A guard that fires on those is one somebody
switches off (gotchas 52/103/107 — a scan matching too broadly is as useless as
one matching too little). The discriminator is a vendor name in the lead-in, so
the rule is: an unqualified "Settings → X" is OURS and X must be a real
section; a vendor-qualified one is theirs and is not ours to check.

⚠ THE SECTION LIST IS READ OUT OF `SettingsPage.tsx`, never retyped. A
hand-typed copy is how the original fan-out was missed (gotcha 45/99/122), and
it goes stale the first time somebody renames a tab.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "helixis"
FRONTEND = REPO / "frontend" / "src"
SETTINGS_PAGE = FRONTEND / "components" / "SettingsPage.tsx"

# "Settings → X" / "Settings -> X", capturing the destination's first segment.
# The arrow forms are the two this product actually writes; a third spelling
# would simply not be checked, which is the safe direction for a guard whose
# false positive costs a correct vendor path.
_SETTINGS_PATH = re.compile(r"Settings\s*(?:→|->)\s*([A-Z][A-Za-z&' ]*)")

# A vendor named this close BEFORE "Settings" means the path is inside that
# vendor's product, not ours.
#
# ⚠ THE WINDOW IS MEASURED ON THE RENDERED TEXT, NOT THE SOURCE BYTES. The two
# correct Buildium instructions are JSX: "In Buildium:" sits on one line and
# "Settings → Developer Tools" three lines later, inside an <a> whose href,
# className and style put 288 characters of MARKUP between them. A raw-byte
# window wide enough to span that is wide enough to be meaningless. So tags are
# blanked and whitespace collapsed first — the same rule gotcha 77 learned when
# `Reply <strong>STOP</strong>` read as missing the phrase "Reply STOP": check
# the question actually being asked, not the bytes.
_VENDOR_LOOKBACK = 60
_VENDORS = (
    "Buildium",
    "Rentvine",
    # "In DoorLoop, open Settings → Zapier & API Keys": DoorLoop's own screen.
    "DoorLoop",
    "Twilio",
    "Google",
    "Composio",
    "Stripe",
    "Gmail",
    "Supabase",
)

_TAG = re.compile(r"<[^>]*>")


def _lead_in(text: str, at: int) -> str:
    """The rendered words immediately before a destination."""
    raw = text[max(0, at - 400) : at]
    return re.sub(r"\s+", " ", _TAG.sub(" ", raw))[-_VENDOR_LOOKBACK:]


def settings_sections() -> set[str]:
    """The labels a customer can actually click, read from the one home."""
    src = SETTINGS_PAGE.read_text(encoding="utf-8")
    block = re.search(r"const SECTIONS:.*?\n\];", src, re.DOTALL)
    assert block, "SECTIONS moved in SettingsPage.tsx — re-point this guard"
    labels = set(re.findall(r'label:\s*"([^"]+)"', block.group(0)))
    # A scan that finds nothing is a wrong anchor, not an empty nav
    # (gotchas 49/100/101/114).
    assert len(labels) >= 5, f"only {len(labels)} section labels found — wrong anchor?"
    return labels


def _found(text: str) -> list[tuple[int, str]]:
    """(offset, destination) for every path that is OURS to check."""
    out: list[tuple[int, str]] = []
    for m in _SETTINGS_PATH.finditer(text):
        if any(v in _lead_in(text, m.start()) for v in _VENDORS):
            continue
        out.append((m.start(), m.group(1).strip()))
    return out


def _destinations(text: str) -> list[str]:
    """Every destination in a string, minus the ones inside a vendor's UI."""
    return [d for _, d in _found(text)]


def _is_real(dest: str, sections: set[str]) -> bool:
    """A destination is real when it names a section, or starts with one.

    "Connectors → Twilio SMS" and "Billing to pick a plan" both lead with a
    real section and then say where to go inside it, which is the shape the
    product uses everywhere. Only the FIRST segment is a claim about the nav.
    """
    if dest in sections:
        return True
    return any(dest == s or dest.startswith(s + " ") for s in sections)


def _python_user_strings(path: Path) -> list[tuple[int, str]]:
    """String literals, docstrings excluded — a docstring is a note to the next
    reader, and this file's own docstring quotes the dead paths to explain them
    (gotchas 40/59/91/114/124)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_ids.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
        ):
            out.append((node.lineno, node.value))
    return out


_TS_COMMENT = re.compile(r"^\s*(//|\*|/\*)")


def _frontend_prose(path: Path) -> str:
    """The file with comment-only lines blanked IN PLACE.

    ⚠ Blanked, not dropped: the vendor lead-in is three JSX lines above the
    destination, so the scan has to run over the whole file text, and a
    deleted line would slide every later offset — which is what the line
    number is computed from.
    """
    kept = [
        " " * len(line) if _TS_COMMENT.match(line) else line
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return "\n".join(kept)


def test_the_section_list_is_readable_and_names_the_expected_tabs() -> None:
    sections = settings_sections()
    # Not an exhaustive pin — renaming a tab is allowed. These four are the
    # ones other messages point at most, so losing one silently is the drift
    # worth failing on.
    for expected in ("Account", "Connectors", "Billing"):
        assert expected in sections, f"{expected} is no longer a Settings section"


def test_every_backend_settings_instruction_names_a_real_section() -> None:
    sections = settings_sections()
    examined = 0
    offenders: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        for lineno, text in _python_user_strings(path):
            examined += 1
            for dest in _destinations(text):
                if not _is_real(dest, sections):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}: Settings → {dest}")
    assert examined > 5000, f"only {examined} literals examined — wrong root?"
    assert not offenders, (
        "a message sends the reader to a Settings section that does not exist. "
        f"Real sections: {sorted(sections)}\n" + "\n".join(offenders)
    )


def test_every_frontend_settings_instruction_names_a_real_section() -> None:
    sections = settings_sections()
    examined = 0
    offenders: list[str] = []
    for path in sorted(FRONTEND.rglob("*.ts*")):
        if ".test." in path.name or ".spec." in path.name:
            continue
        text = _frontend_prose(path)
        examined += text.count("\n") + 1
        for offset, dest in _found(text):
            if not _is_real(dest, sections):
                lineno = text.count("\n", 0, offset) + 1
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: Settings → {dest}")
    assert examined > 20000, f"only {examined} lines examined — wrong root?"
    assert not offenders, (
        "a message sends the reader to a Settings section that does not exist. "
        f"Real sections: {sorted(sections)}\n" + "\n".join(offenders)
    )


def test_a_vendors_own_settings_path_is_not_flagged() -> None:
    """The five correct "Buildium → Settings → Developer Tools" occurrences are
    the input this guard is most likely to get wrong, so they are pinned
    directly rather than left to the corpus (gotcha 122's reasoning: supply the
    distinguishing input, do not hope the corpus contains one)."""
    assert _destinations("Buildium → Settings → Developer Tools → Create API Key") == []
    assert _destinations("go to Buildium, then Settings -> Developer Tools") == []
    # …while ours, unqualified, is still read.
    assert _destinations("Open Settings → Templates to edit it") == ["Templates to edit it"]


def test_a_vendor_name_split_from_its_path_by_markup_still_counts() -> None:
    """The real shape both live instructions have, supplied directly.

    "In Buildium:" and the path are three JSX lines apart with an <a> tag's
    href/className/style between them — 288 source characters. The corpus
    contains this, but pinning it here is what makes the tag-blanking
    deliberate rather than incidental (gotcha 122's reasoning).
    """
    jsx = (
        "        <p>\n"
        "          In Buildium:{' '}\n"
        "          <a\n"
        '            href="https://developer.buildium.com"\n'
        '            target="_blank"\n'
        '            className="inline-flex items-center gap-1 underline underline-offset-2"\n'
        "            style={{ color: 'var(--iris)' }}\n"
        "          >\n"
        "            Settings → Developer Tools → Create API Key\n"
        "          </a>\n"
        "        </p>\n"
    )
    assert _destinations(jsx) == []
    # And the same markup WITHOUT a vendor is still read — otherwise the
    # blanking would have exempted every multi-line JSX instruction we write.
    assert _destinations(jsx.replace("In Buildium:", "Open")) == ["Developer Tools"]


def test_the_scan_catches_the_three_paths_this_fix_removed() -> None:
    """The canary. Each of these shipped and named nothing real; if the
    matcher stops seeing them, a green run proves nothing (gotchas 49/100)."""
    sections = settings_sections()
    for dead in ("Settings → Templates.", "Settings → Inbox.", "check Settings → SMS"):
        dests = _destinations(dead)
        assert dests, f"the matcher no longer sees {dead!r}"
        assert not any(_is_real(d, sections) for d in dests), (
            f"{dead!r} now resolves to a real section — if a Templates/Inbox/SMS "
            "section was added, delete this case and say so"
        )
    # And the replacements must pass, or the fix would fail its own guard.
    assert all(
        _is_real(d, sections) for d in _destinations("check Settings → Connectors → Twilio SMS")
    )
