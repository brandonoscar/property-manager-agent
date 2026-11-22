"""The recorder must refuse to film a sign-in screen.

Three takes were lost to the same defect before this file existed: the session
expires, the app drops to a login form, and the screencast records it happily
for as long as the script runs. The last one was going to be a 22-question,
35-minute, real-money take.

⚠ **The old check was a RACE, and that is why it failed in BOTH directions.**
It ran `locator(COMPOSER).count()` immediately after
`goto(wait_until="domcontentloaded")` — before React has painted anything — so
0 was the answer for a signed-IN app too. Which way it fell depended on
incidental timing.

⚠ **These tests RUN the helper.** A hang or a race is invisible to static
analysis by construction: the source of a check that polls correctly looks
exactly like one that reads too early (gotchas 43 and 71). The whole file is
under `asyncio.wait_for` so a re-introduced infinite wait fails in seconds
rather than costing another take.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load():
    """Import `record_demo` the way it imports itself (it path-shims `scripts/`)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("record_demo", _SCRIPTS / "record_demo.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_demo"] = module
    spec.loader.exec_module(module)
    return module


try:
    rd = _load()
except (Exception, SystemExit) as exc:  # pragma: no cover - playwright absent in some envs
    # ⚠ `SystemExit`, and it is the whole point — gotcha 7, in a test file.
    #
    # `record_demo` imports `chat_probe`, which calls `sys.exit(...)` when
    # playwright is missing. `SystemExit` is a `BaseException`, so it walks
    # straight through `except Exception`. During pytest COLLECTION that is not
    # a skipped module: it is an INTERNALERROR that aborts the whole session
    # with exit code 3, so the backend job reported "1 skipped" while 5,279
    # collected tests never ran at all.
    #
    # It was invisible locally because playwright IS installed here — the
    # import succeeds, the guard is never exercised, and the suite is green.
    # Only CI, which has no playwright, takes this branch. Same shape as
    # gotcha 42's "pytest exits 0 on a run it never finished": read the
    # summary line, not the fact that nothing said FAILED.
    pytest.skip(f"record_demo not importable here: {exc}", allow_module_level=True)


class FakeLocator:
    def __init__(self, count_at: dict[str, int]) -> None:
        self._counts = count_at

    async def count(self) -> int:
        return self._counts.get("n", 0)


class FakePage:
    """A page whose selectors appear after N polls, like hydration.

    `appears` maps a selector to the poll on which it starts existing; a
    selector absent from the map never appears.
    """

    def __init__(self, appears: dict[str, int]) -> None:
        self.appears = appears
        self.polls = 0

    def locator(self, selector: str):
        # Every locator() call is one observation; count them so a test can
        # assert the helper actually POLLED rather than answering once.
        self.polls += 1
        first = self.appears.get(selector)
        present = first is not None and self.polls >= first
        return FakeLocator({"n": 1 if present else 0})


def _run(coro):
    # ⚠ Own event loop + a hard timeout: the failure this guards against is a
    # WAIT that never ends, and a test that awaits it forever is the same bug
    # wearing a test's clothes.
    async def main():
        return await asyncio.wait_for(coro, timeout=10)

    return asyncio.run(main())


def test_a_signed_in_app_is_recognised_even_though_it_paints_late() -> None:
    """The composer arriving on a later poll is the SIGNED-IN case, not a refusal.

    This is the direction the old check got wrong most often: a correct
    session refused because React had not finished.
    """
    page = FakePage({rd.COMPOSER: 4})
    assert _run(rd.signed_in_verdict(page)) == "signed-in"


def test_a_sign_in_form_is_recognised_and_never_reads_as_signed_in() -> None:
    page = FakePage({"button:has-text('Sign in')": 2})
    assert _run(rd.signed_in_verdict(page)) == "signed-out"


def test_a_page_that_never_paints_is_undecided_and_that_is_not_a_pass() -> None:
    """Neither surface appearing is a THIRD state, and it must not read as ok.

    A dead page produces frames too. Collapsing undecided into either answer
    is how a harness reports on a run that never happened.
    """
    rd._HYDRATION_TIMEOUT_S = 1.0  # keep the test honest AND fast
    page = FakePage({})
    verdict = _run(rd.signed_in_verdict(page))
    assert verdict.startswith("undecided")
    assert verdict != "signed-in"


