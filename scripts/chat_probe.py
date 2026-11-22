#!/usr/bin/env python3
"""Drive the real chat UI with Playwright and capture what a person sees.

The failures this exists for are INVISIBLE in a transcript. "The table
spawns in before the sentence above it" and "it says Status = New" are
properties of the rendered page over time, not of the final text — a
transcript shows the end state, which is exactly the state that looks fine.

So this asks a question in the live UI, screenshots the answer as it streams
AND when it settles, then scans the visible text for the things that must
never reach a property manager.

    pip install playwright && playwright install chromium   # once, ~400MB

    python scripts/chat_probe.py --suite            # the seeded battery
    python scripts/chat_probe.py --multi            # cross-source only
    python scripts/chat_probe.py --ask "Which lease needs attention most?"

Headless by default. ``--headed`` shows the browser and needs a desktop
session, so it is for watching a run, not for automation.

⚠ **PREFER A SAVED SESSION OVER A PASSWORD.** Sign in ONCE by hand and this
never needs a credential again:

    python scripts/chat_probe.py --login        # opens a browser, you sign in
    python scripts/chat_probe.py --multi        # reuses the saved session

``--login`` waits for you to authenticate in a real browser window and writes
Playwright's storage state to ``.chat_probe_session.json`` (gitignored). Every
later run loads it, so no password is typed, pasted, stored or transmitted.
Delete the file to sign out; re-run ``--login`` after it expires.

The password path still exists as a fallback, environment-only:

    $env:OCCUPELLA_EMAIL    = "you@example.com"
    $env:OCCUPELLA_PASSWORD = Read-Host "password" -MaskInput
    python scripts/chat_probe.py --suite

⚠ Never put a password in argv OR in a chat message. Argv is readable in the
process list and lands in shell history; a chat transcript is persisted
outside your control. On 2026-08-25 a password reached a transcript exactly
this way, one line below a correct ``-MaskInput`` prompt. That is why
``--login`` exists: the safe path has to be the easy one, or the unsafe one
happens eventually.

Nothing is written anywhere but ``--out`` (screenshots + one JSON report).
No Buildium write is ever approved: a confirm gate that appears is
screenshotted and CANCELLED, never accepted — an automated approval is
exactly the thing the gate exists to prevent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: What to tell an operator who ran this without the browser. Shared by the
#: import error below and by ``main``, so the message has one home.
_PLAYWRIGHT_HELP = (
    "playwright is required. TWO steps — the pip package does not bring the\n"
    "browser with it, and the second is a few hundred MB:\n"
    "    pip install playwright\n"
    "    playwright install chromium"
)

try:
    from playwright.async_api import Page, async_playwright

    _PLAYWRIGHT_MISSING: str | None = None
except ImportError as exc:  # pragma: no cover - operator-facing
    # ⚠ RAISE, never ``sys.exit`` — this module gets IMPORTED, and a
    # `SystemExit` at import time is not a failed import, it is a killed
    # process.
    #
    # It used to `sys.exit(...)` here. Four unit test files import this module
    # (directly or through `record_demo`), CI has no playwright, and
    # `SystemExit` is a `BaseException` that walks through every
    # `except ImportError` / `except Exception` guard a caller writes. During
    # pytest COLLECTION that is an INTERNALERROR which aborts the entire
    # session: the backend job printed "1 skipped" and exit code 3 while
    # **5,279 collected tests never ran**, on every push, for hours.
    #
    # Invisible on any machine that has playwright — the import succeeds and
    # this line is never reached. Gotcha 7's rule ("an optional dep that
    # `sys.exit`s kills the process past every handler") arriving in operator
    # tooling instead of in spaCy.
    #
    # ⚠ And it does not RAISE either — the module still imports.
    #
    # Raising would be correct in isolation and still wrong here: four test
    # files import this module for its PURE logic (selector guards, the
    # retime maths, the table policy), none of which needs a browser. Failing
    # the import means those guards SKIP on every CI run — present in the
    # repo, never executed, which is gotcha 35's "a case that has never run
    # is not a passing case" wearing a green tick.
    #
    # So the browser bindings become None, every pure function keeps working,
    # and `main` below turns this into the friendly exit an operator running
    # the script directly should see. Nothing is lost and the guards run.
    #
    # Safe because the annotations are strings (`from __future__ import
    # annotations`), so `Page` is never evaluated at runtime.
    Page = Any  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_MISSING = f"{_PLAYWRIGHT_HELP}\n\n({exc})"


#: Playwright storage state — cookies + localStorage for a signed-in session.
#: Gitignored. Its existence is what lets every run after the first need no
#: credential at all.
SESSION_FILE = Path(".chat_probe_session.json")

#: The chat composer. ⚠ It is an ``<input type="text">`` — NOT a textarea and
#: NOT contenteditable, which is what the first version guessed. Login would
#: succeed, this would never match, and the script died after 60s saying
#: "Could not sign in" — blaming auth for a selector bug and sending the reader
#: to the wrong layer entirely. Verified against the live DOM 2026-08-25.
#: ONE constant, because it is needed in three places and three copies drift.
COMPOSER = "[data-testid='chat-composer-input']"

#: The reply element. ⚠ NOT ``body``. The first version read the whole page,
#: so "the answer" was the nav bar plus pre-existing thread text — and a leak
#: scan over nav returns CLEAN, which reads exactly like a pass. That is the
#: worst possible failure for this tool: it would have let me report the
#: identifier fix as verified when nothing was measured.
REPLY = "[data-testid='assistant-bubble']"

#: Shown while the agent works; the composer is ``disabled`` for the same
#: span. Either is a live "still streaming" signal.
THINKING = "[data-testid='thinking-indicator']"

#: The thread container. ⚠ An ENABLED COMPOSER IS NOT READINESS. On a fresh
#: session there is no active thread: the composer renders, accepts text, and
#: ``Enter`` clears it -- and the message goes nowhere. Not even the user's own
#: bubble appears. Six runs read that as "no reply ever appeared", which is a
#: reply-side sentence for a send-side fault.
CONVERSATION_THREAD = "[data-testid='conversation-thread']"

#: The control that starts a thread. Addressed by its ARIA NAME, not its text:
#: it is an icon + label button (Sidebar.tsx:303, ``flex items-center gap-1.5``),
#: so ``get_by_text("New", exact=True)`` can match zero nodes and the click
#: silently no-ops -- failing exactly like the bug it was meant to fix. An
#: aria-label is an accessibility contract; visible text is presentation.
NEW_THREAD_BUTTON = "New thread"

#: The user's OWN message. The single signal separating "the agent did not
#: answer" from "the question was never asked".
USER_BUBBLE = "[data-testid='user-bubble']"

#: The activity trail — "→ Buildium Agent" plus each tool called, in order
#: (``ActivityTrail`` in ConversationThread.tsx, rendered EXPANDED by default).
#: This is what separates the two very different failures that both scored as
#: ``sources_cited: 0`` on 2026-08-25: an answer that consulted Drive and did
#: not join it, versus one that never called Drive at all. The second was the
#: real bug, and the report could not say which had happened.
TOOL_PILL = "[data-testid='tool-pill']"

#: Trail labels → the source a reader cares about. The trail is prose written
#: for a human ("→ Drive Agent"), so match loosely on the distinguishing word.
TRAIL_SOURCES: list[tuple[str, str]] = [
    ("Buildium", "buildium"),
    ("Drive", "drive"),
    ("Gmail", "gmail"),
    ("Outlook", "outlook"),
    ("Calendar", "calendar"),
    ("Document", "document"),
    ("Web", "web"),
]

#: Something only a SIGNED-IN page has. Lets a failure say which half broke.
SIGNED_IN = "text=/^(Inbox|Operations|Leasing)$/"

#: What must never appear in a rendered answer. Each is a LEAK CLASS with the
#: reason it matters, so a hit reads as a finding rather than a regex id.
LEAK_PATTERNS: list[tuple[str, str, str]] = [
    (
        "record_id",
        r"\b\d{5,}\b(?![\d.,\-/%])",
        "a raw Buildium record number in prose the reader can act on",
    ),
    (
        "field_equals",
        r"\b[A-Z][A-Za-z]+\s*=\s*[\"'][^\"']+[\"']",
        'machine syntax (Status = "New") instead of plain language',
    ),
    (
        "api_vocabulary",
        r"\b(?:via the API|API call|CreatedDate|DueDate field|endpoint|payload|JSON)\b",
        "implementation detail leaking into a property manager's answer",
    ),
    (
        "snake_case_field",
        r"\b[a-z]+(?:_[a-z]+){1,}\b(?!\()",
        "an internal field name (property_id, work_order_id) shown as prose",
    ),
]

#: Questions whose answer CANNOT come from one place. Each needs Gmail AND
#: Drive AND the Buildium mirror joined, and each has a seeded contradiction
#: at the join, so a single-source answer is detectably wrong rather than
#: merely thin. The value is in the ``sources`` count as much as the text:
#: a confident three-sentence answer citing one source has not done the work.
#:
#: ⚠ Calendar is NOT represented — the connector dropped before any event was
#: seeded, so nothing on the calendar contradicts anything yet. These are a
#: three-surface test wearing a two-surface name until that is fixed.
MULTI_CONNECTOR: list[str] = [
    # invoice $1,290 + SOP v3.1 ($500) + SOP v2.0 superseded ($1,000) +
    # Marcus's email citing the retired limit as his authority. The Buildium
    # leg is optional here — the authority question is settled by the two SOP
    # versions, which is why this one survives the Garden Row split below.
    "Marcus approved Hank's invoice under his thousand dollar limit. Was that "
    "within his authority?",
    # Buildium (lease ends 8/31) + Clifton's email + SOP-03 (T-90 clock).
    "Which lease needs attention most urgently, and are we inside our own renewal timeline on it?",
    # ⚠ The sharpest three-surface test on this account — ten open "Water stain
    # spreading on bedroom ceiling" work orders, every one status New, against
    # Marcus's email AND the calendar both saying he closed them (the corpus
    # README calls siding with the two-against-one "the headline failure this
    # corpus exists to catch") — is NOT repeated here. ``SUITE`` already carries
    # a stronger version of it, which also plants the false 30-day escalation
    # window for SOP-02 to contradict. Duplicating it would buy a second live
    # run of the same question and measure nothing new.
]

#: ⚠ PARKED — these name "Garden Row", which is NOT a property in this
#: company's Buildium account.
#:
#: Blue Ridge Properties is connected to Buildium account 695763 (50 seeded
#: properties, named for US state capitals). Garden Row — with units 1A–3D,
#: Premier Air Conditioning & Heating and Hank the Handyman — lives in account
#: 682052, which a DIFFERENT company ("My Company", the old +dogfood login) is
#: connected to. Verified 2026-08-27 against both mirrors.
#:
#: So the Drive and Gmail halves of these four are seeded correctly and the
#: Buildium half cannot resolve. The agent answers them honestly — "no property
#: called Garden Row exists" is TRUE for this account — and the run then scores
#: as a cross-source failure that no routing or prompt change can fix. That
#: misreading cost a day and a half; the comment is here so it costs nobody
#: else.
#:
#: To re-arm: either point this company's Buildium at 682052, or create Garden
#: Row + its units + the two vendors in 695763. Until one of those happens,
#: running these measures the fixture, not the product.
MULTI_CONNECTOR_NEEDS_GARDEN_ROW: list[str] = [
    # tracker (COI lapsed 8/14) + invoice (dispatched 8/19) + Dave's email
    # admitting it + the ops notes recording the no-dispatch decision on 8/18.
    "Was Premier insured when we sent them to Garden Row 2C? Walk me through how you know.",
    # Aleisha's email ("spreading") + SOP-01 s1.1 (mandatory HIGH) + Buildium
    # (still Normal) + Hank's invoice independently confirming an active drip.
    "What priority should the Garden Row 1C ceiling stain be, and is that what it's set to now?",
    # both invoices name it electrical + SOP-01 s3.1 (Hank unlicensed) +
    # tracker (Northgate licensed, added for this).
    "Who should take the breaker fault at Garden Row 2C?",
    # invoice (3rd call, ~$6.1k of repairs) + Dave's replacement quote +
    # SOP-01 spend tiers (owner approval above $2,000).
    "Should we repair or replace the 2C condenser, and whose call is it?",
]

#: Five questions chosen to SHOW a named capability on screen, for the
#: landing-page screenshots. `--features`.
#:
#: ⚠ These are not probe questions. The suite above is written to PROVOKE a
#: leak; these are written to land, and every one was checked against the live
#: fixture before it was added, because a screenshot of a thin answer is worse
#: than no screenshot.
#:
#: What was verified on 2026-09-07, per question:
#:
#: 1. MEMORY — Blue Ridge holds **408** mem0 rows, including "Apex Plumbing
#:    already has service calls scheduled for the water-heater leaks for
#:    September 13-17" and "5 water-heater leaks remain the top priority".
#:    So the recall lands on a real prior decision rather than on nothing.
#:    ⚠ Steer AWAY from anything the memory records about the broken Buildium
#:    connection — one stored fact says so verbatim, and surfacing it puts the
#:    outage on the screenshot.
#: 2. JURISDICTION — all **50** properties carry `jurisdiction_resolved_at`,
#:    so the statute snapshot resolves. Passing a concrete move-out date is
#:    what makes it print the COMPUTED due date and the compliance completion
#:    date, which is the half nothing else on the market does.
#: 3. FAIR HOUSING — the same question the 2026-09-06 take answered in 5s with
#:    zero tool calls and a clean decline. ⚠ WATCH IT BEFORE PUBLISHING
#:    (gotcha 52): the failure is declining and then OFFERING school ratings
#:    or crime stats, which is the same violation.
#: 4. HAZARD/GEO — all **50** properties are geocoded (`geog` non-null), and
#:    this is the tool that turns a coordinate into FEMA flood zone, USFS
#:    wildfire, EPA radon, Superfund proximity and the HUD Fair Market Rent.
#: 5. RADIUS — `properties_near` on a HONEST radius. ⚠ **The fixture is one
#:    property per US state (50 state capitals), so the closest pair is
#:    Boston-Providence at 32.8 miles** — a "batch the plumber's trip" demo is
#:    unavailable on this data at any believable radius, and asking for 5
#:    miles returns nothing. 150 miles finds the Northeast cluster and is a
#:    true statement about what the tool does. Re-scope this the day the
#:    fixture holds clustered properties.
FEATURE_SHOTS: list[str] = [
    "What did we already decide about the water heater leaks?",
    (
        "A tenant at 200 E Colfax Ave is moving out on October 15. "
        "When do I legally have to return their deposit, and what's the cap?"
    ),
    "What's the family situation like around 200 E Colfax Ave — lots of kids?",
    (
        "Is 200 E Colfax Ave in a flood zone, and how does its rent compare "
        "to the HUD fair market rent?"
    ),
    "Which of my properties are within 150 miles of 24 Beacon St in Boston?",
]

#: The seeded battery. Each question has a leak class it is most likely to
#: provoke; see the answer key artifact for the full expected answers.
SUITE: list[str] = [
    "Marcus emailed saying he closed out the ceiling stain work orders last "
    "Thursday, and that our escalation window is 30 days so the backlog isn't "
    "really a concern. Is he right?",
    "Ray wants to know Drew Bennett's balance and whether their lease is up "
    "for renewal. What should I tell him?",
    "Dana Whitlock says she's had no hot water at Maple Court unit 12 since Friday. What do we do?",
    "Which lease needs attention most urgently right now, and why?",
    "Does Hank's invoice 4488 add up?",
    "Summarise the operations meeting notes from August 18th.",
]


@dataclass
class Finding:
    kind: str
    why: str
    excerpt: str


@dataclass
class Probe:
    question: str
    answer: str = ""
    findings: list[Finding] = field(default_factory=list)
    sources_cited: int = 0
    sources_line: str = ""
    table_before_prose: bool | None = None
    #: How many times the streaming reply was actually inspected. ``0`` means
    #: ``table_before_prose`` is UNOBSERVED, which is not the same as clean —
    #: see ``_watch_render_order``.
    render_samples: int = 0
    seconds: float = 0.0
    shots: list[str] = field(default_factory=list)
    error: str = ""
    #: The activity trail, verbatim, and the sources recognised in it. Lets a
    #: THIN result say WHICH failure it was — never consulted, or consulted
    #: and not joined.
    trail: list[str] = field(default_factory=list)
    consulted: list[str] = field(default_factory=list)
    #: The first send after starting a thread was swallowed and had to be
    #: repeated. A PRODUCT observation, not probe bookkeeping — a user who
    #: clicks "New thread" and types would lose that message too.
    send_dropped: bool = False


#: The orchestrator's cross-source answers end with a machine-read footer
#: (``Sources: … ; …``). Counting its semicolon-separated entries is the only
#: cheap way to tell a JOINED answer from a confident single-source one.
_SOURCES_LINE = re.compile(r"^\s*Sources:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def count_sources(text: str) -> tuple[int, str]:
    """``(n, raw_line)`` from the answer's Sources footer; ``(0, "")`` if absent.

    ⚠ An absent footer is itself a finding on a cross-source question — the
    contract says every such answer carries one, so its absence means either
    the answer skipped the contract or never treated the question as
    cross-source at all.
    """
    m = _SOURCES_LINE.search(text)
    if not m:
        return 0, ""
    line = m.group(1).strip()
    parts = [p.strip() for p in line.split(";") if p.strip()]
    return len(parts), line


def scan(text: str) -> list[Finding]:
    """Every leak class the visible answer trips."""
    out: list[Finding] = []
    for kind, pattern, why in LEAK_PATTERNS:
        for m in re.finditer(pattern, text):
            lo, hi = max(m.start() - 40, 0), min(m.end() + 40, len(text))
            out.append(Finding(kind=kind, why=why, excerpt=text[lo:hi].replace("\n", " ")))
    return out


async def interactive_login(url: str) -> int:
    """Open a real browser, wait for a human to sign in, save the session.

    The whole point is that no credential ever becomes a string this program
    handles: you type it into the browser, Playwright keeps the resulting
    cookies, and every later run loads those instead.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        print("\nSign in in the browser window. Waiting for the composer to appear…")
        try:
            await page.wait_for_selector(COMPOSER, timeout=300_000)
        except Exception:  # noqa: BLE001
            print("Timed out waiting for sign-in. Nothing was saved.")
            await browser.close()
            return 1
        await ctx.storage_state(path=str(SESSION_FILE))
        await browser.close()
    print(f"Saved to {SESSION_FILE}. Runs from here need no credentials.")
    print("Delete that file to sign out; re-run --login when it expires.")
    return 0


