"""Anthropic prompt caching for every LiteLLM completion (#27 cost lever).

The Buildium specialist injects a ~57-tool schema (27k–95k tokens) into EVERY
sub-run, and each agent turn makes several sequential completions with an
identical prefix — the ideal shape for Anthropic's prompt caching, where a
cache read bills at ~10% of the input price (writes at 125%, 5-minute TTL
refreshed on every use). This module wires that in at one seam with ZERO
behavior change: request semantics are identical, the marks are billing hints.

Mechanism: :func:`configure_prompt_caching` wraps ``litellm.acompletion`` once
per process (idempotent). The OpenAI Agents SDK's ``LitellmModel`` calls
``litellm.acompletion`` through the module attribute, so the wrapper sees every
agent completion — and the planner/summarizer/eval paths that call
``litellm.acompletion`` directly ride the same wrapper. For any
``anthropic/``-bound call the wrapper adds ``cache_control`` breakpoints:

* on the LAST entry of ``tools`` — Anthropic caches the whole prefix up to the
  breakpoint, so this one mark covers the entire tool-schema block;
* on the system message — instructions are the other stable prefix chunk.

⚠ **The connector specialists put their per-user Composio schemas FIRST and
their stable in-repo tools last, and that is deliberately NOT being changed.**
Reordering them and spending the spare fourth breakpoint on the stable block
looks obviously right and does not pay: measured 2026-08-29, those in-repo
tails are 991 / 1,121 / 931 tokens against Anthropic's 1024-token minimum, so
two of three marks would be silently ignored and the third clears the bar by
9% on a tokenizer that is not Anthropic's. The cost on those agents is the
~186 unallowlisted Composio schemas (``connectors/composio_client.get_tools``
applies no allowlist), which no ordering makes shareable — they are genuinely
per-user. ``tests/unit/test_stable_tool_block_cacheability.py`` re-measures on
every run and fails if the stable block grows enough to change that answer.

Non-Anthropic models pass through untouched; injection failures degrade to the
un-marked call (never raise — same rule as spend tracking). LiteLLM translates
the OpenAI-format ``cache_control`` keys into Anthropic's native blocks.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.llm.call_policy import (
    apply_retry_budget,
    fallbacks_for,
    record_failover,
    should_fail_over,
)

logger = logging.getLogger(__name__)

#: Cache lifetimes Anthropic accepts. Anything else is refused rather than
#: sent — an unrecognised ``ttl`` would make Anthropic reject the whole
#: request, and a typo in an env var must not be able to take chat down.
_VALID_TTLS = frozenset({"5m", "1h"})

_original_acompletion: Any = None


def _cache_mark() -> dict[str, Any]:
    """The ``cache_control`` value to stamp on a cacheable block.

    ⚠ **The default five-minute TTL is why prompt caching was barely helping.**
    Measured on the first real turn after the 2026-08-26 boot fix ("what's the
    rent on unit 4B", 4 calls, $0.146): 44,148 cache-WRITE tokens at $2.50/M —
    **76% of the whole turn's cost** — against 65,906 reads at $0.20/M, a 55%
    hit rate on a prompt of 118,879 tokens.

    A five-minute cache only helps under sustained traffic. A property manager
    asking something every twenty minutes never hits it and re-pays the write
    every turn, which is the realistic shape of this product's usage.

    A 1-hour write is $4.00/M — 1.6x a five-minute write for a cache that
    lives twelve times longer — so it pays for itself on the SECOND turn
    inside the hour. Both rates come from LiteLLM's own model map, and
    ``test_prompt_cache_ttl`` asserts the ratio still makes that true rather
    than trusting the numbers quoted here (they are a provider's prices, not
    ours, and gotcha 25's rule is to confirm a value rather than recall it).

    Reads ``settings.llm.cache_ttl`` so the lifetime is an env var, not a
    deploy. Falls back to the safe five-minute default if settings cannot be
    loaded or the value is not one Anthropic accepts — a billing hint must
    never be able to fail a call.
    """
    ttl = "5m"
    try:
        from helixis.settings import get_settings

        configured = str(get_settings().llm.cache_ttl or "").strip()
        if configured in _VALID_TTLS:
            ttl = configured
        elif configured:
            logger.warning(
                "llm.cache_ttl=%r is not one of %s — falling back to 5m",
                configured,
                sorted(_VALID_TTLS),
            )
    except (Exception, SystemExit):  # noqa: BLE001 — a billing hint never breaks a call
        logger.debug("cache TTL lookup failed; using the 5m default", exc_info=True)
    # "5m" is Anthropic's default, so send the bare mark for it — fewer moving
    # parts on the path that was already working.
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": ttl}


def _is_anthropic_model(model: Any) -> bool:
    return isinstance(model, str) and model.lower().startswith(("anthropic/", "claude/"))


def inject_cache_control(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ``cache_control`` breakpoints to an anthropic-bound completion's
    kwargs (tools tail + system message). Idempotent: non-Anthropic calls and
    already-marked payloads are returned unchanged, and the caller's nested
    structures are copied rather than edited in place.

    ⚠ **The RESULT is the marked payload — callers must use it.** The top-level
    dict is copied here, so mutating the argument is no longer how the marks
    reach the request. That was previously the only thing making this work,
    undocumented and one honest refactor away from silently disabling caching
    across the fleet (see ``configure_prompt_caching``).
    """
    if not _is_anthropic_model(kwargs.get("model")):
        return kwargs
    kwargs = dict(kwargs)
    # Resolved ONCE per request. Reading it per breakpoint would let a single
    # call carry two different TTLs if the setting changed between them —
    # Anthropic treats those as distinct cache entries, which would quietly
    # double the write instead of halving it.
    mark = _cache_mark()

    # Tool Use Examples ride the same seam and the same rules: Anthropic-only,
    # idempotent, never raises. Attached BEFORE the cache breakpoint so the
    # examples sit inside the cached prefix rather than invalidating it.
    from helixis.llm.tool_examples import inject_tool_examples

    kwargs = inject_tool_examples(kwargs)

    tools = kwargs.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        if "cache_control" not in tools[-1]:
            kwargs["tools"] = [*tools[:-1], {**tools[-1], "cache_control": dict(mark)}]

    messages = kwargs.get("messages")
    if isinstance(messages, list):
        for i, message in enumerate(messages):
            if not (isinstance(message, dict) and message.get("role") == "system"):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                marked = {
                    **message,
                    "content": [{"type": "text", "text": content, "cache_control": dict(mark)}],
                }
            elif (
                isinstance(content, list)
                and content
                and isinstance(content[-1], dict)
                and "cache_control" not in content[-1]
            ):
                marked = {
                    **message,
                    "content": [*content[:-1], {**content[-1], "cache_control": dict(mark)}],
                }
            else:
                break  # empty or already marked — leave as-is
            kwargs["messages"] = [*messages[:i], marked, *messages[i + 1 :]]
            break

    kwargs = _mark_conversation_tail(kwargs, mark)
    return kwargs


def _mark_conversation_tail(kwargs: dict[str, Any], mark: dict[str, Any]) -> dict[str, Any]:
    """A third breakpoint, on the LAST message — so history caches too.

    ⚠ Until this existed, **no message was ever cached.** The two breakpoints
    above sit on the tools tail and the system message, and Anthropic caches
    only the prefix up to the last mark — everything after it is fresh input
    at the full rate, every call. That is the conversation history, the user's
    question, every tool result, the company-knowledge block and the
    fair-housing block. ``chat_history`` measured its own share at 24,519
    uncached tokens per turn and called it the largest line; it was
    understated, because its author believed only history was affected.

    Why marking the LAST message works rather than costing more: within one
    agent run, turn N's message list is a strict PREFIX of turn N+1's — the SDK
    appends the tool call and its result and re-sends everything
    (``run_loop._prepare_turn_input_items``). So a mark at the tail on turn N
    is a cache HIT for that whole span on turn N+1, and only the delta is
    written. Reads bill at a tenth of fresh input.

    ⚠ Anthropic allows FOUR breakpoints; this is the third, so it cannot push
    the tools or system marks out. A fourth is deliberately left spare.

    ⚠ Below Anthropic's minimum cacheable block (1024 tokens on Sonnet) a mark
    is simply ignored by the provider — it does not error and does not cost.
    So a short conversation is unaffected rather than penalised, and no length
    check is needed here. Being explicit because "why is there no size guard"
    is the obvious review question.
    """
    messages = kwargs.get("messages")
    if not (isinstance(messages, list) and len(messages) > 1):
        return kwargs
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") == "system":
        return kwargs

    content = last.get("content")
    if isinstance(content, str) and content:
        marked = {**last, "content": [{"type": "text", "text": content, "cache_control": mark}]}
    elif (
        isinstance(content, list)
        and content
        and isinstance(content[-1], dict)
        and "cache_control" not in content[-1]
    ):
        marked = {**last, "content": [*content[:-1], {**content[-1], "cache_control": mark}]}
    else:
        return kwargs  # empty, already marked, or a shape we do not recognise

    return {**kwargs, "messages": [*messages[:-1], marked]}


def configure_prompt_caching() -> bool:
    """Wrap ``litellm.acompletion`` once per process. Idempotent; returns True
    when the wrapper is (already) installed. Called from
    ``configure_litellm_retries`` so both processes (API + worker) and any
    caller that boots the retry config get caching for free."""
    global _original_acompletion
    if _original_acompletion is not None:
        return True

    import litellm

    original = litellm.acompletion

    async def acompletion_with_prompt_caching(*args: Any, **kwargs: Any) -> Any:
        try:
            # ⚠ USE THE RETURN VALUE. This discarded it and worked anyway, by
            # accident: ``inject_cache_control`` documents itself as copying
            # rather than mutating, and the only reason the caller's dict saw
            # the breakpoints was that its first act —
            # ``kwargs = inject_tool_examples(kwargs)`` — happened to rebind
            # to the SAME object, so the later ``kwargs[...] = ...`` writes
            # landed on the original.
            #
            # Making either helper honour its own docstring would therefore
            # have switched prompt caching off across the fleet with ZERO test
            # failures and no error — just a silently ~10x input bill.
            # Measured on the live ledger the day this was found: 89–92% of
            # prompt tokens are served from cache, so the accident was load
            # bearing. Same class as gotchas 45/46 — a cost control whose
            # failure mode is silent.
            kwargs = inject_cache_control(kwargs)
        except (Exception, SystemExit):  # noqa: BLE001 — a billing hint must never break a call
            logger.debug("prompt-cache injection skipped for one call", exc_info=True)

        # ⚠ RESILIENCE RIDES THE SAME SEAM, because this wrapper is the one
        # place every model call in the product passes through — and the
        # globals ``configure_litellm_retries`` sets are never read on this
        # path. See ``llm/call_policy`` for the measurements: ``litellm.
        # fallbacks`` has one reader, inside a Router class we never
        # instantiate, and ``litellm.num_retries`` is set to None by the
        # library after the first failure and never restored.
        apply_retry_budget(kwargs)
        try:
            return await original(*args, **kwargs)
        except (Exception, SystemExit) as exc:
            siblings = fallbacks_for(kwargs.get("model"))
            if not siblings or not should_fail_over(exc):
                raise
            for sibling in siblings:
                # ⚠ The cause goes in the line. Without it the nightly analyst
                # logged "failing over" on 11 of 14 nights (2026-10-09..22) and
                # nothing said whether that was a timeout, an overload or a 5xx.
                logger.warning(
                    "model %s failed transiently (%s: %.200s); failing over to %s",
                    kwargs.get("model"),
                    type(exc).__name__,
                    str(exc),
                    sibling,
                )
                try:
                    answer = await original(*args, **{**kwargs, "model": sibling})
                except (Exception, SystemExit):  # noqa: BLE001 — try the next SKU
                    logger.warning("failover model %s also failed", sibling, exc_info=True)
                else:
                    # ⚠ Recorded ONLY on success, and that is the whole point
                    # of the else-clause. A sibling that also failed did not
                    # produce any of the answer, so disclosing it would tell
                    # the user their reply came from a model that contributed
                    # nothing — a false statement in the sentence whose only
                    # job is to be true about how the answer was made.
                    record_failover(asked=kwargs.get("model"), served=sibling)
                    return answer
            raise

    _original_acompletion = original
    litellm.acompletion = acompletion_with_prompt_caching
    logger.info("anthropic prompt caching enabled (litellm.acompletion wrapped)")
    return True


__all__ = ["configure_prompt_caching", "inject_cache_control"]
