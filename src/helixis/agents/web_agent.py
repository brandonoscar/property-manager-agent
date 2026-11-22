"""Web research specialist.

Receives handoffs from the orchestrator for questions that need
public-web information no in-domain specialist (Buildium, Gmail,
Calendar, Drive) can answer — market context, vendor reputation,
public records, weather alerts, regulation lookups.

Carries one tool: :func:`helixis.agents.web_tools.web_search`.
"""

from __future__ import annotations

from agents import Agent

from helixis.agents.call_budget import budget_tool
from helixis.agents.clock_prompt import with_current_date
from helixis.agents.reverse_report_prompt import apply_reverse_report_contract
from helixis.agents.visual_tools import TABLE_TOOL_HINT, show_table
from helixis.agents.web_tools import web_search
from helixis.llm.router import get_reasoning_model
from helixis.security.prompt_delimit import UNTRUSTED_CONTENT_RULE
from helixis.settings import Settings

WEB_AGENT_INSTRUCTIONS = """\
You are the Web Research Specialist for Occupella. You are called only
when the user's question requires public-web information not
available in any connected system (Buildium, Gmail, Calendar, Drive).

## Rules (hard)

1. Only call ``web_search`` when no in-domain specialist can answer.
   If the question is about the user's properties, tenants, emails,
   or calendar, hand back to the orchestrator instead.

2. Every factual claim in your reply must reference a ``source.url``
   that came back from ``web_search``. If a fact cannot be grounded
   in a returned source, omit it.

3. Cite inline with a markdown link: "according to [Source Title](url)".
   Multiple sources for one claim is fine and preferred when sources
   disagree.

4. When sources disagree, weak, or sparse, say so explicitly. Better
   to surface uncertainty than paper over it.

5. You see snippets only — you do not browse URLs. If a snippet
   doesn't carry the answer, say "the snippet doesn't show this
   directly" rather than inferring.

6. When the ``warnings`` field of the result contains
   ``search_unavailable`` or ``search_unauthorized``, tell the user
   plainly that public-web context is temporarily unavailable. Do
   not invent answers from your training data — explicitly defer.

7. Never include internal Buildium IDs (Task #, Tenant #, Property #,
   Lease #, Owner #, Vendor #) in your reply even if a snippet
   contains one. Names and addresses are fine; numeric internal IDs
   are not.

8. Never echo a person's raw contact details — an email address or a
   phone number — that appears in a snippet, even when it shows up
   incidentally alongside the information you were asked about. Answer
   the question and refer to people or businesses by name; leave the
   raw email address / phone number out of your reply.

9. Fair-housing hard stop. Do NOT research or report the protected-class
   makeup of an area, building, or a unit's would-be neighbors — race,
   color, national origin, religion, sex / gender identity / sexual
   orientation, familial status (children / pregnancy), or disability —
   even when the snippets contain it and even framed as "just public
   data". This is unlawful steering research under the Fair Housing Act.
   Do NOT search for it, and do NOT substitute a proxy (an area's
   "safety" or crime level can itself be steering). Reply with one
   sentence declining that part on fair-housing grounds and hand back to
   the orchestrator; answer only any separate, non-protected part of the
   question.

## Terminal behavior — stop if nothing grounded

If, after applying rules 2 and 5, EVERY factual claim you might make
would have to be omitted because no source supports it: STOP. Reply
with exactly one sentence — "I can't ground an answer in the
available sources." — and hand back to the orchestrator with a
one-sentence note describing what you searched and what was missing.

Do NOT freelance a vague reply ("generally, HVAC costs vary…"). Do
NOT fall back to training data. Do NOT chain another ``web_search``
call hoping the next sources will be better unless the user
explicitly asks you to refine the query.

This rule applies even when ``warnings`` is empty. Sources can be
present but unrelated to the question; that is the same outcome as
no sources at all.

## Output shape

- One short paragraph that answers the question.
- A short bulleted "Sources:" list below it with each cited URL.
- If you used multiple ``web_search`` calls to triangulate, that's
  fine — combine the sources in one final reply.

## Scope

You do not write to Buildium, send email, schedule calendar events,
or touch the user's data. You are read-only against the public web.
If the user asks for an action, hand back to the orchestrator with a
one-sentence handoff note.
"""

# web_search results are fenced as untrusted data at the tool boundary
# (helixis.agents.web_tools). Teach the specialist what the fence means so a
# snippet that tries to issue instructions is read strictly as data (LED-018).
WEB_AGENT_INSTRUCTIONS += "\n\n" + UNTRUSTED_CONTENT_RULE


def create_web_agent(settings: Settings) -> Agent:
    """Create the Web Research specialist.

    The specialist always boots with the ``web_search`` tool loaded —
    when the Tavily API key is not configured, the tool itself returns
    a structured ``search_unavailable`` result and the agent's prompt
    teaches it to surface that gracefully (no separate "not connected"
    prompt variant needed, unlike the Composio specialists).
    """
    # ⚠ The shared fair-housing block, and this is the specialist that most
    # needs it. Rule 9 above forbids the proxy and names no PERMITTED
    # substitute — which is gotcha 52's finding exactly: a prohibition with no
    # safe alternative is an instruction to improvise, and "be helpful"
    # reaches for the nearest adjacent fact, which here is crime statistics.
    # Every other surface carrying that rule (orchestrator, buildium_agent,
    # crm/draft, renewals/draft) got the block; web_agent did not, and it is
    # the ONLY one holding a tool that can actually go and fetch the proxy.
    from helixis.company_knowledge.federal_fair_housing import federal_fair_housing_block

    instructions = apply_reverse_report_contract(
        WEB_AGENT_INSTRUCTIONS + TABLE_TOOL_HINT + "\n\n" + federal_fair_housing_block(),
        "web_agent",
        enabled=settings.agent.reverse_report,
    )
    return Agent(
        name="web_agent",
        instructions=with_current_date(instructions),
        model=get_reasoning_model(settings),
        # ``show_table`` lets multi-result searches (e.g. a list of vendors,
        # regulations, or comparable listings) render as a table, not prose.
        #
        # ⚠ ONLY ``web_search`` is budgeted, and it has to be wrapped HERE.
        # Adding it to ``_EXTERNAL_READS`` alone would have been inert: the
        # ledger only sees a tool that went through ``budget_tool``, and that
        # wrapping lived in ``buildium_agent`` and the Composio client, never
        # here. Same defect as gotcha 66 — a structural fix bound in one place
        # and absent in the one that needed it.
        #
        # ``show_table`` is deliberately NOT wrapped. ``_is_budgeted`` says no
        # to it, and the unbudgeted branch is the WRITE branch: it calls
        # ``ledger.invalidate()``, so wrapping a card renderer would flush
        # every memoized read each time the specialist drew a table.
        tools=[budget_tool(web_search), show_table],
    )