def test_it_polls_rather_than_deciding_on_one_look() -> None:
    """The property the race violated: more than one observation is made.

    Mutation-checked — a helper that reads once and returns fails this even
    when it happens to get the right answer on a fast page.
    """
    rd._HYDRATION_TIMEOUT_S = 1.0
    page = FakePage({rd.COMPOSER: 6})
    assert _run(rd.signed_in_verdict(page)) == "signed-in"
    assert page.polls >= 6, f"decided after {page.polls} observation(s) — that is the race"


def test_a_page_without_a_composer_is_not_a_signed_out_page() -> None:
    """The defect that killed a live take four questions in.

    The mid-take guard's first cut asked `not locator(COMPOSER).count()`. Step
    5 of `pm-day` navigates to Operations, which has no chat composer — so
    step 6 read a healthy session as signed out and aborted a real, paid run.
    Absence of the composer is the NORMAL state of four of the five screens.
    """
    page = FakePage({})  # no composer, no sign-in form: an inner page
    assert _run(rd._sign_in_form_present(page)) is False


def test_a_real_sign_in_form_still_aborts() -> None:
    """The other direction — the guard must keep the job it was written for."""
    page = FakePage({"input[type='password']": 1})
    assert _run(rd._sign_in_form_present(page)) is True


def test_the_mid_take_check_asks_about_the_sign_in_form_not_the_composer() -> None:
    """AST guard on the CALL SITE: a behavioural test cannot see a revert.

    The loop is not exercised by these tests, so nothing here would notice the
    condition changing back to a composer check (gotcha 62).
    """
    import ast

    src = (_SCRIPTS / "record_demo.py").read_text(encoding="utf-8")
    calls = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_sign_in_form_present"
    ]
    assert calls, "the mid-take guard no longer asks whether a sign-in form is present"


def test_asking_after_a_tour_returns_to_chat_first() -> None:
    """The defect that lost seven questions: a nav leaves the app elsewhere.

    `pm-day` tours Operations between questions. No page but Chat has a
    composer, so `_wait_idle` timed out at 30s on the next ask and would have
    on every ask after it.
    """
    import ast

    src = (_SCRIPTS / "record_demo.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ask = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_do_ask"
    )
    calls = [
        c
        for c in ast.walk(ask)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "_return_to_chat"
    ]
    assert calls, "_do_ask no longer returns to the chat surface before asking"


# ── the gate wait, and why it has to be diagnosable ──────────────────────────
#
# ⚠ The take of 2026-09-06 lost its ONE write step to a bare
# `wait_for(state="visible")`. Playwright's TimeoutError names the selector and
# nothing else, so 400s of a paid run produced one fact — no element matched —
# and five different explanations were equally consistent with it. Three were
# ruled out afterwards by reading code, which is the work a diagnostic skips.
#
# Same rule `_wait_idle` already carries (gotcha 43): a wait that cannot say
# what it was waiting for is not diagnosable. These RUN the helper, because a
# wait is invisible to static analysis by construction — the source of a
# correct one looks exactly like the source of a useless one (gotchas 43/71).


class GatePage:
    """A page whose gate appears on poll N, with a settable idle state.

    `reply` is what the assistant bubble holds — the text the diagnostic has to
    surface, since a refusal and a deadline are one sentence apart.
    """

    def __init__(
        self,
        *,
        gate_at: int | None = None,
        thinking: int = 0,
        enabled: bool = True,
        reply: str = "",
    ) -> None:
        self.gate_at = gate_at
        self.thinking = thinking
        self.enabled = enabled
        self.reply = reply
        self.polls = 0
        self.clicks = 0

    def locator(self, selector: str):
        page = self

        class _Loc:
            def __init__(self, sel: str) -> None:
                self.sel = sel

            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            async def count(self) -> int:
                if self.sel == rd.CONFIRM_CARD:
                    page.polls += 1
                    return 1 if page.gate_at is not None and page.polls >= page.gate_at else 0
                if self.sel == rd.THINKING:
                    return page.thinking
                return 1 if page.reply else 0

            async def is_enabled(self) -> bool:
                return page.enabled

            async def inner_text(self) -> str:
                return page.reply

            async def click(self) -> None:  # pragma: no cover - must never run
                page.clicks += 1

        return _Loc(selector)


def test_it_returns_as_soon_as_the_card_appears() -> None:
    page = GatePage(gate_at=3)
    _run(rd._wait_for_gate(page, timeout_s=5))
    assert page.polls >= 3, "decided before the card could appear"
    assert page.clicks == 0, "the recorder clicked a confirm gate — it must only ever watch"


