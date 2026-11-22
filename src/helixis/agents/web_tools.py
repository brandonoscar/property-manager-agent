"""Public-web research tool — Tavily-backed.

The orchestrator's web specialist (:mod:`helixis.agents.web_agent`)
hands questions here that require information no in-domain specialist
covers — typical service-call cost, vendor reputation, local
regulation, weather alerts, public records.

Design rules (from the capability series Prompt 1):

* Single tool: ``web_search``.
* Graceful degradation, never raise back into the agent loop. Missing
  API key, transport errors, 5xx after one retry, and 4xx auth failures
  all yield a :class:`WebSearchResult` with empty sources and a
  ``warnings`` entry the specialist's prompt teaches it to surface.
* Validate inputs at the boundary so a hallucinated argument
  surfaces as ``ValueError`` before we burn a network call.
* Activity-log every successful invocation so the Activity page can
  audit "what did Helixis go look up on the web?" — best-effort, never
  blocks the tool's return.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from agents import function_tool
from pydantic import BaseModel, Field

from helixis.db import engine as db_engine
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.memory.context import current_memory_context
from helixis.security.pii import redact
from helixis.security.prompt_delimit import wrap_untrusted
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)


_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_RETRY_BACKOFF_SECONDS = 0.5
_MAX_QUERY_LENGTH = 512
_MIN_RESULTS = 1
_MAX_RESULTS = 10
_PROVIDER = "tavily"


SearchDepth = Literal["basic", "advanced"]


class WebSearchSource(BaseModel):
    """One hit returned by the search provider."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    score: float | None = None
    published_date: str | None = None


class WebSearchResult(BaseModel):
    """Structured envelope every ``web_search`` invocation returns.

    ``warnings`` is the agent-visible degradation channel — when it
    contains ``"search_unavailable"`` or ``"search_unauthorized"``,
    the web_agent prompt teaches the model to say so plainly instead
    of inventing facts.
    """

    answer: str | None = None
    sources: list[WebSearchSource] = Field(default_factory=list)
    query: str = ""
    search_depth: SearchDepth = "basic"
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str = _PROVIDER
    warnings: list[str] = Field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────


def _sanitize_query_for_log(query: str) -> str:
    """Strip ID-shaped substrings before persisting a query.

    The identity rule applies everywhere — even when the LLM's query
    string accidentally echoed a Buildium ID from earlier in the
    conversation, the activity log should not store it. This is
    defense-in-depth: the prompt tells the model not to do it, the
    sanitizer guarantees it.
    """
    return redact(query)[:200]


def _validate_inputs(query: str, max_results: int, search_depth: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if len(query) > _MAX_QUERY_LENGTH:
        raise ValueError(f"query length {len(query)} exceeds {_MAX_QUERY_LENGTH}")
    if not isinstance(max_results, int) or not (_MIN_RESULTS <= max_results <= _MAX_RESULTS):
        raise ValueError(
            f"max_results must be an int in [{_MIN_RESULTS},{_MAX_RESULTS}], got {max_results!r}"
        )
    if search_depth not in ("basic", "advanced"):
        raise ValueError(f"search_depth must be 'basic' or 'advanced', got {search_depth!r}")


def _unavailable(query: str, search_depth: SearchDepth, warning: str) -> WebSearchResult:
    return WebSearchResult(
        query=query,
        search_depth=search_depth,
        warnings=[warning],
    )


def _parse_tavily_payload(
    payload: dict[str, Any], query: str, search_depth: SearchDepth
) -> WebSearchResult:
    sources: list[WebSearchSource] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        # The snippet/content is arbitrary text pulled from a public web page —
        # an untrusted party's content that can carry injection. Fence it as data
        # so the reasoning loop can't be steered by it mid-run (LED-018). Empty
        # snippets stay unfenced (no content = no spurious fence).
        raw_snippet = str(raw.get("content") or raw.get("snippet") or "")
        snippet = wrap_untrusted(raw_snippet, label="web search results") if raw_snippet else ""
        sources.append(
            WebSearchSource(
                title=str(raw.get("title") or ""),
                url=str(raw.get("url") or ""),
                snippet=snippet,
                score=raw.get("score") if isinstance(raw.get("score"), (int, float)) else None,
                published_date=(str(raw["published_date"]) if raw.get("published_date") else None),
            )
        )
    answer = payload.get("answer")
    # Tavily's synthesized answer is derived from the same untrusted pages — fence
    # it too. A None/blank answer stays unfenced.
    answer_str = str(answer) if isinstance(answer, str) and answer.strip() else None
    return WebSearchResult(
        answer=wrap_untrusted(answer_str, label="web search results") if answer_str else None,
        sources=sources,
        query=query,
        search_depth=search_depth,
    )


async def _call_tavily(
    *,
    api_key: str,
    query: str,
    max_results: int,
    search_depth: SearchDepth,
    timeout_seconds: float,
) -> WebSearchResult:
    """Single Tavily request with one retry on 5xx. Maps every
    network/HTTP failure mode to a structured :class:`WebSearchResult`."""
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        # Tavily's ``include_answer`` returns a synthesized one-line
        # answer alongside the source snippets. The web_agent prompt
        # treats it as an additional context source, not as the truth.
        "include_answer": True,
    }
    timeout = httpx.Timeout(timeout_seconds)
    last_5xx: int | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in (0, 1):
            try:
                response = await client.post(_TAVILY_ENDPOINT, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "tavily request failed (%s) attempt=%d: %s",
                    type(exc).__name__,
                    attempt,
                    exc,
                )
                if attempt == 0:
                    continue
                return _unavailable(query, search_depth, "search_unavailable")

            status = response.status_code
            if status >= 500:
                last_5xx = status
                logger.warning("tavily 5xx status=%d attempt=%d", status, attempt)
                if attempt == 0:
                    continue
                return _unavailable(query, search_depth, "search_unavailable")
            if status in (401, 403):
                logger.warning("tavily auth failure status=%d", status)
                return _unavailable(query, search_depth, "search_unauthorized")
            if status >= 400:
                logger.warning("tavily 4xx status=%d body=%s", status, response.text[:200])
                return _unavailable(query, search_depth, "search_unavailable")

            try:
                payload = response.json()
            except ValueError as exc:
                logger.warning("tavily returned non-JSON body: %s", exc)
                return _unavailable(query, search_depth, "search_unavailable")
            return _parse_tavily_payload(payload, query, search_depth)

    # Defensive — the loop either returned or fell through after both
    # attempts hit 5xx. last_5xx informs the log line but the
    # structured result is the same shape we already returned above.
    logger.warning("tavily exhausted retries (last_5xx=%s)", last_5xx)
    return _unavailable(query, search_depth, "search_unavailable")


