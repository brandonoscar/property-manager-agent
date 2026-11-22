"""Unit tests for the page-context routing-harness additions (#312).

Covers the two deterministic pieces added to ``orchestrator_routing`` so the
04x page-context cases (041 / 047) can run without a live model:

* ``_with_context_block`` renders ``AgentRequest.context`` into the prompt
  exactly as ``run_agent_streaming`` does.
* ``handoff_trail_must_not_contain`` fails (critically) when a forbidden agent
  appears anywhere in the handoff trail — stronger than ``must_not_route_to``,
  which only checks the terminal agent.
"""

from __future__ import annotations

from helixis.evals.orchestrator_routing import (
    RoutingRunResult,
    _check_routing_assertions,
    _with_context_block,
)
from helixis.security.prompt_delimit import wrap_untrusted


def test_with_context_block_matches_streaming_format() -> None:
    out = _with_context_block(
        "what's on my screen?",
        {"current_page_url": "https://x.test/lease", "current_page_title": "Lease 4B"},
    )
    # Page context is fenced as untrusted data (LED-009). Build the expected
    # with the same wrapper the production path uses so a fence-wording change
    # can't silently drift this golden out of sync again.
    context_str = "- current_page_url: https://x.test/lease\n- current_page_title: Lease 4B"
    assert out == (
        "what's on my screen?\n\nContext:\n" + wrap_untrusted(context_str, label="page context")
    )


def test_with_context_block_noop_without_context() -> None:
    assert _with_context_block("hello", {}) == "hello"


def _assertion(result: RoutingRunResult, name_prefix: str):
    return next(a for a in result.assertions if a.name.startswith(name_prefix))


def test_handoff_trail_must_not_contain_fails_on_transient_dispatch() -> None:
    # Terminal agent is the orchestrator (handed back), so must_not_route_to
    # would PASS — but the browser was dispatched mid-run, which this catches.
    result = RoutingRunResult(last_agent_name="Helixis", handoff_trail=["browser_agent"])
    _check_routing_assertions(
        result=result,
        expected={
            "must_route_to": "Helixis",
            "handoff_trail_must_not_contain": ["browser_agent"],
        },
    )
    a = _assertion(result, "handoff_trail_must_not_contain[0]")
    assert a.passed is False
    assert a.critical is True  # safety — fails the single-run gate even quarantined


def test_handoff_trail_must_not_contain_passes_when_clean() -> None:
    result = RoutingRunResult(last_agent_name="Helixis", handoff_trail=[])
    _check_routing_assertions(
        result=result,
        expected={
            "must_route_to": "Helixis",
            "handoff_trail_must_not_contain": ["browser_agent", "document_agent"],
        },
    )
    assert _assertion(result, "handoff_trail_must_not_contain[0]").passed is True
    assert _assertion(result, "handoff_trail_must_not_contain[1]").passed is True
