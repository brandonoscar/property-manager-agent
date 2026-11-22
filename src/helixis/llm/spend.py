"""Per-company daily LLM token ledger + budget gate (pre-launch audit).

The mirror made Buildium reads free, but every chat turn still spends
Gemini tokens — and nothing capped what one account could burn. The
runaway scenario is adversarial: a leaked JWT scripting ``/agent/run``
at the rate limit all night is hundreds of dollars by morning.

Design:
* **Counting** rides LiteLLM's success callback: every completion,
  anywhere in the fleet (web chat, worker planner/summarizer), reports
  its usage attributed to the company on the current
  :func:`~helixis.observability.context.current_trace_context`. One
  upsert per call into ``helixis_llm_usage`` (migrations 0050 + 0075).
* **Cache split** (0075). 0050 stored a single ``total_tokens``, which
  made prompt caching — our largest implemented cost lever — invisible,
  and made the budget wrong: it counted a cached read at full weight
  when Anthropic bills it at 10%. The components were always on
  LiteLLM's ``usage.prompt_tokens_details``; we discarded them. The gate
  now reads ``effective_tokens`` (billable weight); ``tokens`` remains
  the raw total for anything still reading it.
* **Enforcement** happens ONLY at the ``/agent/run`` gate — the one
  user-driven, unbounded surface. Webhook processing is never blocked
  (it's internally attempt-capped and must drain), but its usage still
  counts toward the ledger.
* **Failure posture**: counting is best-effort (a DB hiccup never fails
  an LLM call) and the gate FAILS OPEN (a ledger read error admits the
  run — availability over accounting).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

from helixis.llm.turn_cost import record_call as record_turn_call
from helixis.observability.context import current_trace_context
from helixis.observability.reason_codes import record_reason_code

logger = logging.getLogger(__name__)

_configured = False


#: Anthropic's published cache multipliers against the base input rate.
#: A read is a tenth of an input token; a write is a quarter dearer than one.
_CACHE_READ_WEIGHT = 0.1
_CACHE_WRITE_WEIGHT = 1.25


@dataclass(frozen=True)
class Usage:
    """One completion's token counts, split into what actually gets billed.

    ``cached`` and ``cache_write`` are SUBSETS of ``prompt`` — Anthropic's
    ``input_tokens`` excludes them and LiteLLM adds them back in
    (``AnthropicConfig``: ``prompt_tokens += cache_creation_input_tokens`` /
    ``+= cache_read_input_tokens``). Every arithmetic here depends on that
    relationship, and it is read off the transform rather than assumed —
    getting it backwards would silently double-count the cache.
    """

    total: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    cache_write: int = 0

    @property
    def effective(self) -> int:
        """Billable-weight tokens — what the daily budget should count.

        The raw total treats a cached read as a full token. Anthropic bills it
        at a tenth, so on a turn carrying ~13k of cached tool schema the raw
        figure overstates real spend by close to 10x, and the company whose
        cache works BEST trips its daily 429 first.

        Clamped at zero: if a provider ever reports components exceeding the
        prompt total, an unclamped result would go negative and silently hand
        out free budget.
        """
        uncached = max(0, self.prompt - self.cached - self.cache_write)
        weighted = (
            uncached
            + self.cached * _CACHE_READ_WEIGHT
            + self.cache_write * _CACHE_WRITE_WEIGHT
            + self.completion
        )
        return max(0, int(round(weighted)))


def _int(source: Any, name: str) -> int:
    """One non-negative int off an object-or-dict, never raising."""
    value = getattr(source, name, None)
    if value is None and isinstance(source, dict):
        value = source.get(name)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_breakdown(response_obj: Any) -> Usage:
    """Split one LiteLLM response's usage. Absent fields read as zero, so a
    non-Anthropic provider (no cache details) still records totals correctly."""
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return Usage()

    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")

    cached = cache_write = 0
    if details is not None:
        cached = _int(details, "cached_tokens")
        # LiteLLM has carried the write count under two names across versions;
        # take whichever is present rather than silently reading zero forever.
        cache_write = _int(details, "cache_creation_tokens") or _int(details, "cache_write_tokens")

    return Usage(
        total=_int(usage, "total_tokens"),
        prompt=_int(usage, "prompt_tokens"),
        completion=_int(usage, "completion_tokens"),
        cached=cached,
        cache_write=cache_write,
    )


async def record_usage(company_id: str, usage: Usage) -> None:
    """Add one completion's usage to the company's ledger for today (UTC).

    Best-effort: a DB hiccup must never fail an LLM call.
    """
    if not company_id or usage.total <= 0:
        return
    from helixis.db.engine import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO helixis_llm_usage (
                company_id, day, tokens, requests,
                prompt_tokens, completion_tokens,
                cached_tokens, cache_write_tokens, effective_tokens
            )
            VALUES ($1::uuid, $2, $3, 1, $4, $5, $6, $7, $8)
            ON CONFLICT (company_id, day) DO UPDATE SET
                tokens             = helixis_llm_usage.tokens + EXCLUDED.tokens,
                requests           = helixis_llm_usage.requests + 1,
                prompt_tokens      = helixis_llm_usage.prompt_tokens + EXCLUDED.prompt_tokens,
                completion_tokens  = helixis_llm_usage.completion_tokens
                                     + EXCLUDED.completion_tokens,
                cached_tokens      = helixis_llm_usage.cached_tokens + EXCLUDED.cached_tokens,
                cache_write_tokens = helixis_llm_usage.cache_write_tokens
                                     + EXCLUDED.cache_write_tokens,
                effective_tokens   = helixis_llm_usage.effective_tokens
                                     + EXCLUDED.effective_tokens,
                updated_at         = now()
            """,
            company_id,
            dt.datetime.now(dt.UTC).date(),
            usage.total,
            usage.prompt,
            usage.completion,
            usage.cached,
            usage.cache_write,
            usage.effective,
        )


