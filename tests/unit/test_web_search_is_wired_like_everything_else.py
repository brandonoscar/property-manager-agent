"""Web search had the capability and none of the scaffolding around it.

Every other dependency in this product carries four things: a daily canary so
silent degradation gets noticed, a run budget so an identical call is not
bought twice, the shared fair-housing rule wherever a model writes prose, and a
bound on what an anonymous caller can spend. Public-web search — live on two
surfaces, the ``web_agent`` specialist and the Electron browser's results
page — carried none of them.

⚠ IT WAS NOT BROKEN, and that is the point. Measured against the live activity
log on 2026-09-04: 17 web searches in production history, 16 clean with
sources, 1 ``search_unavailable``, 0 unauthorized, the most recent eight all
returning sources. The Tavily key works. What was missing is every way of
finding out when it stops — ``web_search_impl`` never raises by design, so a
revoked key becomes "public-web context is temporarily unavailable" and nobody
is told. Same silent-degradation class as the embedder 403 that ran ~19 days
undetected (gotcha 17).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from helixis.agents.call_budget import (
    PER_RECORD_CAPS,
    CallLedger,
    _is_budgeted,
    bind_call_ledger,
    reset_call_ledger,
)
from helixis.agents.web_agent import create_web_agent
from helixis.agents.web_tools import WebSearchResult, WebSearchSource
from helixis.settings import Settings

_WEB_AGENT_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "src/helixis/agents/web_agent.py"
).read_text(encoding="utf-8")
_SEARCH_ROUTE_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes/search.py"
).read_text(encoding="utf-8")


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


# ── the budget ─────────────────────────────────────────────────────────────


def test_web_search_is_a_budgeted_read() -> None:
    """A billed provider call that the ledger cannot see gets no memo and no
    out-of-time refusal. Every other read family has both."""
    assert _is_budgeted("web_search")


def test_web_search_has_no_per_record_cap_on_purpose() -> None:
    """⚠ A cap counts DISTINCT calls, and distinct web queries are the tool
    working — triangulating three phrasings is research, not a fan-out loop.
    What was missing is the memo and the clock, not a ceiling. Pinned so a
    later reader does not "finish the job" by adding one."""
    assert "web_search" not in PER_RECORD_CAPS


def test_the_web_agent_actually_wraps_the_tool_in_the_budget(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ THE ONE THAT MATTERS, and adding the name to the registry did not
    achieve it.

    ``_is_budgeted`` only ever runs on a tool that went through
    ``budget_tool``, and that wrapping lived in ``buildium_agent`` and the
    Composio client — never here. So the registry entry alone was inert, which
    is gotcha 66 exactly: a structural fix bound in one place and absent from
    the one that needed it, with a green registry test either way.

    Behavioural, therefore, not a source grep: bind a ledger, call the agent's
    tool twice with identical arguments, and require the provider to be hit
    once.
    """
    calls: list[str] = []

    async def _fake_impl(query, max_results=5, search_depth="basic", **_kw):  # noqa: ANN001
        calls.append(query)
        return WebSearchResult(
            query=query,
            sources=[WebSearchSource(title="T", url="https://example.com", snippet="s")],
        )

    monkeypatch.setattr("helixis.agents.web_tools.web_search_impl", _fake_impl)

    agent = create_web_agent(settings)
    tool = next(t for t in agent.tools if getattr(t, "name", "") == "web_search")

    # ⚠ A real ToolContext, not None. The SDK's FunctionTool wrapper reads
    # ``ctx.tool_name`` and then ``ctx.run_config`` on its error path, so a
    # None context fails inside the SDK's own error handling — which surfaces
    # as an AttributeError about ``run_config`` and reads nothing like "the
    # budget is missing". The existing budget tests pass None because they use
    # a hand-rolled fake tool; this one drives the REAL decorated tool, which
    # is the whole point.
    from agents.tool_context import ToolContext

    args = '{"query": "typical HVAC service call cost"}'
    ctx = ToolContext(context=None, tool_name="web_search", tool_call_id="t1", tool_arguments=args)

    token = bind_call_ledger(CallLedger())
    try:
        import asyncio

        first = asyncio.run(tool.on_invoke_tool(ctx, args))
        second = asyncio.run(tool.on_invoke_tool(ctx, args))
    finally:
        reset_call_ledger(token)

    assert len(calls) == 1, (
        f"the identical query reached the provider {len(calls)} times — the web "
        "specialist's tool is not going through budget_tool, so the ledger "
        "never sees it (gotcha 66)"
    )
    assert first
    assert "cache" in second.lower(), (
        "the second call did not come back as a memo hit, so nothing was cached"
    )


