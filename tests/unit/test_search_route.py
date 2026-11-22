"""GET /search — the unauthenticated, IP-rate-limited web search endpoint.

Powers the Helixis-branded browser results page. Thin wrapper over the tested
Tavily core (`web_search_impl`), which is mocked here so no live call happens:

  - happy path → trimmed render-ready shape; url-less sources dropped
  - blank query → 422, core never called
  - degraded provider (warnings, no sources) → 200 with warnings (NOT a 500)
  - summary box: grounded by OUR LLM call over the returned results —
    Tavily's ungrounded `answer` is ignored (it conflated "LeadSimple"
    with the LEAD stock ticker in production); LLM failure / NO_ANSWER
    → `answer: null`, never a 500.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from helixis.agents.web_tools import WebSearchResult, WebSearchSource
from helixis.main import create_app
from helixis.settings import Settings


@pytest.fixture
def test_settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


@pytest.fixture
def client(test_settings: Settings):
    from helixis.api.rate_limit import search_daily_limiter, search_limiter

    # BOTH limiters. They are module singletons, so a leftover count from an
    # earlier test is state carried into this one — and the daily limiter's
    # window is 24h, so unlike the per-minute one it never ages out mid-suite.
    search_limiter.counter._log.clear()
    search_daily_limiter.counter._log.clear()

    with patch("helixis.main.get_settings", return_value=test_settings):
        app = create_app(test_settings)
        with TestClient(app) as c:
            yield c


def test_search_happy_path(client: TestClient) -> None:
    result = WebSearchResult(
        # Tavily's own answer must be IGNORED by the route — the
        # rendered summary comes from _grounded_answer below.
        answer="Tavily hallucination that must not surface.",
        sources=[
            WebSearchSource(title="Zillow", url="https://zillow.com", snippet="Homes."),
            # url-less hit must be dropped from the rendered list
            WebSearchSource(title="No URL", url="", snippet="ignored"),
        ],
        query="zillow",
    )
    impl = AsyncMock(return_value=result)
    grounded = AsyncMock(return_value="Zillow is a real-estate marketplace.")
    with (
        patch("helixis.api.routes.search.web_search_impl", new=impl),
        patch("helixis.api.routes.search._grounded_answer", new=grounded),
    ):
        resp = client.get("/api/v1/search", params={"q": "zillow"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "zillow"
    assert data["answer"] == "Zillow is a real-estate marketplace."
    assert len(data["results"]) == 1
    assert data["results"][0]["url"] == "https://zillow.com"
    impl.assert_awaited_once()
    # The grounded summarizer sees only the url-bearing sources.
    grounded.assert_awaited_once()
    assert [s.title for s in grounded.await_args.args[1]] == ["Zillow"]


def test_search_blank_query_422(client: TestClient) -> None:
    impl = AsyncMock()
    with patch("helixis.api.routes.search.web_search_impl", new=impl):
        resp = client.get("/api/v1/search", params={"q": "   "})
    assert resp.status_code == 422
    impl.assert_not_awaited()  # never reached the provider


def test_search_degraded_provider_is_200_with_warnings(client: TestClient) -> None:
    # Missing key / outage → web_search_impl returns empty + a warning,
    # never raises. The endpoint must pass that through, not 500.
    result = WebSearchResult(query="zillow", warnings=["search_unavailable"])
    impl = AsyncMock(return_value=result)
    with patch("helixis.api.routes.search.web_search_impl", new=impl):
        resp = client.get("/api/v1/search", params={"q": "zillow"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert "search_unavailable" in data["warnings"]


def test_search_caps_max_results(client: TestClient) -> None:
    impl = AsyncMock(return_value=WebSearchResult(query="x"))
    with patch("helixis.api.routes.search.web_search_impl", new=impl):
        resp = client.get("/api/v1/search", params={"q": "x", "max_results": "999"})
    assert resp.status_code == 200
    # Second positional arg to web_search_impl is the capped count (≤ 10).
    assert impl.await_args.args[1] == 10


# ── _grounded_answer unit tests ─────────────────────────────────────


def _fake_completion(content: str | None) -> AsyncMock:
    """Build a completion_fn returning a litellm-shaped response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return AsyncMock(return_value=response)


_SOURCES = [
    WebSearchSource(
        title="Property Management CRM | LeadSimple",
        url="https://leadsimple.com",
        snippet="Automate and scale operations with LeadSimple.",
    ),
    WebSearchSource(
        title="LeadSimple 2026 Company Profile | PitchBook",
        url="https://pitchbook.com/profiles/leadsimple",
        snippet="Valuation, funding, cap tables, and investors for LeadSimple.",
    ),
]


@pytest.mark.anyio
async def test_grounded_answer_returns_model_text(test_settings: Settings) -> None:
    from helixis.api.routes.search import _grounded_answer

    fn = _fake_completion("LeadSimple is a private property-management CRM.")
    answer = await _grounded_answer(
        "leadsimple market cap", _SOURCES, test_settings, completion_fn=fn
    )
    assert answer == "LeadSimple is a private property-management CRM."
    # The prompt must carry the results — that's the grounding.
    user_msg = fn.await_args.kwargs["messages"][1]["content"]
    assert "pitchbook.com" in user_msg
    assert "leadsimple market cap" in user_msg


@pytest.mark.anyio
async def test_grounded_answer_no_answer_sentinel_yields_none(test_settings: Settings) -> None:
    from helixis.api.routes.search import _grounded_answer

    for raw in ("NO_ANSWER", "NO_ANSWER.", "  NO_ANSWER — results inconclusive"):
        fn = _fake_completion(raw)
        assert await _grounded_answer("q", _SOURCES, test_settings, completion_fn=fn) is None


@pytest.mark.anyio
async def test_grounded_answer_llm_failure_yields_none(test_settings: Settings) -> None:
    from helixis.api.routes.search import _grounded_answer

    fn = AsyncMock(side_effect=RuntimeError("provider down"))
    assert await _grounded_answer("q", _SOURCES, test_settings, completion_fn=fn) is None


@pytest.mark.anyio
async def test_grounded_answer_systemexit_yields_none(test_settings: Settings) -> None:
    # Gotcha 7 class — a library sys.exit must not kill the request.
    from helixis.api.routes.search import _grounded_answer

    fn = AsyncMock(side_effect=SystemExit(1))
    assert await _grounded_answer("q", _SOURCES, test_settings, completion_fn=fn) is None


@pytest.mark.anyio
async def test_grounded_answer_empty_sources_skips_llm(test_settings: Settings) -> None:
    from helixis.api.routes.search import _grounded_answer

    fn = _fake_completion("should never be called")
    assert await _grounded_answer("q", [], test_settings, completion_fn=fn) is None
    fn.assert_not_awaited()


def test_search_answer_null_when_grounding_declines(client: TestClient) -> None:
    """End-to-end through the route: grounding declines → answer null,
    results still render, still a 200."""
    result = WebSearchResult(
        answer="Tavily's confident nonsense",
        sources=[WebSearchSource(title="T", url="https://t.example", snippet="s")],
        query="q",
    )
    with (
        patch("helixis.api.routes.search.web_search_impl", new=AsyncMock(return_value=result)),
        patch(
            "helixis.api.routes.search._grounded_answer",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = client.get("/api/v1/search", params={"q": "q"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] is None
    assert len(data["results"]) == 1
