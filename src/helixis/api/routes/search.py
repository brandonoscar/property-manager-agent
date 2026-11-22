"""Public-web search endpoint — powers the Helixis-branded browser results page.

`GET /search?q=…` is a thin, read-only wrapper over the document/web
specialist's tested Tavily core (`web_search_impl`). The Helixis Electron
browser's `helixis://search` page (main process) fetches this and renders the
results under the Helixis logo instead of redirecting to Google.

UNAUTHENTICATED by design: the browser chrome has no Helixis JWT, and the
results page must work logged-out. The underlying Tavily key stays server-side
here — it is never shipped in the desktop app.

Two limits, on two different axes, because one hit costs a Tavily call AND a
grounded-summary LLM call:

* `search_limiter` — per IP, per minute. Bounds ONE caller.
* `search_daily_limiter` — per PROCESS, per day, via `check_global`. Bounds
  total spend, which the per-IP cap never did: an anonymous endpoint has no
  account to meter, so a caller rotating addresses stays under 20/min forever
  while the bill does not. The ceiling is far above any real browser session,
  so it cannot break the Electron client.

Auth remains deliberately absent — adding it would break the logged-out
results page, which is the whole feature.

Summary grounding: Tavily's ``include_answer`` one-liner is NOT trustworthy
enough for the page's authoritative "Summary" box — observed live conflating
"LeadSimple" (a private CRM) with the LEAD stock ticker and inventing a
market cap. This route therefore IGNORES Tavily's answer and generates its
own summary with :func:`_grounded_answer`, prompted to use ONLY the returned
results and to decline (``NO_ANSWER``) when they don't answer the query.
Failure/timeout/decline all degrade to ``answer: null`` — no summary beats a
confident wrong one. (The web *agent* path keeps Tavily's answer as auxiliary
context; its prompt treats it as a hint, never the truth.)
"""

from __future__ import annotations

import logging
from typing import Any

import litellm
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from helixis.agents.web_tools import WebSearchSource, web_search_impl
from helixis.api.rate_limit import search_daily_limiter, search_limiter
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# Hard cap so a crafted ``max_results`` can't fan out a large Tavily call.
_MAX_RESULTS = 10
_DEFAULT_RESULTS = 8

# Grounded-summary budget. The page load already carries Tavily's ~1-2s;
# the summary adds one flash-tier call on top. 6s cap keeps the worst
# case tolerable, and a timeout simply omits the summary box.
_ANSWER_TIMEOUT_SECONDS = 6.0
_ANSWER_MAX_TOKENS = 120
_ANSWER_SNIPPET_CAP = 400
_ANSWER_MAX_CHARS = 500
_NO_ANSWER_SENTINEL = "NO_ANSWER"

_ANSWER_SYSTEM_PROMPT = (
    "You write the short summary shown at the top of a web-search results "
    "page. You are given the user's query and the search results (title, "
    "URL, snippet). Rules:\n"
    "- Use ONLY the provided results. No outside knowledge, no guessing, "
    "no filling gaps.\n"
    "- Never conflate similarly-named entities (e.g. a private company vs. "
    "a stock ticker vs. an ETF vs. a different company with a similar "
    "name). If the results mix entities, summarize only what clearly "
    "matches the query's subject.\n"
    "- 1-2 short sentences, plain text, no markdown, no preamble.\n"
    f"- If the results do not clearly answer the query, reply with exactly "
    f"{_NO_ANSWER_SENTINEL}."
)


class SearchResultItem(BaseModel):
    title: str
    url: str
    snippet: str


class SearchResponse(BaseModel):
    """Trimmed, render-ready shape for the browser results page."""

    query: str
    answer: str | None = None
    results: list[SearchResultItem]
    warnings: list[str]


def _answer_model_name(settings: Settings) -> str:
    """Cheapest configured slot wins — same fallback chain the event
    summarizer uses (``summarizer`` → ``reasoning`` → default)."""
    models = settings.llm.models or {}
    for slot in ("summarizer", "reasoning"):
        block = models.get(slot) or {}
        if block.get("model"):
            return str(block["model"])
    return str(settings.llm.default_model)


async def _grounded_answer(
    query: str,
    sources: list[WebSearchSource],
    settings: Settings,
    *,
    completion_fn: Any | None = None,
) -> str | None:
    """Generate the results-page summary from the results themselves.

    Returns ``None`` (→ no summary box) when there are no sources, the
    model declines with ``NO_ANSWER``, or anything at all goes wrong —
    the search results must render regardless, and an absent summary is
    strictly better than an ungrounded one. ``completion_fn`` is a test
    seam; production uses ``litellm.acompletion``.
    """
    if not sources:
        return None

    lines = []
    for i, s in enumerate(sources, start=1):
        snippet = (s.snippet or "").strip()[:_ANSWER_SNIPPET_CAP]
        lines.append(f"[{i}] {s.title.strip()}\nURL: {s.url}\n{snippet}")
    user_prompt = f"Query: {query}\n\nSearch results:\n\n" + "\n\n".join(lines)

    try:
        model_name = _answer_model_name(settings)
        api_key = _api_key_for_model(settings, model_name)
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            **sampling_kwargs(model_name, 0.1),
            "max_tokens": _ANSWER_MAX_TOKENS,
            "timeout": _ANSWER_TIMEOUT_SECONDS,
        }
        if api_key:
            kwargs["api_key"] = api_key
        fn = completion_fn or litellm.acompletion
        response = await fn(**kwargs)
        content = response.choices[0].message.content
    except (Exception, SystemExit):
        logger.warning("grounded search summary failed — omitting", exc_info=True)
        return None

    if not isinstance(content, str):
        return None
    text = content.strip()
    # Sentinel check is prefix-tolerant: models occasionally wrap the
    # sentinel ("NO_ANSWER.") instead of emitting it bare.
    if not text or _NO_ANSWER_SENTINEL in text[:24]:
        return None
    return text[:_ANSWER_MAX_CHARS]


@router.get("")
async def web_search_endpoint(
    request: Request,
    q: str,
    max_results: int = _DEFAULT_RESULTS,
) -> SearchResponse:
    """Run a public-web search and return render-ready results.

    Read-only. ``web_search_impl`` degrades gracefully (never raises) —
    a missing/blank Tavily key or a provider outage comes back as an
    empty result set with a ``warnings`` entry the page surfaces as
    "couldn't reach search right now", not a 500."""
    # Per-IP first so a single hammering caller gets the honest "slow down"
    # rather than eating the shared budget; then the day's ceiling, which is
    # the only bound that survives a caller rotating addresses (see
    # ``check_global`` — an anonymous endpoint that spends money per hit has
    # no account to meter, so the endpoint itself is the thing with a budget).
    await search_limiter.check_by_ip(request)
    await search_daily_limiter.check_global(request)

    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="q (query) is required")

    capped = max(1, min(max_results, _MAX_RESULTS))
    settings = request.app.state.settings

    result = await web_search_impl(query, capped, "basic", settings=settings)

    rendered_sources = [s for s in result.sources if s.url]

    # Grounded summary — generated from the results we're about to
    # render, NEVER Tavily's ungrounded one-liner (see module docstring).
    answer = await _grounded_answer(query, rendered_sources, settings)

    return SearchResponse(
        query=result.query or query,
        answer=answer,
        results=[
            SearchResultItem(title=s.title, url=s.url, snippet=s.snippet) for s in rendered_sources
        ],
        warnings=list(result.warnings),
    )