def test_the_card_tool_is_deliberately_left_unbudgeted() -> None:
    """``show_table`` is not a read, and ``budget_tool``'s unbudgeted branch is
    the WRITE branch — it calls ``ledger.invalidate()``. Wrapping a card
    renderer would flush every memoized read each time a table was drawn."""
    assert not _is_budgeted("show_table")
    tree = ast.parse(_WEB_AGENT_SRC)
    wrapped: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "budget_tool"
            and node.args
        ):
            wrapped.append(ast.unparse(node.args[0]))
    assert wrapped == ["web_search"], (
        f"budget_tool is applied to {wrapped} — it must wrap web_search and "
        "nothing else in this module"
    )


# ── the fair-housing block ─────────────────────────────────────────────────


def test_the_web_specialist_carries_the_shared_fair_housing_block(
    settings: Settings,
) -> None:
    """⚠ The specialist most exposed to this was the one surface without it.

    Its own rule 9 forbids substituting a proxy and names no PERMITTED
    alternative — gotcha 52's finding verbatim: a prohibition with no safe
    substitute is an instruction to improvise, and "be helpful" reaches for the
    nearest adjacent fact, which for a web researcher is crime statistics. The
    orchestrator, buildium_agent, crm/draft and renewals/draft all got the
    shared block; web_agent is the only one of the five holding a tool that can
    actually go and fetch the proxy.
    """
    instructions = create_web_agent(settings).instructions
    assert "Declining and then OFFERING a proxy is the same violation" in instructions
    assert "What you MAY offer instead" in instructions, (
        "the permitted-substitute half is the half that fixes the behaviour — a "
        "prohibition alone is what produced the violation in the first place"
    )


def test_the_specialists_own_proxy_rule_is_still_there_too() -> None:
    """Both halves. The shared block is general fair-housing guidance; rule 9
    is the SEARCH-specific stop ("do NOT search for it"), which the block does
    not say because no other surface can search. Losing either is a regression.
    """
    assert "Do NOT search for it" in _WEB_AGENT_SRC


# ── the daily canary ───────────────────────────────────────────────────────


async def _run_check(settings: Settings):
    from helixis.smoke.checks import check_web_search

    return await check_web_search(settings)


def _settings_with_key(key: str) -> Settings:
    return Settings(env="test", memory={"enabled": False}, web={"tavily_api_key": key})


@pytest.mark.asyncio
async def test_a_blank_key_fails_rather_than_skips() -> None:
    """⚠ Unlike ``check_twilio``, which skips. Outbound SMS is dark on purpose
    until carriers approve it; web search is a LIVE capability on two surfaces,
    so a blank key is not an unconfigured feature — it is every user's web
    search silently returning nothing."""
    result = await _run_check(_settings_with_key(""))
    assert result.failed
    assert "TAVILY" in result.detail


@pytest.mark.asyncio
async def test_a_revoked_key_fails_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unauthorized(*_a, **_kw):
        return WebSearchResult(query="q", warnings=["search_unauthorized"])

    monkeypatch.setattr("helixis.agents.web_tools.web_search_impl", _unauthorized)
    result = await _run_check(_settings_with_key("k"))
    assert result.failed
    assert "key" in result.detail.lower()


