"""The emails Supabase sends on this product's behalf.

They were the stock Supabase templates — plain, unbranded, indistinguishable
from a phishing attempt — and they are the FIRST thing a new customer sees,
before they ever open the app. `docs/deployment/auth-emails/` holds the branded
replacements.

⚠ A CORPUS guard (gotcha 45's shape). These files are pasted into a console by
hand and nothing executes them, so every failure mode here is silent: a
template that loses its link still sends and still looks right, and a template
that names a legal page which does not ship gives a recipient a 404 on the one
document they were told to read. Nothing else in this repo can notice either.

⚠ It cannot check that they were APPLIED. Supabase stores templates in its own
console; this is the same "committed is not applied" line as gotcha 8, and it
is written into the README rather than pretended away.
"""

from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "docs" / "deployment" / "auth-emails"
LEGAL_PAGES = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "public" / "legal"

# The host that serves `frontend/public/legal/*.html`. The web app took
# `app.` on 2026-09-15 and the marketing site took the apex; only one of the
# two ships these files.
APP_HOST = "app.occupella.com"

# ⚠ RECURSIVE, and deliberately still so now that the directory is flat.
# `auth-emails/auth-emails/` held a byte-identical copy of all four templates
# and the README — an accidental nested duplicate. Until 2026-09-15 a
# non-recursive glob meant the guard read neither those files' links nor their
# branding; on 2026-09-16 the duplicate was deleted instead (identical file by
# file, checked first). `test_the_template_directory_is_flat` now holds that
# line. Keeping `rglob` costs nothing while the directory is flat, and means a
# re-introduced copy is READ as well as reported rather than silently skipped.
FILES = sorted(TEMPLATES.rglob("*.html"))

_COMMENT = re.compile(r"<!--.*?-->", re.S)


def markup(path: pathlib.Path) -> str:
    """The template WITHOUT its leading instruction comment.

    ⚠ Not cosmetic, and it caught this file's own author. Each template opens
    with a comment naming its console slot and warning that a full `<html>`
    here would nest a document — so a whole-file scan for `<html` fires on the
    sentence explaining the rule and reports the correct template as broken.
    Gotchas 40 and 59, third instance.

    Stripping is right HERE and would be wrong on the checks below: the comment
    is pasting instructions that never reach an inbox, whereas the link and
    branding checks must read what actually gets sent — which is why only this
    one uses it.
    """
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def test_the_scan_finds_the_templates() -> None:
    """⚠ Every assertion below passes trivially over an empty list. A wrong
    path or a moved directory would report green on nothing, which is the exact
    failure mode of a guard whose job is 'silently permits more'."""
    assert len(FILES) >= 5, f"found {len(FILES)} templates under {TEMPLATES}"
    names = {f.name for f in FILES}
    assert {
        "confirm-signup.html",
        "invite.html",
        "magic-link.html",
        "reset-password.html",
        "change-email.html",
    } <= names


def test_every_template_carries_its_link_twice() -> None:
    """⚠ `{{ .ConfirmationURL }}` IS the email — without it nothing the message
    asks for can be done. Twice, because the button is the first thing a
    plain-text client or a corporate link-scanner eats, and the paste-it-
    yourself fallback is what survives that.

    A template that lost it would still render, still send, and still look
    correct in the console preview.
    """
    for f in FILES:
        body = f.read_text(encoding="utf-8")
        count = body.count("{{ .ConfirmationURL }}")
        assert count >= 2, f"{f.name} uses the confirmation link {count} time(s), needs 2"


# The templates whose email a screen answers with a 6-digit CODE, and the
# call on that screen that checks it. Both sides are read: the template must
# render the code, and the frontend must still verify that type, so a flow
# that moves to links (or a template that loses the code) fails here.
CODE_TEMPLATES = {
    "confirm-signup.html": "type: 'signup'",
    "magic-link.html": "type: 'email'",
}
AUTH_TS = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "services" / "auth.ts"


def test_the_code_emails_carry_the_code() -> None:
    """⚠ `{{ .Token }}` is what the wizard's code step and the sign-in
    screen's "Email me a code" ask for. The repo's Confirm signup template had
    lost it (2026-09-30): pasting it would have sent a link-only email to a
    screen waiting for six digits, and nothing would have errored.

    Read with the instruction comment STRIPPED: the comment names the token to
    explain it, and a template whose only `{{ .Token }}` is in that comment
    sends no code at all.
    """
    auth = AUTH_TS.read_text(encoding="utf-8")
    for name, verify_type in CODE_TEMPLATES.items():
        body = markup(TEMPLATES / name)
        assert "{{ .Token }}" in body, f"{name} does not render the 6-digit code"
        assert verify_type in auth, (
            f"{name} sends a code that services/auth.ts no longer verifies ({verify_type})"
        )


def test_the_email_change_template_names_the_new_address() -> None:
    """ "Someone asked to change your email" without saying to WHAT is an alarm
    with no information — and this is the one message whose whole job is to let
    the real owner notice an account takeover."""
    body = (TEMPLATES / "change-email.html").read_text(encoding="utf-8")
    assert "{{ .NewEmail }}" in body


def test_no_template_wraps_its_own_document() -> None:
    """The console supplies the outer document. An `<html>` here nests one,
    which some clients render and others refuse."""
    for f in FILES:
        body = markup(f).lower()
        for tag in ("<html", "<body", "<head"):
            assert tag not in body, f"{f.name} contains {tag} — the console wraps its own"


