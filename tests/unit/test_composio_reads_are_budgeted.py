"""Composio reads get the per-run memo. Composio WRITES never do.

WHAT HAPPENED (live turn, 2026-08-27)
-------------------------------------
"can you tell me about the drew bennet one" — one question, about one email —
cost **28 model calls, 151.9s and $0.83**, with SIX specialist consults:
``gmail_agent`` x4 and ``buildium_agent`` x2, against 7 orchestrator-level
tool calls.

The Gmail half is the interesting number. Each ``.as_tool()`` consult is a
nested run that starts COLD, so four consults meant four independent inbox
fetches of the same mailbox inside a single turn. Nothing collapsed them,
because ``call_budget`` had never known about Composio at all: ``_MIRROR_READS``
names fourteen Buildium/Maps tools and ``_READ_PREFIXES`` is ``buildium_*``, so
``_is_budgeted`` returned False for every Gmail, Drive, Docs, Sheets, Calendar
and Outlook slug. No memo, no cap, no soft-deadline channel — the three things
gotchas 25/32/37 exist to provide.

⚠ WHY THIS FILE READS THE 603-SLUG DUMP AND NOT JUST THE FUNCTION
-----------------------------------------------------------------
Gotcha 45's rule. A test feeding hand-written slugs to ``_is_composio_read``
would pass happily even if the classifier had drifted off the real namespace —
that is exactly how ``_case_needs_gemini`` kept "working" for five weeks
against a key no golden file declared any more. The corpus is the only input
that can catch a classifier which is self-consistent and pointed at nothing.

⚠ AND WHY THE WRITE HALF IS THE LOAD-BEARING ONE
-------------------------------------------------
A memo replays a stored result for an identical call. On a READ that is the
whole point. On a WRITE it would let a second send return the first one's
success text without executing, or let a read-back verify see pre-write state.
So the write assertions here are not symmetry for its own sake — they are the
property that makes the read half safe to ship.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from helixis.agents.call_budget import _is_budgeted, _is_composio_read
from helixis.connectors.tool_allowlist import TOOLKIT_ALLOWLIST
from helixis.connectors.tool_gate import is_write_tool

FIXTURE = pathlib.Path("tests/fixtures/composio_tool_slugs.json")

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no live Composio slug dump — run scripts/composio_tool_dump.py",
)


def _all_slugs() -> list[str]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [entry["slug"] for entries in (data.get("toolkits") or {}).values() for entry in entries]


def test_no_write_in_the_whole_corpus_is_memoized() -> None:
    """⚠ THE safety assertion. 603 real slugs; every one the gate calls a
    WRITE must be a pass-through, or an approved send could be replayed from
    a memo instead of executed."""
    memoized_writes = [s for s in _all_slugs() if is_write_tool(s) and _is_budgeted(s)]
    assert not memoized_writes, (
        f"{len(memoized_writes)} write-classified Composio slugs would be memoized: "
        f"{sorted(memoized_writes)[:12]}. A memo replays a stored result for an "
        f"identical call — on a write that means the second one never runs."
    )


def test_the_reads_we_actually_ship_are_memoized() -> None:
    """The allowlist is what production loads (#917). Every read in it must
    get the memo, or the fix reaches nothing the product uses."""
    shipped_reads = [
        slug for slugs in TOOLKIT_ALLOWLIST.values() for slug in slugs if not is_write_tool(slug)
    ]
    assert shipped_reads, "no readable slug in the allowlist — the fixture or the list moved"
    missed = [s for s in shipped_reads if not _is_budgeted(s)]
    assert not missed, f"these shipped Composio reads get no memo: {sorted(missed)}"


def test_the_corpus_actually_exercises_both_branches() -> None:
    """⚠ Assert the probe RAN (gotcha 33). A dump that produced only reads,
    or only writes, would make one of the two tests above vacuous while
    looking green."""
    slugs = _all_slugs()
    reads = [s for s in slugs if not is_write_tool(s)]
    writes = [s for s in slugs if is_write_tool(s)]
    assert len(slugs) > 500, f"dump looks truncated: {len(slugs)} slugs"
    assert reads and writes, (
        f"the corpus is one-sided (reads={len(reads)} writes={len(writes)}) — "
        f"one of the assertions above proves nothing"
    )


def test_our_own_tools_are_untouched_by_the_composio_branch() -> None:
    """The namespace guard. Composio slugs are SCREAMING_SNAKE and every tool
    this repo writes is lower snake_case, so nothing of ours can pick up a
    memo it was never reviewed for — including the two dark code-exec tools
    and the teammate-delegation write."""
    for name in (
        "store_memory",
        "send_task_to_teammate",
        "run_computation",
        "apply_actions",
        "remember_contact_alias",
        "buildium_record_payment",
        "send_text_message",
    ):
        assert not _is_composio_read(name), f"{name} matched the Composio branch"


def test_the_classifier_can_fail() -> None:
    """A self-test, because every assertion above is a negative. If
    ``_is_composio_read`` returned False for everything, three of the four
    tests here would pass for the wrong reason."""
    assert _is_composio_read("GMAIL_FETCH_EMAILS") is True
    assert _is_composio_read("GMAIL_SEND_EMAIL") is False
    assert _is_composio_read("") is False