@pytest.mark.asyncio
async def test_authenticated_but_empty_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ Assert on SOURCES, not on the absence of a warning. A key that
    authenticates and returns nothing — quota exhausted, or a response-shape
    change ``_parse_tavily_payload`` no longer reads — is the failure that
    matters, and a warnings-only check passes it happily."""

    async def _empty(*_a, **_kw):
        return WebSearchResult(query="q", sources=[])

    monkeypatch.setattr("helixis.agents.web_tools.web_search_impl", _empty)
    result = await _run_check(_settings_with_key("k"))
    assert result.failed
    assert "0 usable sources" in result.detail


@pytest.mark.asyncio
async def test_a_healthy_provider_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(*_a, **_kw):
        return WebSearchResult(
            query="q",
            sources=[WebSearchSource(title="T", url="https://example.com", snippet="s")],
        )

    monkeypatch.setattr("helixis.agents.web_tools.web_search_impl", _ok)
    result = await _run_check(_settings_with_key("k"))
    assert result.passed


@pytest.mark.asyncio
async def test_a_url_less_source_does_not_count_as_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route drops url-less sources before rendering, so a result made
    entirely of them is an empty results page. The canary must agree with what
    the user sees, or it goes green on a blank page."""

    async def _no_urls(*_a, **_kw):
        return WebSearchResult(query="q", sources=[WebSearchSource(title="T", url="")])

    monkeypatch.setattr("helixis.agents.web_tools.web_search_impl", _no_urls)
    result = await _run_check(_settings_with_key("k"))
    assert result.failed


def test_the_check_is_registered_in_the_runner() -> None:
    """A check nothing calls is the ``comms_triage`` failure (gotcha 62) — the
    canary exists and never sings."""
    runner = (
        pathlib.Path(__file__).resolve().parents[2] / "src/helixis/smoke/runner.py"
    ).read_text(encoding="utf-8")
    assert "check_web_search" in runner


# ── the spend bound ────────────────────────────────────────────────────────


def test_the_route_checks_the_daily_ceiling_not_only_the_per_ip_one() -> None:
    """⚠ Call-site guard, because a limiter nothing calls bounds nothing.

    The per-IP cap bounds ONE caller. This endpoint is anonymous by design and
    buys a Tavily call plus an LLM call on every hit, so there is no account to
    meter and a caller rotating addresses stays under 20/min forever while the
    bill does not.
    """
    tree = ast.parse(_SEARCH_ROUTE_SRC)
    awaited = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "search_limiter.check_by_ip" in awaited
    assert "search_daily_limiter.check_global" in awaited, (
        "the day's ceiling is declared but the route never asks it — the "
        "endpoint is still unbounded"
    )


def test_the_global_check_ignores_who_is_calling() -> None:
    """Keying on a constant is the whole point: two different addresses must
    share one budget, or it is just the per-IP limiter again under a new name.
    """
    import asyncio

    from helixis.api.rate_limit import RateLimiter

    limiter = RateLimiter(requests=2, window_seconds=3600)

    def _req(ip: str):
        class _R:
            headers = {"X-Forwarded-For": ip}
            client = None
            state = type("S", (), {})()

        return _R()

    asyncio.run(limiter.check_global(_req("1.1.1.1")))
    asyncio.run(limiter.check_global(_req("2.2.2.2")))
    with pytest.raises(Exception) as exc:
        asyncio.run(limiter.check_global(_req("3.3.3.3")))
    assert "429" in str(exc.value) or "Rate limit" in str(exc.value), (
        "a third distinct address was served — the budget is being keyed per "
        "caller, so rotating addresses still buys unlimited provider calls"
    )


def test_the_ceiling_is_far_above_a_real_browser_session() -> None:
    """It must bound abuse without ever touching the Electron client. A person
    searching all day is nowhere near this; the number exists so the worst case
    is a figure rather than an open question."""
    from helixis.api.rate_limit import search_daily_limiter, search_limiter

    assert search_daily_limiter.window_seconds == 86_400
    assert search_daily_limiter.requests >= 500
    assert search_daily_limiter.requests > search_limiter.requests