async def _log_activity(result: WebSearchResult, query: str) -> None:
    """Best-effort activity-log write. Pool missing / DB failure /
    no memory context bound → silent no-op."""
    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    user_id = ctx.user_id if ctx else None
    if not company_id:
        # No tenant binding — refuse to write a row we can't scope.
        return

    description = _sanitize_query_for_log(query)
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="chat",
        action_type="web_search",
        description=description,
        user_id=user_id or None,
        raw={
            "query": description,
            "max_results": len(result.sources),
            "search_depth": result.search_depth,
            "sources_count": len(result.sources),
            "warnings": list(result.warnings),
            "provider": result.provider,
            "retrieved_at": result.retrieved_at.isoformat(),
        },
    )
    await write_activity(pool, entry)


# ── implementation (testable, no SDK wrapper) ────────────────────


async def web_search_impl(
    query: str,
    max_results: int = 5,
    search_depth: SearchDepth = "basic",
    *,
    settings: Settings | None = None,
) -> WebSearchResult:
    """Run a public-web search via Tavily. Returns a structured
    :class:`WebSearchResult` for every outcome — graceful degradation
    rules out raising back into the agent loop, including for input
    validation errors. ``warnings`` carries the failure signal.

    ``settings`` is injected from production; tests pass a custom
    ``Settings`` with their own Tavily key. Activity-log writes are
    best-effort and silently skip when no DB pool / memory context
    is bound.
    """
    safe_depth: SearchDepth = search_depth if search_depth in ("basic", "advanced") else "basic"
    try:
        _validate_inputs(query, max_results, search_depth)
    except ValueError as exc:
        result = WebSearchResult(
            query=str(query) if isinstance(query, str) else "",
            search_depth=safe_depth,
            warnings=[f"invalid_input: {exc}"],
        )
        # Skip the activity log on validation errors — nothing
        # interesting happened on the network side.
        return result

    cfg = (settings or get_settings()).web

    if not cfg.tavily_api_key:
        logger.info("web_search called with no Tavily API key configured")
        result = _unavailable(query, search_depth, "search_unavailable")
    else:
        result = await _call_tavily(
            api_key=cfg.tavily_api_key,
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            timeout_seconds=cfg.request_timeout_seconds,
        )

    await _log_activity(result, query)
    return result


@function_tool
async def web_search(
    query: str,
    max_results: int = 5,
    search_depth: SearchDepth = "basic",
) -> str:
    """Search the public web for information no in-domain specialist
    can answer — market rates, vendor reputation, public regulation,
    weather alerts, general background facts.

    Args:
        query: Search query, 1-512 chars. Plain English, not a URL.
        max_results: How many sources to retrieve, 1-10. Default 5.
        search_depth: 'basic' (fast) or 'advanced' (deeper crawl).
            Default 'basic'.

    Returns:
        A JSON-encoded :class:`WebSearchResult` — the agent can
        cite the contained ``sources[].url`` values. When
        ``warnings`` contains ``search_unavailable``,
        ``search_unauthorized``, or ``invalid_input:*``, the model
        must tell the user plainly rather than invent an answer.
    """
    result = await web_search_impl(query, max_results, search_depth)
    return result.model_dump_json()


__all__ = [
    "SearchDepth",
    "WebSearchResult",
    "WebSearchSource",
    "web_search",
    "web_search_impl",
]
