"""Tests for the Composio client wrapper."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import helixis.connectors.composio_client as cc_mod
from helixis.connectors.composio_client import ComposioClient
from helixis.settings import Settings


def _client() -> ComposioClient:
    """Build a Composio client with a stubbed SDK so we can assert on
    the underlying ``tools.get`` call without touching the network."""
    settings = Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
        composio={"api_key": "test-key"},
    )
    client = ComposioClient(settings)
    client._initialized = True
    client._client = MagicMock()
    client._client.tools.get.return_value = []
    return client


def test_get_tools_requests_high_limit_so_list_variants_are_included():
    """Composio's API defaults to ~20 tools/page when ``limit`` is
    omitted, which dropped LIST_EVENTS / FIND_EVENT from the calendar
    toolkit and produced the "I can create but not list events"
    failure. We must explicitly request enough tools to cover every
    variant in a single page."""
    client = _client()
    client.get_tools(toolkits=["GOOGLECALENDAR"], user_id="company-1")
    kwargs = client._client.tools.get.call_args.kwargs
    assert kwargs["toolkits"] == ["GOOGLECALENDAR"]
    assert kwargs["user_id"] == "company-1"
    assert kwargs["limit"] >= 100  # comfortably above any toolkit's real count


def test_get_tools_falls_back_when_sdk_rejects_limit():
    """Older Composio SDKs didn't accept ``limit`` on .get(). The
    wrapper must retry without it so the request still succeeds."""
    client = _client()
    # First call raises TypeError (older SDK), second call succeeds.
    client._client.tools.get.side_effect = [TypeError("unexpected kwarg 'limit'"), []]
    result = client.get_tools(toolkits=["GMAIL"], user_id="company-1")
    assert result == []
    # Two calls: the first with limit, the fallback without it.
    assert client._client.tools.get.call_count == 2
    fallback_kwargs = client._client.tools.get.call_args_list[1].kwargs
    assert "limit" not in fallback_kwargs


# ── Per-user tool cache (stops the per-turn re-fetch that froze the loop) ──


def test_get_tools_cached_between_turns_no_refetch():
    """The second turn for the same (user, toolkit) is served from cache —
    the whole point: a chat turn must not re-fetch ~200 tool schemas."""
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert client._client.tools.get.call_count == 1


def test_tool_cache_keyed_per_user_and_toolkit():
    """Different user or different toolkit is a distinct cache entry."""
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    client.get_tools(toolkits=["GMAIL"], user_id="u2")  # different user
    client.get_tools(toolkits=["GOOGLEDRIVE"], user_id="u1")  # different toolkit
    assert client._client.tools.get.call_count == 3


def test_invalidate_user_forces_refetch():
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    client.invalidate_user("u1")
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert client._client.tools.get.call_count == 2


def test_expired_cache_entry_refetches():
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    key = ("u1", ("GMAIL",))
    _, cached = client._tools_cache[key]
    client._tools_cache[key] = (time.monotonic() - 1.0, cached)  # force-expire
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert client._client.tools.get.call_count == 2


def test_nonempty_tools_get_long_ttl(monkeypatch):
    """A non-empty list is stable → long TTL; empty lists get the short one."""
    client = _client()
    sentinel = [object(), object()]
    client._client.tools.get.return_value = sentinel
    monkeypatch.setattr(cc_mod, "gate_tools", lambda t: list(t))
    out = client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert out == sentinel
    expiry, cached = client._tools_cache[("u1", ("GMAIL",))]
    assert cached == sentinel
    assert expiry - time.monotonic() > cc_mod._EMPTY_TOOLS_CACHE_TTL


def test_initiate_connection_busts_tool_cache():
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert ("u1", ("GMAIL",)) in client._tools_cache
    client.initiate_connection(user_id="u1", toolkit="GMAIL")
    assert ("u1", ("GMAIL",)) not in client._tools_cache


def test_disconnect_busts_tool_cache():
    client = _client()
    client.get_tools(toolkits=["GMAIL"], user_id="u1")
    assert ("u1", ("GMAIL",)) in client._tools_cache
    # No active connections → disconnect is an idempotent success, but it must
    # still have cleared the cached tool surface for the user.
    client._client.connected_accounts.list.return_value = MagicMock(items=[])
    assert client.disconnect(user_id="u1", toolkit="GMAIL") is True
    assert ("u1", ("GMAIL",)) not in client._tools_cache


# ── execute_tool (email-ingest poll + reply path) ────────────────────


def test_execute_tool_passes_slug_user_and_arguments():
    client = _client()
    client._client.tools.execute.return_value = {"messages": []}
    out = client.execute_tool(user_id="u1", slug="GMAIL_FETCH_EMAILS", arguments={"max_results": 5})
    assert out == {"messages": []}
    args, kwargs = client._client.tools.execute.call_args
    assert args[0] == "GMAIL_FETCH_EMAILS"
    assert kwargs["user_id"] == "u1"
    assert kwargs["arguments"] == {"max_results": 5}
    # SDK 0.13.1 requires this for manual execution or it raises
    # ToolVersionRequiredError → silent None.
    assert kwargs["dangerously_skip_version_check"] is True


def test_execute_tool_falls_back_when_sdk_lacks_version_kwarg():
    """An older SDK that doesn't accept dangerously_skip_version_check
    raises TypeError on the first call; the wrapper retries without it."""
    client = _client()
    client._client.tools.execute.side_effect = [
        TypeError("unexpected kwarg 'dangerously_skip_version_check'"),
        {"ok": True},
    ]
    out = client.execute_tool(user_id="u1", slug="X", arguments={})
    assert out == {"ok": True}
    assert client._client.tools.execute.call_count == 2
    fallback_kwargs = client._client.tools.execute.call_args_list[1].kwargs
    assert "dangerously_skip_version_check" not in fallback_kwargs


def test_execute_tool_normalises_object_result_via_data():
    client = _client()
    result_obj = MagicMock(spec=["data"])
    result_obj.data = {"ok": True}
    client._client.tools.execute.return_value = result_obj
    assert client.execute_tool(user_id="u1", slug="X", arguments={}) == {"ok": True}


def test_execute_tool_returns_none_when_unavailable():
    client = _client()
    client._initialized = False
    assert client.execute_tool(user_id="u1", slug="X", arguments={}) is None


def test_execute_tool_returns_none_on_sdk_error():
    client = _client()
    client._client.tools.execute.side_effect = RuntimeError("boom")
    assert client.execute_tool(user_id="u1", slug="X", arguments={}) is None
