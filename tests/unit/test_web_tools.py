"""Unit tests for the Tavily-backed web_search tool.

Covers:

* No API key configured → ``search_unavailable`` warning, no exception.
* Happy-path payload parses into ``WebSearchResult`` with sources.
* 5xx with one retry → second success returns sources.
* 5xx exhausted → ``search_unavailable`` warning.
* 401 / 403 → ``search_unauthorized`` warning.
* Network/transport error → one retry, then ``search_unavailable``.
* Input validation (empty query, oversized query, bad max_results,
  bad search_depth) → ``invalid_input:*`` warning, never raises.
* Activity-log write: best-effort, no crash when DB pool is absent.

We test ``web_search_impl`` (the underscored entry) because the public
``web_search`` is wrapped by the OpenAI Agents SDK's ``@function_tool``
into a ``FunctionTool`` object that's not directly callable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents import web_tools as wt
from helixis.settings import Settings


def _settings_with_key(key: str = "tvly-fake") -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        web={"tavily_api_key": key, "default_max_results": 5, "request_timeout_seconds": 1.0},
    )


def _settings_no_key() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        web={"tavily_api_key": "", "default_max_results": 5, "request_timeout_seconds": 1.0},
    )


def _mock_post_response(status_code: int, json_body: dict[str, Any] | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = "" if json_body is None else str(json_body)
    response.json = MagicMock(return_value=json_body or {})
    return response


# ── happy paths + degraded paths ────────────────────────────────


@pytest.mark.asyncio
async def test_no_key_returns_search_unavailable() -> None:
    result = await wt.web_search_impl("market data", settings=_settings_no_key())
    assert result.warnings == ["search_unavailable"]
    assert result.sources == []
    assert result.query == "market data"


@pytest.mark.asyncio
async def test_happy_path_parses_sources() -> None:
    payload = {
        "answer": "HVAC service calls in NYC typically run $100-$300.",
        "results": [
            {
                "title": "Cost of Living NYC",
                "url": "https://example.com/hvac-nyc",
                "content": "HVAC service in NYC starts around $100.",
                "score": 0.91,
            },
            {
                "title": "Yelp local",
                "url": "https://example.com/yelp",
                "content": "Most NYC HVAC vendors charge $150-$250.",
            },
        ],
    }
    mock_post = AsyncMock(return_value=_mock_post_response(200, payload))
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("HVAC nyc", settings=_settings_with_key())
    assert result.warnings == []
    assert len(result.sources) == 2
    assert result.sources[0].url == "https://example.com/hvac-nyc"
    assert "HVAC" in (result.answer or "")


@pytest.mark.asyncio
async def test_5xx_retried_then_succeeds() -> None:
    payload = {
        "answer": "ok",
        "results": [{"title": "T", "url": "https://ex.test", "content": "x"}],
    }
    responses = [_mock_post_response(503), _mock_post_response(200, payload)]
    mock_post = AsyncMock(side_effect=responses)
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == []
    assert len(result.sources) == 1
    assert mock_post.await_count == 2


@pytest.mark.asyncio
async def test_5xx_exhausted_returns_unavailable() -> None:
    responses = [_mock_post_response(500), _mock_post_response(502)]
    mock_post = AsyncMock(side_effect=responses)
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unavailable"]
    assert mock_post.await_count == 2


@pytest.mark.asyncio
async def test_401_returns_unauthorized() -> None:
    mock_post = AsyncMock(return_value=_mock_post_response(401))
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unauthorized"]
    assert mock_post.await_count == 1  # no retry on auth failures


@pytest.mark.asyncio
async def test_403_returns_unauthorized() -> None:
    mock_post = AsyncMock(return_value=_mock_post_response(403))
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unauthorized"]


@pytest.mark.asyncio
async def test_transport_error_retried_then_unavailable() -> None:
    mock_post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unavailable"]
    assert mock_post.await_count == 2  # one retry then give up


@pytest.mark.asyncio
async def test_timeout_returns_unavailable() -> None:
    mock_post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unavailable"]


@pytest.mark.asyncio
async def test_non_json_body_returns_unavailable() -> None:
    bad = _mock_post_response(200, None)
    bad.json = MagicMock(side_effect=ValueError("not json"))
    mock_post = AsyncMock(return_value=bad)
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert result.warnings == ["search_unavailable"]


# ── input validation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_query_returns_invalid_input_warning() -> None:
    result = await wt.web_search_impl("", settings=_settings_with_key())
    assert any(w.startswith("invalid_input:") for w in result.warnings)
    assert "non-empty" in result.warnings[0]


@pytest.mark.asyncio
async def test_oversized_query_returns_invalid_input_warning() -> None:
    result = await wt.web_search_impl("x" * 600, settings=_settings_with_key())
    assert any(w.startswith("invalid_input:") for w in result.warnings)
    assert "length" in result.warnings[0]


@pytest.mark.asyncio
async def test_max_results_out_of_range_returns_invalid_input_warning() -> None:
    result = await wt.web_search_impl("q", max_results=99, settings=_settings_with_key())
    assert any("max_results" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_bad_search_depth_returns_invalid_input_warning() -> None:
    result = await wt.web_search_impl(
        "q",
        search_depth="ludicrous",
        settings=_settings_with_key(),  # type: ignore[arg-type]
    )
    assert any("search_depth" in w for w in result.warnings)


# ── activity logging (best-effort) ──────────────────────────────


@pytest.mark.asyncio
async def test_activity_log_swallows_missing_pool() -> None:
    """No DB pool initialized → tool still returns cleanly."""
    payload = {"results": [{"title": "t", "url": "https://x.test", "content": "c"}]}
    mock_post = AsyncMock(return_value=_mock_post_response(200, payload))
    # db_engine.get_pool raises RuntimeError when no pool is initialized;
    # the tool's _log_activity helper catches that path.
    with patch.object(httpx.AsyncClient, "post", mock_post):
        result = await wt.web_search_impl("q", settings=_settings_with_key())
    assert len(result.sources) == 1


@pytest.mark.asyncio
async def test_activity_log_writes_when_context_bound() -> None:
    """With memory context + a pool, write_activity is called once with
    company-scoped fields and a sanitized description."""
    from helixis.memory.context import memory_context

    payload = {"results": [{"title": "t", "url": "https://x.test", "content": "c"}]}
    mock_post = AsyncMock(return_value=_mock_post_response(200, payload))

    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(httpx.AsyncClient, "post", mock_post),
        patch.object(wt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(wt, "write_activity", new=_fake_write),
        memory_context(company_id="co-1", user_id="u-1", session_id="s-1"),
    ):
        await wt.web_search_impl(
            "what is plumbing rate near Task #12345 and Tenant 9001",
            settings=_settings_with_key(),
        )

    entry = write_called["entry"]
    assert entry.source == "chat"
    assert entry.action_type == "web_search"
    assert entry.company_id == "co-1"
    assert entry.user_id == "u-1"
    # Identity rule: sanitized description must redact the ID-shaped
    # substrings even though the LLM put them in the query.
    assert "Task #12345" not in entry.description
    assert "Tenant 9001" not in entry.description
    assert "[id-redacted]" in entry.description
    assert entry.raw["sources_count"] == 1
    assert entry.raw["provider"] == "tavily"
