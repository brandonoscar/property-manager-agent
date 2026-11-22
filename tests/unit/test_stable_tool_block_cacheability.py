"""Why the connector specialists' tool arrays are NOT reordered for caching.

Finding #63 proposed reordering `gmail_agent` / `drive_agent` /
`calendar_agent` so the stable in-repo tools LEAD and the per-user Composio
schemas TRAIL, then spending the spare fourth `cache_control` breakpoint on the
end of the stable block — so a Composio change would invalidate only the tail
and the in-repo prefix would be shared across every user.

The premise is correct: `tools.extend(<composio>)` does come first
(gmail_agent.py, drive_agent.py, calendar_agent.py) and the in-repo tools are
appended after, so today the volatile block IS the cached prefix.

**The payoff is not.** Measured 2026-08-29 (tiktoken cl100k), the in-repo tail
each connector agent appends is:

    drive_agent      991 tokens
    gmail_agent    1,121 tokens
    calendar_agent   931 tokens

Anthropic's minimum cacheable block on Sonnet is **1024 tokens**, and a mark on
a shorter block is silently IGNORED by the provider (`prompt_cache.
_mark_conversation_tail` documents this). So two of the three would gain
nothing at all, and the third clears the bar by 9% — measured with OpenAI's
tokenizer, not Anthropic's, which is not a margin to bet a live prompt change
on. Against a Composio block of ~186 schemas on a single drive call, a ~1k
shared prefix is not where the money is either.

⚠ **The lever that measurement actually points at is the one
`scripts/measure_tool_payload.py` already names: `get_tools` applies NO
allowlist.** 186 tool schemas per drive model call is the cost; no ordering of
them makes them cacheable across users, because they are genuinely per-user.
Reordering is a live prompt change for three specialists that buys ~0 today —
and the finding's own note is that a wrong ordering silently costs money
rather than failing.

**This file exists so that decision expires instead of rotting.** It is a
justification written from a number, which is the shape gotcha 48 records: the
prose cannot fail, so only re-measuring can tell you it stopped being true. If
the stable block grows — more visual tools, a richer `drive_find_files`
schema — the arithmetic flips and the reorder becomes worth doing. Then this
test fails and says so, rather than the conclusion silently outliving its
evidence.
"""

from __future__ import annotations

import json

import pytest

from helixis.agents import visual_tools
from helixis.agents.drive_agent import drive_find_files
from helixis.agents.visual_tools import show_table, suggest_next_steps

#: Anthropic's minimum cacheable block on Sonnet. A `cache_control` mark on a
#: shorter block is ignored by the provider — no error, no cost, no cache.
ANTHROPIC_MIN_CACHEABLE_TOKENS = 1024

#: The bar at which finding #63 is worth re-opening. Deliberately ABOVE
#: today's largest tail (1,121) rather than at the provider minimum: gmail
#: already clears 1024 on its own and the reorder still does not pay, because
#: one agent out of three, at a margin inside the tokenizer's error, is not a
#: reason to change three live prompts. A tail this size IS.
REVISIT_AT_TOKENS = 2000

#: The in-repo tools each connector specialist appends AFTER its
#: `tools.extend(<composio>)`. These are the "stable block" the reorder would
#: move to the front. Kept as literal lists rather than derived from the agent
#: factories: those need a live Composio key to take the branch that appends
#: them, and a fixture that measures zero would report "no problem here" —
#: the exact artifact `measure_tool_payload.py`'s own header warns about.
STABLE_TAILS = {
    "drive_agent": [drive_find_files, show_table, suggest_next_steps],
    "gmail_agent": [show_table, visual_tools.show_email_draft, suggest_next_steps],
    "calendar_agent": [show_table, visual_tools.show_schedule, suggest_next_steps],
}


def _tool_tokens(tools: list) -> tuple[int, str]:
    """Serialize the way the SDK does and count. Falls back to bytes/4 and
    SAYS SO — a skip here would read as a pass on a cost question."""
    from agents.models.chatcmpl_converter import Converter

    blob = "".join(json.dumps(Converter.tool_to_openai(t)) for t in tools)
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(blob)), "tiktoken cl100k"
    except Exception:  # noqa: BLE001 — the estimate is the point, not the tokenizer
        return len(blob) // 4, "bytes/4 (tiktoken unavailable)"


@pytest.mark.parametrize("agent_name", sorted(STABLE_TAILS))
def test_the_stable_tool_block_is_still_too_small_to_be_worth_reordering(agent_name: str) -> None:
    tokens, method = _tool_tokens(STABLE_TAILS[agent_name])
    assert tokens < REVISIT_AT_TOKENS, (
        f"{agent_name}'s in-repo tool block is now ~{tokens} tokens ({method}), past the "
        f"{REVISIT_AT_TOKENS}-token bar this decision was taken under.\n\n"
        "RE-OPEN finding #63: reorder the connector specialists so the stable in-repo "
        "tools LEAD and the per-user Composio schemas TRAIL, and spend the spare fourth "
        "cache_control breakpoint on the end of the stable block. A block this size is "
        "worth sharing across users; a ~1k one was not.\n\n"
        "Read this module's docstring before doing it — the ordering has to be checked "
        "against LiteLLM's Anthropic transform rather than assumed, and a wrong ordering "
        "costs money silently instead of failing."
    )


def test_the_provider_minimum_this_decision_rests_on_is_stated_where_it_is_used() -> None:
    """Both-sides (gotcha 28). The number above is only meaningful because
    `prompt_cache` relies on the same provider behaviour — a mark under the
    minimum is ignored rather than charged. If that note is edited away, the
    reasoning here loses the source it was read from and this test says so
    instead of leaving two files quietly disagreeing.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "helixis" / "llm" / "prompt_cache.py"
    text = src.read_text(encoding="utf-8")
    assert str(ANTHROPIC_MIN_CACHEABLE_TOKENS) in text, (
        "prompt_cache.py no longer states Anthropic's minimum cacheable block size — "
        "that number is the whole basis for not reordering the connector tool arrays"
    )
