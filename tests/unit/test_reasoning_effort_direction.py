"""What `adaptive_effort` actually does to an Anthropic request.

`effort.py`'s stated contract is "Design rule: only ever DOWNGRADE", and
`settings.agent.adaptive_effort` ships **True**. Nothing in the repo measured
what the parameter does at the provider, so the direction was assumed.

Read off the installed LiteLLM transform (`AnthropicConfig._map_reasoning_
effort`, litellm 1.94.1) rather than assumed, the direction is **the opposite
of the design rule, by two different mechanisms depending on the model**:

* `reasoning_effort=None` → **no thinking param at all**.
* `claude-sonnet-5` (production primary) **is an adaptive-thinking model**, so
  the mapping returns `{"type": "adaptive"}` on the FIRST branch — for EVERY
  level. `low`, `medium` and `high` are byte-identical there. So on the
  production model the effort LEVEL is inert, and the only effect of
  `adaptive_effort` is to switch adaptive thinking **ON** for exactly the
  questions the regex judged trivial.
* `claude-haiku-4-5` (the 503-fallback and PR-gate tier) is NOT adaptive, so
  `low` falls through to `{"type": "enabled", "budget_tokens": 1024}` —
  thinking enabled with a fixed budget, which is the finding's literal claim.

Either way the lever cannot DOWNGRADE anything; at best it is a no-op on level
and at worst it turns thinking on. **The flag is deliberately NOT flipped
here**: what remains genuinely unmeasured is the MAGNITUDE (what adaptive
thinking on a trivial question costs against no thinking param), and that needs
a controlled A/B on real questions — a paid battery run per arm plus judge
scoring. Settling direction does not settle cost.

⚠ This is entirely a property of a THIRD-PARTY transform, so it can change on
any dependency bump with nothing in this repo failing. That is why the check is
executable rather than a comment: same reasoning as gotcha 27 (a per-request
payload the provider interprets needs a local conformance check) and gotcha 60
(a decision taken from a measurement needs a test that re-measures).
"""

from __future__ import annotations

import pytest

from helixis.settings import Settings

pytest.importorskip("litellm", reason="the whole point is the installed transform")

from litellm.llms.anthropic.chat.transformation import AnthropicConfig  # noqa: E402

#: The production primary and the 503-fallback / PR-gate tier (gotcha 18).
PRIMARY = "claude-sonnet-5"
FALLBACK = "claude-haiku-4-5"


def _mapped(effort: str | None, model: str) -> dict | None:
    return AnthropicConfig._map_reasoning_effort(effort, model, "anthropic")


def test_no_effort_is_the_only_way_to_send_no_thinking_param() -> None:
    """The baseline the "downgrade" framing implicitly compares against. If
    this ever stops being None, the whole argument below needs re-reading."""
    assert _mapped(None, PRIMARY) is None
    assert _mapped("none", PRIMARY) is None


def test_low_effort_does_not_reduce_thinking_on_the_production_model() -> None:
    """`adaptive_effort` cannot honour "only ever DOWNGRADE" on Sonnet 5.

    Every level collapses to the same adaptive param, so the classifier's
    verdict changes nothing except whether thinking is on at all.
    """
    low = _mapped("low", PRIMARY)
    assert low is not None, (
        "low no longer enables thinking on the production model — the direction "
        "this test records has changed; re-read the module docstring before "
        "trusting any conclusion drawn from it"
    )
    assert low.get("type") == "adaptive"
    assert _mapped("medium", PRIMARY) == low and _mapped("high", PRIMARY) == low, (
        "the effort LEVEL is no longer inert on the production model — "
        "`adaptive_effort` may now actually downgrade, which would make the "
        "lever worth measuring rather than merely worth doubting"
    )


def test_low_effort_enables_a_fixed_thinking_budget_on_the_fallback_tier() -> None:
    """Haiku is not an adaptive-thinking model, so it takes the literal branch —
    and it is the model a 503 failover and every PR-gate eval run lands on."""
    low = _mapped("low", FALLBACK)
    assert low is not None and low.get("type") == "enabled"
    assert low.get("budget_tokens", 0) > 0, (
        "`low` requests a positive thinking budget on the fallback tier — it "
        "adds reasoning tokens, it does not remove them"
    )


def test_the_flag_is_still_on_so_this_is_a_live_path_not_a_dark_one() -> None:
    """⚠ Not a code property — the reason these guards earn their keep.

    Gotcha 29: a default plus a config grep does not prove a live value, and
    env vars outrank both. This pins the REPO default, so if someone turns
    `adaptive_effort` off the failure is a prompt to re-read the docstring
    rather than a set of tests quietly describing a path nothing takes.
    """
    settings = Settings(env="test", auth={"jwt_secret": "x" * 40}, memory={"enabled": False})
    assert settings.agent.adaptive_effort is True
