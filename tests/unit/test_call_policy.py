"""Retry + failover on the ONE seam every model call passes through.

The globals ``configure_litellm_retries`` sets are inert on this product's
call path (see ``llm/call_policy``'s module docstring for the measurements),
so the Haiku failover had never fired. These guards pin the replacement, and
each was verified to FAIL against the pre-fix wrapper before being trusted.

⚠ The interesting assertions are the NEGATIVE ones. A failover that fires on
everything is worse than none: it would launder our own malformed payloads
(gotcha 27) and a spend cap (gotcha 46) into "the provider was down", burning
a second SKU's quota to reproduce the identical failure and suppressing the
one signal that finds the real cause.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.llm.call_policy import (
    CallPolicy,
    apply_retry_budget,
    fallbacks_for,
    set_call_policy,
    should_fail_over,
)

PRIMARY = "anthropic/claude-sonnet-5"
SIBLING = "anthropic/claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _clean_policy() -> Any:
    """A policy is process-wide state — clear it either side of every test so
    one case cannot leak its configuration into the next."""
    set_call_policy(None)
    yield
    set_call_policy(None)


def _install(num_retries: int = 3) -> None:
    set_call_policy(CallPolicy(fallbacks={PRIMARY: (SIBLING,)}, num_retries=num_retries))


# ── fallbacks_for ────────────────────────────────────────────────────


def test_no_policy_means_no_failover() -> None:
    """Unconfigured is the safe state: an un-booted process (a bare unit test,
    a script) behaves exactly as it did before this module existed."""
    assert fallbacks_for(PRIMARY) == ()


def test_siblings_resolve_for_the_primary_only() -> None:
    _install()
    assert fallbacks_for(PRIMARY) == (SIBLING,)
    # The sibling itself has no onward fallback — failing over FROM the last
    # SKU in the chain has nowhere to go, and inventing a cycle would retry
    # the model that just failed.
    assert fallbacks_for(SIBLING) == ()
    assert fallbacks_for("anthropic/claude-opus-4") == ()


def test_a_non_string_model_never_resolves() -> None:
    """``kwargs.get("model")`` can be absent or a model OBJECT. Neither is a
    key in the map, and a lookup that raised would break the call it exists
    to protect."""
    _install()
    assert fallbacks_for(None) == ()
    assert fallbacks_for(object()) == ()


# ── should_fail_over ─────────────────────────────────────────────────


def test_a_transient_provider_failure_fails_over() -> None:
    assert should_fail_over(Exception("litellm.InternalServerError: Overloaded"))


def test_a_spend_cap_does_not_fail_over() -> None:
    """gotcha 35/46: the cap is on the ACCOUNT, and the sibling bills to the
    same account. Failing over would spend a second impossible call and
    re-describe a terminal billing state as an outage."""
    assert not should_fail_over(
        Exception(
            "You have exceeded your specified API usage limits. You will "
            "regain access on 2026-09-01 at 00:00 UTC"
        )
    )


def test_an_unrecognised_error_does_not_fail_over() -> None:
    """gotcha 27: a malformed ``input_examples`` 400s every call in the suite.
    Retrying that on another SKU produces the same 400 and teaches the reader
    the provider is flaky. A control that cannot tell what it is looking at
    must decline to act."""
    assert not should_fail_over(
        Exception('tools.41.custom: Example at index 0 is invalid: true is not of type "string"')
    )
    assert not should_fail_over(ValueError("boom"))


def test_systemexit_is_accepted_as_an_argument() -> None:
    """gotcha 7: a spaCy-backed dep can ``sys.exit()`` under us, and the
    wrapper catches ``(Exception, SystemExit)``. The classifier is handed
    whatever that catch produced, so it must take a ``BaseException`` without
    raising a TypeError of its own inside an except block."""
    assert not should_fail_over(SystemExit(1))


# ── apply_retry_budget ───────────────────────────────────────────────


def test_the_retry_budget_is_threaded_per_call() -> None:
    """The whole reason this exists: ``litellm.num_retries`` is set to None by
    the library after the first failure and never restored, so a boot-time
    global is single-use in a long-lived process. Per-call cannot be zeroed
    underneath us."""
    _install(num_retries=5)
    kwargs: dict[str, Any] = {"model": PRIMARY}
    apply_retry_budget(kwargs)
    assert kwargs["num_retries"] == 5


def test_an_explicit_caller_budget_wins() -> None:
    """A caller that has thought about its own retries (the planner's 8s
    budget, the summarizer's 30s) must not have a fleet-wide default
    silently multiply its worst-case latency."""
    _install(num_retries=5)
    kwargs: dict[str, Any] = {"model": PRIMARY, "num_retries": 0}
    apply_retry_budget(kwargs)
    assert kwargs["num_retries"] == 0


def test_no_policy_adds_no_kwarg() -> None:
    """An unconfigured process must send byte-identical requests to what it
    sent before — not ``num_retries=0``, which is a different request."""
    kwargs: dict[str, Any] = {"model": PRIMARY}
    apply_retry_budget(kwargs)
    assert "num_retries" not in kwargs


# ── the wrapper actually does it ─────────────────────────────────────
#
# The three tests above pin the primitives. These drive the real
# ``acompletion`` wrapper, because the primitives were all individually
# correct in the OLD code too — what was missing was anything calling them
# (gotcha 28's both-sides rule, and gotcha 54's "documented, never written
# to the wire").


class _FakeCompletion:
    """Records every model it was called with, and fails on demand."""

    def __init__(self, failures: dict[str, BaseException]) -> None:
        self.failures = failures
        self.models: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        model = kwargs.get("model")
        self.models.append(model)
        self.kwargs.append(dict(kwargs))
        exc = self.failures.get(str(model))
        if exc is not None:
            raise exc
        return f"answer from {model}"


def _wrapped(fake: _FakeCompletion, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install the real wrapper over ``fake`` and hand it back.

    ⚠ ``configure_prompt_caching`` is idempotent via a module global, so the
    global has to be reset or a wrapper installed by an earlier test in the
    session silently wins and this measures nothing.
    """
    import litellm

    from helixis.llm import prompt_cache

    monkeypatch.setattr(prompt_cache, "_original_acompletion", None)
    monkeypatch.setattr(litellm, "acompletion", fake)
    assert prompt_cache.configure_prompt_caching() is True
    return litellm.acompletion


@pytest.mark.asyncio
async def test_wrapper_fails_over_to_the_sibling_on_a_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install()
    fake = _FakeCompletion({PRIMARY: Exception("litellm.InternalServerError: Overloaded")})
    wrapped = _wrapped(fake, monkeypatch)

    result = await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert result == f"answer from {SIBLING}"
    assert fake.models == [PRIMARY, SIBLING], (
        "the sibling was never tried — this is the defect the module exists "
        "to fix, and it looked exactly like this for months"
    )


@pytest.mark.asyncio
async def test_the_failover_warning_names_what_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The analyst failed over on 11 of 14 nights and the log never said why."""
    import logging

    _install()
    boom = TimeoutError("litellm.Timeout: Connection timed out after 30.0 seconds")
    fake = _FakeCompletion({PRIMARY: boom})
    wrapped = _wrapped(fake, monkeypatch)

    with caplog.at_level(logging.WARNING, logger="helixis.llm.prompt_cache"):
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    (line,) = [r.getMessage() for r in caplog.records if "failing over" in r.getMessage()]
    assert "TimeoutError" in line and "timed out after 30.0 seconds" in line
    assert PRIMARY in line and SIBLING in line


@pytest.mark.asyncio
async def test_wrapper_does_not_fail_over_on_a_spend_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install()
    cap = Exception("You have exceeded your specified API usage limits")
    fake = _FakeCompletion({PRIMARY: cap, SIBLING: cap})
    wrapped = _wrapped(fake, monkeypatch)

    with pytest.raises(Exception, match="usage limits"):
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert fake.models == [PRIMARY], "a terminal failure must not spend the sibling's quota"


@pytest.mark.asyncio
async def test_wrapper_raises_the_original_error_when_every_sku_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-facing handler in ``orchestrator`` catches litellm's transient
    types. Re-raising the FIRST error keeps that type, so a total outage still
    renders the honest "trouble reaching the model" message rather than
    whatever the last sibling happened to throw."""
    _install()
    first = Exception("litellm.InternalServerError: Overloaded (primary)")
    fake = _FakeCompletion(
        {PRIMARY: first, SIBLING: Exception("litellm.InternalServerError: Overloaded (sibling)")}
    )
    wrapped = _wrapped(fake, monkeypatch)

    with pytest.raises(Exception) as caught:
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert caught.value is first
    assert fake.models == [PRIMARY, SIBLING]


@pytest.mark.asyncio
async def test_wrapper_threads_the_retry_budget_onto_the_real_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(num_retries=4)
    fake = _FakeCompletion({})
    wrapped = _wrapped(fake, monkeypatch)

    await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert fake.kwargs[0]["num_retries"] == 4


@pytest.mark.asyncio
async def test_an_unconfigured_process_is_byte_for_byte_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No policy installed → no failover, no retry kwarg, and the original
    exception straight through. This is the state every unit test in the
    repo runs in, so a regression here would be enormous and quiet."""
    boom = Exception("litellm.InternalServerError: Overloaded")
    fake = _FakeCompletion({PRIMARY: boom})
    wrapped = _wrapped(fake, monkeypatch)

    with pytest.raises(Exception, match="Overloaded"):
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert fake.models == [PRIMARY]
    assert "num_retries" not in fake.kwargs[0]


# ── the failover LEDGER: was the substitution recorded? ──────────────
#
# A failover that fires correctly and tells nobody is the defect these cover.
# The user's reply reads identically whichever SKU wrote it, so silence is
# indistinguishable from the primary having answered — and gotcha 86 measured
# the sibling answering DIFFERENTLY on fair-housing prompts, which is what
# makes the difference worth a sentence rather than a log line.
#
# ⚠ Every assertion below has a negative twin. A ledger that records on every
# call would put "a backup answered this" on replies the primary wrote, which
# trains the reader to ignore the one line that matters.

SECOND_SIBLING = "anthropic/claude-haiku-4-5-second"


@pytest.fixture
def _ledger() -> Any:
    """Bind a ledger for one test and unbind it after, so a recording cannot
    leak into the next case."""
    from helixis.llm.call_policy import bind_failover_ledger, reset_failover_ledger

    ledger, token = bind_failover_ledger()
    yield ledger
    reset_failover_ledger(token)


@pytest.mark.asyncio
async def test_a_successful_failover_is_recorded(
    monkeypatch: pytest.MonkeyPatch, _ledger: Any
) -> None:
    _install()
    fake = _FakeCompletion({PRIMARY: Exception("litellm.InternalServerError: Overloaded")})
    wrapped = _wrapped(fake, monkeypatch)

    await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert _ledger.events == [(PRIMARY, SIBLING)]
    assert _ledger.served_models == (SIBLING,)
    assert bool(_ledger) is True


@pytest.mark.asyncio
async def test_the_primary_answering_records_nothing(
    monkeypatch: pytest.MonkeyPatch, _ledger: Any
) -> None:
    """The normal case, and the one that must stay silent — a footer on every
    reply is a footer nobody reads."""
    _install()
    fake = _FakeCompletion({})
    wrapped = _wrapped(fake, monkeypatch)

    await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert _ledger.events == []
    assert bool(_ledger) is False


@pytest.mark.asyncio
async def test_a_refused_failover_records_nothing(
    monkeypatch: pytest.MonkeyPatch, _ledger: Any
) -> None:
    """A spend cap raises without trying the sibling, so no model substituted
    for another and there is nothing to disclose. Pinned separately from the
    'no failover happened' case because it reaches a different branch."""
    _install()
    cap = Exception("You have exceeded your specified API usage limits")
    fake = _FakeCompletion({PRIMARY: cap})
    wrapped = _wrapped(fake, monkeypatch)

    with pytest.raises(Exception, match="usage limits"):
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert _ledger.events == []


@pytest.mark.asyncio
async def test_a_sibling_that_also_failed_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, _ledger: Any
) -> None:
    """⚠ The load-bearing one. A sibling that was TRIED and failed contributed
    nothing to the answer, so naming it would make the disclosure — whose only
    job is to be true about how the reply was produced — itself false.

    Two siblings, the first failing: only the model that actually answered is
    recorded, and it is recorded as having answered for the PRIMARY, not for
    the sibling that failed just before it.
    """
    set_call_policy(CallPolicy(fallbacks={PRIMARY: (SIBLING, SECOND_SIBLING)}, num_retries=3))
    overloaded = "litellm.InternalServerError: Overloaded"
    fake = _FakeCompletion({PRIMARY: Exception(overloaded), SIBLING: Exception(overloaded)})
    wrapped = _wrapped(fake, monkeypatch)

    result = await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert result == f"answer from {SECOND_SIBLING}"
    assert fake.models == [PRIMARY, SIBLING, SECOND_SIBLING]
    assert _ledger.events == [(PRIMARY, SECOND_SIBLING)]


@pytest.mark.asyncio
async def test_a_total_outage_records_nothing(
    monkeypatch: pytest.MonkeyPatch, _ledger: Any
) -> None:
    """Every SKU down → the run raises and there is no answer to caveat."""
    _install()
    overloaded = "litellm.InternalServerError: Overloaded"
    fake = _FakeCompletion({PRIMARY: Exception(overloaded), SIBLING: Exception(overloaded)})
    wrapped = _wrapped(fake, monkeypatch)

    with pytest.raises(Exception, match="Overloaded"):
        await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}])

    assert _ledger.events == []


@pytest.mark.asyncio
async def test_an_unbound_ledger_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ledger bound — every unit test, every script, the worker's background
    paths. The failover must still WORK; only the disclosure is absent."""
    from helixis.llm.call_policy import current_failover_ledger

    assert current_failover_ledger() is None
    _install()
    fake = _FakeCompletion({PRIMARY: Exception("litellm.InternalServerError: Overloaded")})
    wrapped = _wrapped(fake, monkeypatch)

    assert await wrapped(model=PRIMARY, messages=[{"role": "user", "content": "hi"}]) == (
        f"answer from {SIBLING}"
    )


def test_the_ledger_survives_a_task_boundary() -> None:
    """⚠ Runs its OWN ``asyncio.run``, because this property is invisible to a
    test that only calls the recorder directly.

    Most model calls in this product happen inside a nested specialist
    ``.as_tool()`` sub-run (gotcha 37: 195 of 232 tool calls). A sub-run is a
    Task and a Task COPIES the context, so a ``ContextVar.set`` in there is
    invisible outside (gotchas 34/42) — a failover during a consult would be
    recorded into a copy nobody reads, and the turn would disclose nothing.
    Mutating an object bound BEFORE the run is what crosses that boundary.
    """
    import asyncio

    from helixis.llm.call_policy import (
        bind_failover_ledger,
        record_failover,
        reset_failover_ledger,
    )

    async def main() -> Any:
        ledger, token = bind_failover_ledger()
        try:

            async def nested() -> None:
                record_failover(asked=PRIMARY, served=SIBLING)

            await asyncio.create_task(nested())
            return list(ledger.events)
        finally:
            reset_failover_ledger(token)

    assert asyncio.run(main()) == [(PRIMARY, SIBLING)]
