"""The customer never sees the carrier's name.

Occupella white-labels its messaging: the Twilio account is OURS, and a
customer gets a dedicated number on it. They have no login there, no
credentials to supply and no invoice from them. So a user-visible "Twilio"
is not a cosmetic slip — it invites someone to go looking for an account
that does not exist, and it named a $1.15/month line charge they will never
be billed for.

This is exactly the kind of invariant that erodes one string at a time: every
individual mention looks harmless while someone is deep in a feature, and
nothing fails when one slips back in. Same shape as gotcha 45 — a control
whose failure mode is "silently permits more" needs the corpus measured, not
the rule restated.

⚠ **Capitalisation is the discriminator, and it is deliberate.** A quoted
lowercase ``'twilio'`` is an internal identifier — the connector id, a route
segment, a settings key — and engineers need it to stay. A quoted capitalised
``"Twilio"`` is prose written for a human. That split is what lets this guard
be strict about display copy without banning the vendor from the codebase,
which would be absurd: the backend integrates with them.

Comments and docstrings are stripped first: they SHOULD name the vendor, and
several explain this very rule. A guard that fired on the sentence documenting
it is gotcha 40's trap, and the tempting fix — dropping the comment handling —
is what eventually makes it blind.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: Files allowed to carry a capitalised vendor name in a string literal.
#: Empty on purpose — every one found while writing this was fixable, and an
#: exemption here is a promise that some screen still says it.
_ALLOW: set[str] = set()

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<![:'\"])//[^\n]*")
#: A quoted string or template literal carrying the capitalised vendor name.
_QUOTED_VENDOR = re.compile(r"""(['"`])[^'"`\n]*\bTwilio\b[^'"`\n]*\1""")


def _strip_comments(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def _sources() -> list[Path]:
    return sorted(p for p in _FRONTEND.rglob("*.ts*") if ".test." not in p.name)


def test_the_scan_reads_a_real_corpus() -> None:
    """Self-check. Every assertion below is vacuous against an empty file
    list, and a scan that silently reads nothing reports green (gotcha 49)."""
    files = _sources()
    assert len(files) > 50, f"only found {len(files)} frontend sources -- wrong path?"
    assert any("Twilio" in p.read_text(encoding="utf-8") for p in files), (
        "no file mentions the vendor at all -- the scan is not reading what it thinks"
    )


def test_no_user_visible_string_names_the_carrier() -> None:
    offenders: list[str] = []
    for path in _sources():
        if path.name in _ALLOW:
            continue
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for match in _QUOTED_VENDOR.finditer(body):
            rel = path.relative_to(_FRONTEND.parent.parent)
            offenders.append(f"{rel}: {match.group(0)[:80]}")
    assert not offenders, (
        "user-visible copy names the messaging carrier. The account is ours, so "
        "this sends a customer looking for credentials they do not have:\n  "
        + "\n  ".join(offenders)
    )


def test_jsx_prose_does_not_name_the_carrier() -> None:
    """The other half. Rendered text is not always a string literal -- in JSX
    it is bare prose between tags, which the quoted-literal scan above cannot
    see. Both halves are needed; either alone leaves a way to say it."""
    offenders: list[str] = []
    for path in _FRONTEND.rglob("*.tsx"):
        if ".test." in path.name or path.name in _ALLOW:
            continue
        body = _strip_comments(path.read_text(encoding="utf-8"))
        # Strip quoted literals so this half only judges bare JSX text; the
        # test above already owns the quoted case and double-reporting one
        # string as two findings makes the failure harder to act on.
        body = _QUOTED_VENDOR.sub("", body)
        # ⚠ Do NOT require a tag character on the line. The first version of
        # this matched `>...Twilio` or `Twilio...<`, which misses the ordinary
        # case entirely: JSX prose wraps, so a sentence sits on its own line
        # with no bracket anywhere near it. Mutation caught it — a planted
        # "Your Twilio account is not connected yet." sailed through.
        #
        # After comments and quoted strings are gone, a STANDALONE `Twilio` can
        # only be prose: `\b` boundaries exclude every identifier we keep
        # (`TwilioNumber`, `listTwilioNumbers`) because a word character sits
        # against the name on one side.
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "export ")):
                continue
            if re.search(r"\bTwilio\b", stripped):
                offenders.append(f"{path.relative_to(_FRONTEND.parent.parent)}: {stripped[:80]}")
    assert not offenders, "JSX prose names the carrier:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize(
    "identifier",
    ["listTwilioNumbers", "TwilioNumber", "'twilio'"],
)
def test_internal_identifiers_are_deliberately_untouched(identifier: str) -> None:
    """⚠ The counterweight. Renaming the integration itself would be the wrong
    reading of "white-label": an engineer opening this code needs to know which
    vendor it talks to. Only the words a CUSTOMER reads are constrained.

    This fails if someone over-applies the rule and scrubs the codebase.
    """
    hits = sum(identifier in p.read_text(encoding="utf-8") for p in _sources())
    assert hits, (
        f"{identifier} is gone. White-label constrains user-visible COPY, not "
        "internal names -- scrubbing the vendor from the code makes the "
        "integration harder to work on for no compliance benefit."
    )