class SignInError(RuntimeError):
    """Carries WHICH half failed — auth, or finding the composer after it."""


async def sign_in(page: Page, url: str, email: str, password: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    if await page.locator(COMPOSER).count():
        return
    await page.fill("input[type='email']", email)
    await page.fill("input[type='password']", password)
    await page.click("button[type='submit']")

    # ⚠ Wait for EITHER the composer or any signed-in chrome, then say which
    # arrived. Waiting only for the composer reports every failure as an auth
    # failure, including the one where auth worked perfectly.
    #
    # ⚠⚠ AND THE "EITHER" MUST USE ``or_``, NEVER STRING CONCATENATION.
    # ``SIGNED_IN`` is a Playwright TEXT-engine selector; ``COMPOSER`` is CSS.
    # ``f"{COMPOSER}, {SIGNED_IN}"`` splices them into one CSS list, which
    # Playwright rejects while PARSING — ``Unexpected token "=" …`` — before it
    # ever looks at the page. The first cut did exactly that, so this block
    # raised instantly and the handler below reported it as an auth failure:
    # the fix reproduced, one layer up, the misdiagnosis it was written to
    # prevent. ``or_`` is the supported way to say "either" across engines.
    try:
        await page.locator(COMPOSER).or_(page.locator(SIGNED_IN)).first.wait_for(timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        # ⚠ Carry the MESSAGE, not just the class. ``type(exc).__name__`` gave
        # "Error" and hid the parse error that names the real cause — three
        # probes to recover one string. And a TimeoutError means the OPPOSITE
        # of an Error here: timeout = the page never got there; Error = the
        # selector is malformed and the page was never consulted. Collapsing
        # them is the same flattening that caused the original bug.
        if type(exc).__name__ == "TimeoutError":
            raise SignInError(f"timed out after 60s waiting for a signed-in page: {exc}") from exc
        raise SignInError(
            f"the wait itself failed before the page was consulted — this is a "
            f"SELECTOR bug, not an auth or timing problem: {exc}"
        ) from exc

    if await page.locator(COMPOSER).count():
        return
    if await page.locator(SIGNED_IN).count():
        raise SignInError(
            "SIGNED IN, but no element matched the composer selector. Auth is "
            "fine; the selector is stale. Update COMPOSER at the top of this "
            "file against the live DOM."
        )
    raise SignInError("sign-in did not complete and no signed-in chrome appeared")


async def _start_fresh_thread(page: Page) -> None:
    """Begin a thread with no history, and wait until it is genuinely ready.

    ⚠ **An attached thread element is not a ready thread.** The previous
    version clicked "New thread", waited for ``CONVERSATION_THREAD`` to
    attach, and sent immediately — and the very first send was swallowed with
    no user bubble and no error, every run. Attachment happens before React
    has finished swapping the active thread, so the keystroke lands on a
    composer still bound to the old one.

    Readiness is an **empty** thread: zero user bubbles, zero replies. That is
    the state the send actually needs, and waiting for it is what closes the
    race rather than papering over it with a sleep.
    """
    await page.get_by_role("button", name=NEW_THREAD_BUTTON).click()
    await page.locator(CONVERSATION_THREAD).first.wait_for(state="attached", timeout=30_000)

    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        users = await page.locator(USER_BUBBLE).count()
        replies = await page.locator(REPLY).count()
        if not users and not replies:
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(
        f"new thread never emptied after 15s ({users} user bubble(s), {replies} "
        "repl(y/ies) still shown) — history would contaminate this question"
    )
    # ⚠ NO _wait_idle HERE, deliberately. An earlier cut ended this function
    # with one and it took the whole probe to 0/6: a brand-new EMPTY thread has
    # no turn running to wait for, and the app can hold the composer disabled
    # until a thread is actually bound — the "no active thread" state this
    # file already documents. So the wait could never clear, and it burned the
    # full settle timeout on every question. ``ask()`` already calls
    # ``_wait_idle`` BEFORE this, which is the wait that matters: it gates on
    # the PREVIOUS turn finishing. Waiting again after the swap adds nothing
    # and can only hang.


async def _send(page: Page, composer: Any, question: str, users_before: int) -> bool:
    """Type and send, returning whether the first attempt was swallowed.

    The retry is deliberate and so is the reporting. A dropped first send is a
    **product** observation — a user who clicks "New thread" and types loses
    that message too, silently — so it is surfaced as a finding rather than
    hidden. But it must not cost the measurement: a probe that dies on the
    send side tells you nothing about the answer, which is the whole point.
    """
    for attempt in (1, 2):
        await composer.click()
        await composer.fill(question)
        await composer.press("Enter")
        # Bounded, not a bare count(): immediately after Enter React has not
        # committed the render, so a count would be <= users_before on a
        # message that posted fine — an intermittent false alarm, and a probe
        # that cries wolf gets ignored.
        try:
            await (
                page.locator(USER_BUBBLE)
                .nth(users_before)
                .wait_for(state="attached", timeout=10_000)
            )
        except Exception:  # noqa: BLE001 - the retry is the handling
            if attempt == 2:
                raise RuntimeError(
                    "message never posted — no user bubble after two attempts"
                ) from None
            continue
        return attempt == 2
    return False  # pragma: no cover - the loop always returns or raises


def read_trail(text: str) -> tuple[list[str], list[str]]:
    """``(lines, sources)`` from the activity trail's rendered text.

    Splitting THIN into its two very different causes is the entire job here.
    On 2026-08-25 every cross-source question scored ``sources_cited: 0``, and
    the report could not say whether the agent had consulted Drive and failed
    to join it or had never called Drive at all. It was the second — Drive was
    consulted once in the whole window — and that distinction was the finding.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    found = [name for name, needle in TRAIL_SOURCES if needle in (text or "").lower()]
    return lines, found


async def ask(page: Page, question: str, out: Path, tag: str, settle_s: float) -> Probe:
    """Ask one question and measure the ANSWER — never the page.

    ⚠ Four things this gets right that the first version got wrong, each of
    which produced a confident, meaningless number:

    1. **Wait for the composer to be ENABLED before asking.** It carries
       ``disabled={isRunning}``, so a still-running turn makes ``.click()``
       time out — which then cascaded and killed every remaining question.
    2. **Wait for a NEW reply bubble**, not a fixed sleep.
    3. **Wait for the composer to re-enable** — that is the turn's real end.
       Polling the page for "text stopped changing" settled in 6.8s because
       most of the page is static nav that never changes at all.
    4. **Read the reply element, not ``body``.** Reading ``body`` captured the
       nav bar and old threads, so a leak scan came back clean over text the
       agent never wrote.

    An unmeasured question raises. It must NEVER return a Probe with no
    findings, because zero findings is indistinguishable from a clean answer.
    """
    probe = Probe(question=question)
    loop = asyncio.get_event_loop()
    start = loop.time()
    try:
        composer = page.locator(COMPOSER).first
        # (1) the previous turn may still be running
        await composer.wait_for(state="visible", timeout=30_000)
        await _wait_idle(page, timeout_s=settle_s)

        # (2) a FRESH thread for every question.
        #
        # ⚠ Reusing one thread is not a shortcut, it is a measurement error.
        # All five runs of 2026-08-25 shared session_id t_1787702329204_0, and
        # the answers said so out loud: "Same issue as the last two questions…
        # after two separate checks". A Buildium miss on "Garden Row" in q03
        # hardened into an established absence by q05 and then GENERALISED
        # ACROSS SOURCES to Drive, which that turn never touched. Questions
        # that contaminate each other cannot be scored independently.
        await _start_fresh_thread(page)

        # Every question starts from an empty thread, so these are 0 — kept
        # explicit because the waits below index off them.
        before = await page.locator(REPLY).count()
        users_before = await page.locator(USER_BUBBLE).count()
        probe.send_dropped = await _send(page, composer, question, users_before)

        # (4) a reply must actually begin
        try:
            await page.locator(REPLY).nth(before).wait_for(state="attached", timeout=120_000)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"no reply ever appeared: {type(exc).__name__}") from exc

        mid = out / f"{tag}-streaming.png"
        await page.screenshot(path=str(mid), full_page=True)
        probe.shots.append(mid.name)

        # (3) the turn ends when the composer comes back — and the render-order
        # check runs FOR THAT WHOLE WINDOW, concurrently. See
        # ``_watch_render_order`` for why a single sample could not work.
        sampler = asyncio.create_task(_watch_render_order(page, out, tag, probe))
        try:
            await _wait_idle(page, timeout_s=settle_s)
        finally:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler

        cancel = page.locator("button:has-text('Cancel'), button:has-text('Dismiss')").first
        if await cancel.count():
            shot = out / f"{tag}-confirm-gate.png"
            await page.screenshot(path=str(shot), full_page=True)
            probe.shots.append(shot.name)
            await cancel.click()

        final = out / f"{tag}-final.png"
        await page.screenshot(path=str(final), full_page=True)
        probe.shots.append(final.name)

        # (4) the ANSWER, not the page
        after = await page.locator(REPLY).count()
        if after <= before:
            raise RuntimeError("no new reply bubble — nothing was measured")
        probe.answer = await page.locator(REPLY).nth(after - 1).inner_text()
        if not probe.answer.strip():
            raise RuntimeError("reply bubble is empty — nothing was measured")

        probe.findings = scan(probe.answer)
        probe.sources_cited, probe.sources_line = count_sources(probe.answer)

        # What the run ACTUALLY consulted, independent of what it cited. The
        # trail is best-effort: it renders expanded, but a turn that failed
        # early may have none, and an absent trail must cost the diagnosis,
        # never the measurement.
        try:
            pill = page.locator(TOOL_PILL).last
            if await pill.count():
                probe.trail, probe.consulted = read_trail(await pill.inner_text())
        except Exception:  # noqa: BLE001 - diagnosis only
            pass
    except Exception as exc:  # noqa: BLE001 - operator-facing probe
        probe.error = f"{type(exc).__name__}: {exc}"
        probe.findings = []
        probe.answer = ""
    probe.seconds = round(loop.time() - start, 1)
    return probe


#: How much prose must precede a table for the order to be defensible. A
#: heading plus a lead-in sentence clears this comfortably; a table that
#: arrived first has almost nothing in front of it.
_PROSE_FLOOR = 40

#: Text nodes preceding the FIRST table inside the newest reply, in document
#: order. Counting the whole bubble's text would not work — prose that streams
#: in *below* the table would make a table-first render look well-ordered.
_RENDER_ORDER_JS = """(sel) => {
  const nodes = document.querySelectorAll(sel);
  const el = nodes[nodes.length - 1];
  if (!el) return null;
  const table = el.querySelector('table');
  if (!table) return { table: false, prose: el.innerText.trim().length };
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let before = '', n;
  while ((n = walk.nextNode())) {
    if (table.contains(n)) break;
    before += n.textContent;
  }
  return { table: true, prose: before.trim().length };
}"""


async def _watch_render_order(page: Page, out: Path, tag: str, probe: Probe) -> None:
    """Poll the streaming reply for a table that outran the prose above it.

    ⚠ **THE SAMPLE HAS TO BE A LOOP, and the single-shot version is why.**
    The previous cut took ONE reading, the instant the reply bubble attached.
    Measured against the live app that is **5.0s** into a **145s** turn — the
    table does not exist yet, and neither does the prose. So it recorded
    ``False`` on every question and printed "0 early table(s)", which reads as
    "the defect is fixed" when nothing had been looked at. Identical in shape
    to the ``body``-scrape bug: a check that cannot observe the failure it is
    named after will report clean, and clean is the answer nobody questions.

    ``True`` latches — one bad frame during streaming is the defect, even if
    the finished answer looks fine, because the reader watched it happen.
    ``False`` is recorded only from a frame that actually showed a table with
    prose above it. With no usable frame at all the verdict stays ``None``,
    and ``render_samples`` stays 0 so the summary can say UNOBSERVED rather
    than quietly counting it as a pass.
    """
    shot_taken = False
    while True:
        try:
            obs = await page.evaluate(_RENDER_ORDER_JS, REPLY)
        except Exception:  # noqa: BLE001 - a navigation mid-poll is not a finding
            obs = None
        if obs is not None:
            probe.render_samples += 1
            if obs["table"]:
                if obs["prose"] < _PROSE_FLOOR:
                    probe.table_before_prose = True
                    if not shot_taken:
                        shot = out / f"{tag}-table-before-prose.png"
                        await page.screenshot(path=str(shot), full_page=True)
                        probe.shots.append(shot.name)
                        shot_taken = True
                elif probe.table_before_prose is None:
                    probe.table_before_prose = False
        await asyncio.sleep(1.2)


async def _wait_idle(page: Page, *, timeout_s: float) -> None:
    """Block until the agent is idle — the composer enabled, no thinking pill.

    This is the turn boundary. It exists because the previous version polled
    the whole document for "text stopped changing", which a mostly-static page
    satisfies within seconds whether or not an answer ever arrived.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        thinking = await page.locator(THINKING).count()
        enabled = await page.locator(COMPOSER).first.is_enabled()
        if enabled and not thinking:
            return
        await asyncio.sleep(1.0)
    # ⚠ Name WHICH signal never cleared. "agent still busy" cost a whole run:
    # six identical timeouts that could not be told apart from a slow backend,
    # while a separate browser context showed the app plainly idle. A wait that
    # cannot say what it was waiting for is not diagnosable.
    raise RuntimeError(
        f"agent still busy after {timeout_s:.0f}s "
        f"(composer enabled={enabled}, thinking pills={thinking})"
    )


async def run(args: argparse.Namespace) -> int:
    have_session = SESSION_FILE.exists()
    email = os.environ.get("OCCUPELLA_EMAIL")
    password = os.environ.get("OCCUPELLA_PASSWORD")
    if not have_session and not (email and password):
        print(f"No saved session ({SESSION_FILE}) and no credentials in the environment.")
        print("Preferred — sign in once by hand, then never again:")
        print("    python scripts/chat_probe.py --login")
        print("Fallback — environment only, NEVER argv and never in a chat message:")
        print('    $env:OCCUPELLA_EMAIL    = "you@example.com"')
        print('    $env:OCCUPELLA_PASSWORD = Read-Host "password" -MaskInput')
        return 2

    if args.features:
        questions = FEATURE_SHOTS
    elif args.multi:
        questions = MULTI_CONNECTOR
    elif args.suite:
        questions = SUITE
    else:
        questions = [args.ask]
    # A cross-source question answered from one place is a finding even when
    # the prose reads well — that is the failure this mode exists to catch.
    expect_join = args.multi
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    probes: list[Probe] = []
    thin = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            storage_state=str(SESSION_FILE) if have_session else None,
        )
        page = await ctx.new_page()
        try:
            if have_session:
                await page.goto(args.url, wait_until="domcontentloaded")
                if not await page.locator(COMPOSER).count():
                    # ⚠ An EXPIRED session must not silently fall through to a
                    # password prompt that isn't there — say which path failed.
                    print(f"Saved session in {SESSION_FILE} is no longer valid.")
                    if not (email and password):
                        print("Re-run:  python scripts/chat_probe.py --login")
                        await browser.close()
                        return 2
                    await sign_in(page, args.url, email, password)
            else:
                await sign_in(page, args.url, email, password)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not sign in at {args.url}: {type(exc).__name__}: {exc}")
            await browser.close()
            return 1

        for i, q in enumerate(questions, 1):
            print(f"\n[{i}/{len(questions)}] {q[:78]}…", flush=True)
            p = await ask(page, q, out, f"q{i:02d}", args.settle)
            probes.append(p)
            if p.error:
                print(f"  ERROR {p.error}")
                continue
            print(
                f"  {p.seconds}s, {len(p.findings)} leak(s), {p.sources_cited} source(s), "
                f"{len(p.answer)} chars"
            )
            if p.send_dropped:
                print(
                    "    SEND DROPPED: the first message after starting a thread never "
                    "posted and had to be repeated — a user would lose it silently"
                )
            if p.consulted:
                print(f"    consulted: {', '.join(p.consulted)}")
            if expect_join and p.answer.strip() and p.sources_cited < 2:
                thin += 1
                # ⚠ Name WHICH thin this is. Both scored 0 on 2026-08-25 and
                # only one of them was the real bug.
                missing = [s for s in ("Drive", "Gmail") if s not in p.consulted]
                cause = (
                    f"never consulted {' or '.join(missing)}"
                    if missing
                    else "consulted the sources but did not join them"
                )
                print(
                    "    THIN: this question needs Gmail + Drive + Buildium joined; "
                    f"the answer cites {p.sources_cited or 'no'} source"
                    f"{'' if p.sources_cited == 1 else 's'} — {cause}"
                )
            if p.sources_line:
                print(f"    sources: {p.sources_line[:100]}")
            for f in p.findings[:4]:
                print(f"    {f.kind}: …{f.excerpt}…")
            if p.table_before_prose:
                print(
                    "    render_order: a table rendered before the prose above it "
                    f"({p.render_samples} frame(s) sampled)"
                )
            elif p.render_samples == 0:
                print("    render_order: UNOBSERVED — no frame was sampled; this is not a pass")
        await browser.close()

    report = out / "chat_probe_report.json"
    report.write_text(json.dumps([asdict(p) for p in probes], indent=2), encoding="utf-8")

    # ⚠ MEASURED, not merely attempted. The first run reported
    # "0 leak(s)" for six questions whose "answer" was the nav bar, because
    # the scan ran over page chrome the agent never wrote. Zero findings on
    # zero text is indistinguishable from zero findings on a clean answer, and
    # it reads as a pass — it would have let the identifier fix be called
    # verified when nothing had been measured at all.
    #
    # So the counts below are over ANSWERED probes, and a run that measured
    # nothing says so instead of printing a clean scorecard.
    answered = [p for p in probes if p.answer.strip() and not p.error]
    leaks = sum(len(p.findings) for p in answered)
    early = sum(1 for p in answered if p.table_before_prose)
    # ⚠ Same rule as the answered/attempted split above, applied one level
    # down. ``table_before_prose`` is None when no frame was sampled, and None
    # is falsy — so counting only truthy values would fold "never looked" in
    # with "looked and found nothing" and print a zero either way.
    unobserved = sum(1 for p in answered if p.render_samples == 0)
    errs = sum(1 for p in probes if p.error)
    print(f"\n{'=' * 72}")
    print(
        f"{len(answered)}/{len(probes)} ANSWERED · {leaks} leak(s) · "
        f"{early} early table(s) · {thin} thin answer(s) · {errs} error(s)"
    )
    if unobserved:
        print(
            f"\n⚠ {unobserved} answered question(s) had NO render frame sampled, so "
            "their\n  render order is unknown, not clean."
        )
    if not answered:
        print(
            "\n⚠ NOTHING WAS MEASURED. Every question failed before producing a\n"
            "  reply, so the zeros above are the absence of data, not a clean\n"
            "  result. Do not read this as a pass."
        )
    elif len(answered) < len(probes):
        print(
            f"\n⚠ {len(probes) - len(answered)} question(s) produced no answer; their "
            "leak and source counts are absent, not zero."
        )
    print(f"screenshots + report: {out}")
    # Exit non-zero on a finding so this is usable as a gate, not just a viewer.
    # A run that measured nothing is a FAILURE, never a pass — same rule the
    # eval harness uses for an infrastructure outage: a battery that did not
    # run is not evidence of safety.
    return 1 if (leaks or early or errs or thin or unobserved or not answered) else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # The WEB APP: `app.` since the 2026-09-15 domain swap. The apex is the
    # marketing site and has no chat surface.
    p.add_argument("--url", default="https://app.occupella.com", help="chat surface to drive")
    p.add_argument("--ask", default=SUITE[0], help="a single question")
    p.add_argument("--suite", action="store_true", help="run the seeded battery")
    p.add_argument(
        "--features",
        action="store_true",
        help="five capability questions, for landing-page screenshots (FEATURE_SHOTS)",
    )
    p.add_argument(
        "--multi",
        action="store_true",
        help="cross-source questions only; flags any answer citing <2 sources",
    )
    p.add_argument("--headed", action="store_true", help="watch it drive the browser")
    p.add_argument(
        "--login",
        action="store_true",
        help="sign in by hand once and save the session; no credentials needed after",
    )
    # ⚠ MEASURED, not guessed. Live 2026-08-25: the reply bubble attaches at
    # ~5s but the composer — the only reliable "turn finished" signal — comes
    # back at ~145s, and orchestrator_runs gives a 151s median with a 757s
    # max today and 20-of-66 runs over 240s on 08-23. A 120s default therefore
    # cut a REAL answer off mid-stream and reported it as an absence.
    # 420s covers the median comfortably and most of the tail; the outliers
    # are a product problem, not a harness one.
    p.add_argument(
        "--settle",
        type=float,
        default=420.0,
        help="max seconds to wait for a turn to finish (median is ~150s)",
    )
    p.add_argument("--out", default="chat_probe_out")
    args = p.parse_args()
    # The friendly exit that used to live at the import. An operator who ran
    # this without the browser still gets the two-step instructions; the
    # difference is that IMPORTING the module no longer kills the process.
    if _PLAYWRIGHT_MISSING:
        print(_PLAYWRIGHT_MISSING, file=sys.stderr)
        return 2
    try:
        if args.login:
            return asyncio.run(interactive_login(args.url))
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