def test_a_finished_run_with_no_gate_reports_the_models_own_words() -> None:
    """The case that actually happened, and the one the old wait could not tell
    apart from four others: the composer is back, nothing is thinking, and the
    reply explains itself."""
    page = GatePage(
        gate_at=None,
        thinking=0,
        enabled=True,
        reply="I could not find an open work order at 200 E Colfax Ave, so there is nothing "
        "to add a note to.",
    )
    with pytest.raises(RuntimeError) as exc:
        _run(rd._wait_for_gate(page, timeout_s=1))
    msg = str(exc.value)
    assert "composer enabled=True" in msg
    assert "thinking pills=0" in msg
    assert "could not find an open work order" in msg, (
        "the reply text is missing — a refusal and a deadline are one sentence apart, and "
        "the sentence is the only thing that separates them"
    )


def test_a_still_running_turn_is_distinguishable_from_a_finished_one() -> None:
    """The OTHER branch, and two branches of one rule need two tests
    (gotcha 74). A live turn at the cut-off means the ceiling is the wall clock
    or the cost gate — not the write surface."""
    page = GatePage(gate_at=None, thinking=1, enabled=False, reply="")
    with pytest.raises(RuntimeError) as exc:
        _run(rd._wait_for_gate(page, timeout_s=1))
    msg = str(exc.value)
    assert "composer enabled=False" in msg and "thinking pills=1" in msg
    assert "reply chars=0" in msg, "a stream that never delivered must be visible as such"


def test_the_memory_question_is_asked_in_a_thread_that_has_history() -> None:
    """⚠ Its own comment says the beat "only reads as real on a thread with
    history", and without `same_thread` every ask starts a FRESH one — so the
    question about what came earlier was asked in an empty thread. A comment
    cannot fail (gotcha 48); this is the assertion that can.
    """
    steps = rd.SCRIPTS["pm-day"]
    memory = [s for s in steps if "ask" in s and "first today" in s["ask"]]
    assert memory, "the memory beat is gone from pm-day — if that is deliberate, delete this test"
    for step in memory:
        assert step.get("same_thread") is True, (
            "the memory question starts a fresh thread, so there is no history to recall. "
            "load_session_history is per-session and Mem0 stores company facts, not the "
            "order of today's questions."
        )


def test_mirror_day_never_tries_to_film_a_confirm_card() -> None:
    """The write beat is IMPOSSIBLE without a Buildium credential, not merely
    risky — so it must not be in the take that exists because the credential
    is gone.

    Every write tool confirms the record exists before writing
    (``buildium_agent``'s own rule: "Never fire a write at an id you haven't
    confirmed exists"). That lookup is a live Buildium call, it 401s, and the
    run stops before any gate is raised — which is exactly what Step 24 of the
    2026-09-06 ``pm-day`` take did: it succeeded in 13s and answered in prose
    ("once Buildium reconnects it's a single quick write") with no card on
    screen.

    ⚠ This is a claim about the ACCOUNT, not about the script, so it will stop
    being true the day a working credential lands. Delete this test then —
    do not weaken it to keep it green (gotcha 33's rule for a contract test).
    """
    steps = rd.SCRIPTS["mirror-day"]
    gated = [s for s in steps if s.get("stop_at_gate")]
    assert not gated, (
        "mirror-day carries a stop_at_gate step, and the take exists because "
        "Buildium auth is dead. _wait_for_gate will burn the full settle "
        f"timeout and fail the run: {gated}"
    )


def test_every_follow_up_has_a_question_to_follow() -> None:
    """A ``same_thread`` step joins the thread the PREVIOUS ask created, so the
    first ask of a script can never be one — it would join an empty thread and
    the follow-up would have nothing to build on.

    That is the same defect the memory beat had (see the test above), stated as
    a property of the whole catalogue rather than of one step, so a new script
    cannot reopen it. ⚠ Reads every script, not just the two in use: the
    failure mode is "silently permits more" (gotcha 45).
    """
    checked = 0
    for name, steps in rd.SCRIPTS.items():
        seen_ask = False
        for i, step in enumerate(steps):
            if "ask" not in step:
                continue
            if step.get("same_thread"):
                checked += 1
                assert seen_ask, (
                    f"{name} step {i} is a follow-up with no preceding ask in the "
                    f"script — it joins an empty thread: {step['ask']!r}"
                )
            seen_ask = True
    assert checked, "no same_thread steps were read — a renamed key reports green"