class SpendTracker(CustomLogger):
    """LiteLLM success hook: attribute each completion's tokens to the
    company on the current trace context. Never raises."""

    async def async_log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        try:
            breakdown = _usage_breakdown(response_obj)
            # Per-TURN accounting rides the same callback deliberately: one
            # seam, two horizons. A day total cannot tell one question from
            # another (gotcha 37 — a three-fold cost regression hid for a
            # month behind an average taken on the cheapest day), and two
            # independent parsers of the same usage object would eventually
            # disagree about what a turn spent.
            #
            # Bound only during an agent run, so this is a no-op for the
            # worker's planner/summarizer calls and for boot warmups. It runs
            # BEFORE the company check, because a turn's cost is worth
            # recording even on a path with no company attribution.
            record_turn_call(response_obj, kwargs.get("model"), breakdown)

            ctx = current_trace_context()
            if ctx is None or not ctx.company_id:
                return  # boot warmups / keyless paths — nothing to attribute
            await record_usage(ctx.company_id, breakdown)
        except (Exception, SystemExit):  # noqa: BLE001 — accounting never breaks a call
            logger.debug("spend tracking skipped for one call", exc_info=True)


def configure_spend_tracking() -> None:
    """Register the tracker once per process (web + worker both call this)."""
    global _configured
    if _configured:
        return
    litellm.callbacks = list(getattr(litellm, "callbacks", None) or []) + [SpendTracker()]
    _configured = True
    logger.info("LLM spend tracking enabled (per-company daily ledger)")


async def tokens_used_today(company_id: str) -> int:
    from helixis.db.engine import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT effective_tokens FROM helixis_llm_usage
             WHERE company_id = $1::uuid AND day = $2
            """,
            company_id,
            dt.datetime.now(dt.UTC).date(),
        )
    return int(row["effective_tokens"]) if row else 0


async def budget_exceeded(company_id: str, daily_token_budget: int) -> bool:
    """True when today's usage is at/over budget. ``budget <= 0`` disables.
    FAILS OPEN: a ledger error admits the run rather than locking chat out."""
    if daily_token_budget <= 0:
        return False
    try:
        exceeded = await tokens_used_today(company_id) >= daily_token_budget
    except (Exception, SystemExit):  # noqa: BLE001
        logger.warning("budget check failed — admitting run (fail-open)", exc_info=True)
        return False
    if exceeded:
        record_reason_code("budget_exceeded", detail=f"daily_token_budget={daily_token_budget}")
    return exceeded


__all__ = [
    "SpendTracker",
    "Usage",
    "abandoned_write_count",
    "budget_exceeded",
    "configure_spend_tracking",
    "record_usage",
    "tokens_used_today",
]


async def abandoned_write_count(company_id: str, *, days: int = 30) -> int:
    """How many irreversible writes ended in ``abandoned_unknown`` recently.

    This is the number that decides whether durable execution (DBOS/Temporal)
    is worth building. ``guarded_action`` refuses to refire when it cannot
    prove the side effect did NOT happen, which is the correct posture for a
    payment — but each one is a human review, not a resumed workflow. At zero
    it is a theoretical gap; non-zero and growing, it is an operational cost
    that scales with volume.

    Read-only and best-effort: returns 0 rather than raising, because nothing
    here is worth failing a request over.
    """
    if not company_id:
        return 0
    from helixis.db.engine import get_pool

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS n
                  FROM helixis_action_idempotency
                 WHERE company_id = $1
                   AND status = 'abandoned_unknown'
                   AND updated_at >= now() - ($2::text || ' days')::interval
                """,
                company_id,
                str(days),
            )
        return int(row["n"]) if row else 0
    except (Exception, SystemExit):  # noqa: BLE001 — telemetry never breaks a call
        logger.debug("abandoned-write count unavailable", exc_info=True)
        return 0