def test_no_template_depends_on_something_an_inbox_will_strip() -> None:
    """⚠ Email clients strip `<link>` and most strip `<style>`, so a template
    that keeps its styling in either renders as unstyled text in exactly the
    clients most people use — and looks perfect in the console preview, which
    is a browser."""
    for f in FILES:
        body = markup(f).lower()
        assert "<link" not in body, f"{f.name} loads an external stylesheet"
        assert "<style" not in body, f"{f.name} uses a <style> block"
        assert "var(--" not in body, f"{f.name} uses a CSS variable; an inbox has no tokens"


def test_every_legal_link_names_a_page_that_ships() -> None:
    """Same rule as `legalLinks.test.tsx`, one layer over — and this layer is
    the one a stranger reads with no account, so a 404 here is the worse half.

    ⚠ Absolute URLs only: `/legal/privacy.html` resolves to nothing in an
    inbox, so the check is that the path component names a real file AND that
    the link is not relative.

    ⚠ It reads EVERY occupella.com link, not only the ones already under
    `/legal/`. The first cut filtered on `/legal/` first — so a mutation that
    changed the footer to `https://app.occupella.com/privacy`, which is
    precisely the login-screen URL this whole change removed from the app,
    walked straight past it. A guard that only inspects links already in the
    right shape cannot catch one in the wrong shape.

    ⚠ AND IT PINS THE HOST, which it did not have to before the 2026-09-15
    domain swap. These files ship `/legal/*.html`, and those exist on the WEB
    APP only — the marketing site at the apex serves `/terms` and `/privacy` as
    React routes and has no `/legal/` directory at all. Splitting on the bare
    domain accepts both, so an apex link would read as correct here and 404 in
    a stranger's inbox.
    """
    shipped = {p.name for p in LEGAL_PAGES.glob("*.html")}
    assert len(shipped) >= 4, f"found {len(shipped)} legal pages under {LEGAL_PAGES}"

    seen = 0
    for f in FILES:
        body = f.read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"]+)"', body):
            if href.startswith("mailto:") or "{{" in href:
                continue
            assert href.startswith("http"), f"{f.name}: {href} is relative; an inbox has no origin"
            if "occupella.com" not in href:
                continue
            seen += 1
            assert href.startswith(f"https://{APP_HOST}/"), (
                f"{f.name} links {href} — these documents ship on {APP_HOST}. "
                "The apex is the marketing site and has no /legal/ directory"
            )
            path = href.split(APP_HOST, 1)[1]
            assert path.startswith("/legal/"), (
                f"{f.name} links {href} — {APP_HOST} is the app, an SPA that "
                "answers that path with the login screen"
            )
            page = path.rsplit("/", 1)[-1]
            assert page in shipped, f"{f.name} links {page}, which is not in frontend/public/legal"
    assert seen, "no legal links were checked at all — the footer lost them"


def test_the_template_directory_is_flat() -> None:
    """RETIRES `test_the_glob_reaches_the_nested_duplicate_copies` (2026-09-16),
    and names its replacement rather than simply going away (gotcha 33's rule).

    That test asserted the recursive glob COVERED `auth-emails/auth-emails/` —
    five byte-identical copies of the README and all four templates — and said
    in its own docstring that the right fix was probably to delete them. It is
    deleted now (verified identical file by file first), so the property worth
    holding flipped: not "the duplicate is read" but "there is no duplicate".

    ⚠ This is STRICTLY STRONGER than what it replaces, which is the only reason
    the retirement is legitimate. The old test carried an early return for
    exactly this state — *the duplicate directory was cleaned up; nothing to
    cover* — so the moment the folder went, it passed vacuously forever and
    nothing stopped the copy coming back. A guard that cannot fail after the
    thing it guards is fixed is the shape gotcha 127 records: an early return
    written for "it was deleted" makes "the scan stopped reaching it"
    indistinguishable from it.

    ⚠ `FILES` stays RECURSIVE deliberately. Flatness makes `rglob` and `glob`
    equivalent today, so the spelling costs nothing — and if this ever fails,
    the guard below has already read whatever appeared rather than ignoring it.
    Narrowing the glob to match the new invariant would mean a re-introduced
    duplicate is both unread AND unreported.
    """
    nested = sorted(f for f in TEMPLATES.rglob("*") if f.parent != TEMPLATES)
    assert not nested, (
        f"{TEMPLATES} has subdirectory content again: "
        f"{[str(f.relative_to(TEMPLATES)) for f in nested]}. These files are "
        "pasted into the Supabase console BY HAND, so a second copy beside the "
        "real one is the file somebody eventually grabs — and nothing executes "
        "them, so a stale paste is silent. Keep the directory flat."
    )
    # The canary: flatness is only meaningful while the real templates are
    # where this guard thinks they are (gotchas 49/100/101 — a scan that finds
    # zero is a claim to check, not a result).
    assert len(FILES) >= 4, f"only {len(FILES)} template(s) found under {TEMPLATES}"


def test_every_template_says_who_is_writing() -> None:
    """An unbranded transactional email is indistinguishable from a phishing
    attempt, which is the whole reason these exist. The product name and a real
    reply address are the two things that make it identifiable.

    ⚠ The MASTHEAD, not "the word appears somewhere". The first cut asserted
    `"Occupella" in body` and a mutation that stripped the wordmark survived,
    because the footer's "operated by Oscar Ventures LLC" line still carried
    the name — so a message whose top said nothing scored as branded. What a
    recipient reads first is the thing being pinned (gotcha 58: the mutation
    DID change behaviour, so the gap was in the test).
    """
    for f in FILES:
        body = markup(f)
        head = body[: body.index("<h1")]
        assert "Occupella" in head, f"{f.name} has no wordmark above the heading"
        assert "team@occupella.com" in body, f"{f.name} gives nobody to reply to"
