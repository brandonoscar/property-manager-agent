"""Two route maps describe the same nine specialists, and nothing compared them.

The orchestrator carries the routing decision twice:

* **Handoff mode** — the routing prose in `ORCHESTRATOR_INSTRUCTIONS`
  (orchestrator.py ~703-1073).
* **Tool mode** — `_SPECIALIST_TOOL_DESCRIPTIONS`, the `tool_description` each
  specialist-tool advertises to the model.

The source itself says these do "the same job the routing prose does for
handoffs" — and no test compared them. Both strings are individually
well-formed prose, so every existing test passes while they say different
things, which is gotcha 28's both-sides rule applied to routing.

⚠ **They disagree today, on the live path.** Per gotcha 29 `reasoning_loop` is
ON in production, so TOOL mode is live and the tool description is the route
map the model actually reads:

* prose: *"Writes read-only SQL over Occupella's synced Buildium data"*, for
  *"quantitative questions over the user's OWN portfolio data"*
* tool: *"Analytical synthesis over data already gathered — trends,
  comparisons, and multi-record reasoning"*

Verified against the agent rather than either description:
`create_analysis_agent` carries `describe_mirror_schema`, `run_analysis` and
`show_table`. **It queries the mirror.** The prose is right; the LIVE map is
wrong.

That mis-description has a cost beyond the one the finding names. An
orchestrator reading "synthesis over data already gathered" will not send a
numbers-across-many-records question straight to the analyst — it gathers from
`buildium_agent` first and hands rows over, which is precisely the
specialist fan-out gotchas 25 / 32 / 37 / 50 are all about.

**The description is deliberately NOT changed here.** It is a live prompt edit,
so §3's "changes to live agent behavior get isolated verification" applies, and
this batch was scoped as guards with no live-agent risk. It is recorded in
`KNOWN_DIVERGENCES` so the next divergence cannot hide among the noise, and so
fixing it is a decision someone takes on purpose.

⚠ **What this file does and does not prove.** Comparing prose semantically is
not something a test can do. It checks what is decidable — that both maps name
the same specialists, and that every name resolves to a real agent — plus a
register of the one semantic contradiction that was found by reading. It is
not a general contradiction detector, and claiming otherwise would be the
false-confidence shape this repo keeps recording.
"""

from __future__ import annotations

import re

from helixis.agents.orchestrator import (
    _SPECIALIST_TOOL_DESCRIPTIONS,
    ORCHESTRATOR_INSTRUCTIONS,
    SPECIALIST_AGENT_NAMES,
)

#: agent name -> the Title Case heading the routing prose uses for it. The
#: prose is written for a model, so it says "Data Analyst", not
#: "analysis_agent" — the mapping has to be explicit or the comparison is
#: between two vocabularies rather than two claims.
PROSE_HEADINGS = {
    "buildium_agent": "Buildium",
    "calendar_agent": "Calendar",
    "gmail_agent": "Gmail",
    "outlook_agent": "Outlook",
    "drive_agent": "Drive",
    "document_agent": "Document",
    "web_agent": "Web",
    "analysis_agent": "Data Analyst",
    "browser_agent": "Browser",
}

#: Specialists whose two descriptions make OPPOSITE capability claims, with
#: the reading that the code supports. Found by reading, not by the guard —
#: see this module's docstring on what is and is not decidable here.
KNOWN_DIVERGENCES = {
    "analysis_agent": (
        "prose says it WRITES SQL over the mirror; the live tool description says it "
        "SYNTHESISES rows already gathered. create_analysis_agent carries "
        "describe_mirror_schema + run_analysis, so the prose is correct and the LIVE map "
        "is wrong. Fixing it is a live prompt change and needs isolated verification (§3)."
    ),
}


def test_both_maps_name_the_same_specialists() -> None:
    """The drift class a test CAN catch: a specialist added to one map and not
    the other. In tool mode an absent entry means the model never sees the
    capability at all — the specialist exists and is unreachable."""
    tool_map = set(_SPECIALIST_TOOL_DESCRIPTIONS)
    assert tool_map == set(SPECIALIST_AGENT_NAMES), (
        "SPECIALIST_AGENT_NAMES is derived from the tool map and no longer matches it"
    )
    assert tool_map == set(PROSE_HEADINGS), (
        "the tool map and this file's prose-heading table have drifted:\n"
        f"  only in the tool map: {sorted(tool_map - set(PROSE_HEADINGS))}\n"
        f"  only in the table:    {sorted(set(PROSE_HEADINGS) - tool_map)}\n"
        "A new specialist needs an entry in BOTH route maps — that is the whole finding."
    )


def test_every_specialist_in_the_tool_map_appears_in_the_routing_prose() -> None:
    """Both sides, read from the real strings. A specialist the prose never
    mentions is unroutable on the handoff path."""
    missing = [
        name
        for name, heading in PROSE_HEADINGS.items()
        if not re.search(rf"\*\*{re.escape(heading)}[^*]*\*\*", ORCHESTRATOR_INSTRUCTIONS)
    ]
    assert not missing, (
        f"specialists absent from the routing prose: {missing} — they exist as tools and "
        "cannot be reached by a handoff"
    )


def test_no_specialist_description_is_empty_or_a_placeholder() -> None:
    """A description IS the routing decision in tool mode. An empty or stub one
    silently removes the specialist from consideration without removing it from
    the tool array — you pay its schema tokens and never get the routing."""
    for name, desc in _SPECIALIST_TOOL_DESCRIPTIONS.items():
        assert len(desc.strip()) >= 40, f"{name}'s tool description is too thin to route on"
        assert "TODO" not in desc.upper(), f"{name} carries a placeholder description"


def test_the_known_divergence_is_still_exactly_what_was_recorded() -> None:
    """⚠ Pins the OPEN defect rather than hiding it.

    The analyst's tool description still says "already gathered" while the
    agent it describes queries the mirror. If someone fixes the description,
    this test fails and the `KNOWN_DIVERGENCES` entry must be removed with it —
    a register that outlives its defect is the stale-exemption problem, and
    that one has already caught its own author once this week.
    """
    assert set(KNOWN_DIVERGENCES) == {"analysis_agent"}, (
        "the divergence register changed — add the reading that the CODE supports, "
        "or remove the entry if the maps now agree"
    )
    desc = _SPECIALIST_TOOL_DESCRIPTIONS["analysis_agent"]
    assert "already gathered" in desc, (
        "analysis_agent's tool description no longer claims it works on data already "
        "gathered — if that was the fix, delete the KNOWN_DIVERGENCES entry and this "
        "assertion together, and verify the routing change in isolation (§3)"
    )


def test_the_analyst_really_does_query_the_mirror() -> None:
    """Which map is wrong is settled by the AGENT, not by either description.
    Without this, the register above would be one more unverified claim about
    routing — and unverified claims about routing are the finding."""
    import inspect

    from helixis.agents import analysis_agent as mod

    src = inspect.getsource(mod.create_analysis_agent)
    assert "run_analysis" in src and "describe_mirror_schema" in src, (
        "analysis_agent no longer carries the mirror-query tools — re-read which route "
        "map is correct before trusting KNOWN_DIVERGENCES"
    )
