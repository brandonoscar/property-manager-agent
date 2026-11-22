"""Every build-time knob the web app reads is named in `frontend/.env.example`.

⚠ A CORPUS guard (gotcha 45's shape), because the failure mode is "silently
permits more": the loud direction — a var documented that nothing reads — is
a tidy-up nobody is hurt by, while the quiet one is a knob an operator has no
way to learn exists. Nothing else can notice it. Vite inlines `VITE_*` at
BUILD time, so an unset var is not an error, not a warning and not a blank
screen; it is whatever fallback the code happened to write down.

**What this was written for.** `VITE_RENDER_BACKEND_URL` was read by
`services/buildium.ts` and documented nowhere — flagged as undocumented by the
2026-04-30 execution-layer audit and still undocumented five months later,
which is gotcha 48's shape at the scale of an audit finding: a note cannot
fail, so it went on being true. It is the one var here whose absence is worse
than a blank. It builds the webhook URL a customer pastes into Buildium, and
its fallback is Occupella's own production backend hostname — so a second
deployment silently tells its customer to point Buildium at somebody else's
backend. Neither side reports it: the receiver does not recognise the
`AccountId` and answers 200 (`webhooks/router.py`, and gotcha 114 records why
that 200 is correct), and the sending deployment's panel shows no events.

⚠ Python, not vitest, and that is the established pattern rather than a
preference. `.env.example` lives at `frontend/`, outside the vitest root, and
`test_favicon_contrast.py` reads `frontend/public/` from pytest for the same
reason. Reaching it from a frontend test means `node:fs`, which runs green
under vitest and turns `tsc --noEmit` RED because that tsconfig has no node
types (gotcha 97).
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = _ROOT / "frontend"
ENV_EXAMPLE = FRONTEND / ".env.example"
SRC = FRONTEND / "src"

#: A var the app reads at build time. Vite's own builtins (`DEV`, `PROD`,
#: `MODE`, `SSR`, `BASE_URL`) are NOT deployment knobs and carry no `VITE_`
#: prefix, so anchoring on the prefix excludes them without an allowlist that
#: would go stale the next time Vite adds one.
_READS = re.compile(r"import\.meta\.env\.(VITE_[A-Z0-9_]+)")

#: An ASSIGNMENT, never a mention — gotcha 40, and this file supplies the
#: distinguishing input itself: `.env.example` names `VITE_PUBLIC_POSTHOG_KEY`
#: and `VITE_SENTRY_DSN` in the COMMENTS above their assignments. A substring
#: check would therefore pass on a var that had only ever been talked about,
#: which is exactly how a knob gets "documented" without being documented.
_ASSIGNS = re.compile(r"^\s*(VITE_[A-Z0-9_]+)\s*=", re.M)


def _production_sources() -> list[pathlib.Path]:
    """Every shipped `.ts`/`.tsx`, excluding tests.

    A var read only by a test is not a deployment knob — documenting it would
    tell an operator to set something that reaches no build.
    """
    return sorted(
        p for p in SRC.rglob("*.ts*") if p.suffix in {".ts", ".tsx"} and ".test." not in p.name
    )


def vars_read() -> set[str]:
    return {
        name
        for path in _production_sources()
        for name in _READS.findall(path.read_text(encoding="utf-8"))
    }


def vars_documented() -> set[str]:
    return set(_ASSIGNS.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_the_scan_reaches_both_sides() -> None:
    """A scan that finds zero is a claim to check, not a result.

    Gotchas 49/100/101/114/128 — six recorded instances of a probe pointed at
    the wrong directory or the wrong key reporting a clean corpus. A renamed
    file, a moved `.env.example` or a bad glob would otherwise turn every
    assertion below into a comparison of two empty sets.
    """
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"
    assert len(_production_sources()) > 100, "the source glob collapsed"
    assert len(vars_read()) >= 5, f"only {len(vars_read())} VITE_ reads found"
    assert len(vars_documented()) >= 5, f"only {len(vars_documented())} documented"


def test_a_var_that_is_only_mentioned_does_not_count_as_documented() -> None:
    """The gotcha-40 trap, pinned with a supplied input because the corpus
    cannot produce one.

    Every var `.env.example` names in a comment today is also assigned below
    it, so a substring implementation and this one agree on every real input —
    which means a corpus-driven mutation of `_ASSIGNS` could not change any
    output and would score ALIVE for a reason about the file rather than the
    code (gotcha 58, and gotcha 122's third mutation is the same shape). The
    distinguishing input is a var talked about and never set, so it is handed
    over directly.

    That input is not hypothetical: "documented" in a comment and never
    assigned is precisely how somebody closes this guard's failure without
    giving an operator a line to copy.
    """
    mention_only = "# Set VITE_TALKED_ABOUT_ONLY if you want the thing.\n"
    assert not _ASSIGNS.findall(mention_only)
    # …and the positive direction, so a pattern that matched NOTHING would not
    # pass this by refusing everything (gotchas 38/51).
    assert _ASSIGNS.findall("VITE_REALLY_SET=x\n") == ["VITE_REALLY_SET"]
    assert _ASSIGNS.findall("  VITE_INDENTED = y\n") == ["VITE_INDENTED"]


def test_every_var_the_app_reads_is_documented() -> None:
    """The direction that matters: an undocumented knob is an unset knob.

    The consequence is per-var and the guard cannot judge it, which is why the
    bar is documentation rather than a safe default. What it can insist on is
    that whoever adds the next one writes down what happens when it is absent.
    """
    missing = sorted(vars_read() - vars_documented())
    assert not missing, (
        f"{len(missing)} build-time var(s) are read by the app and assigned "
        f"nowhere in frontend/.env.example: {missing}. An operator copying that "
        "file has no way to learn they exist, and Vite inlines the fallback "
        "silently. Add each one WITH the consequence of leaving it blank."
    )


def test_every_documented_var_is_actually_read() -> None:
    """The mirror, and it is a staleness check rather than a safety one.

    A var left in `.env.example` after its reader is deleted is a knob an
    operator sets that changes nothing — the same class as a stale allowlist
    entry (gotcha 63), which reads as a licence somebody still needs.
    """
    stale = sorted(vars_documented() - vars_read())
    assert not stale, (
        f"frontend/.env.example documents {stale}, which no shipped frontend "
        "source reads. Delete the line, or re-point it at whatever replaced "
        "the reader."
    )


def test_the_backend_origin_var_states_what_a_blank_costs() -> None:
    """The one var whose absence hands a customer another deployment's host.

    ⚠ It is pinned by NAME and not by the general rule above, because the
    general rule is satisfied by a bare `VITE_RENDER_BACKEND_URL=` with no
    prose — which documents that the knob exists and not that leaving it blank
    points Buildium somewhere else. `services/buildium.ts` still carries the
    hardcoded fallback deliberately: this session could not read Vercel's env
    (gotcha 80), so changing the fallback could break the live webhook URL,
    and the honest move was to document it rather than guess.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # ⚠ ITS OWN comment block, not "everything above the assignment". The
    # loose form passes off a neighbouring var's prose, which is gotcha 103's
    # too-broad scan arriving inside the guard written to be precise. The
    # block is the run of `#` lines immediately preceding the assignment.
    before = text.split("VITE_RENDER_BACKEND_URL", 1)[0].splitlines()
    comment: list[str] = []
    for line in reversed(before):
        if not line.lstrip().startswith("#"):
            break
        comment.append(line)
    block = "\n".join(comment)
    assert "webhook" in block.lower(), (
        "VITE_RENDER_BACKEND_URL is documented without saying it builds the "
        "Buildium webhook URL — which is the only reason its blank is unsafe."
    )
    assert re.search(r"\bfall(s|back| back)\b", block, re.I), (
        "VITE_RENDER_BACKEND_URL is documented without saying what happens "
        "when it is unset. 'It defaults to something' is the whole finding."
    )
