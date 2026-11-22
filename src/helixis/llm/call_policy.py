"""Per-call retry + failover, because the globals we set are never read.

``configure_litellm_retries`` has always configured resilience by assigning
LiteLLM module globals. Both assignments are inert on the path this product
actually calls, and both were verified against the installed library rather
than reasoned about:

* **``litellm.fallbacks`` is read in exactly ONE place** —
  ``litellm/router.py:585``, inside ``litellm.Router``. A repo-wide grep for
  ``litellm.Router`` in ``src/`` returns zero hits: we never instantiate it.
  Our path is ``LitellmModel`` → ``litellm.acompletion``, which reads
  ``kwargs["fallbacks"]`` or the DEPRECATED ``litellm.model_fallbacks``
  (``main.py:500`` and ``:620``) and never looks at ``litellm.fallbacks``.
  So the Haiku failover has never fired and could not.

* **The shape is wrong too.** We wrote ``[{primary: [sibling, ...]}]`` — the
  Router's map form. ``acompletion`` wants a FLAT list and prepends the
  primary itself: ``fallbacks = [original_model] + kwargs.pop("fallbacks", [])``.
  So even reaching the right key with the old value would not have worked.

* **``litellm.num_retries`` is single-use.** The library sets
  ``litellm.num_retries = None`` at four sites in ``utils.py`` (1516, 1555,
  1826, 1856) — "set retries to None to prevent infinite loops" — and nothing
  restores it. ``configure_litellm_retries`` runs ONCE at boot (main.py:94,
  worker.py:646), so in a long-lived Render process the retry budget survives
  until the first failure and is gone thereafter. Exactly when it matters.

``orchestrator.py`` states the opposite as fact — "We've already retried
inside LiteLLM (configure_litellm_retries sets num_retries=3) and the fallback
model also tipped over". Both halves were false, which is gotcha 48's shape
pointed at a resilience control: a comment cannot fail.

**Why this does failover itself rather than passing ``fallbacks=``.** Handing
LiteLLM the kwarg would work, but its fallback loop retries on ANY exception —
including a malformed request of OUR OWN making. Gotcha 27's malformed
``input_examples`` 400'd every call in the suite; laundering that into "the
provider was down" would suppress the one signal that finds it, and gotcha 46
exists precisely to keep that distinction. So failover is gated on
``classify_provider_failure``: **transient** fails over, **terminal** (spend
cap, bad key) raises immediately, and an unrecognised error raises too — a
control that cannot tell what it is looking at must decline to act.

⚠ **Streaming is only partly covered, and saying so is the point.** A failure
raised while awaiting ``acompletion`` is caught here. A stream that dies
mid-iteration surfaces later, at the consumer, and this wrapper is long gone —
that half needs the SSE work (finding 10) and is not claimed here.

**A failover that nobody is told about (added 2026-09-09).** Everything above
decides *whether* to try a different SKU. Nothing recorded that one had
happened, so a user could receive an answer from a smaller model with no
indication, and an operator had only a log line to find it by. That matters
here more than it would in most products: gotcha 86 MEASURED the failover SKU
behaving differently on fair-housing prompts — a live answer the deterministic
scanner had to be widened to catch. The reply is the same shape either way, so
silence is indistinguishable from the primary having answered.

:class:`FailoverLedger` is the record. It is written by the one wrapper (see
``llm/prompt_cache``) and read by the orchestrator, which appends an advisory
footer and files a ``model_failover`` reason code.

⚠ **The ledger is MUTATED, never re-``set``** — the same reason ``TurnUsage``
gives at length: a specialist consult is a nested ``.as_tool()`` sub-run, a
Task COPIES the context, and a ``ContextVar.set`` inside one is invisible
outside (gotchas 34/42). Binding a mutable object once and mutating it is what
makes a sub-run's failover land on the parent turn instead of vanishing — and
a sub-run is where most model calls in this product happen (gotcha 37: 195 of
232 tool calls were nested consults).
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from helixis.llm.provider_errors import classify_provider_failure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallPolicy:
    """What every model call should carry, resolved once at boot."""

    #: primary model → the siblings to try, in order.
    fallbacks: Mapping[str, tuple[str, ...]]
    #: Passed PER CALL, because the global is zeroed after the first failure.
    num_retries: int


_policy: CallPolicy | None = None


def set_call_policy(policy: CallPolicy | None) -> None:
    """Install (or clear) the process-wide policy. Called from
    ``configure_litellm_retries``; ``None`` restores the unconfigured state so
    a test cannot leak its policy into the next one."""
    global _policy
    _policy = policy


def current_call_policy() -> CallPolicy | None:
    return _policy


def fallbacks_for(model: Any) -> tuple[str, ...]:
    """Siblings to try for ``model``, or empty when there is no policy, no
    model, or no configured sibling."""
    policy = _policy
    if policy is None or not isinstance(model, str):
        return ()
    return tuple(policy.fallbacks.get(model, ()))


def should_fail_over(exc: BaseException) -> bool:
    """Is this failure worth trying a different SKU for?

    ⚠ Deliberately narrow. Only a POSITIVELY transient provider error fails
    over. A terminal one (the spend cap names the date access returns) would
    fail identically on the sibling — the same account pays for both — and an
    unrecognised error is far more likely to be our own bad payload than a
    provider outage. Both raise straight through.
    """
    return classify_provider_failure(str(exc)) == "transient"


@dataclasses.dataclass
class FailoverLedger:
    """Which model actually answered this turn, when it was not the one asked
    for. Empty is the normal state and means the primary served every call."""

    #: ``(asked_for, actually_served)`` in the order the failovers happened.
    #: A list rather than a set because the ORDER is diagnostic — two
    #: failovers to different SKUs in one turn is a different story from two
    #: to the same one.
    events: list[tuple[str, str]] = dataclasses.field(default_factory=list)

    def record(self, *, asked: str, served: str) -> None:
        self.events.append((asked, served))

    @property
    def served_models(self) -> tuple[str, ...]:
        """The substitute SKUs, de-duplicated, first-seen order."""
        seen: dict[str, None] = {}
        for _asked, served in self.events:
            seen.setdefault(served, None)
        return tuple(seen)

    def __bool__(self) -> bool:
        return bool(self.events)


_failovers: contextvars.ContextVar[FailoverLedger | None] = contextvars.ContextVar(
    "helixis_failover_ledger", default=None
)


def bind_failover_ledger() -> tuple[FailoverLedger, contextvars.Token[FailoverLedger | None]]:
    """Start recording failovers for one run. Returns the ledger AND the token,
    because the caller needs the object to read at the end of the run — reading
    it back through the ContextVar would work here and would stop working the
    moment the read moved inside a Task."""
    ledger = FailoverLedger()
    return ledger, _failovers.set(ledger)


def reset_failover_ledger(token: contextvars.Token[FailoverLedger | None]) -> None:
    _failovers.reset(token)


def current_failover_ledger() -> FailoverLedger | None:
    return _failovers.get()


def record_failover(*, asked: Any, served: Any) -> None:
    """Note that ``served`` answered a call meant for ``asked``. Never raises.

    ⚠ No-ops when nothing bound a ledger, which is every unit test, every
    script and the worker's background paths. A disclosure that cannot be
    shown to anybody must not cost the answer it describes.
    """
    try:
        ledger = _failovers.get()
        if ledger is None or not isinstance(asked, str) or not isinstance(served, str):
            return
        ledger.record(asked=asked, served=served)
    except Exception:  # noqa: BLE001 — a disclosure must never break the call
        logger.debug("failover ledger write skipped (non-fatal)", exc_info=True)


def apply_retry_budget(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Thread ``num_retries`` onto one call, unless the caller set its own.

    Returns the same dict for convenience; mutation is deliberate so this can
    sit inside the existing wrapper without a second copy of the kwargs.
    """
    policy = _policy
    if policy is not None and "num_retries" not in kwargs:
        kwargs["num_retries"] = policy.num_retries
    return kwargs


__all__ = [
    "CallPolicy",
    "FailoverLedger",
    "apply_retry_budget",
    "bind_failover_ledger",
    "current_call_policy",
    "current_failover_ledger",
    "fallbacks_for",
    "record_failover",
    "reset_failover_ledger",
    "set_call_policy",
    "should_fail_over",
]
