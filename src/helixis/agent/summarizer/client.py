"""Gemini-only summarizer client — mirrors ActionPlanner shape.

One LLM call per ``summarize()``, with the same transient-retry
behavior the planner uses (litellm.Timeout / InternalServerError /
RateLimitError / APIConnectionError → retry up to ``_max_retries``
with a 1s backoff). Hard timeout per call is 30s — we're in the
worker (PR #89), not the receiver, so we're decoupled from
Buildium's 10s SLA.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import litellm

from helixis.agent.summarizer.prompt import (
    SUMMARY_PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from helixis.agent.summarizer.quality import (
    assess_summary_quality,
    build_correction_nudge,
    quality_flag,
)
from helixis.agent.summarizer.sanitizer import (
    SummarySanitizationError,
    redact_id_references,
    sanitize_summary_text,
)
from helixis.agent.summarizer.types import SUMMARY_TEXT_MAX_CHARS, SummaryResult
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.observability.context import trace_context
from helixis.observability.tracing import (
    model_slug,
    new_trace_id,
    record_score,
    start_trace,
)
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SummarizerError(RuntimeError):
    """Raised when the summarizer fails to produce a valid summary
    after the retry budget is exhausted. The worker catches this and
    flips the row to ``summary_status='retrying'`` (or ``'failed'``
    once attempts >= 5)."""


# PR #92 — mirrors planner's _LLM_TIMEOUT_SECONDS. Summarizer runs
# in the worker, not the receiver, so 30s is safe (we're decoupled
# from Buildium's 10s SLA).
_LLM_TIMEOUT_SECONDS = 30.0

# Bumped from 0.5s for the same reason the planner did (PR #89):
# realistic budget means we'd rather give the upstream a real
# chance to recover than retry-hammer.
_TRANSIENT_RETRY_BACKOFF_SECONDS = 1.0

# Cap output tokens.
#
# PR #105 — bumped 280 → 1500. The previous value assumed ~280 tokens
# = ~1100 chars of OUTPUT text, but the default summarizer model is
# ``gemini/gemini-2.5-flash`` which uses internal reasoning tokens
# that count against the output budget. Production showed summaries
# being cut mid-sentence at ~60 chars (e.g. "...this afternoon at 1")
# with no ellipsis — the LLM had burned most of 280 tokens on internal
# reasoning and only had room for a short answer before being cut off
# by max_tokens. 1500 gives thinking + answer enough headroom without
# blowing the cost ceiling (1-3 sentences typically lands well under).
# The hard ``SUMMARY_TEXT_MAX_CHARS = 500`` post-hoc truncate is the
# real ceiling for what we persist.
_SUMMARY_MAX_TOKENS = 1500

# Last-resort fail-safe when even a redacted summary still trips the sanitizer
# (should never happen). Guaranteed ID-free; terse but honest.
_FAILSAFE_SUMMARY_TEXT = (
    "A new update was recorded for this item. Open it in Occupella for the details."
)

_TRANSIENT_LLM_EXCEPTIONS: tuple[type[Exception], ...] = (
    litellm.Timeout,
    litellm.APIConnectionError,
    litellm.InternalServerError,
    litellm.RateLimitError,
)


class Summarizer:
    """Plan a narrative summary for an event.

    ``completion_fn`` lets tests inject a fake LiteLLM client without
    touching litellm.acompletion's module-level wrapper. Production
    passes ``None`` and the production litellm path is used.
    ``max_retries`` is the retry count AFTER the first attempt — so
    ``max_retries=1`` (the default) caps total attempts at 2.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        completion_fn: Any | None = None,
        max_retries: int = 1,
    ) -> None:
        self._settings = settings or get_settings()
        self._completion_fn = completion_fn or litellm.acompletion
        self._max_retries = max_retries

    async def summarize(
        self,
        payload: dict[str, Any],
        enrichment: dict[str, Any] | None,
        company_id: str,
        *,
        event_time_iso: str | None = None,
        session_id: str | None = None,
    ) -> SummaryResult:
        """Produce a one-paragraph summary for the event.

        Raises ``SummarizerError`` when the LLM call fails after the
        retry budget OR when sanitization rejects the model's output
        (ID leak). Both end up as ``summary_status='retrying'`` /
        ``'failed'`` on the row depending on attempts.

        ``session_id`` (optional) groups this summarize's Langfuse trace
        with the event that triggered it (the worker passes
        ``webhook-{row.id}``); it defaults to a per-company session.
        """
        model_name = self._summarizer_model_name()
        api_key = _api_key_for_model(self._settings, model_name)

        prompt = build_user_prompt(payload, enrichment, event_time_iso=event_time_iso)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        # Observability (Q72): open one Langfuse trace per summarize, tagged
        # with the prompt version + model slug, so the summary_quality scores
        # emitted below chart as a weekly baseline per prompt version. The
        # trace_context wrapping also lets the LiteLLM callback attach this
        # summarize's generation to the same trace. All best-effort — every
        # tracing call no-ops when Langfuse keys are unset.
        run_trace_id = new_trace_id()
        trace_sess = session_id or f"summary-{company_id}"
        trace_tags = (
            "summarizer",
            f"prompt={SUMMARY_PROMPT_VERSION}",
            f"model={model_slug(model_name)}",
        )
        with trace_context(
            company_id=company_id,
            session_id=trace_sess,
            trace_name="summarize",
            tags=trace_tags,
            trace_id=run_trace_id,
        ):
            start_trace(
                trace_id=run_trace_id,
                name="summarize",
                company_id=company_id,
                session_id=trace_sess,
                tags=trace_tags,
            )
            return await self._summarize_with_retries(
                messages=messages,
                model_name=model_name,
                api_key=api_key,
                payload=payload,
                enrichment=enrichment,
                company_id=company_id,
                trace_id=run_trace_id,
            )

    async def _summarize_with_retries(
        self,
        *,
        messages: list[dict[str, str]],
        model_name: str,
        api_key: str | None,
        payload: dict[str, Any],
        enrichment: dict[str, Any] | None,
        company_id: str,
        trace_id: str,
    ) -> SummaryResult:
        """Retry loop: call → sanitize → quality-check → (nudge & retry | ship).

        Runs inside the trace context opened by :meth:`summarize`, so the LLM
        generations attach to ``trace_id`` and the shipped summary's quality
        is scored against it."""
        last_error: Exception | None = None
        last_low_quality_reason: str | None = None
        t0 = time.perf_counter()
        for attempt in range(self._max_retries + 1):
            try:
                raw_text = await self._call_llm(messages, model_name, api_key)
                cleaned = _truncate(raw_text.strip(), SUMMARY_TEXT_MAX_CHARS)
                # Validate AFTER truncation so a sneaky ID at char
                # 1200 still gets caught even if our cap drops most
                # of the run-on output.
                sanitize_summary_text(cleaned)

                # PR #122 — post-generation quality check. The model
                # occasionally returns syntactically valid but
                # semantically empty output ("A task was created this
                # evening." despite rich enrichment — production row
                # 39bb7547). When that happens, retry once with a
                # correction nudge that names the specific facts the
                # model should have used. Mirrors the planner's
                # invalid-JSON retry-with-nudge pattern.
                #
                # The validator is conservative: it only flags
                # too-short / generic-filler / ungrounded outputs.
                # Stylistically clumsy summaries that ARE grounded
                # pass through; that's a prompt-tuning problem, not
                # a safety-net one.
                low_quality_reason = assess_summary_quality(cleaned, payload, enrichment)
                if low_quality_reason is not None:
                    last_low_quality_reason = low_quality_reason
                    if attempt < self._max_retries:
                        logger.warning(
                            "summarizer output flagged low quality on attempt %d (%s) — "
                            "retrying with correction nudge",
                            attempt,
                            low_quality_reason,
                            extra={
                                "company_id": company_id,
                                "model_name": model_name,
                                "reason": low_quality_reason,
                                "char_count": len(cleaned),
                                "attempt": attempt,
                            },
                        )
                        # Append the assistant's bad output + a user-role
                        # correction nudge so the model has explicit
                        # feedback on what went wrong, not just a blind
                        # rerun. Same shape as the planner does at
                        # action_planner.py:639-651.
                        messages = messages + [
                            {"role": "assistant", "content": cleaned},
                            {
                                "role": "user",
                                "content": build_correction_nudge(
                                    low_quality_reason,
                                    payload,
                                    enrichment,
                                ),
                            },
                        ]
                        continue
                    # Out of retry budget — ship the (still-bad) output
                    # rather than fail the row entirely. A weak summary
                    # is more useful than a "Summary unavailable" state
                    # for the user, and the warning above gives ops a
                    # signal that the quality net is firing without
                    # silently masking the regression.
                    logger.warning(
                        "summarizer shipping low-quality output (%s) — out of "
                        "retry budget on attempt %d",
                        low_quality_reason,
                        attempt,
                        extra={
                            "company_id": company_id,
                            "model_name": model_name,
                            "reason": low_quality_reason,
                            "char_count": len(cleaned),
                            "attempt": attempt,
                            "shipped_anyway": True,
                        },
                    )

                # Score the shipped summary's quality against the run trace
                # (Q72): 1.0 when it passed the heuristics, 0.0 when we're
                # shipping flagged output out of retry budget. The flag
                # category rides in the comment so a chart can split the
                # failure modes (too_short / filler_opener / ungrounded).
                record_score(
                    trace_id=trace_id,
                    name="summary_quality",
                    value=1.0 if low_quality_reason is None else 0.0,
                    comment=quality_flag(low_quality_reason),
                )
                duration_ms = int((time.perf_counter() - t0) * 1000)
                logger.info(
                    "summarizer produced summary",
                    extra={
                        "company_id": company_id,
                        "model_name": model_name,
                        "duration_ms": duration_ms,
                        "char_count": len(cleaned),
                        "attempt": attempt,
                        "low_quality_at_ship": last_low_quality_reason,
                    },
                )
                return SummaryResult(
                    text=cleaned,
                    prompt_version=SUMMARY_PROMPT_VERSION,
                    model_name=model_name,
                    duration_ms=duration_ms,
                )
            except SummarySanitizationError as exc:
                # The model leaked an ID-shaped reference the sanitizer caught.
                # First give it an explicit correction nudge and retry (the
                # generic prompt rule clearly wasn't enough for this model);
                # this recovers the common case. If it STILL leaks after the
                # retry budget, ship a redacted, ID-free fail-safe summary
                # rather than fail the row — a clean (terser) summary beats a
                # "Summary unavailable" state, mirroring the low-quality path.
                # The sanitizer validator is unchanged; redaction is a separate,
                # explicit degrade path (see sanitizer.redact_id_references).
                last_error = exc
                if attempt < self._max_retries:
                    logger.warning(
                        "summarizer output tripped the ID sanitizer on attempt %d "
                        "(%s) — retrying with an ID-avoidance nudge",
                        attempt,
                        exc,
                        extra={"company_id": company_id, "model_name": model_name},
                    )
                    messages = messages + [
                        {"role": "assistant", "content": cleaned},
                        {
                            "role": "user",
                            "content": (
                                "That summary included an internal Buildium "
                                f"ID-shaped reference ({exc}). Rewrite it WITHOUT "
                                "any internal IDs — refer to people by name and "
                                "places by property name plus unit or street "
                                "address. Keep the same facts; just drop the ID."
                            ),
                        },
                    ]
                    continue
                safe_text = redact_id_references(cleaned)
                try:
                    sanitize_summary_text(safe_text)
                except SummarySanitizationError:
                    # Redaction somehow left a match — fall back to a generic,
                    # guaranteed-clean summary rather than ship or raise.
                    safe_text = _FAILSAFE_SUMMARY_TEXT
                duration_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "summarizer shipped a redacted ID-free summary after the "
                    "sanitizer exhausted retries on attempt %d",
                    attempt,
                    extra={
                        "company_id": company_id,
                        "model_name": model_name,
                        "duration_ms": duration_ms,
                        "redacted": True,
                    },
                )
                return SummaryResult(
                    text=safe_text,
                    prompt_version=SUMMARY_PROMPT_VERSION,
                    model_name=model_name,
                    duration_ms=duration_ms,
                )
            except _TRANSIENT_LLM_EXCEPTIONS as exc:
                last_error = exc
                logger.warning(
                    "summarizer LLM transient error on attempt %d: %s — retrying",
                    attempt,
                    exc,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_SECONDS)
                    continue
                raise SummarizerError(f"summarizer exhausted transient retries: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 — diagnostic catch-all
                # Anything we didn't classify is non-retryable. Wrap
                # so the worker can record a clean error.
                raise SummarizerError(f"summarizer call failed: {exc}") from exc

        # Loop fell through without returning — shouldn't reach here
        # but raise defensively.
        raise SummarizerError(f"summarizer exhausted retries: {last_error}")

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        model_name: str,
        api_key: str | None,
    ) -> str:
        """Single LiteLLM ``acompletion`` call. Same shape as the
        planner's ``_call_llm`` but without the ``response_format``
        kwarg — we want plain text, not JSON."""
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            **sampling_kwargs(model_name, 0.3),
            "max_tokens": _SUMMARY_MAX_TOKENS,
            "timeout": _LLM_TIMEOUT_SECONDS,
        }
        if api_key:
            kwargs["api_key"] = api_key
        response = await self._completion_fn(**kwargs)
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError) as exc:
            raise SummarizerError(f"LLM response shape unexpected: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise SummarizerError("LLM returned empty content")
        return content

    def _summarizer_model_name(self) -> str:
        """Resolve the model to use for summarization.

        Follows the same lookup pattern as
        ``ActionPlanner._reasoning_model_name``: prefer a
        configured ``summarizer`` slot in
        ``settings.llm.models``, fall back to ``reasoning``,
        and ultimately to ``settings.llm.default_model``. Adding a
        ``[llm.models.summarizer]`` table to the config lets us
        run summaries on a cheaper model (e.g. flash-lite) without
        touching code.
        """
        llm = self._settings.llm
        models = llm.models or {}
        summarizer = models.get("summarizer") or {}
        if summarizer.get("model"):
            return str(summarizer["model"])
        reasoning = models.get("reasoning") or {}
        if reasoning.get("model"):
            return str(reasoning["model"])
        return str(llm.default_model)


def _truncate(text: str, max_chars: int) -> str:
    """Cap ``text`` at ``max_chars`` characters, appending a single
    Unicode ellipsis when we cut. Operates on the already-stripped
    text so trailing whitespace doesn't push us over."""
    if len(text) <= max_chars:
        return text
    # Reserve 1 char for the ellipsis so the result is exactly
    # ``max_chars`` long.
    return text[: max_chars - 1].rstrip() + "…"
