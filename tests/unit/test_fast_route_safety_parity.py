"""The fast-routed path must carry the same prompt-injection rule as the
orchestrator it replaces.

`fast_router` is ON in production (`render.yaml` sets
`HELIXIS_AGENT__FAST_ROUTER: "true"`), and on a `buildium` / `analyst` verdict
the specialist becomes the top-level agent for the WHOLE turn. Everything
`create_orchestrator` appends to its own instructions is therefore absent on
that path — including `UNTRUSTED_CONTENT_RULE`, the rule that tells the model
the ⟦BEGIN_UNTRUSTED⟧ delimiters mean *data, never instructions*.

⚠ The fences are applied on every route regardless: `context_block` is built
AFTER the routing branch, so page context and recalled memory arrive wrapped no
matter which agent is running. Missing the rule does not mean "unfenced" — it
means the model is handed delimiters nobody explained, which is worse than not
fencing, because the wrapping reads as a control that is doing something.

These guards read BOTH sides (gotcha 28): the router's declared verdicts AND
the agent each verdict produces. A test that only checked `harden_fast_routed`
in isolation would pass forever while a new verdict routed around it — the
same shape as the finding this closes.
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest

from helixis.agents import orchestrator as orch
from helixis.agents.analysis_agent import create_analysis_agent
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.fast_router import FastRoute
from helixis.agents.orchestrator import harden_fast_routed
from helixis.security.prompt_delimit import UNTRUSTED_CONTENT_RULE
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", auth={"jwt_secret": "x" * 40}, memory={"enabled": False})


# ── the property, on every agent the router can hand back ────────────


@pytest.mark.parametrize("factory", [create_buildium_agent, create_analysis_agent])
def test_a_fast_routed_agent_carries_the_untrusted_content_rule(factory) -> None:
    agent = harden_fast_routed(factory(_settings()))
    assert UNTRUSTED_CONTENT_RULE in (agent.instructions or ""), (
        f"{factory.__name__} runs as the TOP-LEVEL agent on a fast route, so it "
        "needs the rule that gives the untrusted fences their meaning"
    )


@pytest.mark.parametrize("factory", [create_buildium_agent, create_analysis_agent])
def test_hardening_keeps_the_specialist_own_prompt(factory) -> None:
    """Appending must not replace. A specialist that lost its own instructions
    would still pass the rule check above while being comprehensively broken."""
    plain = factory(_settings()).instructions or ""
    hardened = harden_fast_routed(factory(_settings())).instructions or ""
    assert plain, "specialist has no instructions at all — check the factory"
    assert hardened.startswith(plain)


def test_hardening_is_idempotent() -> None:
    """`create_*_agent` is a fresh factory today, but a future caller that
    hardens an already-hardened agent must not double the rule — a prompt is
    charged per token on every turn."""
    agent = harden_fast_routed(create_buildium_agent(_settings()))
    twice = harden_fast_routed(agent)
    assert (twice.instructions or "").count(UNTRUSTED_CONTENT_RULE) == 1


# ── both-sides: no verdict may route around the hardening ────────────


def test_every_fast_route_verdict_is_either_hardened_or_the_orchestrator() -> None:
    """The corpus check (gotcha 45's rule): read the router's OWN declared
    verdicts, not a list this test invented, and require the dispatch to handle
    each one. A new `FastRoute` value that forgets `harden_fast_routed` reopens
    the gap silently — nothing fails, the turn is just unprotected.
    """
    verdicts = set(typing.get_args(FastRoute))
    assert "orchestrator" in verdicts, "the fall-open verdict disappeared"

    src = inspect.getsource(orch.run_agent_streaming)
    tree = ast.parse(inspect.cleandoc(src))

    # Find each `fast_route == "<verdict>"` test and the call it guards.
    hardened: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cmp_ = node.test
        if not (
            isinstance(cmp_, ast.Compare)
            and isinstance(cmp_.left, ast.Name)
            and cmp_.left.id == "fast_route"
            and isinstance(cmp_.comparators[0], ast.Constant)
        ):
            continue
        verdict = cmp_.comparators[0].value
        # ⚠ Walk the BODY, never the whole `If` node. These branches are an
        # if/elif chain, so the `elif` lives in the first node's `orelse` and
        # `ast.walk(node)` descends straight into it — the buildium branch then
        # "finds" the analyst branch's call and marks itself hardened. Measured:
        # with the buildium call deleted (the exact original defect) the
        # walk-the-node form still passed. Scoping to `body` catches it.
        calls = {
            n.func.id
            for stmt in node.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "harden_fast_routed" in calls:
            hardened.add(verdict)

    specialist_verdicts = verdicts - {"orchestrator"}
    missing = specialist_verdicts - hardened
    assert not missing, (
        f"fast-route verdict(s) {sorted(missing)} bypass harden_fast_routed — "
        "that agent becomes top-level with no untrusted-content rule"
    )


def test_the_orchestrator_still_adds_the_rule_itself() -> None:
    """The non-fast path must not silently start depending on the helper.
    Asserted on the SOURCE of `create_orchestrator` because building it needs
    Composio; the point is that the append is still there, not where it runs.
    """
    src = inspect.getsource(orch.create_orchestrator)
    assert "UNTRUSTED_CONTENT_RULE" in src


def test_fast_router_is_live_in_the_deploy_manifest() -> None:
    """Not a code property — a reminder that this file guards a LIVE path.

    If someone turns the router off, this test failing is the prompt to decide
    whether these guards still earn their keep, rather than leaving a test
    whose stated premise ("ON in production") has quietly become false — the
    expired-justification shape of gotcha 48.
    """
    manifest = Path(__file__).resolve().parents[2] / "render.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert "HELIXIS_AGENT__FAST_ROUTER" in text
    idx = text.index("HELIXIS_AGENT__FAST_ROUTER")
    assert '"true"' in text[idx : idx + 120], (
        "fast_router is no longer enabled in render.yaml — re-read this module's "
        "docstring and decide whether the fast path still needs hardening"
    )
