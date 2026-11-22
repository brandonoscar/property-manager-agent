"""Selectors are strings resolved by an engine that isn't Python.

``chat_probe`` has now cost three debugging rounds, all on the same seam:
a selector string that Python happily builds and Playwright rejects, or
matches nothing, at a point far from where it was written.

  1. ``COMPOSER`` guessed ``textarea`` — the live element is an ``<input>``.
     Login succeeded and the script reported an auth failure.
  2. The fix left a stale copy of the old selector in a second function,
     because ``ruff format`` had reflowed the line the edit targeted.
  3. The fix for *that* spliced a TEXT-engine selector into a CSS list with
     an f-string. Playwright threw while PARSING, before touching the page,
     and the handler reported it as — again — an auth failure. The comment
     directly above it explains that this must not happen.

Round 3 is the interesting one: the code was reviewed, the intent was
documented in a warning comment, and it still shipped, because nothing
mechanical checked the claim. That is gotcha 27's rule (a payload another
system validates needs a LOCAL conformance check) and gotcha 28's (a string
crossing a producer/consumer boundary needs a test reading both sides).

Playwright is not a dependency of this repo, so these cannot resolve
selectors against a browser. They check the STRUCTURAL property that failed
each time instead: that the file never builds a multi-engine selector by
string concatenation, and never keeps a second copy of one.

⚠ VERIFIED BY MUTATION, and the first attempt at that was itself vacuous.
Reintroducing the round-3 bug with a text ``.replace()`` matched nothing —
``ruff format`` had reflowed the target lines — but the script printed
"bug reintroduced" unconditionally, so the guards were run against the FIXED
file twice and "passed" meaninglessly. The second attempt broke syntax, and a
guard that fires on a SyntaxError proves nothing about detecting a splice.

Only the third attempt was evidence: locate the statement via AST, replace it,
assert the mutation applied AND that the result still parses, then run the
guards. Two fail (this file's first two tests), seven pass. That is the shape
a mutation check has to have — assert the mutation landed, assert the mutant is
still valid, and only then read the verdict.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

PROBE = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "chat_probe.py"
SOURCE = PROBE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe_module():
    """Import ``chat_probe`` with Playwright stubbed.

    Playwright is not a dependency of this repo and the script ``sys.exit``s
    at import without it — which is itself a trap this suite has hit: an
    earlier verification piped ``--help`` into ``grep``, got nothing because
    the exit happened before argparse, and read the empty output as a pass.

    Stubbing the import lets the render-order sampler be tested for REAL
    behaviour against a fake page, rather than by asserting on the shape of
    its source. That is a much stronger guard, and it is available here
    because the sampler only needs ``evaluate`` and ``screenshot``.
    """
    saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.async_api", "cp")}
    pw = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    for name in ("Page", "Browser", "BrowserContext", "Playwright", "Error", "TimeoutError"):
        setattr(api, name, type(name, (), {}))
    api.async_playwright = lambda: None
    pw.async_api = api
    sys.modules["playwright"], sys.modules["playwright.async_api"] = pw, api
    spec = importlib.util.spec_from_file_location("cp", PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cp"] = module  # @dataclass resolves annotations via sys.modules
    spec.loader.exec_module(module)
    yield module
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


class _FakePage:
    """Replays a fixed sequence of ``_RENDER_ORDER_JS`` observations."""

    def __init__(self, frames):
        self.frames, self.i, self.shots = frames, 0, 0

    async def evaluate(self, js, sel):
        frame = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return frame

    async def screenshot(self, path, full_page=False):
        self.shots += 1


async def _drive(mod, frames, ticks=30):
    """Run the sampler over ``frames``, collapsing its 1.2s tick to nothing."""
    probe = mod.Probe(question="q")
    page = _FakePage(frames)
    real_sleep = asyncio.sleep

    async def instant(_):
        await real_sleep(0)

    mod.asyncio.sleep = instant
    try:
        task = asyncio.create_task(mod._watch_render_order(page, pathlib.Path("/tmp"), "t", probe))
        for _ in range(ticks):
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        mod.asyncio.sleep = real_sleep
    return probe, page


def test_the_probe_still_parses() -> None:
    ast.parse(SOURCE)


def test_no_selector_is_built_by_string_concatenation() -> None:
    """⚠ The round-3 bug, as an executable rule.

    ``f"{COMPOSER}, {SIGNED_IN}"`` is valid Python and invalid Playwright:
    a text-engine selector cannot be comma-joined into a CSS list. Any
    f-string interpolating two selector constants is the same mistake, so
    forbid the shape rather than that one instance.
    """
    tree = ast.parse(SOURCE)
    selector_names = {"COMPOSER", "SIGNED_IN"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        used = {
            v.value.id
            for v in node.values
            if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)
        }
        if len(used & selector_names) >= 2:
            offenders.append(ast.unparse(node))
    assert not offenders, (
        "selector constants combined in an f-string — Playwright cannot parse a "
        f"mixed-engine list. Use locator(a).or_(locator(b)). Found: {offenders}"
    )


def test_either_is_expressed_with_or_() -> None:
    """The supported cross-engine 'either', present and used."""
    assert ".or_(" in SOURCE, "the composer/signed-in wait must use locator.or_()"


def test_selectors_key_on_test_ids_not_presentation_text() -> None:
    """⚠ Round 4, and it RETIRES two earlier proxies — see the note below.

    The composer's placeholder CHANGES while the agent works:
    "What should Occupella do next?" becomes "Agent is working — press Esc to
    stop". A placeholder-matching selector therefore stops matching mid-run,
    and a permissive fallback (``input[type='text'][placeholder]``) matched the
    DISABLED composer instead — so the script clicked a disabled element and
    timed out, killing every remaining question.

    ``data-testid="chat-composer-input"`` does not move when the presentation
    text does. Keying on it is the property that matters, so this replaces
    both ``test_the_composer_matches_an_input_not_only_a_textarea`` (which
    asserted the placeholder — the very thing that drifts) and
    ``test_the_composer_selector_is_defined_exactly_once`` (which counted
    copies of a literal that no longer exists). Retiring a proxy once the real
    measurement exists is legitimate; weakening one to go green is not.
    """
    assert 'COMPOSER = "[data-testid=' in SOURCE, "the composer must be keyed by test id"
    assert "placeholder" not in SOURCE.split("REPLY =")[0], (
        "no selector may key on placeholder text — it changes while the agent runs"
    )


def test_the_answer_is_read_from_the_reply_element_not_the_page() -> None:
    """⚠ The worst bug this file has seen, because it PASSED.

    Reading ``body`` captured the nav bar and pre-existing threads, so six
    questions reported "0 leak(s)" over text the agent never wrote. Zero
    findings on zero answer is indistinguishable from zero findings on a clean
    answer — it reads as a pass, and would have let today's identifier fix be
    called verified when nothing was measured.
    """
    assert 'REPLY = "[data-testid=' in SOURCE, "the reply element must be keyed by test id"
    assert 'inner_text("body")' not in SOURCE, "never scan the whole page as if it were the answer"


def test_an_unmeasured_run_cannot_report_clean() -> None:
    """The liveness rule, mirroring the eval harness's infrastructure kind:
    a battery that did not run is not evidence of safety."""
    assert "NOTHING WAS MEASURED" in SOURCE
    assert "not answered" in SOURCE, "a run with no answers must exit non-zero"
    assert "absent, not zero" in SOURCE, "partial runs must not print zeros for missing data"


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        ("SELECTOR bug, not an auth", "a parse error must not be reported as an auth failure"),
        ("timed out after 60s", "a timeout must be distinguished from a malformed selector"),
        ("{exc}", "the exception MESSAGE must be carried, not just its class name"),
    ],
)
def test_failures_name_the_right_layer(marker: str, why: str) -> None:
    """Every round here was extended by an error that pointed elsewhere.

    ``type(exc).__name__`` alone yielded "Error", hiding the parse message
    that identified the cause immediately. The message is the fix that stops
    the *next* person searching in the wrong place.
    """
    assert marker in SOURCE, why


def test_no_password_flag_exists() -> None:
    """Credentials come from the environment or a saved session, never argv —
    argv is readable in the process list and lands in shell history."""
    assert '"--password"' not in SOURCE and "'--password'" not in SOURCE
    assert "--login" in SOURCE, "the credential-free path must remain available"


@pytest.mark.asyncio
async def test_a_first_frame_sample_cannot_see_the_defect_but_the_loop_can(probe_module):
    """⚠ Round 5, and it is the same failure as the ``body`` scrape.

    The previous cut sampled render order ONCE, the instant the reply bubble
    attached. Measured against the live app that is **5.0s into a 145s turn** —
    the table does not exist yet and neither does the prose. So it recorded
    ``False`` for every question and the summary printed "0 early table(s)",
    which reads as *the defect is fixed* when nothing had been looked at.

    The frames below are that exact shape: frame 0 is the empty bubble a
    first-frame sample would have seen, frame 1 is the defect. This asserts
    both halves — that frame 0 alone is indistinguishable from clean, and that
    the loop still catches it.
    """
    frames = [
        {"table": False, "prose": 0},  # what the OLD one-shot sample saw, at 5s
        {"table": True, "prose": 3},  # the table beat its prose
        {"table": True, "prose": 900},  # by the end it looks fine
    ]
    assert frames[0]["table"] is False, "frame 0 must look clean, or this proves nothing"

    probe, page = await _drive(probe_module, frames)
    assert probe.render_samples > 0, "VACUOUS: the sampler never observed a frame"
    assert probe.table_before_prose is True
    assert page.shots == 1, "the offending frame must be captured, exactly once"


@pytest.mark.asyncio
async def test_a_table_with_prose_above_it_is_not_a_finding(probe_module):
    probe, _ = await _drive(probe_module, [{"table": True, "prose": 500}])
    assert probe.render_samples > 0
    assert probe.table_before_prose is False


@pytest.mark.asyncio
async def test_an_answer_with_no_table_gets_no_verdict(probe_module):
    """``None`` is "no opinion", matching ``classify_table_need``'s contract.
    An answer that never rendered a table cannot have rendered one early."""
    probe, _ = await _drive(probe_module, [{"table": False, "prose": 700}])
    assert probe.render_samples > 0
    assert probe.table_before_prose is None


@pytest.mark.asyncio
async def test_an_unobservable_reply_is_unobserved_not_clean(probe_module):
    """The liveness rule again, one level down.

    ``table_before_prose`` is ``None`` when nothing was sampled, and ``None``
    is falsy — so a summary counting only truthy values folds "never looked"
    in with "looked and found nothing" and prints a zero either way. The
    sample COUNT is what separates them, so it must stay at 0 here.
    """
    probe, _ = await _drive(probe_module, [None])
    assert probe.render_samples == 0
    assert probe.table_before_prose is None


def test_the_render_order_check_is_a_loop_not_a_moment() -> None:
    """Belt and braces on the shape, since the behaviour above depends on it."""
    tree = ast.parse(SOURCE)
    watcher = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_watch_render_order"
        ),
        None,
    )
    assert watcher is not None, "the render-order sampler must exist"
    assert any(isinstance(n, (ast.While, ast.For)) for n in ast.walk(watcher)), (
        "the sampler must poll; a single reading lands 5s into a 145s turn and sees nothing"
    )


def test_an_unsampled_render_order_fails_the_run() -> None:
    """It must reach the exit code, not just the printout — otherwise the
    warning scrolls past and the run still reports success."""
    assert "render_samples == 0" in SOURCE, "the summary must count unsampled probes"
    assert "unobserved or not answered" in SOURCE, "unobserved must gate the exit code"


def test_a_send_failure_is_not_reported_as_a_reply_failure() -> None:
    """Six runs blamed the reply side for a send-side problem. The user
    bubble is the one signal that separates them."""
    assert "message never posted" in SOURCE
    assert 'USER_BUBBLE = "[data-testid=' in SOURCE


def _func(name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found")


def _calls(node: ast.AST) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_a_thread_is_ensured_before_asking() -> None:
    """An enabled composer is NOT readiness. With no active thread the
    composer renders, accepts text, and Enter clears it into nothing — which
    is indistinguishable from a silent agent unless the thread is checked.

    ⚠ Asserted through the AST, not by looking for ``CONVERSATION_THREAD``
    after ``async def ask`` in the source text. The first version did the
    latter and broke the moment the logic moved into a helper — a guard that
    fails on a refactor while the property it names still holds (gotcha 40).
    """
    assert 'CONVERSATION_THREAD = "[data-testid=' in SOURCE
    assert "_start_fresh_thread" in _calls(_func("ask")), (
        "ask() must establish a ready thread before it types"
    )
    assert "CONVERSATION_THREAD" in {
        n.id for n in ast.walk(_func("_start_fresh_thread")) if isinstance(n, ast.Name)
    }, "the thread helper must actually wait on the thread element"


def test_every_question_gets_its_own_thread() -> None:
    """The measurement error that made five answers uninterpretable.

    All five runs of 2026-08-25 shared one ``session_id``: the probe started a
    thread only when none existed, so q03's Buildium miss on "Garden Row" was
    still in context at q05, hardened into an established absence, and then
    generalised to Drive — a source that turn never touched. Questions that
    contaminate each other cannot be scored independently.

    So the call must be UNCONDITIONAL. A guard that only checked the call
    exists would pass against the exact bug being fixed.
    """
    ask = _func("ask")
    conditional = {
        n.func.id
        for branch in ast.walk(ask)
        if isinstance(branch, ast.If)
        for n in ast.walk(branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_start_fresh_thread" not in conditional, (
        "the new thread is conditional again — questions will share context "
        "and their answers will contaminate each other"
    )


def test_readiness_is_an_empty_thread_not_an_attached_one() -> None:
    """The q01 send race. Attachment happens before React finishes swapping the
    active thread, so the first keystroke lands on a composer still bound to
    the old one and the message is swallowed with no error at all."""
    helper = _func("_start_fresh_thread")
    names = {n.id for n in ast.walk(helper) if isinstance(n, ast.Name)}
    assert "USER_BUBBLE" in names and "REPLY" in names, (
        "readiness must be an EMPTY thread — waiting only for attachment is "
        "the race that dropped the first send of every run"
    )


def test_a_dropped_send_is_reported_not_just_retried() -> None:
    """A user who clicks "New thread" and types loses that message too. The
    retry keeps the measurement alive; it must not hide the product defect."""
    assert "send_dropped" in SOURCE
    assert "SEND DROPPED" in SOURCE, "a swallowed first send must reach the operator's report"


def test_the_trail_separates_the_two_kinds_of_thin(probe_module) -> None:
    """Both scored ``sources_cited: 0`` on 2026-08-25 and only one was the
    bug: an answer that consulted Drive and failed to join it, versus one that
    never called Drive at all. It was the second, and the report could not
    say so.

    ⚠ Behavioural, against the real function. The first cut asserted that the
    strings "never consulted" and "did not join them" appear in the source —
    which stayed green when the branch computing them was gutted, because the
    OTHER branch's literal still existed. A substring is not a behaviour.
    """
    read_trail = probe_module.read_trail

    # The live shape: ActivityTrail renders "N steps" then one line per step.
    lines, sources = read_trail("3 steps\n→ Buildium Agent\nbuildium_list_tasks\n→ Drive Agent")
    assert "Buildium" in sources and "Drive" in sources
    assert "→ Drive Agent" in lines

    # The run that produced the bug: Buildium only, Drive never consulted.
    _, buildium_only = read_trail("2 steps\n→ Buildium Agent\nbuildium_list_tasks")
    assert buildium_only == ["Buildium"]
    assert "Drive" not in buildium_only, (
        "a trail with no Drive step must not report Drive as consulted — that "
        "conflation is what hid the real failure for a whole run"
    )

    # A turn that called nothing at all — the nine-second, zero-tool answer.
    assert read_trail("") == ([], [])


def test_the_report_names_which_thin_it_is() -> None:
    """The wording the operator actually reads, both branches present."""
    assert 'TOOL_PILL = "[data-testid=' in SOURCE
    assert "never consulted" in SOURCE
    assert "did not join them" in SOURCE


def test_the_new_thread_control_is_addressed_by_aria_name() -> None:
    """It is an icon + label button, so a bare text match can resolve to zero
    nodes and the click no-ops — failing exactly like the bug it fixes. An
    aria-label is an accessibility contract; visible text is presentation.

    ⚠ **Asserted on the AST, not on the source text, and the first cut of this
    guard shows why.** It read ``'get_by_text("New"' not in SOURCE`` — a
    substring check on one exact literal — and had to strip ``#`` lines first,
    because the constant's own warning comment QUOTES the wrong form and the
    guard fired on the sentence explaining the guard.

    Stripping comments made it pass, and left it blind to:

    * a call ``ruff format`` has WRAPPED, which puts ``get_by_text(`` and
      ``"New"`` on different lines so the substring matches neither. That is
      round 2 of this file's own history repeating — the stale selector
      survived precisely because ruff had reflowed the line the edit targeted;
    * ``get_by_text('New')`` — different quotes, same bug;
    * ``get_by_text(label)`` — the literal moved into a variable;
    * any OTHER text-engine call, which the substring never mentioned at all.

    Walking the tree catches every one, and needs no comment handling
    whatsoever: a comment is never a ``Call`` node. Both halves are checked
    this way, so a reflow cannot turn the positive assertion into a false
    RED either.
    """
    tree = ast.parse(SOURCE)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    text_calls = [
        ast.unparse(n)
        for n in calls
        if isinstance(n.func, ast.Attribute) and n.func.attr == "get_by_text"
    ]
    assert not text_calls, (
        "presentation text is not a selector. The New control is an icon+label "
        "button, so an exact text match can resolve to zero nodes and the click "
        f"silently no-ops. Use get_by_role with the aria name. Found: {text_calls}"
    )

    by_role = [
        n
        for n in calls
        if isinstance(n.func, ast.Attribute)
        and n.func.attr == "get_by_role"
        and any(
            kw.arg == "name"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == "NEW_THREAD_BUTTON"
            for kw in n.keywords
        )
    ]
    assert by_role, "the New control must be addressed by get_by_role(name=NEW_THREAD_BUTTON)"


# ── the helper is RUN, not just read ────────────────────────────────────────
#
# ⚠ Every other guard in this file is static — AST walks and source checks.
# That is why a regression that took the probe to 0/6 shipped with all of them
# green: `_start_fresh_thread` ended with a `_wait_idle` call, a brand-new
# EMPTY thread has no turn to wait for, the app can hold the composer disabled
# until a thread is bound, and the wait could never clear. It burned the full
# settle timeout on all six questions.
#
# No amount of reading the source catches "this waits for something that never
# happens". You have to run it against a page that reproduces the state.


class _FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def count(self):
        return self.page.counts.get(self.selector, 0)

    async def wait_for(self, state="attached", timeout=0):
        return None

    async def is_enabled(self):
        return self.page.composer_enabled

    async def click(self):
        self.page.clicks += 1


class _FakeThreadPage:
    """A page where the composer stays DISABLED after starting a thread —
    exactly the 'no active thread' state this file's own comments describe."""

    def __init__(self, *, composer_enabled: bool):
        self.composer_enabled = composer_enabled
        self.clicks = 0
        self.counts = {}

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def get_by_role(self, role, name=None):
        return _FakeLocator(self, f"role={role}[{name}]")


@pytest.mark.asyncio
async def test_starting_a_thread_does_not_hang_on_a_disabled_composer(probe_module) -> None:
    """The 0/6 regression, as an executable claim.

    If this ever hangs again, the timeout fails the test instead of costing a
    whole probe run.
    """
    page = _FakeThreadPage(composer_enabled=False)  # the state that hung
    await asyncio.wait_for(probe_module._start_fresh_thread(page), timeout=5.0)
    assert page.clicks == 1, "it must actually click New thread"


@pytest.mark.asyncio
async def test_starting_a_thread_refuses_to_proceed_on_a_dirty_thread(probe_module) -> None:
    """The isolation guarantee still holds: leftover history must raise, not
    silently let the next question inherit the last one's context."""
    page = _FakeThreadPage(composer_enabled=True)
    page.counts[probe_module.USER_BUBBLE] = 2  # never clears
    with pytest.raises(RuntimeError, match="never emptied"):
        await asyncio.wait_for(probe_module._start_fresh_thread(page), timeout=30.0)


def test_the_thread_helper_never_waits_for_idle(probe_module) -> None:
    """The specific call that caused it, forbidden by name.

    ``ask()`` already calls ``_wait_idle`` BEFORE creating the thread — that is
    the wait that matters, because it gates on the PREVIOUS turn finishing.
    Waiting again after the swap adds nothing and can only hang.
    """
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_fresh_thread":
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "_wait_idle" not in called, (
                "_start_fresh_thread waits for idle again — a fresh empty thread "
                "has no turn to wait for and this took the probe to 0/6"
            )
            return
    raise AssertionError("_start_fresh_thread not found")


def test_a_busy_timeout_names_which_signal_was_stuck(probe_module) -> None:
    """Six identical 'agent still busy' timeouts could not be told apart from a
    slow backend, while a separate browser context showed the app plainly idle.
    A wait that cannot say what it was waiting for is not diagnosable."""
    assert "composer enabled=" in SOURCE
    assert "thinking pills=" in SOURCE
